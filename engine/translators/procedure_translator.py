"""
Stored procedure / user-defined function translator: T-SQL <-> PL/pgSQL.

This is a pragmatic, best-effort translator. It reliably converts the common
constructs found in real migration projects — parameters, DECLARE/SET
variables, IF/ELSE/WHILE blocks, TRY/CATCH, RAISE/PRINT, temp tables, RETURN,
assignment SELECTs, INSERT/UPDATE/DELETE — and emits warnings for anything it
cannot translate safely (it never silently drops a statement).

The body is transformed line-by-line while tracking block nesting (BEGIN/
END, IF, WHILE, TRY/CATCH). Expressions within statements are pushed through
the builtin-function translator.
"""
from __future__ import annotations

import re

from engine.translators.functions import translate_functions
from engine.translators.sql_builder import convert_type, fix_boolean_predicates, _translate_top

# A T-SQL type token: int, [int], decimal(18,2), [decimal](18,2), varchar(50),
# or a user-defined schema-qualified type such as dbo.ProductBulkType.
_TSQL_TYPE_RE = r"\[?[A-Za-z_][A-Za-z0-9_]*\]?(?:\s*\.\s*\[?[A-Za-z_][A-Za-z0-9_]*\]?)?\s*(?:\([^)]*\))?"

PARAM_RE = re.compile(
    rf"@([A-Za-z_][A-Za-z0-9_]*)\s+"
    rf"(?:AS\s+|IS\s+)?"
    rf"({_TSQL_TYPE_RE})"
    r"\s*(OUTPUT|OUT)?"
    r"(?:\s*=\s*[^,]+)?",
    re.IGNORECASE,
)


def _strip_sql_comments(sql: str) -> str:
    """Remove block comments (/* ... */) and line comments (-- ...) from SQL."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _routine_error_context(sql: str, max_len: int = 80) -> str:
    """Return a truncated first line of the definition for error messages."""
    first = sql.strip().split("\n")[0].strip()[:max_len]
    return first + ("..." if len(sql.strip().split("\n")[0].strip()) > max_len else "")


# ---------------------------------------------------------------------------
# T-SQL -> PL/pgSQL
# ---------------------------------------------------------------------------

_SPT_VALUES_RE = re.compile(r"\bspt_values\b", re.IGNORECASE)


def _tsql_to_plpgsql(sql: str, tables: list | None = None,
                     user_types: list | None = None) -> tuple[str | None, list[str]]:
    warnings: list[str] = []

    sql = _strip_sql_comments(sql)

    if not sql.strip():
        return None, ["Routine definition is empty — object may be encrypted or inaccessible"]

    # `master..spt_values` is a SQL Server internal numbers table. Routines
    # built on it are the dynamic-SQL + PIVOT report generators, which have no
    # PostgreSQL form at all. Emitting a guessed translation would compile but
    # never run correctly, so the routine is reported for manual rewrite.
    if _SPT_VALUES_RE.search(sql):
        return None, [
            "references master..spt_values (a SQL Server internal numbers "
            "table) — this routine builds a dynamic PIVOT and has no "
            "PostgreSQL equivalent; rewrite it by hand using generate_series()"
        ]

    # ---- header -----------------------------------------------------------
    header_match = re.search(
        r"CREATE\s+(?:OR\s+ALTER\s+)?(PROCEDURE|PROC|FUNCTION)\s*"
        r"(?:\[?[\w\d_]+\]?\.)?\[?([\w\d_]+)\]?"
        # Parenthesized parameter lists may themselves contain parentheses
        # (DECIMAL(18,2)); require the closing paren to be followed by a
        # header keyword so body predicates are never mistaken for params.
        r"(?:\s*\((.*?)\)(?=\s*(?:WITH|AS|RETURNS)\b)|\s+(@.*?))?"
        r"(?=\s*(?:WITH|AS|RETURNS)\b|\s*$)",
        sql, re.IGNORECASE | re.DOTALL,
    )
    if not header_match:
        ctx = _routine_error_context(sql)
        return None, [f"Could not parse routine header — starts with: {ctx}"]

    kind = header_match.group(1).upper()
    name = header_match.group(2)
    param_text = header_match.group(3) or header_match.group(4) or ""

    # ---- RETURNS clause for functions --------------------------------------
    returns = "void"
    if kind == "FUNCTION":
        ret_match = re.search(r"RETURNS\s+(.+?)\s*(?:WITH\b|AS\b|BEGIN\b|RETURN\b)", sql, re.IGNORECASE | re.DOTALL)
        if ret_match:
            returns = ret_match.group(1).strip()
    return_table_name, declared_return_columns = _parse_tsql_table_return(returns)

    # ---- body -------------------------------------------------------------
    body = _extract_tsql_body(sql)
    if body is None:
        ctx = _routine_error_context(sql[header_match.end():])
        return None, [f"Could not extract routine body — found: {ctx}"]

    declared_tables = _extract_tsql_table_declarations(body)
    if (kind == "FUNCTION" and "table" in returns.lower()
            and not declared_return_columns and len(declared_tables) == 1):
        return_table_name, declared_return_columns, start, end = declared_tables[0]
        body = body[:start] + body[end:]
    else:
        # PostgreSQL has no SQL Server table variables. They are local to the
        # routine invocation, which maps cleanly to transaction-local temp
        # tables. Replace the balanced declaration before line parsing so it
        # cannot be mistaken for a scalar variable of type TABLE.
        for table_name, columns, start, end in reversed(declared_tables):
            column_sql = ", ".join(
                f'"{column_name}" {data_type}'
                for column_name, data_type in columns
            )
            replacement = (
                f"CREATE TEMP TABLE {table_name} ({column_sql}) ON COMMIT DROP;"
            )
            body = body[:start] + replacement + body[end:]

    params, param_warns = _parse_params(param_text, "tsql")
    warnings.extend(param_warns)

    table_params: dict[str, object] = {}
    for pname, ptype, _output in params:
        normalized = re.sub(r"[\[\]\"\s]", "", ptype).casefold()
        for user_type in user_types or []:
            full_name = re.sub(r"[\[\]\"\s]", "", user_type.name).casefold()
            if user_type.kind == "table_type" and normalized in {full_name, full_name.rsplit(".", 1)[-1]}:
                table_params[pname.casefold()] = user_type
                break

    # SQL Server TVPs are relations. PostgreSQL receives an array of composite
    # rows, so every FROM/JOIN reference must expand that array with unnest().
    for pname, _ptype, _output in params:
        if pname.casefold() not in table_params:
            continue
        pattern = re.compile(
            rf"\b(FROM|JOIN)\s+@{re.escape(pname)}\b"
            rf"(?:\s+(?:AS\s+)?(?!(?:WHERE|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|ON|ORDER|GROUP)\b)"
            rf"([A-Za-z_][A-Za-z0-9_]*))?",
            re.IGNORECASE,
        )
        def replace_tvp(match):
            alias = match.group(2)
            suffix = f" AS {alias}" if alias else f" AS {pname}"
            return f"{match.group(1)} unnest(@{pname}){suffix}"
        body = pattern.sub(replace_tvp, body)

    # detect result-returning SELECTs
    has_result_select = _has_result_select(body)

    # ---- transform body ----------------------------------------------------
    transformed, t_warns, declared = _transform_tsql_body(body, kind, has_result_select)
    warnings.extend(t_warns)

    if return_table_name and declared_return_columns:
        transformed = _rewrite_return_table_writes(
            transformed, return_table_name, warnings,
        )

    # Comparisons against BIT/BOOLEAN columns (col = 1) are invalid in
    # PostgreSQL (boolean = integer) — rewrite to true/false.
    if tables:
        transformed = [fix_boolean_predicates(ln, tables, "tsql") for ln in transformed]

    if kind == "FUNCTION":
        if returns.lower().strip() == "table" or "table" in returns.lower():
            result_columns = declared_return_columns or _infer_result_columns(transformed, tables)
            if not result_columns:
                return None, warnings + [
                    "Table-valued function result columns could not be inferred safely — manual conversion required"
                ]
            sig_returns = "TABLE(" + ", ".join(
                f'"{column}" {data_type}' for column, data_type in result_columns
            ) + ")"
        else:
            # Scalar return types are still T-SQL types — map to PostgreSQL
            # (nvarchar -> TEXT, decimal(p,s) -> NUMERIC(p,s), ...).
            converted_returns, ret_warn = convert_type(returns, "tsql", "postgres")
            if ret_warn:
                warnings.append(f"RETURNS {returns}: {ret_warn}")
            sig_returns = converted_returns or "TEXT"
    else:
        sig_returns = ""

    param_list = []
    for pname, ptype, output in params:
        table_type = table_params.get(pname.casefold())
        if table_type is not None:
            parts = [part.strip('[]"') for part in table_type.name.split(".")]
            tgt_type = ".".join(f'"{part}"' for part in parts) + "[]"
            warn = None
        else:
            tgt_type, warn = convert_type(ptype, "tsql", "postgres")
        if warn:
            warnings.append(f"Parameter @{pname} type '{ptype}': {warn}")
        tgt_type = tgt_type or "TEXT"
        if output:
            param_list.append(f"OUT {_safe_var(pname)} {tgt_type}")
        else:
            param_list.append(f"{_safe_var(pname)} {tgt_type}")
    if kind in ("PROCEDURE", "PROC"):
        transformed, cursor_params = _procedure_result_cursors(transformed)
        param_list.extend(cursor_params)

    warnings.extend(_ambiguous_variable_warnings(name, params, declared, tables))

    signature = ", ".join(param_list)
    if kind == "FUNCTION":
        header = f"CREATE OR REPLACE FUNCTION {name}({signature}) RETURNS {sig_returns} AS $$"
    else:
        header = f"CREATE OR REPLACE PROCEDURE {name}({signature}) AS $$"
    footer = "$$ LANGUAGE plpgsql;"

    converted = _assemble_plpgsql(header, declared, transformed, footer)
    return converted, warnings


def _ambiguous_variable_warnings(routine: str, params: list[tuple[str, str, bool]],
                                declared: dict[str, str], tables: list | None) -> list[str]:
    """Warn when a routine variable shares a name with a migrated column.

    T-SQL keeps ``@Status`` and the column ``Status`` apart with the sigil.
    PL/pgSQL has no sigil, so after conversion the bare name is ambiguous and
    PostgreSQL rejects the reference at run time. The conversion cannot pick a
    side safely, so the collision is reported instead of guessed.
    """
    if not tables:
        return []
    columns = {
        column.name.lower()
        for table in tables
        for column in table.columns
    }
    names = [pname for pname, _type, _out in params] + list(declared)
    clashing = sorted({n for n in names if n.lower() in columns}, key=str.lower)
    if not clashing:
        return []
    return [
        f"Routine '{routine}': variable(s) {', '.join(clashing)} share a name with a "
        f"table column — PostgreSQL cannot tell the variable from the column, so "
        f"qualify the column (or rename the parameter) before using this routine"
    ]


def _parse_tsql_table_return(returns: str) -> tuple[str | None, list[tuple[str, str]]]:
    """Parse ``RETURNS @result TABLE (col type, ...)`` from a multi-statement TVF."""
    match = re.match(
        r'^@?([A-Za-z_]\w*)\s+TABLE\s*\((.*)\)\s*$',
        returns.strip(), re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None, []

    return match.group(1), _parse_tsql_table_columns(match.group(2))


def _parse_tsql_table_columns(definition: str) -> list[tuple[str, str]]:
    columns: list[tuple[str, str]] = []
    for item in _split_args_balanced(definition):
        column = re.match(
            rf'^\s*(?:\[([^]]+)\]|"([^"]+)"|([A-Za-z_]\w*))\s+({_TSQL_TYPE_RE})',
            item, re.IGNORECASE | re.DOTALL,
        )
        if not column:
            return []
        name = column.group(1) or column.group(2) or column.group(3)
        source_type = column.group(4).strip()
        target_type, _warning = convert_type(source_type, "tsql", "postgres")
        if target_type is None:
            return []
        columns.append((name, target_type))
    return columns


def _extract_tsql_table_declarations(body: str) -> list[tuple[str, list[tuple[str, str]], int, int]]:
    """Find balanced ``DECLARE @name TABLE (...)`` definitions in a body."""
    declarations = []
    pattern = re.compile(r"\bDECLARE\s+@([A-Za-z_]\w*)\s+TABLE\s*\(", re.IGNORECASE)
    for match in pattern.finditer(body):
        open_paren = match.end() - 1
        depth, in_string, index = 1, False, open_paren + 1
        while index < len(body) and depth:
            char = body[index]
            if in_string:
                if char == "'":
                    if index + 1 < len(body) and body[index + 1] == "'":
                        index += 1
                    else:
                        in_string = False
            elif char == "'":
                in_string = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth:
            continue
        columns = _parse_tsql_table_columns(body[open_paren + 1:index - 1])
        if columns:
            end = index
            while end < len(body) and body[end] in " \t;":
                end += 1
            declarations.append((match.group(1), columns, match.start(), end))
    return declarations


def _rewrite_return_table_writes(transformed: list[str], table_name: str,
                                 warnings: list[str]) -> list[str]:
    """Turn inserts into a SQL Server TVF return variable into RETURN QUERY."""
    output: list[str] = []
    target = re.escape(table_name)
    for statement in transformed:
        match = re.match(
            rf'^INSERT\s+INTO\s+"?{target}"?\s*(?:\([^)]*\))?\s+(SELECT\b.*);$',
            statement, re.IGNORECASE | re.DOTALL,
        )
        if match:
            output.append(f"RETURN QUERY {match.group(1)};")
            continue
        values_match = re.match(
            rf'^INSERT(?:\s+INTO)?\s+"?{target}"?\s*(?:\([^)]*\))?\s+'
            rf'VALUES\s*\((.*)\);$',
            statement, re.IGNORECASE | re.DOTALL,
        )
        if values_match:
            output.append(f"RETURN QUERY SELECT {values_match.group(1)};")
            continue
        if re.search(rf'\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"?{target}"?\b',
                     statement, re.IGNORECASE):
            warnings.append(
                f"Return table '@{table_name}' contains a non-SELECT write that requires manual review"
            )
        output.append(statement)
    return output


def _procedure_result_cursors(transformed: list[str]) -> tuple[list[str], list[str]]:
    """Expose SQL Server procedure result sets as PostgreSQL refcursors."""
    out, cursor_params = [], []
    cursor_number = 0
    for line in transformed:
        if line.startswith("RETURN QUERY "):
            cursor_number += 1
            cursor = "result_cursor" if cursor_number == 1 else f"result_cursor_{cursor_number}"
            query = line[len("RETURN QUERY "):]
            out.append(f"OPEN {cursor} FOR {query}")
            cursor_params.append(f"INOUT {cursor} refcursor DEFAULT '{cursor}'")
        else:
            out.append(line)
    return out, cursor_params


def _assemble_plpgsql(header: str, declared: dict[str, str], transformed: list[str], footer: str) -> str:
    """Join header, optional DECLARE block, and a BEGIN...END body."""
    body_lines = []
    if declared:
        body_lines.append("DECLARE")
        body_lines.append(_join_declares(declared))
    body_lines.append("BEGIN")
    body_lines.extend(transformed)
    body_lines.append("END;")
    return header + "\n" + "\n".join(body_lines) + "\n" + footer


# ``AS BEGIN TRY`` opens an error-handling block, not the routine body. Treating
# its BEGIN as the body wrapper shifted every block one level and consumed the
# ``END`` of ``END CATCH`` as the routine's closing END.
_BODY_BEGIN_RE = re.compile(
    r"\bAS\s+BEGIN\b(?!\s*(?:TRY|CATCH|TRANSACTION|TRAN|WORK|DISTRIBUTED)\b)",
    re.IGNORECASE,
)
_TRAILING_END_RE = re.compile(r"\bEND\b(?!\s*(?:TRY|CATCH)\b)", re.IGNORECASE)


def _extract_tsql_body(sql: str) -> str | None:
    """Return the text between the trailing AS [BEGIN] ... final END."""
    m = _BODY_BEGIN_RE.search(sql)
    if not m:
        # functions like inline TVF: AS RETURN (SELECT ...); also procedures
        # whose body opens directly with BEGIN TRY or a bare statement.
        m2 = re.search(r"\bAS\s+(.*)$", sql, re.IGNORECASE | re.DOTALL)
        return m2.group(1).strip() if m2 else None
    start = m.end()
    # find matching final END (last standalone END that is not END TRY/CATCH)
    tail = sql[start:]
    end_matches = list(_TRAILING_END_RE.finditer(tail))
    if not end_matches:
        return None
    end_pos = end_matches[-1].start()
    return tail[:end_pos]


def _parse_params(param_text: str, source: str) -> tuple[list[tuple[str, str, bool]], list[str]]:
    params = []
    warnings = []
    if not param_text.strip():
        return params, warnings
    for pname, ptype, output in PARAM_RE.findall(param_text):
        params.append((pname, ptype.strip(), bool(output)))
    return params, warnings


def _has_result_select(body: str) -> bool:
    # bare SELECT returning rows: not "SELECT @x =", not "SELECT INTO #"
    stripped = re.sub(r"SELECT\s+(?:TOP\s*(?:\(\d+\)|\d+)\s+)?@[\w]+\s*=", "", body, flags=re.IGNORECASE)
    stripped = re.sub(r"SELECT\s+[^#]*?\s+INTO\s+", "", stripped, flags=re.IGNORECASE)
    return bool(re.search(r"\bSELECT\b", stripped, flags=re.IGNORECASE))


def _infer_result_columns(transformed: list[str], tables: list | None) -> list[tuple[str, str]]:
    """Guess (alias, type) for the result columns of the first RETURN QUERY.

    Column references are resolved against the source schema when available;
    aggregates map to NUMERIC/BIGINT and anything unknown to text. Returns []
    when there is no result SELECT or it can't be parsed, in which case the
    caller falls back to SETOF record.
    """
    if not tables:
        return []
    for line in transformed:
        if not line.startswith("RETURN QUERY "):
            continue
        select_list = _top_level_select_list(line[len("RETURN QUERY "):].strip())
        if not select_list:
            continue
        cols = []
        for i, item in enumerate(select_list):
            alias = _item_alias(item) or f"column{i + 1}"
            coltype = _infer_column_type(item, tables)
            cols.append((alias, coltype))
        return cols
    return []


def _item_alias(item: str) -> str | None:
    """Return the AS-alias of a select-list item, or the bare column name."""
    m = re.match(r"^(.*?)\s+AS\s+([A-Za-z_]\w*)$", item, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(2)
    m = re.match(r"^[A-Za-z_]\w*\.([A-Za-z_]\w*)$", item.strip())
    if m:
        return m.group(1)
    m = re.match(r"^([A-Za-z_]\w*)$", item.strip())
    return m.group(1) if m else None


def _infer_column_type(item: str, tables: list) -> str:
    name = _item_alias(item) or ""
    for table in tables:
        for col in table.columns:
            if col.name.lower() == name.lower():
                tgt, _ = convert_type(col.data_type, "tsql", "postgres")
                return tgt or "text"
    if re.match(r"^COUNT\s*\(", item, re.IGNORECASE):
        return "BIGINT"
    if re.match(r"^(SUM|AVG|MIN|MAX)\s*\(", item, re.IGNORECASE):
        return "NUMERIC"
    return "text"


def _top_level_select_list(select_stmt: str) -> list[str]:
    """Return the top-level (comma-split) select list of a SELECT statement."""
    m = re.match(r"^\s*SELECT\s+(.*)$", select_stmt, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    rest = m.group(1)
    depth, quote = 0, None
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and _is_keyword_at(rest, i, "FROM") and i != 0:
            rest = rest[:i]
            break
    return _split_top_level(rest)


def _is_keyword_at(text: str, i: int, keyword: str) -> bool:
    end = i + len(keyword)
    if text[i:end].upper() != keyword:
        return False
    if i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
        return False
    return end >= len(text) or not (text[end].isalnum() or text[end] == "_")


def _split_top_level(text: str) -> list[str]:
    parts, depth, quote, buf = [], 0, None, ""
    for ch in text:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf += ch
        elif ch == "(":
            depth += 1
            buf += ch
        elif ch == ")":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


# Words PostgreSQL refuses as an *unquoted* PL/pgSQL variable name. Derived by
# testing every pg_get_keywords() entry plus the PL/pgSQL-only keywords against
# `DO $$ DECLARE <word> int; BEGIN END $$`. T-SQL happily allows @End / @Type /
# @Case, so colliding names are renamed (consistently at every reference).
_PLPGSQL_RESERVED_VARS = frozenset(
    "all begin by case declare else end execute for foreach from if in into "
    "not null or strict then to using when".split()
)


def _safe_var(name: str) -> str:
    """Rename a variable/parameter whose name PL/pgSQL reserves."""
    return f"v_{name}" if name.lower() in _PLPGSQL_RESERVED_VARS else name


def _join_declares(declared: dict[str, str]) -> str:
    if not declared:
        return ""
    lines = []
    for name, type_and_init in declared.items():
        lines.append(f"  {_safe_var(name)} {type_and_init};")
    return "\n".join(lines)


# Keywords that can start the single-statement body of an unparenthesized
# ``IF <cond> <stmt>`` / ``WHILE <cond> <stmt>`` (T-SQL allows the branch body
# on the same line without BEGIN/END).
_IF_BODY_START = re.compile(
    r"\b(SET|RETURN|SELECT|UPDATE|DELETE|INSERT|PRINT|RAISERROR|THROW|"
    r"BREAK|CONTINUE|WAITFOR|BEGIN|DECLARE|EXEC(?:UTE)?|MERGE|GOTO|"
    r"COMMIT|ROLLBACK|TRUNCATE|CREATE|DROP|ALTER|WHILE|IF)\b",
    re.IGNORECASE,
)


def _split_cond_body(rest: str) -> tuple[str, str]:
    """Split ``IF (cond) stmt`` into ``((cond), "stmt")``.

    When the rest starts with a balanced parenthesised condition and more
    follows on the same line, the tail is the single-statement branch body.
    Unparenthesised conditions (``IF @x IS NULL SET @y = 1``) are split at the
    first top-level statement keyword so the body is not swallowed into the
    condition (which produced invalid ``IF ... SET ... THEN`` output). When no
    such boundary exists the whole rest is the condition with no tail."""
    if not rest.startswith("("):
        cond, tail = _split_unparenthesized_cond(rest)
        return (rest, "") if tail is None else (cond, tail)
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                tail = rest[i + 1:].strip()
                # The text after a balanced group is only a branch body when it
                # actually starts a statement. `IF (PARSENAME(@t,3)) IS NOT NULL`
                # continues the *condition*, and splitting it emitted the
                # nonsense `IF (...) THEN IS NOT NULL;`.
                if tail and not _IF_BODY_START.match(tail):
                    return rest, ""
                return rest[: i + 1], tail
    return rest, ""


def _split_unparenthesized_cond(rest: str) -> tuple[str, str | None]:
    """Split an unparenthesized condition from its single-statement body.

    Returns ``(condition, body)`` with ``body=None`` when no statement keyword
    is found at paren depth 0 (so the whole rest is the condition). Keywords
    inside string literals, parenthesised subqueries and ``(x)`` grouping are
    ignored, and a keyword must be followed by whitespace/``(`` to count."""
    if not rest:
        return rest, None
    depth = 0
    in_str = False
    i = 0
    n = len(rest)
    while i < n:
        ch = rest[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and rest[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            i += 1
            continue
        if ch in "([":
            depth += 1
            i += 1
            continue
        if ch in ")]":
            depth -= 1
            i += 1
            continue
        if depth == 0:
            m = _IF_BODY_START.match(rest, i)
            if m:
                after = m.end()
                if after >= n or not (rest[after].isalnum() or rest[after] == "_"):
                    return rest[:i].strip(), rest[i:].strip()
        i += 1
    return rest, None


def _declare_continues(line: str) -> bool:
    """True when a ``DECLARE`` line continues onto the next one.

    T-SQL declares several variables in one statement across many lines
    (``DECLARE @a varchar(8000),`` / ``@b varchar(8000)``). Flushing on the
    first line declared only ``@a`` and emitted the remaining declarations as
    a bare statement, which PostgreSQL rejects.
    """
    stripped = line.rstrip().rstrip(";").rstrip()
    return stripped.endswith(",") or _paren_delta(line) != 0


def _paren_delta(text: str) -> int:
    """Net change in open-paren depth across ``text``, ignoring parens inside
    string literals and ``--`` line comments (brackets are identifiers in
    T-SQL and must NOT count as grouping)."""
    delta, in_str = 0, False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "-" and i + 1 < len(text) and text[i + 1] == "-":
            break
        elif ch == "(":
            delta += 1
        elif ch == ")":
            delta -= 1
        i += 1
    return delta


# Keywords that never continue a statement already in the buffer: a line
# starting with one begins a brand-new statement.
_STMT_START_RE = re.compile(
    r"^(?:RETURN|RAISERROR|THROW|PRINT|DECLARE|EXEC(?:UTE)?|WAITFOR|GOTO|BREAK|CONTINUE|"
    r"IF|WHILE|BEGIN|END|ELSE|UPDATE|INSERT|DELETE|MERGE|COMMIT|ROLLBACK|TRUNCATE|DROP|"
    r"CREATE|ALTER|WITH)\b",
    re.IGNORECASE,
)
_SET_START_EXCEPT = re.compile(r"^(?:UPDATE|INSERT|DELETE|MERGE)\b", re.IGNORECASE)
_MERGE_START_RE = re.compile(r"^MERGE\b", re.IGNORECASE)
_MERGE_ACTION_RE = re.compile(r"^(?:UPDATE|INSERT|DELETE|WHEN|THEN|VALUES|SET|OUTPUT)$", re.IGNORECASE)
_SET_SESSION_OPTION_RE = re.compile(r"^SET\s+\w+(?:\s+\w+)*\s+(?:ON|OFF)\s*;?\s*$", re.IGNORECASE)
_SET_VARIABLE_RE = re.compile(r"^SET\s+@\w+\s*=", re.IGNORECASE)
_SELECT_START_EXCEPT = re.compile(r"^(?:WITH|INSERT|UNION|INTERSECT|EXCEPT)\b", re.IGNORECASE)


def _ends_with_set_op(text: str) -> bool:
    """True if text ends with UNION/INTERSECT/EXCEPT (+ optional ALL)."""
    return bool(re.search(r"\b(?:UNION|INTERSECT|EXCEPT)\s*(?:ALL\s*)?$", text, re.IGNORECASE))
# Keywords that continue a previous statement (never start a new one)
_CONTINUATION_KEYWORDS = re.compile(
    r"^(?:FROM|WHERE|AND|OR|ON|INNER|LEFT|RIGHT|FULL|OUTER|CROSS|JOIN|GROUP|ORDER|HAVING|"
    r"FETCH|OFFSET|LIMIT|INTO|OVER|PARTITION|CASE|WHEN|"
    r"THEN|BETWEEN|LIKE|IN|EXISTS|NOT|AS|ASC|DESC|TOP|DISTINCT|ALL|"
    r"OPTION|RECOMPILE|FOR|OPEN|CLOSE|DEALLOCATE|NEXT|PREVIOUS|FIRST|LAST|ABSOLUTE|RELATIVE)\b",
    re.IGNORECASE,
)


def _split_top_level_else(text: str) -> tuple[str, str | None]:
    """Split ``... ELSE ...`` on the first top-level IF-ELSE keyword.

    Returns ``(before, after)`` where ``after`` is None when the ELSE belongs
    to a CASE expression (an ELSE that is preceded at depth 0 by an unclosed
    CASE is a CASE-ELSE, not an IF-ELSE)."""
    depth, in_str, case_depth = 0, False, 0
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and text[i : i + 4].upper() == "CASE":
            if (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")) and (
                i + 4 >= len(text) or not (text[i + 4].isalnum() or text[i + 4] == "_")
            ):
                case_depth += 1
        elif depth == 0 and text[i : i + 3].upper() == "END":
            if (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")) and (
                i + 3 >= len(text) or not (text[i + 3].isalnum() or text[i + 3] == "_")
            ):
                case_depth = max(0, case_depth - 1)
        elif depth == 0 and case_depth == 0 and text[i : i + 4].upper() == "ELSE":
            if (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")) and (
                i + 4 >= len(text) or not (text[i + 4].isalnum() or text[i + 4] == "_")
            ):
                return text[:i].strip(), text[i + 4:].strip()
        i += 1
    return text.strip(), None


_CONTINUATION_ENDINGS = {
    "(",
    ",",
    "+",
    "-",
    "*",
    "/",
    "%",
    "=",
    ".",
    "AND",
    "OR",
    "WHEN",
    "THEN",
    "ELSE",
    "WHERE",
    "FROM",
    "ON",
    "AS",
    "SET",
    "VALUES",
}


def _is_complete_tail(piece: str) -> bool:
    """A same-line branch body is complete when its parens balance and it does
    not dangle on a continuation token (``+``, ``,``, ``(``, ``=``, ...)."""
    if _paren_delta(piece) != 0:
        return False
    stripped = piece.rstrip().rstrip(";").rstrip()
    if not stripped:
        return False
    # A trailing operator/comma continues the statement even when it is glued
    # to the preceding token (``set col = @col,``), which the token-level check
    # below cannot see.
    if stripped[-1] in ",+-*/%=(.":
        return False
    tokens = stripped.split()
    if not tokens:
        return False
    return tokens[-1].upper() not in _CONTINUATION_ENDINGS


def _case_open(text: str) -> bool:
    """True when ``text`` contains an unclosed CASE expression (CASE keyword
    count exceeds END count at paren depth 0, outside strings). Used to keep a
    multi-line CASE from being mistaken for statement boundaries: while a CASE
    is open, lines starting with ELSE/END/THEN belong to the CASE, not to a new
    statement or a block terminator."""
    depth, in_str, cases, ends = 0, False, 0, 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if text[i : i + 4].upper() == "CASE" and (
                i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")
            ) and (i + 4 >= n or not (text[i + 4].isalnum() or text[i + 4] == "_")):
                cases += 1
                i += 3
            elif text[i : i + 3].upper() == "END" and (
                i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")
            ) and (i + 3 >= n or not (text[i + 3].isalnum() or text[i + 3] == "_")):
                ends += 1
                i += 2
        i += 1
    return cases > ends



def _normalize_statement_ends(body: str) -> str:
    """Insert ``;`` after statements whose T-SQL sources omit it but whose
    boundary is unambiguous: RAISERROR(...) once its balanced parens close, and
    single-line THROW / PRINT statements. This keeps a missing ``;`` from
    merging two statements into one buffer."""
    out = []
    i = 0
    n = len(body)
    in_str = False
    while i < n:
        ch = body[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and body[i + 1] == "'":
                    out.append(body[i + 1])
                    i += 1
                else:
                    in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and body[i + 1] == "-":
            while i < n and body[i] != "\n":
                out.append(body[i])
                i += 1
            continue
        m = re.match(r"\bRAISERROR\b\s*\(", body[i:], re.IGNORECASE)
        if m:
            out.append(body[i : i + m.end()])
            i += m.end()
            depth = 1
            while i < n and depth:
                c = body[i]
                if c == "'":
                    out.append(c)
                    i += 1
                    while i < n:
                        if body[i] == "'":
                            if i + 1 < n and body[i + 1] == "'":
                                out.append("''")
                                i += 2
                                continue
                            out.append("'")
                            i += 1
                            break
                        out.append(body[i])
                        i += 1
                    continue
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                out.append(c)
                i += 1
            j = i
            while j < n and body[j] in " \t":
                j += 1
            if j < n and body[j] not in ";":
                out.append(";")
            continue
        m = re.match(r"\b(THROW|PRINT)\b\s", body[i:], re.IGNORECASE)
        if m and _paren_delta(body[i : body.find("\n", i) if body.find("\n", i) != -1 else n]) == 0:
            end = body.find("\n", i)
            line_end = end if end != -1 else n
            line = body[i:line_end]
            stripped = line.strip()
            if not stripped.endswith(";") and not stripped.endswith("+") and not stripped.endswith(",") and not stripped.endswith("("):
                out.append(line)
                out.append(";")
            else:
                out.append(line)
            i = line_end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _unbalanced_parens(text: str) -> int:
    """Net open-paren count of ``text`` (ignoring string literals)."""
    depth = 0
    in_str = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return depth


def _join_balanced_lines(body: str) -> list[str]:
    """Split ``body`` into logical lines, joining any line that ends with an
    unbalanced ``(`` together with its following lines until the parens close.

    A multi-line T-SQL condition such as ``IF EXISTS (`` ... ``)`` must be
    treated as one line so the IF/WHILE control handling can consume it."""
    joined: list[str] = []
    acc: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("--"):
            joined.append(line)
            continue
        if acc:
            acc.append(line)
            if _unbalanced_parens(" ".join(acc)) == 0:
                joined.append(" ".join(acc))
                acc = []
            continue
        if _unbalanced_parens(line) != 0:
            acc.append(line)
        else:
            joined.append(line)
    if acc:
        joined.append(" ".join(acc))
    return joined


def _transform_tsql_statement(line: str, statement_fn, declared, t_warns, returns_set, is_procedure=False) -> str | None:
    """Rewrite a single completed statement, honoring an optional override."""
    if statement_fn is not None:
        return statement_fn(line, declared, t_warns, returns_set)
    return _transform_statement(line, declared, t_warns, returns_set, is_procedure)


def _transform_tsql_body(body: str, kind: str, returns_set: bool, statement_fn=None) -> tuple[list[str], list[str], dict[str, str]]:
    """Body transformation with statement accumulation.

    Statements may span several lines and end with ``;``, so lines are
    accumulated into a buffer until the terminating semicolon (or a
    control keyword / blank line) and transformed as a whole — a multi-line
    ``SELECT ... FROM ... JOIN ... GROUP BY ...`` must stay one statement.
    Returns (output lines, warnings, declared vars). ``statement_fn``, when
    given, overrides ``_transform_statement`` for completed statements (used
    by the trigger translator).

    Block handling models T-SQL's grammar where an IF/WHILE takes exactly one
    statement (a BEGIN...END block or a single statement) and there is no
    explicit terminator for the whole IF. Stack entries:
      ("if", "branch")  then-branch active
      ("if", "else")    else-branch active
      ("while", "branch")
      "begin" / "try"
    ``waiting`` means the top if's then-branch just completed and the next
    token decides whether an ELSE follows or the IF closes (END IF;).
    """
    body = _normalize_statement_ends(body)
    out: list[str] = []
    t_warns: list[str] = []
    declared: dict[str, str] = {}
    stack: list = []
    buf: list[str] = []
    waiting = False
    depth = 0

    def _branch_completed() -> None:
        """The top if/while entry's branch (single statement or BEGIN block)
        has finished. A branch-mode if waits for a possible ELSE; everything
        else closes immediately."""
        nonlocal waiting
        entry = stack[-1]
        if entry[0] == "if" and entry[1] == "branch":
            waiting = True
        else:
            _close_now()

    def _close_now() -> None:
        """Emit the terminator for the top if/while and pop it. If the closed
        entry was the single-statement branch of the entry below, resolve that
        too."""
        nonlocal waiting
        entry = stack.pop()
        out.append("END IF;" if entry[0] == "if" else "END LOOP;")
        waiting = False
        if stack and stack[-1][0] in ("if", "while"):
            _branch_completed()

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        stmt = _transform_tsql_statement(" ".join(buf).rstrip(";").strip(), statement_fn, declared, t_warns, returns_set, kind in ("PROCEDURE", "PROC"))
        buf = []
        if stmt is not None:
            out.append(stmt)
        if stack and stack[-1][0] in ("if", "while"):
            _branch_completed()

    def _inline_block(block: str) -> None:
        """Single-line ``BEGIN a; b; END`` block: emit BEGIN, each statement,
        then END;. Falls back to buffering when the inner body is not plain
        semicolon-separated statements."""
        inner = block[len("BEGIN"):].rsplit("END", 1)[0]
        out.append("BEGIN")
        for piece in inner.split(";"):
            piece = piece.strip()
            if not piece:
                continue
            stmt = _transform_tsql_statement(piece, statement_fn, declared, t_warns, returns_set, kind in ("PROCEDURE", "PROC"))
            if stmt:
                out.append(stmt)
        out.append("END;")

    def _activate_else() -> None:
        """Switch the top if from its then-branch to its else-branch and emit
        the ``ELSE`` keyword."""
        nonlocal waiting
        waiting = False
        if stack and stack[-1][0] == "if" and stack[-1][1] == "branch":
            stack[-1] = ("if", "else")
        out.append("ELSE")

    def _process_end() -> None:
        """Close the top block on an ``END`` keyword."""
        if not stack:
            out.append("END;")
            return
        block = stack.pop()
        if block == "begin":
            out.append("END;")
            if stack and stack[-1][0] in ("if", "while"):
                _branch_completed()
        elif block[0] in ("if", "while"):
            out.append("END IF;" if block[0] == "if" else "END LOOP;")
        elif block == "try":
            out.append("END;")

    def _handle_branch_body(piece: str, then_branch: bool) -> None:
        """Emit the body of a single-statement branch on the same line as its
        IF/WHILE/ELSE. ``piece`` may be a ``BEGIN ... END`` inline block, an
        ``IF ...`` (else-if), a bare ``BEGIN``, or a single statement that is
        flushed when it looks complete."""
        piece = piece.strip()
        if not piece:
            if then_branch:
                _branch_completed()
            return
        if re.match(r"^BEGIN\b.*\bEND$", piece, re.IGNORECASE):
            _inline_block(piece)
            _branch_completed()
        elif re.match(r"^IF\b", piece, re.IGNORECASE):
            icond, itail = _split_cond_body(piece[2:].strip())
            stack.append(("if", "branch"))
            out.append(f"IF {_expr(icond)} THEN")
            if itail:
                ithen, ielse = _split_top_level_else(itail)
                _handle_branch_body(ithen, then_branch=True)
                if ielse is not None:
                    _activate_else()
                    _handle_branch_body(ielse, then_branch=False)
        elif piece.upper() == "BEGIN":
            stack.append("begin")
            out.append("BEGIN")
        else:
            buf.append(piece)
            declares = piece.upper().startswith("DECLARE ") and not _declare_continues(piece)
            if piece.endswith(";") or declares or _is_complete_tail(piece):
                flush()

    for raw_line in _join_balanced_lines(body):
        line = raw_line.strip()
        # T-SQL authors defensively prefix a CTE with a semicolon (`;WITH x AS`)
        # to terminate whatever came before. Keeping it would emit an empty
        # statement and hide the WITH from the CTE/SELECT continuation check.
        if line.startswith(";"):
            line = line.lstrip(";").strip()
        if not line:
            # Blank lines carry no meaning in T-SQL. Real-world procedures put
            # them *inside* long SELECT lists and JOIN chains, so flushing here
            # chopped one statement into fragments that each got a trailing ';'.
            continue
        # Standalone T-SQL comments must not merge with (and swallow) the next
        # statement; they are documentation only and are dropped.
        if line.startswith("--"):
            continue
        upper = line.upper()

        # A completed single-statement then-branch is resolved by the next
        # token: ELSE continues it, anything else closes the IF first.
        if waiting:
            is_else = bool(re.match(r"^ELSE\b", upper))
            while waiting and not is_else:
                _close_now()

        # Statements whose T-SQL sources omit the trailing ';' must still be
        # split. When the buffer is complete at paren depth 0 and the next
        # line starts a keyword that can never continue the buffered statement
        # (RETURN, a second SET, a fresh RAISERROR, ...), the buffer is a full
        # statement — flush it. Clause keywords that DO continue a statement
        # (SET after UPDATE, SELECT after WITH/INSERT, ...) never trigger.
        # While an unclosed CASE sits in the buffer, ELSE/END/THEN lines belong
        # to the CASE expression, so they are buffered as statement
        # continuation and keyword flushes are suspended entirely.
        buf_case = _case_open(" ".join(buf))
        if buf_case:
            buf.append(line)
            depth += _paren_delta(line)
            if line.endswith(";"):
                flush()
            continue
        if depth == 0 and buf:
            first_word = upper.split(maxsplit=1)[0] if upper.split() else ""
            # Don't flush if the current line is a continuation keyword (FROM, WHERE, UNION, etc.)
            if _CONTINUATION_KEYWORDS.match(first_word):
                pass  # continue accumulating the current statement
            elif _MERGE_START_RE.match(buf[0].lstrip()) and _MERGE_ACTION_RE.match(first_word):
                # MERGE spells its actions with UPDATE / INSERT / DELETE after
                # WHEN ... THEN. Those are clauses of the MERGE, not new
                # statements — splitting there produced ``WHEN MATCHED THEN;``.
                pass
            elif _STMT_START_RE.match(first_word):
                flush()
            elif first_word == "SET" and _SET_SESSION_OPTION_RE.match(line):
                # `SET NOCOUNT OFF` after an UPDATE ... SET is a new statement,
                # never a continuation of the UPDATE's SET clause.
                flush()
            elif first_word == "SET" and _SET_VARIABLE_RE.match(line):
                # `SET @id = SCOPE_IDENTITY()` after an UPDATE/INSERT is a new
                # statement — the @ sigil makes it a variable assignment, never
                # a continuation of that statement's SET clause.
                flush()
            elif first_word == "SET" and not _SET_START_EXCEPT.match(buf[0].lstrip()):
                flush()
            elif first_word == "SELECT" and not _SELECT_START_EXCEPT.match(buf[0].lstrip()):
                # A SELECT after UNION/INTERSECT/EXCEPT continues the set operation
                if not _ends_with_set_op(" ".join(buf)):
                    flush()
            elif (first_word == "SELECT"
                  and re.match(r"^INSERT\b", buf[0].lstrip(), re.IGNORECASE)
                  and (_contains_toplevel_keyword(" ".join(buf), "SELECT")
                       or _contains_toplevel_keyword(" ".join(buf), "VALUES"))
                  and not _ends_with_set_op(" ".join(buf))):
                # `INSERT INTO t SELECT ...` / `INSERT INTO t VALUES (...)` is
                # already complete, so a SELECT starting the next line is a new
                # statement — T-SQL lets the author omit the semicolon.
                flush()

        is_control = (
            upper in ("BEGIN", "END", "ELSE")
            or upper.startswith("BEGIN TRY")
            or upper.startswith("BEGIN CATCH")
            or upper.startswith("END TRY")
            or upper.startswith("END CATCH")
            or upper.startswith("END ELSE")
            or re.match(r"^ELSE\s+IF\b", line, re.IGNORECASE)
            or re.match(r"^IF\b", line, re.IGNORECASE)
            or re.match(r"^WHILE\b", line, re.IGNORECASE)
            or re.match(r"^BEGIN\s+(TRANSACTION|TRAN|WORK)\b", line, re.IGNORECASE)
            or re.match(r"^(COMMIT|ROLLBACK)\s+(TRANSACTION|TRAN|WORK)\b", line, re.IGNORECASE)
        )
        if is_control:
            flush()
        depth += _paren_delta(line)

        # ---- transaction statements -----------------------------------------
        # PL/pgSQL functions cannot contain transaction control and already run
        # inside a single implicit transaction, so BEGIN/COMMIT/ROLLBACK
        # TRANSACTION are dropped (the statements they guarded still run).
        if re.match(r"^BEGIN\s+(TRANSACTION|TRAN|WORK)\b", line, re.IGNORECASE):
            t_warns.append("transaction control 'BEGIN TRANSACTION' is not supported inside PL/pgSQL functions and was removed")
            if stack and stack[-1][0] in ("if", "while"):
                _branch_completed()
            continue
        if re.match(r"^(COMMIT|ROLLBACK)\s+(TRANSACTION|TRAN|WORK)\b", line, re.IGNORECASE):
            t_warns.append(f"transaction control '{upper.split()[0]} TRANSACTION' is not supported inside PL/pgSQL functions and was removed")
            if stack and stack[-1][0] in ("if", "while"):
                _branch_completed()
            continue

        # ---- block openers --------------------------------------------------
        if re.match(r"^IF\b.*\bBEGIN\s*$", line, re.IGNORECASE):
            cond = re.sub(r"\bBEGIN\s*$", "", line, flags=re.IGNORECASE)
            cond = cond[2:].strip()
            stack.append(("if", "branch"))
            stack.append("begin")
            out.append(f"IF {_expr(cond)} THEN")
            out.append("BEGIN")
            continue
        if re.match(r"^IF\b", line, re.IGNORECASE):
            cond, tail = _split_cond_body(line[2:].strip())
            stack.append(("if", "branch"))
            out.append(f"IF {_expr(cond)} THEN")
            if tail:
                then_part, else_part = _split_top_level_else(tail)
                _handle_branch_body(then_part, then_branch=True)
                if else_part is not None:
                    _activate_else()
                    _handle_branch_body(else_part, then_branch=False)
            continue
        if re.match(r"^WHILE\b.*\bBEGIN\s*$", line, re.IGNORECASE):
            cond = re.sub(r"\bBEGIN\s*$", "", line, flags=re.IGNORECASE)
            cond = cond[5:].strip()
            stack.append(("while", "branch"))
            stack.append("begin")
            out.append(f"WHILE {_expr(cond)} LOOP")
            out.append("BEGIN")
            continue
        if re.match(r"^WHILE\b", line, re.IGNORECASE):
            cond, tail = _split_cond_body(line[5:].strip())
            stack.append(("while", "branch"))
            out.append(f"WHILE {_expr(cond)} LOOP")
            if tail:
                _handle_branch_body(tail, then_branch=True)
            continue
        if upper.startswith("BEGIN TRY"):
            stack.append("try")
            out.append("BEGIN")
            continue
        # Standalone TRY without BEGIN (T-SQL shorthand)
        if upper == "TRY":
            stack.append("try")
            out.append("BEGIN")
            continue
        if upper == "BEGIN CATCH" or upper.startswith("BEGIN CATCH"):
            continue  # handled by END TRY
        if upper.startswith("END TRY"):
            out.append("EXCEPTION WHEN OTHERS THEN")
            continue
        if upper.startswith("END CATCH"):
            out.append("END;")
            if stack and stack[-1] == "try":
                stack.pop()
            continue
        if re.match(r"^ELSE\b", line, re.IGNORECASE):
            _activate_else()
            rest = line[4:].strip()
            if rest:
                _handle_branch_body(rest, then_branch=False)
            continue
        if upper == "BEGIN":
            stack.append("begin")
            out.append("BEGIN")
            continue
        m_end_else = re.match(r"^END\s+ELSE\b(.*)$", line, re.IGNORECASE)
        if m_end_else:
            _process_end()
            _activate_else()
            rest = m_end_else.group(1).strip()
            if rest:
                _handle_branch_body(rest, then_branch=False)
            continue
        if upper == "END":
            _process_end()
            continue

        # ---- statements ------------------------------------------------------
        buf.append(line)
        if upper.startswith("DECLARE ") and not _declare_continues(line):
            flush()
        elif line.endswith(";"):
            # Don't flush if the statement ends with UNION/INTERSECT/EXCEPT
            # — the next SELECT is part of the same set operation.
            stripped = line.rstrip(";").strip()
            if re.match(r".*\b(UNION|INTERSECT|EXCEPT)\s+(ALL\s+)?$", stripped, re.IGNORECASE):
                pass  # keep accumulating
            else:
                flush()

    if buf:
        flush()
    while waiting:
        _close_now()
    if stack:
        # A body whose BEGIN/END blocks do not balance used to be emitted
        # truncated, which PostgreSQL rejects with "syntax error at end of
        # input" and loses the whole routine. Close the still-open blocks so
        # the routine at least compiles, and warn so the nesting is reviewed.
        t_warns.append(
            f"Unbalanced BEGIN/END blocks ({len(stack)} left open) — the missing "
            f"block terminators were added automatically; review the converted nesting"
        )
        for entry in reversed(stack):
            if isinstance(entry, tuple):
                out.append("END IF;" if entry[0] == "if" else "END LOOP;")
            else:
                out.append("END;")
        stack.clear()

    return out, t_warns, declared


def _contains_toplevel_keyword(text: str, word: str) -> bool:
    """True when ``word`` appears in ``text`` outside parentheses and strings."""
    upper_word = word.upper()
    width = len(word)
    depth, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            i += 1
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif (depth == 0
              and text[i:i + width].upper() == upper_word
              and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_"))
              and (i + width >= n
                   or not (text[i + width].isalnum() or text[i + width] == "_"))):
            return True
        i += 1
    return False


def _transform_statement(line: str, declared: dict[str, str], warnings: list[str], returns_set: bool, is_procedure: bool = False) -> str | None:
    # A whole statement wrapped in parentheses — `(SELECT ...)` — is how some
    # T-SQL procedures return a result set. Unwrap it so the result-select
    # handling below recognises it.
    wrapped = re.match(r"^\(\s*(SELECT\b.*)\)\s*;?\s*$", line, re.IGNORECASE | re.DOTALL)
    if wrapped and _paren_delta(wrapped.group(1)) == 0:
        line = wrapped.group(1).strip()
    # Normalize a leading SELECT TOP n to a trailing LIMIT n first so the
    # assignment-SELECT regexes below still match "SELECT TOP 1 @x = ...".
    line = _translate_top(line)
    upper = line.upper()

    # T-SQL session settings that have no PostgreSQL equivalent — silently stripped.
    _SET_SKIP = {
        "SET NOCOUNT ON", "SET NOCOUNT OFF",
        "SET ANSI_NULLS ON", "SET ANSI_NULLS OFF",
        "SET ANSI_PADDING ON", "SET ANSI_PADDING OFF",
        "SET ANSI_WARNINGS ON", "SET ANSI_WARNINGS OFF",
        "SET ARITHABORT ON", "SET ARITHABORT OFF",
        "SET QUOTED_IDENTIFIER ON", "SET QUOTED_IDENTIFIER OFF",
        "SET XACT_ABORT ON", "SET XACT_ABORT OFF",
        "SET NUMERIC_ROUNDABORT ON", "SET NUMERIC_ROUNDABORT OFF",
        "SET CONCAT_NULL_YIELDS_NULL ON", "SET CONCAT_NULL_YIELDS_NULL OFF",
        "SET CURSOR_CLOSE_ON_COMMIT ON", "SET CURSOR_CLOSE_ON_COMMIT OFF",
        "SET IMPLICIT_TRANSACTIONS ON", "SET IMPLICIT_TRANSACTIONS OFF",
        "SET DATEFORMAT MDY", "SET DATEFORMAT YMD", "SET DATEFORMAT DMY",
        "SET DATEFIRST 1", "SET DATEFIRST 7",
        "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED",
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
    }
    if upper in _SET_SKIP:
        return None
    # Generic SET <option> ON/OFF that wasn't caught above
    if re.match(r"^SET\s+\w+\s+(ON|OFF)\s*$", line, re.IGNORECASE):
        return None
    # Handle multiple concatenated SET statements on one line
    # e.g. "set nocount on SET XACT_ABORT ON" or "set nocount on set ansi_nulls on"
    if re.match(r"^SET\s+\w+", line, re.IGNORECASE):
        # Split on "SET " boundaries and check if ALL parts are skippable
        parts = re.split(r"(?<=\s)(?=(?:SET)\s)", line, flags=re.IGNORECASE)
        if len(parts) > 1:
            all_skipped = True
            for part in parts:
                p = part.strip().upper()
                if p and p not in _SET_SKIP and not re.match(r"^SET\s+\w+\s+(ON|OFF)\s*$", p, re.IGNORECASE):
                    all_skipped = False
                    break
            if all_skipped:
                return None

    # BEGIN/COMMIT/ROLLBACK TRANSACTION -> plain PL/pgSQL transaction control
    if re.match(r"^BEGIN\s+(TRANSACTION|TRAN|WORK)\b", line, re.IGNORECASE):
        return "BEGIN;"
    if re.match(r"^(COMMIT|ROLLBACK)\s+(TRANSACTION|TRAN|WORK)\b", line, re.IGNORECASE):
        return f"{upper.split()[0]};"

    # DECLARE @x INT / DECLARE @x AS INT / DECLARE @x INT = expr
    m = re.match(
        rf"^DECLARE\s+@([\w]+)\s+(?:AS\s+|IS\s+)?({_TSQL_TYPE_RE})\s*(?:=\s*(.*))?$",
        line, re.IGNORECASE,
    )
    if m:
        name, dtype, init = m.group(1), m.group(2), m.group(3)
        target_type, warn = convert_type(dtype, "tsql", "postgres")
        if warn:
            warnings.append(f"DECLARE @{name}: {warn}")
        target_type = target_type or "TEXT"
        if init is not None:
            declared[name] = f"{target_type} := {_expr(init.strip())}"
        else:
            declared[name] = target_type
        return None  # declaration collected into the header DECLARE block

    # DECLARE name AS type / DECLARE name type (without @ prefix)
    # Also handles multi-variable: DECLARE a int, b varchar(100)
    # And quoted names: DECLARE "name" as varchar(100)
    m = re.match(rf"^DECLARE\s+([\w\"]+)\s+(?:AS\s+)?({_TSQL_TYPE_RE})\s*(?:=\s*(.*))?$", line, re.IGNORECASE)
    if m:
        name, dtype, init = m.group(1).strip('"'), m.group(2), m.group(3)
        target_type, warn = convert_type(dtype, "tsql", "postgres")
        if warn:
            warnings.append(f"DECLARE {name}: {warn}")
        target_type = target_type or "TEXT"
        if init is not None:
            declared[name] = f"{target_type} := {_expr(init.strip())}"
        else:
            declared[name] = target_type
        return None  # declaration collected into the header DECLARE block

    # Multi-variable DECLARE: DECLARE a int, b varchar(100), c datetime
    # or DECLARE a as int, b as varchar(100)
    m = re.match(r"^DECLARE\s+(.+)$", line, re.IGNORECASE)
    if m:
        var_list = m.group(1)
        # Split by comma, but not inside parentheses
        parts = []
        depth = 0
        current = []
        for ch in var_list:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif ch == ',' and depth == 0:
                parts.append(''.join(current).strip())
                current = []
                continue
            current.append(ch)
        if current:
            parts.append(''.join(current).strip())

        all_handled = True
        for part in parts:
            # Try: name type, name AS type, name type = expr, name AS type = expr
            vm = re.match(
                rf"""(?:"([\w]+)"|@?([\w]+))\s+(?:AS\s+|IS\s+)?({_TSQL_TYPE_RE})\s*(?:=\s*(.*))?$""",
                part.strip(), re.IGNORECASE,
            )
            if vm:
                name = vm.group(1) or vm.group(2)
                dtype, init = vm.group(3), vm.group(4)
                target_type, warn = convert_type(dtype, "tsql", "postgres")
                if warn:
                    warnings.append(f"DECLARE {name}: {warn}")
                target_type = target_type or "TEXT"
                if init is not None:
                    declared[name] = f"{target_type} := {_expr(init.strip())}"
                else:
                    declared[name] = target_type
            else:
                all_handled = False
        if all_handled:
            return None

    # A malformed/unparseable table declaration must not be emitted as a
    # scalar variable. Balanced declarations are converted before this stage.
    if re.match(r"^DECLARE\s+@[\w]+\s+TABLE\s*\(", line, re.IGNORECASE):
        warnings.append("Table variable declaration could not be parsed safely")
        return None

    # SET @x = expr
    m = re.match(r"^SET\s+@([\w]+)\s*=\s*(.+)$", line, re.IGNORECASE)
    if m:
        return f"{_safe_var(m.group(1))} := {_expr(m.group(2))};"

    # SELECT @x = expr [FROM ...]  (single assignment)
    m = re.match(r"^SELECT\s+@([\w]+)\s*=\s*(.+?)\s+FROM\s+(.+)$", line, re.IGNORECASE | re.DOTALL)
    if m:
        var, expr, tail = m.group(1), m.group(2), m.group(3)
        return f"SELECT {_expr(expr)} INTO {_safe_var(var)} FROM {_expr(tail)};"

    # SELECT @x = expr  (no FROM)
    m = re.match(r"^SELECT\s+@([\w]+)\s*=\s*(.+)$", line, re.IGNORECASE | re.DOTALL)
    if m:
        var, expr = m.group(1), m.group(2)
        return f"SELECT {_expr(expr)} INTO {_safe_var(var)};"

    # PRINT @x / PRINT 'text'
    m = re.match(r"^PRINT\s+(.+)$", line, re.IGNORECASE)
    if m:
        arg = m.group(1).strip()
        if arg.startswith("@"):
            return f"RAISE NOTICE '%', {_safe_var(arg[1:])};"
        return f"RAISE NOTICE {arg};"

    # RAISERROR / THROW
    m = re.match(r"^RAISERROR\s*\((.*)\)\s*$", line, re.IGNORECASE | re.DOTALL)
    if m:
        args = _split_args_balanced(m.group(1))
        if args:
            msg = args[0].strip()
            if msg.startswith("@"):
                return f"RAISE EXCEPTION '%', {_expr(msg[1:])};"
            # T-SQL %-format specifiers (%d, %s, ...) -> PL/pgSQL bare '%'
            msg_literal = re.sub(r"%\s*[sdiduoxXcgeEfG]", "%", msg)
            msg_expr = _replace_concat(_expr(msg_literal))
            if len(args) > 3:
                warnings.append("RAISERROR positional arguments appended as RAISE EXCEPTION format values")
                extra = ", ".join(_expr(a.strip()) for a in args[3:])
                msg_expr = f"{msg_expr}, {extra}"
            return f"RAISE EXCEPTION {msg_expr};"
    message = _throw_message(line)
    if message is not None:
        return f"RAISE EXCEPTION {message};"
    if upper.startswith("THROW"):
        return "RAISE EXCEPTION 'error';"

    # RETURN [expr] — in procedures, RETURN with a value is invalid in PG.
    # In functions with OUT parameters, RETURN with a value is also invalid.
    m = re.match(r"^RETURN\s+(.+)$", line, re.IGNORECASE)
    if m:
        expr_val = _expr(m.group(1).strip())
        if is_procedure:
            return f"RETURN;"
        return f"RETURN {expr_val};"
    if upper == "RETURN":
        return "RETURN;"

    # WAITFOR DELAY
    m = re.match(r"^WAITFOR\s+DELAY\s+'([^']+)'", line, re.IGNORECASE)
    if m:
        return f"PERFORM pg_sleep({_delay_to_seconds(m.group(1))});"

    # SELECT fields INTO #temp FROM ... creates the temporary table in T-SQL.
    # PostgreSQL expresses the same operation as CREATE TEMP TABLE ... AS.
    select_into = re.match(
        r"^SELECT\s+(.*?)\s+INTO\s+\[?#([\w\d_]+)\]?\s*(.*)$",
        line, re.IGNORECASE | re.DOTALL,
    )
    if select_into:
        select_list, table_name, tail = select_into.groups()
        query = f"SELECT {select_list} {tail}".strip()
        return f"CREATE TEMP TABLE {table_name} ON COMMIT DROP AS {_expr(query)};"

    # result-returning SELECT -> RETURN QUERY. This must follow SELECT INTO,
    # which creates a table rather than returning a result set.
    if returns_set and re.match(r"^SELECT\b", line, re.IGNORECASE) and not re.match(r"^SELECT\s+INTO\b", line, re.IGNORECASE):
        return f"RETURN QUERY {_expr(line)};"
    if re.match(r"^INSERT\s+INTO\s+\[?#[\w\d_]+\]?", line, re.IGNORECASE):
        line = re.sub(r"\[?#([\w\d_]+)\]?", r"\1", line)
        return _expr(line) + ";"

    # EXEC(SQL) or EXECUTE(SQL) — dynamic SQL execution
    m = re.match(r"^(?:EXEC|EXECUTE)\s*\((.+)\)\s*$", line, re.IGNORECASE)
    if m:
        sql_expr = _expr(m.group(1).strip())
        return f"EXECUTE {sql_expr};"
    # EXEC sp_name @args — stored procedure call
    m = re.match(r"^(?:EXEC|EXECUTE)\s+([\w.]+)\s*(.*)$", line, re.IGNORECASE)
    if m:
        sp_name = m.group(1)
        args_str = m.group(2).strip().rstrip(";").strip()
        args = _expr(args_str) if args_str else ""
        return f"CALL {sp_name}({args});" if args else f"CALL {sp_name}();"

    # plain DML / control — translate T-SQL specific syntax before emitting.
    line = _translate_tsql_body_syntax(line)
    return _expr(line).rstrip(";") + ";"


def _translate_tsql_body_syntax(line: str) -> str:
    """Apply T-SQL-specific body transformations that don't go through _expr."""
    # T-SQL allows `INSERT tbl VALUES (...)` without INTO; PostgreSQL requires it.
    line = re.sub(r"^(INSERT)\s+(?!INTO\b)(?=[\w\"\[#@])", r"\1 INTO ", line, flags=re.IGNORECASE)
    # Column definitions inside a body CREATE TABLE keep their T-SQL types.
    line = _translate_body_create_table(line)
    # CREATE TABLE #temp; or CREATE TABLE temp; (no columns) -> CREATE TEMP TABLE temp ();
    line = re.sub(r"(?i)\bCREATE\s+TABLE\s+(?:#([\w]+)|([\w]+))\s*;?\s*$", lambda m: f"CREATE TEMP TABLE {m.group(1) or m.group(2)} ();", line)
    # OPEN cursor FOR select -> cursor_name FOR select  (PG refcursor syntax)
    m = re.match(r"^OPEN\s+([\w]+)\s+FOR\s+(.+)$", line, re.IGNORECASE)
    if m:
        cursor_name = m.group(1)
        query = m.group(2).rstrip(";")
        line = f"{cursor_name} FOR {query}"
    # SET @x = value (without explicit SET keyword appearing as statement start)
    # This handles cases where SET is inside a compound statement
    return line


# T-SQL column types that appear inside a body CREATE TABLE. PostgreSQL has no
# DATETIME/BIT/NVARCHAR, so the declaration fails unless they are mapped.
_BODY_TYPE_REWRITES = [
    (r"\b(?:n?varchar|n?char)\s*\(\s*max\s*\)", "TEXT"),
    (r"\bnvarchar\s*\(", "VARCHAR("),
    (r"\bnchar\s*\(", "CHAR("),
    (r"\bdatetime2\s*(?:\(\s*\d+\s*\))?", "TIMESTAMP"),
    (r"\bsmalldatetime\b", "TIMESTAMP"),
    (r"\bdatetimeoffset\s*(?:\(\s*\d+\s*\))?", "TIMESTAMPTZ"),
    (r"\bdatetime\b", "TIMESTAMP"),
    (r"\buniqueidentifier\b", "UUID"),
    (r"\btinyint\b", "SMALLINT"),
    (r"\bbit\b", "BOOLEAN"),
    (r"\bsmallmoney\b", "NUMERIC(10,4)"),
    (r"\bmoney\b", "NUMERIC(19,4)"),
    (r"\bntext\b", "TEXT"),
    (r"\bimage\b", "BYTEA"),
    (r"\bvarbinary\s*\([^)]*\)", "BYTEA"),
    (r"\bbinary\s*\([^)]*\)", "BYTEA"),
    (r"\bdecimal\b", "NUMERIC"),
    (r"\bfloat\b", "DOUBLE PRECISION"),
    (r"\bsysname\b", "VARCHAR(128)"),
]

_BODY_CREATE_TABLE_RE = re.compile(
    r"^(?P<create>CREATE\s+)(?P<temp>TEMP\s+|TEMPORARY\s+)?TABLE\s+"
    r"(?P<name>#?[\w\".\[\]]+)\s*\((?P<cols>.*)\)(?P<tail>[^)]*)$",
    re.IGNORECASE | re.DOTALL,
)


def _translate_body_create_table(line: str) -> str:
    """Map T-SQL column types (and drop the trailing comma SQL Server allows)
    inside a ``CREATE TABLE`` written in a routine body."""
    match = _BODY_CREATE_TABLE_RE.match(line.strip())
    if not match:
        return line
    name = match.group("name")
    temp = bool(match.group("temp")) or name.startswith("#")
    columns = re.sub(r",\s*$", "", match.group("cols").strip())
    for pattern, replacement in _BODY_TYPE_REWRITES:
        columns = re.sub(pattern, replacement, columns, flags=re.IGNORECASE)
    keyword = "CREATE TEMP TABLE " if temp else "CREATE TABLE "
    return f"{keyword}{name.lstrip('#')} ({columns}){match.group('tail')}"


def _delay_to_seconds(delay: str) -> float:
    parts = delay.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    return float(delay)


def _split_args_balanced(arg_string: str) -> list[str]:
    """Split a comma-separated argument list respecting quotes and nesting."""
    parts, buf, depth, in_str = [], [], 0, False
    i = 0
    while i < len(arg_string):
        ch = arg_string[i]
        if in_str:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(arg_string) and arg_string[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
            buf.append(ch)
        elif ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf).strip())
    return parts


def _throw_message(line: str, translate: bool = True) -> str | None:
    """Return the message expression of a T-SQL ``THROW num, msg, state``.

    The argument list is split on *top-level* commas only: real messages such
    as ``'Name, Url and Status are required'`` contain commas, and a plain
    ``[^,]+`` match truncated them mid-literal, emitting an unterminated
    string that broke the rest of the routine.
    """
    m = re.match(r"^THROW\b\s*(.*)$", line.strip(), re.IGNORECASE | re.DOTALL)
    if not m or not m.group(1).strip():
        return None
    args = _split_args_balanced(m.group(1))
    if len(args) < 2:
        return None
    message = args[1].strip()
    if not message:
        return None
    if message.startswith("@"):
        name = _safe_var(message[1:])
        return f"'%', {name}" if translate else f"'%', {message[1:]}"
    return _replace_concat(_expr(message)) if translate else message


def _replace_concat(text: str) -> str:
    """Convert T-SQL '+' string concatenation to PL/pgSQL '||', leaving '+'
    characters inside string literals untouched."""
    out = []
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    out.append("'")
                    i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
            out.append(ch)
        elif ch == "+":
            out.append(" || ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# Keywords PostgreSQL refuses as a *bare* column alias (`SELECT pc_year Year`)
# but accepts after an explicit AS. Restricted to the type/temporal names real
# schemas actually alias with — clause keywords (FROM/WHERE/GROUP/...) and the
# type-name parts (PRECISION in DOUBLE PRECISION) are deliberately excluded so
# genuine syntax is never rewritten.
_BARE_ALIAS_KEYWORDS = "year month day hour minute second array within without"
_BARE_ALIAS_RE = re.compile(
    rf'(?<=[\w)"])(?<!\bAS)\s+(?P<alias>{_BARE_ALIAS_KEYWORDS.replace(" ", "|")})\s*(?=,|\bFROM\b|$)',
    re.IGNORECASE,
)


def _fix_bare_aliases(text: str) -> str:
    """Insert the optional AS before a column alias PostgreSQL treats as a keyword."""
    return _BARE_ALIAS_RE.sub(lambda m: f" AS {m.group('alias')}", text)


_TABLE_HINT = (
    r"NOLOCK|UPDLOCK|TABLOCK|TABLOCKX|PAGLOCK|ROWLOCK|HOLDLOCK|XLOCK|NOWAIT|READPAST|"
    r"READCOMMITTED|READCOMMITTEDLOCK|REPEATABLEREAD|SERIALIZABLE|INDEX\s*\([^)]*\)"
)
_TABLE_HINT_RE = re.compile(
    rf"(?:\bWITH\s*)?\(\s*(?:{_TABLE_HINT})(?:\s*,\s*(?:{_TABLE_HINT}))*\s*\)",
    re.IGNORECASE,
)


def _expr(text: str) -> str:
    """Translate expressions inside a statement."""
    text = translate_functions(text, "tsql", "postgres")
    # MSSQL bracket identifiers -> PG quoted identifiers
    text = re.sub(r"\[([^\[\]]+)\]", r'"\1"', text)
    # MSSQL N-prefixed string literals have no PG equivalent
    text = re.sub(r"\bN'", "'", text, flags=re.IGNORECASE)
    # Locking hints (`WITH (NOLOCK)`, bare `(NOLOCK)`) have no PG equivalent.
    text = _TABLE_HINT_RE.sub("", text)
    # APPLY operators become lateral joins
    text = re.sub(r"\bCROSS\s+APPLY\b", "CROSS JOIN LATERAL", text, flags=re.IGNORECASE)
    text = re.sub(r"\bOUTER\s+APPLY\b", "LEFT JOIN LATERAL", text, flags=re.IGNORECASE)
    # T-SQL single-quoted column aliases (AS 'Cash+ FLI') are identifiers in PG
    text = re.sub(r"\bAS\s+'([^']*)'", lambda m: f'AS "{m.group(1)}"', text, flags=re.IGNORECASE)
    text = _fix_bare_aliases(text)
    # @@system vars — resolved before the single-@ strip below, which would
    # otherwise turn @@IDENTITY into an unknown @IDENTITY variable.
    text = re.sub(r"@@ROWCOUNT\b", "ROW_COUNT()", text, flags=re.IGNORECASE)
    text = re.sub(r"@@IDENTITY\b", "LASTVAL()", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSCOPE_IDENTITY\s*\(\s*\)", "LASTVAL()", text, flags=re.IGNORECASE)
    # variable references @x -> x (not inside string literals)
    text = re.sub(r"@([A-Za-z_][A-Za-z0-9_]*)",
                  lambda m: _safe_var(m.group(1)), text)
    # #temp references
    text = re.sub(r"\[?#([\w\d_]+)\]?", r"\1", text)
    # SELECT TOP n -> trailing LIMIT n
    text = _translate_top(text)
    # T-SQL '+' is overloaded: numeric addition vs string concatenation. Only
    # treat it as concatenation when a string literal is present, so arithmetic
    # like `@i + 1` keeps its plus sign.
    if "'" in text:
        text = _replace_concat(text)
    return text


# ---------------------------------------------------------------------------
# PL/pgSQL -> T-SQL
# ---------------------------------------------------------------------------

def _redeclare_tsql_scalar_param_tables(lines: list[str], params: list[tuple[str, str, bool]],
                                        tvp_names: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Rebuild local table variables for scalar params used as row sources.

    A table-valued parameter is flattened to ``text`` by an earlier MSSQL ->
    PG round trip, so the PG body references it as ``FROM Products p`` and the
    T-SQL re-translation turns that into ``FROM @products p``. SQL Server then
    fails with "Must declare the table variable '@products'". Detect the row
    source and declare a renamed local table variable (@name_tbl) shaped from
    the alias's columns so the CREATE PROCEDURE stays valid.
    """
    param_names = {p[0].lower() for p in params} - (tvp_names or set())
    out = []
    table_declares: list[str] = []
    rebuilt_aliases: list[str] = []
    joined = "\n".join(lines)
    for line in lines:
        m = re.search(
            r"\bFROM\s+@([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            line, re.IGNORECASE,
        )
        if not m or m.group(1).lower() not in param_names:
            out.append(line)
            continue
        pname, alias = m.group(1), m.group(2)
        var = f"@{pname}_tbl"
        if any(alias.lower() == existing.lower() for existing in rebuilt_aliases):
            out.append(line)
            continue
        cols: list[str] = []
        for cm in re.finditer(
            rf"(?<![\w.@\[])\b{re.escape(alias)}\.\s*(\[[A-Za-z_][A-Za-z0-9_]*\]|[A-Za-z_][A-Za-z0-9_]*)",
            joined, re.IGNORECASE,
        ):
            col = cm.group(1).strip("[]")
            if col.lower() not in {c.lower() for c in cols}:
                cols.append(col)
        if not cols:
            out.append(line)
            continue
        col_defs = ", ".join(f"[{c}] NVARCHAR(400)" for c in cols)
        table_declares.append(f"    DECLARE {var} TABLE ({col_defs});\n")
        rebuilt_aliases.append(alias)
        out.append(re.sub(
            rf"\bFROM\s+@{re.escape(pname)}\s+{re.escape(alias)}\b",
            f"FROM {var} {alias}", line, flags=re.IGNORECASE,
        ))
    # The rebuilt table-variable columns carry the database collation with
    # explicit precedence, so comparing them to a column declared with a
    # different collation is a CREATE-time error (468). Coerce the real column
    # side of every comparison against a rebuilt alias to DATABASE_DEFAULT.
    qcol = r"(?:\[[A-Za-z_][A-Za-z0-9_]*\]|[A-Za-z_][A-Za-z0-9_]*)"
    ops = r"((?:<=|>=|<>|=|<|>))"
    for alias in rebuilt_aliases:
        a = re.escape(alias)
        other = rf"((?!@?{a}\b)[A-Za-z_][A-Za-z0-9_]*\.\s*{qcol})"
        alias_side = rf"((?<![\w.@\[])\b{a}\.\s*{qcol})"
        out = [
            re.sub(
                rf"{alias_side}\s*{ops}\s*{other}",
                lambda m: f"{m.group(1)} {m.group(2)} {m.group(3)} COLLATE DATABASE_DEFAULT",
                ln, flags=re.IGNORECASE,
            )
            for ln in out
        ]
        out = [
            re.sub(
                rf"{other}\s*{ops}\s*{alias_side}",
                lambda m: f"{m.group(1)} COLLATE DATABASE_DEFAULT {m.group(2)} {m.group(3)}",
                ln, flags=re.IGNORECASE,
            )
            for ln in out
        ]
    return out, table_declares


def _collate_tsql_tvp_comparisons(lines: list[str], tvp_names: set[str]) -> list[str]:
    """Resolve comparisons between TVP strings and differently collated columns.

    SQL Server table-type character attributes inherit the target database
    collation. Migrated table columns can retain an explicit source collation,
    so equality joins fail at CREATE PROCEDURE time with error 468 unless the
    persistent-table side is coerced to DATABASE_DEFAULT.
    """
    aliases: set[str] = set()
    joined = "\n".join(lines)
    for pname in tvp_names:
        for match in re.finditer(
            rf"\b(?:FROM|JOIN)\s+@{re.escape(pname)}\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
            joined, re.IGNORECASE,
        ):
            aliases.add(match.group(1))

    qcol = r"(?:\[[A-Za-z_][A-Za-z0-9_]*\]|[A-Za-z_][A-Za-z0-9_]*)"
    ops = r"((?:<=|>=|<>|=|<|>))"
    out = lines
    for alias in aliases:
        escaped = re.escape(alias)
        other = rf"((?!@?{escaped}\b)[A-Za-z_][A-Za-z0-9_]*\.\s*{qcol})"
        tvp_side = rf"((?<![\w.@\[])\b{escaped}\.\s*{qcol})"
        out = [re.sub(
            rf"{tvp_side}\s*{ops}\s*{other}",
            lambda m: f"{m.group(1)} {m.group(2)} {m.group(3)} COLLATE DATABASE_DEFAULT",
            line, flags=re.IGNORECASE,
        ) for line in out]
        out = [re.sub(
            rf"{other}\s*{ops}\s*{tvp_side}",
            lambda m: f"{m.group(1)} COLLATE DATABASE_DEFAULT {m.group(2)} {m.group(3)}",
            line, flags=re.IGNORECASE,
        ) for line in out]
    return out


def _plpgsql_to_tsql(sql: str, user_types: list | None = None) -> tuple[str | None, list[str]]:
    warnings: list[str] = []
    is_pg_procedure = bool(re.search(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\b", sql, re.IGNORECASE
    ))

    # pg_get_functiondef output: LANGUAGE plpgsql comes between RETURNS and the
    # (possibly named) dollar-quoted body; PG procedures have no RETURNS clause.
    header_match = re.search(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+"
        r"(?:(\"?[\w\d_]+\"?)\.)?\"?([\w\d_]+)\"?\s*"
        r"\((.*?)\)\s*(?:RETURNS\s+(.+?))?\s+"
        r"(?:LANGUAGE\s+[\w\d_]+\s+)?AS\s+\$[\w\d_]*\$",
        sql, re.IGNORECASE | re.DOTALL,
    )
    if not header_match:
        return None, ["Could not parse PL/pgSQL header"]
    schema = (header_match.group(1) or "dbo").strip('"')
    name = header_match.group(2)
    param_text = header_match.group(3)
    returns = header_match.group(4) or ""
    returns = re.split(r"\s+LANGUAGE\b", returns, maxsplit=1, flags=re.IGNORECASE)[0].strip()

    # language block end
    body = _extract_plpgsql_body(sql)
    if body is None:
        return None, ["Could not extract PL/pgSQL body"]

    params, p_warns = _parse_params_reverse(param_text)
    warnings.extend(p_warns)

    transformed, t_warns, declared = _transform_plpgsql_body(body)
    warnings.extend(t_warns)
    if any(line == "END TRY" for line in transformed) and not any(line == "BEGIN TRY" for line in transformed):
        end_try = transformed.index("END TRY")
        begin_indexes = [i for i, line in enumerate(transformed[:end_try]) if line == "BEGIN"]
        if begin_indexes:
            transformed[begin_indexes[-1]] = "BEGIN TRY"
    if "BEGIN CATCH" in transformed:
        catch_start = transformed.index("BEGIN CATCH")
        catch_end = next((i for i in range(catch_start + 1, len(transformed)) if transformed[i] == "END"), None)
        if catch_end is not None:
            transformed[catch_end] = "END CATCH"

    param_list = []
    tvp_names: set[str] = set()
    for pname, ptype, output in params:
        is_array = bool(re.search(r"\[\]\s*$", ptype))
        raw_base = re.sub(r"\[\]\s*$", "", ptype) if is_array else ptype
        base_type = re.sub(r'[\[\]"\s]', '', raw_base).casefold()
        composite = next((ut for ut in user_types or [] if ut.kind == "composite" and
                          base_type in {
                              re.sub(r'[\[\]"\s]', '', ut.name).casefold(),
                              re.sub(r'[\[\]"\s]', '', ut.name).casefold().rsplit('.', 1)[-1],
                          }), None)
        if composite is not None and is_array:
            parts = [part.strip('[]"') for part in composite.name.split(".")]
            tgt_type = ".".join(f'[{part}]' for part in parts)
            warn = None
            tvp_names.add(pname.casefold())
        else:
            tgt_type, warn = convert_type(ptype, "postgres", "tsql")
        if warn:
            warnings.append(f"Parameter {pname}: {warn}")
        tgt_type = tgt_type or "NVARCHAR(MAX)"
        suffix = " READONLY" if pname.casefold() in tvp_names else (' OUTPUT' if output else '')
        param_list.append(f"@{pname} {tgt_type}{suffix}")

    # Rewrite bare references to parameters/declared variables with @ so they
    # resolve as T-SQL variables — case-insensitively, skipping SQL keywords,
    # aliases, and already-qualified (quoted/bracketed) references.
    var_names = sorted(set(declared) | {p[0] for p in params}, key=len, reverse=True)
    if var_names:
        def _qualify(ln: str) -> str:
            def _repl(m):
                word = m.group(1)
                if word.upper() in _TSQL_KEYWORDS:
                    return m.group(0)
                canon = next((n for n in var_names if n.lower() == word.lower()), None)
                if canon is None:
                    return m.group(0)
                return "@" + canon
            return re.sub(
                r"(?<![.\w\"@\['])([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*\()",
                _repl, ln, flags=re.IGNORECASE,
            )
        transformed = [_qualify(ln) for ln in transformed]

        # Initializers are stored separately from body statements.
        declared = {name: _qualify(value) for name, value in declared.items()}

        # Composite arrays are the PostgreSQL representation of SQL Server
        # TVPs. Restore the relational table-parameter reference.
        if tvp_names:
            for pname in tvp_names:
                transformed = [re.sub(
                    rf"\b(FROM|JOIN)\s+unnest\s*\(\s*@{re.escape(pname)}\s*\)",
                    rf"\1 @{pname}", line, flags=re.IGNORECASE,
                ) for line in transformed]

        # Older round-tripped bodies can quote a local variable. In an
        # assignment "v" = "v", the left token is the local and the right
        # token is the source column; later bracketed uses are procedural.
        for local_name in declared:
            marker = f"\x02{local_name}\x02"
            transformed = [re.sub(
                rf"\[{re.escape(local_name)}\]\s*=\s*\[{re.escape(local_name)}\]",
                f"@{local_name} = {marker}", ln, flags=re.IGNORECASE,
            ) for ln in transformed]
            transformed = [
                re.sub(rf"\[{re.escape(local_name)}\]", f"@{local_name}", ln, flags=re.IGNORECASE)
                .replace(marker, f"[{local_name}]")
                for ln in transformed
            ]

    transformed, table_var_declares = _redeclare_tsql_scalar_param_tables(transformed, params, tvp_names)
    transformed = [_repair_tsql_parameter_contexts(ln, params) for ln in transformed]
    for pname in tvp_names:
        transformed = [re.sub(
            rf"\b(FROM|JOIN)\s+unnest\s*\(\s*@{re.escape(pname)}\s*\)",
            rf"\1 @{pname}", line, flags=re.IGNORECASE,
        ) for line in transformed]
    transformed = _collate_tsql_tvp_comparisons(transformed, tvp_names)

    declare_lines = []
    for var_name, type_and_init in declared.items():
        pieces = re.split(r"\s*:=\s*", type_and_init, maxsplit=1)
        mapped_type, type_warn = convert_type(pieces[0], "postgres", "tsql")
        if type_warn:
            warnings.append(f"Variable {var_name}: {type_warn}")
        initializer = f" = {pieces[1]}" if len(pieces) == 2 else ""
        declare_lines.append(f"    DECLARE @{var_name} {mapped_type or 'NVARCHAR(MAX)'}{initializer};\n")
    declares = "".join(table_var_declares) + "".join(declare_lines)

    lower_returns = returns.lower()
    is_scalar_fn = not is_pg_procedure and not (
        lower_returns.startswith(("void", "setof "))
        or "table(" in lower_returns
    )
    if is_scalar_fn:
        ret_type, ret_warn = convert_type(returns, "postgres", "tsql")
        if ret_warn:
            warnings.append(f"RETURNS {returns}: {ret_warn}")
        head = (
            f"CREATE FUNCTION [{schema}].[{name}] ({', '.join(param_list)})\n"
            f"RETURNS {ret_type or 'NVARCHAR(MAX)'}\nAS\nBEGIN\n"
        )
        tail = "END;"
    else:
        # void / SETOF / TABLE(...) — all return a result set; the closest
        # working T-SQL equivalent is a stored procedure.
        head = (
            f"CREATE PROCEDURE [{schema}].[{name}]"
            f"{' (' + ', '.join(param_list) + ')' if param_list else ''}\n"
            f"AS\nBEGIN\n    SET NOCOUNT ON;\n"
        )
        tail = "END;"

    converted = head + declares + "\n".join(transformed) + "\n" + tail
    return converted, warnings


def _extract_plpgsql_body(sql: str) -> str | None:
    # pg_get_functiondef uses a named dollar-quote (AS $function$ ... $function$).
    m = re.search(r"\$[\w\d_]*\$(.*?)\$[\w\d_]*\$", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return m.group(1)


def _parse_params_reverse(param_text: str) -> tuple[list[tuple[str, str, bool]], list[str]]:
    params = []
    for part in _split_args_balanced(param_text):
        part = part.strip()
        if not part:
            continue
        output = False
        if part.upper().startswith("INOUT "):
            # Result cursors are an implementation detail used to preserve
            # SQL Server procedure result sets; they disappear on round-trip.
            if re.search(r"\brefcursor\b", part, re.IGNORECASE):
                continue
            output = True
            part = part[6:].strip()
        elif part.upper().startswith("OUT "):
            output = True
            part = part[4:].strip()
        elif part.upper().startswith("IN "):
            part = part[3:].strip()
        # Defaults are PostgreSQL call-site behavior, not part of a T-SQL
        # type. Strip them before type conversion.
        part = re.split(r"\s+DEFAULT\s+", part, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        # name type
        m = re.match(r"\"?([\w\d_]+)\"?\s+(.+)$", part)
        if m:
            params.append((m.group(1), m.group(2), output))
    return params, []


def _transform_plpgsql_body(body: str) -> tuple[list[str], list[str], dict[str, str]]:
    out: list[str] = []
    warnings: list[str] = []
    declared: dict[str, str] = {}
    stack: list[str] = []
    in_declare = False
    declare_types: dict[str, str] = {}

    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(";").strip()
        if not line:
            continue
        upper = line.upper()

        if upper == "DECLARE":
            in_declare = True
            continue
        if in_declare and upper != "BEGIN":
            m = re.match(
                r'^"?([A-Za-z_][A-Za-z0-9_]*)"?\s+(.+?)(?:\s*:=\s*(.+))?$',
                line,
            )
            if m:
                name, dtype, init = m.group(1), m.group(2), m.group(3)
                declare_types[name] = dtype
                declared[name] = dtype + (f" := {_expr_rev(init)}" if init else "")
                continue
            in_declare = False
        if upper == "BEGIN":
            in_declare = False
            stack.append("begin")
            out.append("BEGIN")
            continue

        if upper.startswith("IF ") and upper.endswith(" THEN"):
            cond = line[3:-5].strip()
            stack.append("if")
            out.append(f"IF {_expr_rev(cond)}")
            out.append("BEGIN")
            continue
        if upper.startswith("ELSIF ") or upper.startswith("ELSEIF "):
            cond = line.split(" ", 1)[1][:-5].strip()
            out.append("END")
            stack.append("if")
            out.append(f"ELSE IF {_expr_rev(cond)}")
            out.append("BEGIN")
            continue
        if upper == "ELSE":
            out.append("END")
            out.append("ELSE")
            out.append("BEGIN")
            continue
        if upper == "END IF":
            out.append("END")
            if stack and stack[-1] == "if":
                stack.pop()
            continue
        if upper == "END LOOP":
            out.append("END")
            if stack and stack[-1] == "while":
                stack.pop()
            continue
        if upper == "END" or upper == "END;":
            out.append("END")
            if stack:
                stack.pop()
            continue
        if upper.startswith("WHILE ") and upper.endswith(" LOOP"):
            cond = line[6:-5].strip()
            stack.append("while")
            out.append(f"WHILE {_expr_rev(cond)}")
            out.append("BEGIN")
            continue
        if upper == "EXCEPTION WHEN OTHERS THEN":
            out.append("END TRY")
            out.append("BEGIN CATCH")
            continue

        # variable assignment x := expr
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:=\s*(.+)$", line)
        if m and m.group(1).upper() not in ("OUT", "IN"):
            out.append(f"SET @{m.group(1)} = {_expr_rev(m.group(2))};")
            continue

        # RAISE NOTICE / RAISE EXCEPTION
        m = re.match(r"^RAISE\s+NOTICE\s+(.+)$", line, re.IGNORECASE)
        if m:
            arg = m.group(1).strip()
            m2 = re.match(r"^'%',\s*(.+)$", arg)
            if m2:
                out.append(f"PRINT {_expr_rev(m2.group(1))};")
            else:
                out.append(f"PRINT {arg};")
            continue
        m = re.match(r"^RAISE\s+EXCEPTION\s+(.+)$", line, re.IGNORECASE)
        if m:
            out.append(f"RAISERROR({_expr_rev(m.group(1).strip())}, 16, 1);")
            continue

        # RETURN QUERY / RETURN NEXT / RETURN
        if upper.startswith("RETURN QUERY"):
            out.append(_expr_rev(line[len("RETURN QUERY"):].strip()) + ";")
            continue
        m = re.match(r"^OPEN\s+[A-Za-z_]\w*\s+FOR\s+(.+)$", line, re.IGNORECASE | re.DOTALL)
        if m:
            out.append(_expr_rev(m.group(1)) + ";")
            continue
        if upper.startswith("RETURN NEXT"):
            out.append(_expr_rev(line[len("RETURN NEXT"):].strip()) + ";")
            continue
        if upper.startswith("RETURN "):
            out.append("RETURN " + _expr_rev(line[7:].strip()) + ";")
            continue
        if upper == "RETURN":
            out.append("RETURN;")
            continue

        # SELECT expr INTO var [FROM ...]
        m = re.match(r"^SELECT\s+(.+?)\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+(.+))?$", line, re.IGNORECASE)
        if m:
            expr, target, rest = m.group(1), m.group(2), m.group(3) or ""
            out.append(f"SELECT @{target} = {_expr_rev(expr)}{' ' + _expr_rev(rest) if rest else ''};")
            continue

        # CREATE TEMP TABLE
        if upper.startswith("CREATE TEMP TABLE"):
            line = re.sub(r"^CREATE\s+TEMP\s+TABLE\s+([\w\d_]+)", r"CREATE TABLE #\1", line, flags=re.IGNORECASE)
            out.append(line + ";")
            continue

        out.append(_expr_rev(line) + ";")

    # declarations not referenced as assignments still need DECLARE in T-SQL
    for name, dtype in declare_types.items():
        if name not in declared:
            declared[name] = dtype

    return out, warnings, declared


def _expr_rev(text: str) -> str:
    text = translate_functions(text, "postgres", "tsql")
    text = re.sub(r'"([\w\s\d_]+)"', r"[\1]", text)
    from engine.translators.sql_builder import _strip_pg_casts, _translate_pg_boolean_literals
    text = _strip_pg_casts(text)
    text = _translate_pg_boolean_literals(text)
    text = text.replace("||", "+")
    text = re.sub(r"ROW_COUNT\s*\(\s*\)", "@@ROWCOUNT", text, flags=re.IGNORECASE)
    text = re.sub(r"LASTVAL\s*\(\s*\)", "SCOPE_IDENTITY()", text, flags=re.IGNORECASE)
    text = re.sub(r"pg_sleep\s*\(([^)]+)\)", "WAITFOR DELAY '00:00:00.001'", text, flags=re.IGNORECASE)
    return text


def _repair_tsql_parameter_contexts(text: str, params: list[tuple[str, str, bool]]) -> str:
    """Repair contexts where a PG parameter and column share a name."""
    names = {name.lower(): name for name, _, _ in params}
    if not names:
        return text

    # Protect INSERT column lists: they contain identifiers, never variables.
    protected: list[str] = []
    def _protect_insert(m):
        protected.append(re.sub(r"@([A-Za-z_]\w*)", r"[\1]", m.group(2)))
        return m.group(1) + f"\x03{len(protected) - 1}\x03" + m.group(3)
    text = re.sub(
        r"(INSERT\s+INTO\s+[^()\s]+\s*\()([^)]*)(\))",
        _protect_insert,
        text, flags=re.IGNORECASE,
    )
    # A common round-trip shape is WHERE param = param: the left occurrence
    # originated as the table column, the right one is the routine parameter.
    for lower, canon in names.items():
        column_marker = f"\x04{canon}\x04"
        text = re.sub(
            rf"\b(WHERE|AND|OR)\s+@{re.escape(canon)}\s*=\s*@{re.escape(canon)}\b",
            rf"\1 {column_marker} = @{canon}", text, flags=re.IGNORECASE,
        )
        # Older converted PG bodies may have accidentally quoted parameters.
        text = re.sub(rf"\[{re.escape(canon)}\]\s+IS\s+NULL", f"@{canon} IS NULL", text, flags=re.IGNORECASE)
        text = re.sub(rf"=\s*\[{re.escape(canon)}\]", f"= @{canon}", text, flags=re.IGNORECASE)
        text = re.sub(rf"(?<!\.)\[{re.escape(canon)}\]", f"@{canon}", text, flags=re.IGNORECASE)
        # Quoted source parameters may only become @variables in the step
        # above. Repair the resulting predicate after that normalization too.
        text = re.sub(
            rf"\b(WHERE|AND|OR)\s+@{re.escape(canon)}\s*=\s*@{re.escape(canon)}\b",
            rf"\1 [{canon}] = @{canon}", text, flags=re.IGNORECASE,
        )
        text = text.replace(column_marker, f"[{canon}]")
    for i, value in enumerate(protected):
        text = text.replace(f"\x03{i}\x03", value)
    return text


_TSQL_KEYWORDS = frozenset(
    "select from where join on as group by order having sum count avg min max top distinct "
    "into insert update delete values and or not null is like between in exists case when then "
    "else end inner outer left right full cross union all set table create alter drop index view "
    "procedure function trigger if while begin declare print raiserror throw waitfor commit rollback "
    "exec execute use default primary foreign references constraint unique check identity asc desc "
    "with collate convert cast return returns limit offset".split()
)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def translate_routine(sql: str, source: str = "procedure", target: str = "postgres",
                      tables: list | None = None,
                      user_types: list | None = None) -> tuple[str | None, list[str]]:
    """Translate a CREATE FUNCTION/PROCEDURE between dialects.

    source is 'procedure' or 'function' (used as a hint when T-SQL header
    detection is ambiguous). ``tables`` is the normalized source schema's
    tables, used to infer concrete result column types so `RETURNS TABLE`
    replaces the awkward `SETOF record`. Returns (converted_sql, warnings).
    """
    if target == "postgres":
        return _tsql_to_plpgsql(sql, tables, user_types)
    return _plpgsql_to_tsql(sql, user_types)


if __name__ == "__main__":
    sample = """CREATE PROCEDURE dbo.GetEmployees
    @DepartmentId INT,
    @OutCount INT OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @maxSal MONEY;
    SELECT @maxSal = MAX(Salary) FROM Employees WHERE DepartmentID = @DepartmentId;
    SET @OutCount = @maxSal;
    SELECT EmployeeID, FirstName, Salary FROM Employees WHERE DepartmentID = @DepartmentId ORDER BY EmployeeID;
END"""
    converted, warnings = translate_routine(sample, source="procedure", target="postgres")
    print("=== T-SQL -> PL/pgSQL ===")
    print(converted)
    print("WARNINGS:", warnings)
    print()
    print("=== PL/pgSQL -> T-SQL ===")
    rev, rw = translate_routine(converted, source="procedure", target="tsql")
    print(rev)
    print("WARNINGS:", rw)
