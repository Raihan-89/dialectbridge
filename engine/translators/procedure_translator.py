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
from engine.translators.sql_builder import convert_type

PARAM_RE = re.compile(
    r"@([A-Za-z_][A-Za-z0-9_]*)\s+([^\s,]+(?:\([^)]*\))?)\s*(OUTPUT|OUT)?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# T-SQL -> PL/pgSQL
# ---------------------------------------------------------------------------

def _tsql_to_plpgsql(sql: str, tables: list | None = None) -> tuple[str | None, list[str]]:
    warnings: list[str] = []

    # ---- header -----------------------------------------------------------
    header_match = re.search(
        r"CREATE\s+(?:OR\s+ALTER\s+)?(PROCEDURE|PROC|FUNCTION)\s+"
        r"(?:\[?[\w\d_]+\]?\.)?\[?([\w\d_]+)\]?"
        r"(?:\s*\((.*?)\)|\s+(@.*?))?"
        r"(?=\s*(?:WITH|AS|RETURNS)\b|\s*$)",
        sql, re.IGNORECASE | re.DOTALL,
    )
    if not header_match:
        return None, ["Could not parse routine header"]

    kind = header_match.group(1).upper()
    name = header_match.group(2)
    param_text = header_match.group(3) or header_match.group(4) or ""

    # ---- RETURNS clause for functions --------------------------------------
    returns = "void"
    if kind == "FUNCTION":
        ret_match = re.search(r"RETURNS\s+(.+?)\s*(?:AS\b|BEGIN|RETURN\b)", sql, re.IGNORECASE | re.DOTALL)
        if ret_match:
            returns = ret_match.group(1).strip()

    # ---- body -------------------------------------------------------------
    body = _extract_tsql_body(sql)
    if body is None:
        return None, ["Could not extract routine body (expected AS BEGIN ... END)"]

    params, param_warns = _parse_params(param_text, "tsql")
    warnings.extend(param_warns)

    # detect result-returning SELECTs
    has_result_select = _has_result_select(body)

    # ---- transform body ----------------------------------------------------
    transformed, t_warns, declared = _transform_tsql_body(body, kind, has_result_select)
    warnings.extend(t_warns)

    if kind == "FUNCTION":
        if returns.lower().strip() == "table" or "table" in returns.lower():
            returns = "TABLE(...)"
        sig_returns = returns
    else:
        # Infer concrete result columns so callers can use `SELECT *` directly
        # instead of being forced into a column definition list (SETOF record).
        result_cols = _infer_result_columns(transformed, tables)
        if result_cols:
            sig_returns = "TABLE(" + ", ".join(f'"{alias}" {coltype}' for alias, coltype in result_cols) + ")"
        else:
            sig_returns = "SETOF record" if has_result_select else "void"

    param_list = []
    for pname, ptype, output in params:
        tgt_type, warn = convert_type(ptype, "tsql", "postgres")
        if warn:
            warnings.append(f"Parameter @{pname} type '{ptype}': {warn}")
        tgt_type = tgt_type or "TEXT"
        if output:
            param_list.append(f"OUT {pname} {tgt_type}")
        else:
            param_list.append(f"{pname} {tgt_type}")
    if kind == "FUNCTION" and "TABLE(...)" in sig_returns:
        # parse the @table variable form later; keep a placeholder
        sig_returns = "TABLE(...)"

    signature = ", ".join(param_list)
    header = f"CREATE OR REPLACE FUNCTION {name}({signature}) RETURNS {sig_returns} AS $$"
    footer = "$$ LANGUAGE plpgsql;"

    # table-returning functions need a return statement
    if kind == "FUNCTION" and "TABLE(" in sig_returns:
        transformed.append("RETURN QUERY SELECT ...;")
        warnings.append("Table-valued function body requires manual review of the RETURN clause")

    converted = _assemble_plpgsql(header, declared, transformed, footer)
    return converted, warnings


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


def _extract_tsql_body(sql: str) -> str | None:
    """Return the text between the trailing AS [BEGIN] ... final END."""
    m = re.search(r"\bAS\s+BEGIN\b", sql, re.IGNORECASE)
    if not m:
        # functions like inline TVF: AS RETURN (SELECT ...)
        m2 = re.search(r"\bAS\s+(.*)$", sql, re.IGNORECASE | re.DOTALL)
        return m2.group(1).strip() if m2 else None
    start = m.end()
    # find matching final END (last occurrence of a standalone END)
    tail = sql[start:]
    # The final "END" is the last word-boundary END at the end
    end_matches = list(re.finditer(r"\bEND\b", tail, re.IGNORECASE))
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
        params.append((pname, ptype, bool(output)))
    return params, warnings


def _has_result_select(body: str) -> bool:
    # bare SELECT returning rows: not "SELECT @x =", not "SELECT INTO #"
    stripped = re.sub(r"SELECT\s+@[\w]+\s*=", "", body, flags=re.IGNORECASE)
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


def _join_declares(declared: dict[str, str]) -> str:
    if not declared:
        return ""
    lines = []
    for name, type_and_init in declared.items():
        lines.append(f"  {name} {type_and_init};")
    return "\n".join(lines)


def _transform_tsql_body(body: str, kind: str, returns_set: bool) -> tuple[list[str], list[str], dict[str, str]]:
    """Body transformation with statement accumulation.

    Statements may span several lines and end with ``;``, so lines are
    accumulated into a buffer until the terminating semicolon (or a
    control keyword / blank line) and transformed as a whole — a multi-line
    ``SELECT ... FROM ... JOIN ... GROUP BY ...`` must stay one statement.
    Returns (output lines, warnings, declared vars).
    """
    out: list[str] = []
    t_warns: list[str] = []
    declared: dict[str, str] = {}
    stack: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        stmt = _transform_statement(" ".join(buf).rstrip(";").strip(), declared, t_warns, returns_set)
        buf = []
        if stmt is not None:
            out.append(stmt)

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        upper = line.upper()

        is_control = (
            upper in ("BEGIN", "END", "ELSE")
            or upper.startswith("BEGIN TRY")
            or upper.startswith("BEGIN CATCH")
            or upper.startswith("END TRY")
            or upper.startswith("END CATCH")
            or re.match(r"^ELSE\s+IF\b", line, re.IGNORECASE)
            or re.match(r"^IF\s", line, re.IGNORECASE)
            or re.match(r"^WHILE\s", line, re.IGNORECASE)
        )
        if is_control:
            flush()

        # ---- block openers --------------------------------------------------
        if re.match(r"^IF\s+.*\bBEGIN\s*$", line, re.IGNORECASE):
            cond = re.sub(r"\bBEGIN\s*$", "", line, flags=re.IGNORECASE)
            cond = cond[2:].strip()
            stack.append("if")
            out.append(f"IF {_expr(cond)} THEN")
            continue
        if re.match(r"^IF\s", line, re.IGNORECASE):
            cond = line[2:].strip()
            stack.append("if")
            out.append(f"IF {_expr(cond)} THEN")
            continue
        if re.match(r"^WHILE\s+.*\bBEGIN\s*$", line, re.IGNORECASE):
            cond = re.sub(r"\bBEGIN\s*$", "", line, flags=re.IGNORECASE)
            cond = cond[5:].strip()
            stack.append("while")
            out.append(f"WHILE {_expr(cond)} LOOP")
            continue
        if re.match(r"^WHILE\s", line, re.IGNORECASE):
            cond = line[5:].strip()
            stack.append("while")
            out.append(f"WHILE {_expr(cond)} LOOP")
            continue
        if upper.startswith("BEGIN TRY"):
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
        if upper == "ELSE":
            if stack and stack[-1] == "if":
                out.append("ELSE")
            else:
                out.append("ELSE")
            continue
        if upper.startswith("ELSE IF"):
            out.append("ELSE")
            cond = line[7:].strip()
            stack.append("if")
            out.append(f"IF {_expr(cond)} THEN")
            continue
        if upper == "BEGIN":
            stack.append("begin")
            out.append("BEGIN")
            continue
        if upper == "END":
            block = stack.pop() if stack else "begin"
            if block == "if":
                out.append("END IF;")
            elif block == "while":
                out.append("END LOOP;")
            else:
                out.append("END;")
            continue

        # ---- statements ------------------------------------------------------
        buf.append(line)
        if line.endswith(";") or upper.startswith("DECLARE "):
            flush()

    if buf:
        flush()
    if stack:
        t_warns.append(f"Unbalanced BEGIN/END blocks: {stack}")

    return out, t_warns, declared


def _transform_statement(line: str, declared: dict[str, str], warnings: list[str], returns_set: bool) -> str | None:
    upper = line.upper()

    if upper in ("SET NOCOUNT ON", "SET NOCOUNT OFF"):
        return None

    # DECLARE @x INT / DECLARE @x INT = expr
    m = re.match(r"^DECLARE\s+@([\w]+)\s+([^\s=,]+(?:\s*\([^)]*\))?)\s*(?:=\s*(.*))?$", line, re.IGNORECASE)
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

    # SET @x = expr
    m = re.match(r"^SET\s+@([\w]+)\s*=\s*(.+)$", line, re.IGNORECASE)
    if m:
        return f"{m.group(1)} := {_expr(m.group(2))};"

    # SELECT @x = expr [FROM ...]  (single assignment)
    m = re.match(r"^SELECT\s+@([\w]+)\s*=\s*(.+?)\s+FROM\s+(.+)$", line, re.IGNORECASE | re.DOTALL)
    if m:
        var, expr, tail = m.group(1), m.group(2), m.group(3)
        return f"SELECT {_expr(expr)} INTO {var} FROM {_expr(tail)};"

    # SELECT @x = expr  (no FROM)
    m = re.match(r"^SELECT\s+@([\w]+)\s*=\s*(.+)$", line, re.IGNORECASE | re.DOTALL)
    if m:
        var, expr = m.group(1), m.group(2)
        return f"SELECT {_expr(expr)} INTO {var};"

    # PRINT @x / PRINT 'text'
    m = re.match(r"^PRINT\s+(.+)$", line, re.IGNORECASE)
    if m:
        arg = m.group(1).strip()
        if arg.startswith("@"):
            return f"RAISE NOTICE '%', {arg[1:]};"
        return f"RAISE NOTICE {arg};"

    # RAISERROR / THROW
    m = re.match(r"^RAISERROR\s*\(\s*([^,]+)\s*,\s*(\d+)\s*,\s*\d+", line, re.IGNORECASE)
    if m:
        msg = m.group(1).strip()
        if msg.startswith("@"):
            return f"RAISE EXCEPTION '%', {msg[1:]};"
        return f"RAISE EXCEPTION {msg};"
    m = re.match(r"^THROW\s+(\d+)\s*,\s*([^,]+)\s*,", line, re.IGNORECASE)
    if m:
        return f"RAISE EXCEPTION {m.group(2).strip()};"
    if upper.startswith("THROW"):
        return "RAISE EXCEPTION 'error';"

    # RETURN [expr]
    m = re.match(r"^RETURN\s+(.+)$", line, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if val.startswith("@"):
            return f"RETURN {val[1:]};"
        return f"RETURN {_expr(val)};"
    if upper == "RETURN":
        return "RETURN;"

    # WAITFOR DELAY
    m = re.match(r"^WAITFOR\s+DELAY\s+'([^']+)'", line, re.IGNORECASE)
    if m:
        return f"PERFORM pg_sleep({_delay_to_seconds(m.group(1))});"

    # result-returning SELECT -> RETURN QUERY
    if returns_set and re.match(r"^SELECT\b", line, re.IGNORECASE) and not re.match(r"^SELECT\s+INTO\b", line, re.IGNORECASE):
        return f"RETURN QUERY {_expr(line)};"

    # SELECT ... INTO #temp / INSERT INTO #temp
    if re.match(r"^SELECT\s+.*\s+INTO\s+(\[?#[\w\d_]+\]?)", line, re.IGNORECASE | re.DOTALL):
        line = re.sub(r"\bINTO\s+\[?#([\w\d_]+)\]?", r"INTO \1", line, flags=re.IGNORECASE)
        return _expr(line) + ";"
    if re.match(r"^INSERT\s+INTO\s+\[?#[\w\d_]+\]?", line, re.IGNORECASE):
        line = re.sub(r"\[?#([\w\d_]+)\]?", r"\1", line)
        warnings.append("Temp table referenced - ensure CREATE TEMP TABLE precedes it")
        return _expr(line) + ";"

    # plain DML / control
    return _expr(line) + ";"


def _delay_to_seconds(delay: str) -> float:
    parts = delay.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    return float(delay)


def _expr(text: str) -> str:
    """Translate expressions inside a statement."""
    text = translate_functions(text, "tsql", "postgres")
    # variable references @x -> x (not inside string literals)
    text = re.sub(r"@([A-Za-z_][A-Za-z0-9_]*)", r"\1", text)
    # @@system vars
    text = re.sub(r"@@ROWCOUNT", "ROW_COUNT()", text, flags=re.IGNORECASE)
    text = re.sub(r"@@IDENTITY", "LASTVAL()", text, flags=re.IGNORECASE)
    text = re.sub(r"SCOPE_IDENTITY\s*\(\)", "LASTVAL()", text, flags=re.IGNORECASE)
    # #temp references
    text = re.sub(r"\[?#([\w\d_]+)\]?", r"\1", text)
    return text


# ---------------------------------------------------------------------------
# PL/pgSQL -> T-SQL
# ---------------------------------------------------------------------------

def _plpgsql_to_tsql(sql: str) -> tuple[str | None, list[str]]:
    warnings: list[str] = []

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

    param_list = []
    for pname, ptype, output in params:
        tgt_type, warn = convert_type(ptype, "postgres", "tsql")
        if warn:
            warnings.append(f"Parameter {pname}: {warn}")
        tgt_type = tgt_type or "NVARCHAR(MAX)"
        param_list.append(f"@{pname} {tgt_type}{' OUTPUT' if output else ''}")

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
                r"(?<![.\w\"@\[])([A-Za-z_][A-Za-z0-9_]*)\b",
                _repl, ln, flags=re.IGNORECASE,
            )
        transformed = [_qualify(ln) for ln in transformed]

    declares = "".join(f"    DECLARE @{k} {v};\n" for k, v in declared.items())

    lower_returns = returns.lower()
    is_scalar_fn = not (
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
            f"CREATE PROCEDURE [{schema}].[{name}] ({', '.join(param_list)})\n"
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
    for part in param_text.split(","):
        part = part.strip()
        if not part:
            continue
        output = False
        if part.upper().startswith("OUT "):
            output = True
            part = part[4:].strip()
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
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)|\S+)\s*(?::=\s*(.+))?$", line)
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
        if upper == "END IF;":
            out.append("END")
            if stack and stack[-1] == "if":
                stack.pop()
            continue
        if upper == "END LOOP;":
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
    text = re.sub(r"::[\w\s.]+", "", text)
    text = re.sub(r"ROW_COUNT\s*\(\s*\)", "@@ROWCOUNT", text, flags=re.IGNORECASE)
    text = re.sub(r"LASTVAL\s*\(\s*\)", "SCOPE_IDENTITY()", text, flags=re.IGNORECASE)
    text = re.sub(r"pg_sleep\s*\(([^)]+)\)", "WAITFOR DELAY '00:00:00.001'", text, flags=re.IGNORECASE)
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
                      tables: list | None = None) -> tuple[str | None, list[str]]:
    """Translate a CREATE FUNCTION/PROCEDURE between dialects.

    source is 'procedure' or 'function' (used as a hint when T-SQL header
    detection is ambiguous). ``tables`` is the normalized source schema's
    tables, used to infer concrete result column types so `RETURNS TABLE`
    replaces the awkward `SETOF record`. Returns (converted_sql, warnings).
    """
    if target == "postgres":
        return _tsql_to_plpgsql(sql, tables)
    return _plpgsql_to_tsql(sql)


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
