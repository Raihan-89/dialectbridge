"""
DDL Translator: wraps sqlglot's transpile with our custom pre/post-processing
rules to fix known gaps (MONEY, BIT, TINYINT, etc.) and flag statements
needing manual review.

Design note: we do BOTH pre-processing (on the raw source, before sqlglot
touches it) and post-processing (on sqlglot's output). This is necessary
because sqlglot sometimes mangles unsupported types into something we can
no longer reliably pattern-match (e.g. TINYINT -> UTINYINT, SQL_VARIANT ->
VARIANT). Catching these on the source, before sqlglot sees them, is more
reliable than trying to catch every possible mangled output later.
"""
import re
import sqlglot
from engine.mappers.type_mappings import (
    MSSQL_TO_POSTGRES_TYPE_OVERRIDES,
    POSTGRES_TO_MSSQL_TYPE_OVERRIDES,
    MANUAL_REVIEW_REQUIRED,
    BIT_DEFAULT_VALUE_MAP,
)

# Types that sqlglot mangles into something unrecognizable when transpiling
# tsql -> postgres, so we must rewrite them in the SOURCE before sqlglot
# ever sees them. Value = a type sqlglot's tsql reader understands AND
# transpiles correctly to the desired postgres output.
PRE_TRANSPILE_REWRITES = {
    "tsql_to_postgres": {
        "TINYINT": "SMALLINT",   # sqlglot mangles TINYINT -> UTINYINT otherwise
    },
    "postgres_to_tsql": {},
}


class ConversionResult:
    """Wraps the converted SQL plus metadata about what needs manual review."""
    def __init__(self, sql: str, warnings: list[str]):
        self.sql = sql
        self.warnings = warnings

    def __repr__(self):
        return f"ConversionResult(warnings={len(self.warnings)})"


def _detect_manual_review_types(source_sql: str, review_list: list[str]) -> list[str]:
    """
    Scan the ORIGINAL source SQL (not the converted output) for types with
    no clean equivalent. Must run before any rewriting, since sqlglot may
    rename these types in its output (e.g. SQL_VARIANT -> VARIANT), which
    would make output-based detection silently miss them.
    """
    warnings = []
    for flagged_type in review_list:
        pattern = r'\b' + re.escape(flagged_type) + r'\b'
        if re.search(pattern, source_sql, flags=re.IGNORECASE):
            warnings.append(
                f"MANUAL REVIEW REQUIRED: '{flagged_type}' has no clean equivalent — verify converted output"
            )
    return warnings


def _pre_transpile_rewrite(source_sql: str, rewrite_map: dict[str, str]) -> str:
    """
    Rewrite known-problematic types in the SOURCE SQL, before sqlglot parses
    it, so sqlglot never gets the chance to mangle them into an unrecognized
    form. This runs BEFORE sqlglot.transpile().
    """
    for old_type, safe_type in rewrite_map.items():
        pattern = r'\b' + re.escape(old_type) + r'\b'
        source_sql = re.sub(pattern, safe_type, source_sql, flags=re.IGNORECASE)
    return source_sql


def _apply_type_overrides(sql: str, overrides: dict[str, str], warnings: list[str]) -> tuple[str, list[str]]:
    """
    Post-process sqlglot's output to fix remaining types it left unchanged
    (types that DO survive transpile intact, unlike TINYINT/SQL_VARIANT
    which get pre-rewritten above).
    """
    for old_type, new_type in overrides.items():
        pattern = r'\b' + re.escape(old_type) + r'\b'
        if re.search(pattern, sql, flags=re.IGNORECASE):
            sql = re.sub(pattern, new_type, sql, flags=re.IGNORECASE)
            warnings.append(f"Mapped '{old_type}' -> '{new_type}' (sqlglot default overridden)")
    return sql, warnings


def _fix_boolean_defaults(sql: str, warnings: list[str]) -> tuple[str, list[str]]:
    """
    Fix 'DEFAULT 1' / 'DEFAULT 0' left behind on columns we just converted
    from BIT to BOOLEAN. Only touches DEFAULT clauses immediately following
    a BOOLEAN column, so it never corrupts unrelated integer defaults
    elsewhere in the statement.
    """
    pattern = re.compile(r'(BOOLEAN\s+DEFAULT\s+)([01])\b', flags=re.IGNORECASE)

    def _replace(match):
        prefix, value = match.group(1), match.group(2)
        replacement = BIT_DEFAULT_VALUE_MAP[value]
        warnings.append(f"Fixed boolean default: 'DEFAULT {value}' -> 'DEFAULT {replacement}'")
        return f"{prefix}{replacement}"

    sql = pattern.sub(_replace, sql)
    return sql, warnings


def _fix_bytea_length(sql: str, warnings: list[str]) -> tuple[str, list[str]]:
    """
    BYTEA takes no length parameter in PostgreSQL. BINARY(n)/VARBINARY(n)
    map to BYTEA, but a plain type-name swap leaves the now-invalid '(n)'
    behind (e.g. 'BYTEA(16)'). Strip it.
    """
    pattern = re.compile(r'\bBYTEA\s*\(\s*\d+\s*\)', flags=re.IGNORECASE)
    if pattern.search(sql):
        sql = pattern.sub('BYTEA', sql)
        warnings.append("Stripped length parameter from BYTEA (not valid in PostgreSQL)")
    return sql, warnings


def _fix_inline_foreign_key_syntax(sql: str, warnings: list[str]) -> tuple[str, list[str]]:
    """
    T-SQL allows 'colname TYPE FOREIGN KEY REFERENCES table(col)' as an
    inline column-level constraint. PostgreSQL does NOT accept the
    'FOREIGN KEY' keyword in that inline position — it must simply be
    'colname TYPE REFERENCES table(col)'.

    We only strip 'FOREIGN KEY' when it's immediately followed by
    'REFERENCES' with no opening parenthesis in between — that pattern is
    unique to the invalid inline form. A table-level constraint always has
    a parenthesized column list right after 'FOREIGN KEY', e.g.
    'FOREIGN KEY (DepartmentID) REFERENCES ...', which this pattern does
    NOT match, so table-level constraints are left untouched.
    """
    pattern = re.compile(r'\bFOREIGN\s+KEY\s+REFERENCES\b', flags=re.IGNORECASE)
    if pattern.search(sql):
        sql = pattern.sub('REFERENCES', sql)
        warnings.append(
            "Rewrote inline 'FOREIGN KEY REFERENCES' to 'REFERENCES' "
            "(PostgreSQL doesn't allow FOREIGN KEY keyword in inline column constraints)"
        )
    return sql, warnings


def convert_ddl(source_sql: str, source_dialect: str, target_dialect: str) -> ConversionResult:
    """
    Convert a DDL statement between 'tsql' and 'postgres' dialects,
    applying custom pre/post-processing sqlglot doesn't handle correctly.
    """
    if source_dialect == "tsql" and target_dialect == "postgres":
        overrides = MSSQL_TO_POSTGRES_TYPE_OVERRIDES
        review_list = MANUAL_REVIEW_REQUIRED["mssql_to_postgres"]
        pre_rewrites = PRE_TRANSPILE_REWRITES["tsql_to_postgres"]
    elif source_dialect == "postgres" and target_dialect == "tsql":
        overrides = POSTGRES_TO_MSSQL_TYPE_OVERRIDES
        review_list = MANUAL_REVIEW_REQUIRED["postgres_to_mssql"]
        pre_rewrites = PRE_TRANSPILE_REWRITES["postgres_to_tsql"]
    else:
        overrides, review_list, pre_rewrites = {}, [], {}

    # 1. Detect manual-review types from the ORIGINAL source, before anything
    #    gets rewritten or mangled.
    warnings = _detect_manual_review_types(source_sql, review_list)

    # 2. Pre-rewrite types sqlglot would otherwise mangle into garbage.
    safe_source = _pre_transpile_rewrite(source_sql, pre_rewrites)

    # 3. Run sqlglot's transpile.
    raw_converted = sqlglot.transpile(
        safe_source, read=source_dialect, write=target_dialect, pretty=True
    )
    converted_sql = "\n".join(raw_converted)

    # 4. Post-process: fix types sqlglot leaves unchanged (survive intact).
    final_sql, warnings = _apply_type_overrides(converted_sql, overrides, warnings)

    # 5. Post-process: target-dialect-specific syntax fixes.
    if target_dialect == "postgres":
        final_sql, warnings = _fix_boolean_defaults(final_sql, warnings)
        final_sql, warnings = _fix_bytea_length(final_sql, warnings)
        final_sql, warnings = _fix_inline_foreign_key_syntax(final_sql, warnings)

    return ConversionResult(sql=final_sql, warnings=warnings)


if __name__ == "__main__":
    test_cases = [
        """
        CREATE TABLE Departments (
            DepartmentID INT IDENTITY(1,1) PRIMARY KEY,
            DepartmentName NVARCHAR(100) NOT NULL
        );
        CREATE TABLE Employees (
            EmployeeID INT IDENTITY(1,1) PRIMARY KEY,
            FirstName NVARCHAR(50) NOT NULL,
            DepartmentID INT FOREIGN KEY REFERENCES Departments(DepartmentID),
            EmployeeGuid UNIQUEIDENTIFIER DEFAULT NEWID()
        )
        """,
        """
        CREATE TABLE Locations (
            LocationID INT IDENTITY(1,1) PRIMARY KEY,
            Coordinates GEOGRAPHY,
            Notes SQL_VARIANT
        )
        """,
        """
        CREATE TABLE AuditLog (
            LogID INT IDENTITY(1,1) PRIMARY KEY,
            Severity TINYINT,
            Payload BINARY(16),
            EventTime DATETIMEOFFSET
        )
        """,
    ]

    for i, tsql in enumerate(test_cases, start=1):
        print(f"\n{'='*60}\nTEST CASE {i}\n{'='*60}")
        result = convert_ddl(tsql, source_dialect="tsql", target_dialect="postgres")
        print("--- CONVERTED ---")
        print(result.sql)
        print("--- WARNINGS ---")
        for w in result.warnings:
            print(f"  - {w}")