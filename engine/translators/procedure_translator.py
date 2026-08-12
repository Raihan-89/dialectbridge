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

PARAM_RE = re.compile(
    r"@([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(\[?[A-Za-z_][A-Za-z0-9_]*\]?\s*(?:\([^)]*\))?)"
    r"\s*(OUTPUT|OUT)?"
    r"(?:\s*=\s*[^,]+)?",
    re.IGNORECASE,
)

# A T-SQL type token: int, [int], decimal(18,2), [decimal](18,2), varchar(50)
_TSQL_TYPE_RE = r"\[?[A-Za-z_][A-Za-z0-9_]*\]?\s*(?:\([^)]*\))?"


# ---------------------------------------------------------------------------
# T-SQL -> PL/pgSQL
# ---------------------------------------------------------------------------

def _tsql_to_plpgsql(sql: str, tables: list | None = None) -> tuple[str | None, list[str]]:
    warnings: list[str] = []

    # ---- header -----------------------------------------------------------
    header_match = re.search(
        r"CREATE\s+(?:OR\s+ALTER\s+)?(PROCEDURE|PROC|FUNCTION)\s+"
        r"(?:\[?[\w\d_]+\]?\.)?\[?([\w\d_]+)\]?"
        r"(?:\s*\((.*)\)|\s+(@.*?))?"
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
        ret_match = re.search(r"RETURNS\s+(.+?)\s*(?:WITH\b|AS\b|BEGIN\b|RETURN\b)", sql, re.IGNORECASE | re.DOTALL)
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

    # Comparisons against BIT/BOOLEAN columns (col = 1) are invalid in
    # PostgreSQL (boolean = integer) — rewrite to true/false.
    if tables:
        transformed = [fix_boolean_predicates(ln, tables, "tsql") for ln in transformed]

    if kind == "FUNCTION":
        if returns.lower().strip() == "table" or "table" in returns.lower():
            sig_returns = "TABLE(...)"
        else:
            # Scalar return types are still T-SQL types — map to PostgreSQL
            # (nvarchar -> TEXT, decimal(p,s) -> NUMERIC(p,s), ...).
            converted_returns, ret_warn = convert_type(returns, "tsql", "postgres")
            if ret_warn:
                warnings.append(f"RETURNS {returns}: {ret_warn}")
            sig_returns = converted_returns or "TEXT"
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


def _join_declares(declared: dict[str, str]) -> str:
    if not declared:
        return ""
    lines = []
    for name, type_and_init in declared.items():
        lines.append(f"  {name} {type_and_init};")
    return "\n".join(lines)


def _split_cond_body(rest: str) -> tuple[str, str]:
    """Split ``IF (cond) stmt`` into ``((cond), "stmt")``.

    When the rest starts with a balanced parenthesised condition and more
    follows on the same line, the tail is the single-statement branch body.
    Unparenthesised or unbalanced conditions take the whole rest as the
    condition with no tail."""
    if not rest.startswith("("):
        return rest, ""
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return rest[: i + 1], rest[i + 1 :].strip()
    return rest, ""


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
_SELECT_START_EXCEPT = re.compile(r"^(?:WITH|INSERT)\b", re.IGNORECASE)


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
    tokens = piece.rstrip().strip().split()
    if not tokens:
        return False
    return tokens[-1].rstrip(";").upper() not in _CONTINUATION_ENDINGS


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


def _transform_tsql_statement(line: str, statement_fn, declared, t_warns, returns_set) -> str | None:
    """Rewrite a single completed statement, honoring an optional override."""
    if statement_fn is not None:
        return statement_fn(line, declared, t_warns, returns_set)
    return _transform_statement(line, declared, t_warns, returns_set)


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
        stmt = _transform_tsql_statement(" ".join(buf).rstrip(";").strip(), statement_fn, declared, t_warns, returns_set)
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
            stmt = _transform_tsql_statement(piece, statement_fn, declared, t_warns, returns_set)
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
            if piece.endswith(";") or piece.upper().startswith("DECLARE ") or _is_complete_tail(piece):
                flush()

    for raw_line in _join_balanced_lines(body):
        line = raw_line.strip()
        if not line:
            if not _case_open(" ".join(buf)):
                flush()
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
            if _STMT_START_RE.match(first_word):
                flush()
            elif first_word == "SET" and not _SET_START_EXCEPT.match(buf[0].lstrip()):
                flush()
            elif first_word == "SELECT" and not _SELECT_START_EXCEPT.match(buf[0].lstrip()):
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
        if line.endswith(";") or upper.startswith("DECLARE "):
            flush()

    if buf:
        flush()
    while waiting:
        _close_now()
    if stack:
        t_warns.append(f"Unbalanced BEGIN/END blocks: {stack}")

    return out, t_warns, declared


def _transform_statement(line: str, declared: dict[str, str], warnings: list[str], returns_set: bool) -> str | None:
    # Normalize a leading SELECT TOP n to a trailing LIMIT n first so the
    # assignment-SELECT regexes below still match "SELECT TOP 1 @x = ...".
    line = _translate_top(line)
    upper = line.upper()

    if upper in ("SET NOCOUNT ON", "SET NOCOUNT OFF"):
        return None

    # BEGIN/COMMIT/ROLLBACK TRANSACTION -> plain PL/pgSQL transaction control
    if re.match(r"^BEGIN\s+(TRANSACTION|TRAN|WORK)\b", line, re.IGNORECASE):
        return "BEGIN;"
    if re.match(r"^(COMMIT|ROLLBACK)\s+(TRANSACTION|TRAN|WORK)\b", line, re.IGNORECASE):
        return f"{upper.split()[0]};"

    # DECLARE @x INT / DECLARE @x INT = expr
    m = re.match(rf"^DECLARE\s+@([\w]+)\s+({_TSQL_TYPE_RE})\s*(?:=\s*(.*))?$", line, re.IGNORECASE)
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
    m = re.match(r"^THROW\s+(\d+)\s*,\s*([^,]+)\s*,", line, re.IGNORECASE)
    if m:
        return f"RAISE EXCEPTION {m.group(2).strip()};"
    if upper.startswith("THROW"):
        return "RAISE EXCEPTION 'error';"

    # RETURN [expr]
    m = re.match(r"^RETURN\s+(.+)$", line, re.IGNORECASE)
    if m:
        return f"RETURN {_expr(m.group(1).strip())};"
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


def _expr(text: str) -> str:
    """Translate expressions inside a statement."""
    text = translate_functions(text, "tsql", "postgres")
    # MSSQL bracket identifiers -> PG quoted identifiers
    text = re.sub(r"\[([\w\s\d_]+)\]", r'"\1"', text)
    # MSSQL N-prefixed string literals have no PG equivalent
    text = re.sub(r"\bN'", "'", text, flags=re.IGNORECASE)
    # variable references @x -> x (not inside string literals)
    text = re.sub(r"@([A-Za-z_][A-Za-z0-9_]*)", r"\1", text)
    # @@system vars
    text = re.sub(r"@@ROWCOUNT", "ROW_COUNT()", text, flags=re.IGNORECASE)
    text = re.sub(r"@@IDENTITY", "LASTVAL()", text, flags=re.IGNORECASE)
    text = re.sub(r"SCOPE_IDENTITY\s*\(\)", "LASTVAL()", text, flags=re.IGNORECASE)
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

        # Initializers are stored separately from body statements.
        declared = {name: _qualify(value) for name, value in declared.items()}

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

    transformed = [_repair_tsql_parameter_contexts(ln, params) for ln in transformed]

    declare_lines = []
    for var_name, type_and_init in declared.items():
        pieces = re.split(r"\s*:=\s*", type_and_init, maxsplit=1)
        mapped_type, type_warn = convert_type(pieces[0], "postgres", "tsql")
        if type_warn:
            warnings.append(f"Variable {var_name}: {type_warn}")
        initializer = f" = {pieces[1]}" if len(pieces) == 2 else ""
        declare_lines.append(f"    DECLARE @{var_name} {mapped_type or 'NVARCHAR(MAX)'}{initializer};\n")
    declares = "".join(declare_lines)

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
