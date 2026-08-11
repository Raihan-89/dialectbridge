"""
Build target-dialect DDL from the normalized schema model.

This is the reverse of the extractors: a normalized Database (from MSSQL or
PostgreSQL) becomes a dependency-ordered script of CREATE TABLE / ALTER TABLE
/ CREATE INDEX / CREATE VIEW / CREATE FUNCTION / CREATE PROCEDURE / CREATE
TRIGGER statements in the target dialect, plus standalone sequences where the
target needs them.

Identifier safety notes:
  - PostgreSQL truncates identifiers to 63 bytes; long constraint/index names
    from SQL Server are shortened with a collision-proof suffix.
  - PostgreSQL folds unquoted identifiers to lowercase, so PG output always
    double-quotes identifiers to preserve case.
"""
from __future__ import annotations

import hashlib
import re

from engine.mappers.type_mappings import (
    MSSQL_TO_POSTGRES_TYPE_OVERRIDES,
    POSTGRES_TO_MSSQL_TYPE_OVERRIDES,
)
from engine.schema import Column, Constraint, Database, ForeignKey, Index, Routine, Table, Trigger

_PG_MAX_IDENT = 63

# T-SQL "synonym" types (no parameters) -> canonical target types.
_MSSQL_TYPES = {
    "INT": "INTEGER", "INTEGER": "INTEGER", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT",
    "TINYINT": "SMALLINT", "BIT": "BOOLEAN", "MONEY": "NUMERIC(19,4)",
    "SMALLMONEY": "NUMERIC(10,4)", "FLOAT": "DOUBLE PRECISION", "REAL": "REAL",
    "DATE": "DATE", "TIME": "TIME", "DATETIME": "TIMESTAMP",
    "SMALLDATETIME": "TIMESTAMP", "DATETIMEOFFSET": "TIMESTAMPTZ",
    "DATETIME2": "TIMESTAMP", "TEXT": "TEXT", "NTEXT": "TEXT",
    "IMAGE": "BYTEA", "BINARY": "BYTEA", "VARBINARY": "BYTEA",
    "UNIQUEIDENTIFIER": "UUID", "SYSNAME": "VARCHAR(128)",
    "XML": "XML", "SQL_VARIANT": None, "GEOGRAPHY": None, "GEOMETRY": None,
    "HIERARCHYID": None, "ROWVERSION": None, "TIMESTAMP": None,
}

# PostgreSQL type patterns -> T-SQL. Keys are regexes matched against the
# lowercased type string.
_PG_TYPES = [
    # order matters: more specific first
    (r"^bigserial$", "BIGINT IDENTITY(1,1)"),
    (r"^smallserial$", "SMALLINT IDENTITY(1,1)"),
    (r"^serial$", "INT IDENTITY(1,1)"),
    (r"^integer$", "INT"),
    (r"^int$", "INT"),
    (r"^bigint$", "BIGINT"),
    (r"^smallint$", "SMALLINT"),
    (r"^boolean$", "BIT"),
    (r"^numeric\(([\d,]+)\)$", r"NUMERIC(\1)"),
    (r"^decimal\(([\d,]+)\)$", r"NUMERIC(\1)"),
    (r"^double precision$", "FLOAT"),
    (r"^real$", "REAL"),
    (r"^money$", "MONEY"),
    (r"^character varying\((\d+)\)$", r"NVARCHAR(\1)"),
    (r"^character\((\d+)\)$", r"NCHAR(\1)"),
    (r"^varchar\((\d+)\)$", r"NVARCHAR(\1)"),
    (r"^char\((\d+)\)$", r"NCHAR(\1)"),
    (r"^text$", "NVARCHAR(MAX)"),
    (r"^bytea$", "VARBINARY(MAX)"),
    (r"^uuid$", "UNIQUEIDENTIFIER"),
    (r"^date$", "DATE"),
    (r"^time( without time zone)?(\(\d+\))?$", r"TIME\2"),
    (r"^timetz(\(\d+\))?$", r"TIME\1"),
    (r"^timestamp(\(\d+\))? without time zone$", r"DATETIME2\1"),
    (r"^timestamp(\(\d+\))? with time zone$", r"DATETIMEOFFSET\1"),
    (r"^timestamp without time zone(\(\d+\))?$", r"DATETIME2\1"),
    (r"^timestamp with time zone(\(\d+\))?$", r"DATETIMEOFFSET\1"),
    (r"^timestamp(\(\d+\))?$", r"DATETIME2\1"),
    (r"^jsonb?$", "NVARCHAR(MAX)"),
    (r"^xml$", "XML"),
]

# PG types with no clean MSSQL equivalent.
_PG_REVIEW_TYPES = ("array", "hstore", "int4range", "int8range", "numrange",
                    "tsrange", "tstzrange", "daterange", "inet", "cidr", "macaddr", "point")


def convert_type(data_type: str, source_dialect: str, target_dialect: str) -> tuple[str | None, str | None]:
    """Map a source-native type to a target type.

    Returns (target_type, warning). target_type is None for unmappable types
    (caller must skip the object and record the warning).
    """
    if source_dialect == "tsql" and target_dialect == "postgres":
        base, params = _split_type(data_type)
        base_upper = base.upper()
        mapped = _MSSQL_TYPES.get(base_upper)
        if mapped is None:
            if base_upper in MSSQL_TO_POSTGRES_TYPE_OVERRIDES:
                mapped = MSSQL_TO_POSTGRES_TYPE_OVERRIDES[base_upper]
        if mapped is None:
            if base_upper in ("DECIMAL", "NUMERIC"):
                mapped = f"NUMERIC({params})" if params else "NUMERIC"
            elif base_upper in ("VARCHAR", "CHAR"):
                mapped = f"{base_upper}({params})" if params else "TEXT"
            elif base_upper in ("NVARCHAR", "NCHAR"):
                # PostgreSQL has no N-prefixed types — VARCHAR/CHAR hold unicode.
                mapped = f"{base_upper[1:]}({params})" if params else "TEXT"
            else:
                return None, f"Type '{base_upper}' has no PostgreSQL equivalent — manual review required"
        if base_upper in ("GEOGRAPHY", "GEOMETRY", "HIERARCHYID", "SQL_VARIANT", "ROWVERSION"):
            return mapped, f"Type '{base_upper}' has no clean PostgreSQL equivalent — verify converted output"
        return mapped, None

    if source_dialect == "postgres" and target_dialect == "tsql":
        lowered = data_type.strip().lower()
        for pattern, replacement in _PG_TYPES:
            m = re.match(pattern, lowered)
            if m:
                return m.expand(replacement), None
        for review in _PG_REVIEW_TYPES:
            if review in lowered:
                return None, f"PostgreSQL type '{lowered}' has no clean SQL Server equivalent — manual review required"
        return None, f"Type '{lowered}' has no SQL Server equivalent — manual review required"

    return data_type, None


def _split_type(data_type: str) -> tuple[str, str | None]:
    match = re.match(r"^(\w+)(?:\(([^)]*)\))?$", data_type.strip())
    if not match:
        return data_type.strip(), None
    return match.group(1), match.group(2)


def build_database_ddl(database: Database, target_dialect: str) -> tuple[list[str], list[str]]:
    """Return (statements, warnings) for the target dialect, dependency-ordered."""
    statements: list[str] = []
    warnings: list[str] = []
    source = database.dialect

    for table in database.all_tables_in_dependency_order():
        stmts, tw = build_table_ddl(table, target_dialect, source)
        statements.extend(stmts)
        warnings.extend(tw)

    # Constraints/indexes/FKs added after all tables exist so FK targets exist.
    for table in database.all_tables_in_dependency_order():
        for uc in table.unique_constraints:
            stmts, tw = build_unique_constraint_ddl(table, uc, target_dialect)
            statements.extend(stmts)
            warnings.extend(tw)
        for idx in table.indexes:
            stmts, tw = build_index_ddl(table, idx, target_dialect, source)
            statements.extend(stmts)
            warnings.extend(tw)
        for fk in table.foreign_keys:
            stmts, tw = build_foreign_key_ddl(table, fk, target_dialect)
            statements.extend(stmts)
            warnings.extend(tw)
        for check in table.check_constraints:
            statements.append(build_check_ddl(table, check, target_dialect))

    for view in database.views:
        stmts, tw = build_view_ddl(view, target_dialect, source)
        statements.extend(stmts)
        warnings.extend(tw)

    for fn in database.functions:
        stmts, tw = build_function_ddl(fn, target_dialect, database.tables)
        statements.extend(stmts)
        warnings.extend(tw)

    for proc in database.procedures:
        stmts, tw = build_procedure_ddl(proc, target_dialect, database.tables)
        statements.extend(stmts)
        warnings.extend(tw)

    for trig in database.triggers:
        stmts, tw = build_trigger_ddl(trig, target_dialect)
        statements.extend(stmts)
        warnings.extend(tw)

    table_names = [t.name for t in database.tables]
    statements = [_qualify_body_refs(s, database.tables, target_dialect) for s in statements]

    return statements, warnings


def build_table_ddl(table: Table, target_dialect: str, source_dialect: str) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    col_lines = []
    for col in table.columns:
        line, warn = _build_column(col, target_dialect, source_dialect)
        if line is None:
            warnings.append(f"Table '{table.name}' column '{col.name}': {warn}")
            continue
        col_lines.append(line)
        if warn:
            warnings.append(f"Table '{table.name}' column '{col.name}': {warn}")

    if not col_lines:
        return [], [f"Table '{table.name}' had no convertible columns and was skipped"]

    pk_line = ""
    if table.primary_key:
        pk_line = f",\n  CONSTRAINT {_qident(table.primary_key.name, target_dialect)} PRIMARY KEY ({_qcols(table.primary_key.columns, target_dialect)})"

    create = (
        f"CREATE TABLE {_qident(table.name, target_dialect)} (\n"
        f"  {',\n  '.join(col_lines)}{pk_line}\n)"
    )
    return [create], warnings


def _build_column(col: Column, target: str, source: str) -> tuple[str | None, str | None]:
    target_type, warn = convert_type(col.data_type, source, target)
    if target_type is None:
        return None, warn

    parts = [f"{_qident(col.name, target)} {target_type}"]

    identity = ""
    if col.is_identity and not col.is_computed:
        seed = col.identity_seed if col.identity_seed is not None else 1
        inc = col.identity_increment if col.identity_increment is not None else 1
        if target == "postgres":
            identity = f" GENERATED BY DEFAULT AS IDENTITY (START WITH {seed} INCREMENT BY {inc})"
        else:
            identity = f" IDENTITY({seed},{inc})"

    parts[0] += identity

    default = _translate_default(col.default, target)
    if default:
        if target == "postgres" and target_type.upper() == "BOOLEAN" and default in ("1", "0"):
            default = "true" if default == "1" else "false"
        parts[0] += f" DEFAULT {default}"
    elif col.is_computed:
        if target == "postgres":
            parts[0] += f" GENERATED ALWAYS AS ({_translate_expr(col.computed_definition, 'tsql', 'postgres')}) STORED"
        else:
            parts[0] += f" AS ({col.computed_definition}) PERSISTED"

    if not col.nullable and not col.is_computed:
        parts[0] += " NOT NULL"

    return parts[0], warn


def build_unique_constraint_ddl(table: Table, uc: Constraint, target: str) -> tuple[list[str], list[str]]:
    stmt = (
        f"ALTER TABLE {_qident(table.name, target)} ADD CONSTRAINT "
        f"{_qident(uc.name, target)} UNIQUE ({_qcols(uc.columns, target)})"
    )
    return [stmt], []


def build_index_ddl(table: Table, index: Index, target: str, source: str) -> tuple[list[str], list[str]]:
    unique = "UNIQUE " if index.unique else ""
    stmt = f"CREATE {unique}INDEX {_qident(index.name, target)} ON {_qident(table.name, target)} ({_qcols(index.columns, target)})"
    if index.where and target == "tsql":
        stmt += f" WHERE {index.where}"
    elif index.where:
        # PostgreSQL keeps filtered indexes as partial indexes.
        stmt += f" WHERE {_translate_expr(index.where, source, target)}"
    return [stmt], []


def build_foreign_key_ddl(table: Table, fk: ForeignKey, target: str) -> tuple[list[str], list[str]]:
    stmt = (
        f"ALTER TABLE {_qident(table.name, target)} ADD CONSTRAINT {_qident(fk.name, target)} "
        f"FOREIGN KEY ({_qcols(fk.columns, target)}) "
        f"REFERENCES {_qident(fk.ref_table, target)} ({_qcols(fk.ref_columns, target)})"
    )
    if fk.on_delete.upper() not in ("NO ACTION",):
        stmt += f" ON DELETE {fk.on_delete}"
    if fk.on_update.upper() not in ("NO ACTION",):
        stmt += f" ON UPDATE {fk.on_update}"
    return [stmt], []


def build_check_ddl(table: Table, check_def: str, target: str) -> str:
    if target == "postgres":
        # MSSQL sys.check_constraints.definition looks like "([Status]=N'Pending')"
        expr = _translate_expr(check_def.strip(), "tsql", "postgres")
        if not expr.upper().startswith("CHECK"):
            expr = f"CHECK {expr}"
    else:
        # pg_get_constraintdef returns "CHECK ((Price >= 0))" — normalize identifiers back
        expr = _translate_expr(check_def.strip(), "postgres", "tsql")
    return f"ALTER TABLE {_qident(table.name, target)} ADD CONSTRAINT {_qident(_anon_name(table, check_def), target)} {expr}"


def build_view_ddl(view: View, target: str, source: str) -> tuple[list[str], list[str]]:
    definition = (view.definition or "").strip()
    m = re.search(r"\bAS\s+(SELECT\b.*)$", definition, re.IGNORECASE | re.DOTALL)
    if m:
        definition = m.group(1)
    definition = _translate_expr(definition, source, target)
    prefix = "CREATE OR REPLACE VIEW" if target == "postgres" else "CREATE OR ALTER VIEW"
    stmt = f"{prefix} {_qident(view.name, target)} AS {definition}"
    return [stmt], []


def build_function_ddl(fn: Routine, target: str, tables: list | None = None) -> tuple[list[str], list[str]]:
    from engine.translators.procedure_translator import translate_routine
    converted, warnings = translate_routine(fn.definition, source=f"{fn.kind}", target=target, tables=tables)
    if converted:
        return [converted], warnings
    return [], [f"Function '{fn.name}' could not be converted: {warnings}"]


def build_procedure_ddl(proc: Routine, target: str, tables: list | None = None) -> tuple[list[str], list[str]]:
    from engine.translators.procedure_translator import translate_routine
    converted, warnings = translate_routine(proc.definition, source="procedure", target=target, tables=tables)
    if converted:
        return [converted], warnings
    return [], [f"Procedure '{proc.name}' could not be converted: {warnings}"]


def build_trigger_ddl(trigger: Trigger, target: str) -> tuple[list[str], list[str]]:
    from engine.translators.trigger_translator import translate_trigger
    converted, warnings = translate_trigger(trigger, target)
    if converted:
        return [converted], warnings
    return [], [f"Trigger '{trigger.name}' could not be converted: {warnings}"]


# ---------------------------------------------------------------------------
# expression / default translation (best-effort regex)
# ---------------------------------------------------------------------------

def _translate_default(expr: str | None, target: str) -> str | None:
    if not expr:
        return None
    expr = expr.strip()
    if target == "postgres":
        expr = re.sub(r"\bGETDATE\(\)", "CURRENT_TIMESTAMP", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bNEWID\(\)", "gen_random_uuid()", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bNEWSEQUENTIALID\(\)", "gen_random_uuid()", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bSYSUTCDATETIME\(\)", "CURRENT_TIMESTAMP", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bSYSDATETIME\(\)", "CURRENT_TIMESTAMP", expr, flags=re.IGNORECASE)
    else:
        expr = re.sub(r"\bCURRENT_TIMESTAMP\b", "GETDATE()", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bgen_random_uuid\(\)", "NEWID()", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bnow\(\)", "GETDATE()", expr, flags=re.IGNORECASE)
        if expr.lower() == "true":
            expr = "1"
        elif expr.lower() == "false":
            expr = "0"
    return expr


def _translate_expr(expr: str, source: str, target: str) -> str:
    from engine.translators.functions import translate_functions
    out = translate_functions(expr, source, target)
    if target == "postgres":
        # MSSQL bracket identifiers -> PG quoted identifiers
        out = re.sub(r"\[([\w\s\d_]+)\]", r'"\1"', out)
        # MSSQL N-prefixed string literals have no PG equivalent
        out = re.sub(r"\bN'", "'", out, flags=re.IGNORECASE)
    else:
        # PG quoted identifiers -> MSSQL brackets
        out = re.sub(r'"([\w\s\d_]+)"', r"[\1]", out)
    return out


# ---------------------------------------------------------------------------
# identifier helpers
# ---------------------------------------------------------------------------

def _qident(name: str, target: str) -> str:
    if target == "postgres":
        name = pg_ident(name)
        if "." in name:
            return ".".join(f'"{part}"' for part in name.split("."))
        return f'"{name}"'
    if "." in name:
        return ".".join(f"[{part}]" for part in name.split("."))
    return f"[{name}]"


def _qcols(cols: list[str], target: str) -> str:
    return ", ".join(_qident(c, target) for c in cols)


def pg_ident(name: str) -> str:
    """Truncate an identifier to PostgreSQL's 63-byte limit, deduplicating
    via a short hash so distinct long names stay distinct."""
    if len(name.encode("utf-8")) <= _PG_MAX_IDENT:
        return name
    head = name.encode("utf-8")[:_PG_MAX_IDENT - 9].decode("utf-8", "ignore")
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    return f"{head}_{digest}"


def _anon_name(table: Table, definition: str) -> str:
    return f"{table.name.replace('.', '_')}_chk"


def _qualify_table_refs(text: str, table_names: list[str], target: str) -> str:
    """Qualify bare table references inside view/function/procedure bodies.

    MSSQL bodies reference ``Orders`` and resolve via the default dbo schema;
    PostgreSQL identifiers are case-sensitive so the migrated (quoted) table
    ``"dbo"."Orders"`` would never be found. Every bare reference that matches
    a known table name (case-insensitively, at a word boundary) is rewritten
    to its schema-qualified quoted form. Structural DDL is untouched because
    those identifiers are already quoted.
    """
    for tname in sorted(table_names, key=len, reverse=True):
        bare = tname.split(".")[-1]
        qualified = _qident(tname, target)
        text = re.sub(
            rf"(?<![.\w\"\[])\b{re.escape(bare)}\b(?![.\w\"\[])",
            qualified,
            text,
            flags=re.IGNORECASE,
        )
    return text


_COL_MASK = 0  # "\x00n\x00" marker
_IDENT_MASK = 1  # "\x01n\x01" marker
_SQL_KEYWORDS = frozenset(
    "select from where join on as group by order having sum count avg min max top distinct "
    "into insert update delete values and or not null is like between in exists case when then "
    "else end inner outer left right full cross union all set table create alter drop index view "
    "procedure function trigger if while begin declare print raiserror throw waitfor commit rollback "
    "exec execute use default primary foreign references constraint unique check identity asc desc "
    "with collate convert cast return returns returning returning limit offset".split()
)


def _qualify_body_refs(text: str, tables: list, target: str) -> str:
    """Rewrite body expressions so unquoted MSSQL references resolve in PostgreSQL.

    MSSQL bodies are written case-insensitively (``FROM Orders``, ``o.CustomerID``);
    PostgreSQL quoted identifiers are case-sensitive. We (1) qualify bare table
    names to their quoted schema-qualified form and (2) quote column references
    using the original casing captured from the source schema. String literals
    and already-quoted identifiers are masked so they are never rewritten.
    """
    if target != "postgres":
        return _qualify_table_refs(text, [t.name for t in tables], target)

    masked, stash = _mask(text)
    masked = _qualify_table_refs(masked, [t.name for t in tables], target)

    col_casing: dict[str, str] = {}
    for table in tables:
        for col in table.columns:
            col_casing.setdefault(col.name.lower(), col.name)

    # Bare references to a function's own parameters must stay unquoted so
    # PL/pgSQL resolves them as variables rather than ambiguous columns.
    param_names = _signature_param_names(masked)

    def _repl(match) -> str:
        alias, col = match.group(1), match.group(2)
        if col is None:
            alias, col = "", match.group(3)
            if col.lower() in param_names:
                return match.group(0)
        if col.lower() in _SQL_KEYWORDS:
            return match.group(0)
        canon = col_casing.get(col.lower())
        if not canon:
            return match.group(0)
        return f'{alias}."{canon}"' if alias else f'"{canon}"'

    masked = re.sub(r"(\b\w+)\.([A-Za-z_]\w*)|(?<![\w.\"])([A-Za-z_]\w*)", _repl, masked)
    return _unmask(masked, stash)


def _signature_param_names(statement: str) -> set[str]:
    """Extract parameter names from `FUNCTION name(...)` signatures."""
    m = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+\w+(?:\.\w+)?\s*\((.*?)\)", statement,
                  re.IGNORECASE | re.DOTALL)
    if not m:
        return set()
    names: set[str] = set()
    for part in m.group(1).split(","):
        pm = re.match(r'^\s*(?:(?:OUT|IN)\s+)?"?([\w]+)"?\s+', part)
        if pm:
            names.add(pm.group(1).lower())
    return names


def _mask(text: str) -> tuple[str, list[str]]:
    """Hide string literals and quoted identifiers so regex rewrites skip them."""
    stash: list[str] = []

    def _s1(m):
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    def _s2(m):
        stash.append(m.group(0))
        return f"\x01{len(stash) - 1}\x01"

    text = re.sub(r"'([^']*)'", _s1, text)
    text = re.sub(r'"([^"]*)"', _s2, text)
    return text, stash


def _unmask(text: str, stash: list[str]) -> str:
    for i, s in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", s).replace(f"\x01{i}\x01", s)
    return text
