"""
Trigger translator: T-SQL <-> PL/pgSQL.

T-SQL AFTER/INSTEAD OF triggers fire per-statement with `inserted`/`deleted`
pseudo-tables; PostgreSQL triggers are row-level with NEW/OLD records. This
translator converts the common patterns and warns about the semantic
differences it cannot bridge instead of silently producing wrong code.

Mapping choices:
  - T-SQL AFTER/INSTEAD OF ... ON t  ->  PG FOR EACH ROW trigger function
  - inserted.<col> / deleted.<col>   ->  NEW.<col> / OLD.<col>
  - INSERT ... SELECT ... FROM inserted  ->  INSERT ... VALUES (NEW.col, ...)
  - <expr> IN (SELECT <x> FROM inserted) -> <expr> = NEW.x
"""
from __future__ import annotations

import re

from engine.schema import Trigger
from engine.translators.functions import translate_functions

_HEADER_RE = re.compile(
    r"CREATE\s+(?:OR\s+ALTER\s+)?(?:TRIGGER\s+)?\[?([\w\d_]+)\]?\s+"
    r"(?:ON\s+((?:\[?[\w\d_]+\]?\.)?\[?[\w\d_]+\]?))\s+"
    r"(AFTER|INSTEAD\s+OF|FOR)\s+([A-Z,\s]+?)\s+(?:WITH\s+APPEND\s+)?AS\b",
    re.IGNORECASE | re.DOTALL,
)

_IGNORE_PREFIXES = ("SET NOCOUNT",)


def translate_trigger(trigger: Trigger, target: str) -> tuple[str | None, list[str]]:
    if target == "postgres":
        return _tsql_trigger_to_plpgsql(trigger)
    return _plpgsql_trigger_to_tsql(trigger)


# ---------------------------------------------------------------------------
# shared tokenizer
# ---------------------------------------------------------------------------

def _split_structural(body: str) -> list[str]:
    """Split a T-SQL/PG body into structural keywords and statements.

    Statements may span multiple lines; splitting happens on top-level
    semicolons or structural keywords (BEGIN/END/ELSE/TRY/CATCH) outside
    strings and parentheses.
    """
    keywords = (
        "BEGIN TRY", "BEGIN CATCH", "END TRY", "END CATCH",
        "BEGIN", "END", "ELSE",
    )
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    i = 0
    text = body.replace("\r\n", "\n").replace("\t", " ")
    n = len(text)

    def flush():
        seg = " ".join("".join(buf).split()).strip()
        if seg:
            parts.append(seg)
        buf.clear()

    while i < n:
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            buf.append(ch)
            i += 1
            continue
        if ch in "([":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch in ")]":
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        if ch == ";" and depth == 0:
            flush()
            i += 1
            continue
        if ch == "\n" and depth == 0:
            # check for a keyword at the start of this line
            rest = text[i:].lstrip("\n ")
            upper_rest = rest.upper()
            matched = None
            for kw in keywords:
                if upper_rest.startswith(kw) and (len(rest) == len(kw) or not rest[len(kw)].isalnum() and rest[len(kw)] not in "_"):
                    matched = kw
                    break
            if matched:
                flush()
                parts.append(matched)
                i += text[i:].index(matched) + len(matched)
                continue
            buf.append(" ")
            i += 1
            continue
        buf.append(ch)
        i += 1
    flush()
    return parts


def _expr_tsql_to_pg(text: str) -> str:
    text = translate_functions(text, "tsql", "postgres")
    text = re.sub(r"@([A-Za-z_][A-Za-z0-9_]*)", r"\1", text)
    return text


def _expr_pg_to_tsql(text: str) -> str:
    text = translate_functions(text, "postgres", "tsql")
    text = re.sub(r"ROW_COUNT\s*\(\s*\)", "@@ROWCOUNT", text, flags=re.IGNORECASE)
    return text


# ---------------------------------------------------------------------------
# T-SQL -> PL/pgSQL
# ---------------------------------------------------------------------------

def _tsql_trigger_to_plpgsql(trigger: Trigger) -> tuple[str | None, list[str]]:
    warnings: list[str] = []
    definition = trigger.definition

    m = _HEADER_RE.search(definition)
    if not m:
        return None, ["Could not parse CREATE TRIGGER header"]
    name, table = m.group(1), m.group(2).replace("[", "").replace("]", "")
    timing = m.group(3).upper()
    events = [e for e in re.split(r"[\s,]+", m.group(4)) if e.upper() in ("INSERT", "UPDATE", "DELETE")]

    body = _extract_tsql_body(definition)
    if body is None:
        return None, ["Could not extract trigger body (expected AS BEGIN ... END)"]

    lines = _transform_tsql_trigger_body(body, warnings)

    pg_timing = "INSTEAD OF" if "INSTEAD" in timing else "AFTER"
    pg_events = " OR ".join(e.upper() for e in events) or "INSERT"
    fn_name = f"{name}_fn"
    function = (
        f"CREATE OR REPLACE FUNCTION {fn_name}() RETURNS TRIGGER AS $$\n"
        f"BEGIN\n" + "\n".join("    " + ln for ln in lines) + "\n"
        f"    RETURN NEW;\n"
        f"END;\n$$ LANGUAGE plpgsql;"
    )
    create = (
        f"CREATE TRIGGER {name} {pg_timing} {pg_events} ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {fn_name}();"
    )
    return function + "\n\n" + create, warnings


def _extract_tsql_body(definition: str) -> str | None:
    m = re.search(r"\bAS\s+BEGIN\b", definition, re.IGNORECASE)
    if not m:
        return None
    tail = definition[m.end():]
    ends = list(re.finditer(r"\bEND\b", tail, re.IGNORECASE))
    if not ends:
        return None
    return tail[: ends[-1].start()]


def _transform_tsql_trigger_body(body: str, warnings: list[str]) -> list[str]:
    parts = _split_structural(body)
    out: list[str] = []
    for part in parts:
        upper = part.upper()
        if upper in ("BEGIN", "BEGIN TRY"):
            out.append("BEGIN")
            continue
        if upper == "END":
            out.append("END;")
            continue
        if upper == "END TRY":
            out.append("EXCEPTION WHEN OTHERS THEN")
            continue
        if upper == "BEGIN CATCH":
            continue
        if upper == "END CATCH":
            out.append("END;")
            continue
        if upper == "ELSE":
            out.append("ELSE")
            continue
        if any(part.upper().startswith(p) for p in _IGNORE_PREFIXES):
            continue

        stmt = _rewrite_insert_select(part)
        if re.search(r"\bFROM\s+(?:inserted|deleted)\b", stmt, re.IGNORECASE):
            warnings.append("Statement still references FROM inserted/deleted — multi-row semantics need manual review")
        stmt = re.sub(r"\binserted\b", "NEW", stmt, flags=re.IGNORECASE)
        stmt = re.sub(r"\bdeleted\b", "OLD", stmt, flags=re.IGNORECASE)
        stmt = _expr_tsql_to_pg(stmt)
        out.append(stmt + ";")
    return out


_RESERVED = {"SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "NULL", "IN", "VALUES",
             "INSERT", "UPDATE", "DELETE", "JOIN", "ON", "AS", "SET", "TOP", "DISTINCT",
             "INSERTED", "DELETED", "GETDATE", "NEWID", "NEWSEQUENTIALID", "SCOPE_IDENTITY",
             "ISNULL", "COUNT", "SUM", "MIN", "MAX", "AVG", "COALESCE", "CASE", "WHEN", "THEN", "ELSE", "END"}


def _rewrite_insert_select(stmt: str) -> str:
    """INSERT INTO t(...) SELECT <exprs> FROM inserted  ->  INSERT INTO t(...) VALUES (NEW.x, ...)"""
    # WHERE/AND col IN (SELECT x FROM inserted/deleted) -> col = NEW.x
    stmt = re.sub(
        r"\bIN\s*\(\s*SELECT\s+([A-Za-z_][A-Za-z0-9_.]*)\s+FROM\s+(inserted|deleted)\s*\)",
        lambda m: f"= {'NEW' if m.group(2).lower() == 'inserted' else 'OLD'}.{m.group(1).strip()}",
        stmt, flags=re.IGNORECASE,
    )
    m = re.match(
        r"^(INSERT\s+INTO\s+[^(\s]+\s*\([^)]*\))\s+SELECT\s+(.+?)\s+FROM\s+(inserted|deleted)\s*$",
        stmt, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return stmt
    insert_part, select_list, src_table = m.group(1), m.group(2), m.group(3)
    src = "NEW" if src_table.lower() == "inserted" else "OLD"
    values = []
    for expr in _split_top(select_list):
        expr = expr.strip()
        # qualify a bare column reference with the source record
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", expr) and expr.upper() not in _RESERVED:
            values.append(f"{src}.{expr}")
        else:
            values.append(re.sub(r"(inserted|deleted)\.", f"{src}.", expr, flags=re.IGNORECASE))
    return f"{insert_part} VALUES ({', '.join(values)})"


def _split_top(expr_list: str) -> list[str]:
    parts, cur, depth = [], [], 0
    for ch in expr_list:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


# ---------------------------------------------------------------------------
# PL/pgSQL -> T-SQL
# ---------------------------------------------------------------------------

def _plpgsql_trigger_to_tsql(trigger: Trigger) -> tuple[str | None, list[str]]:
    warnings: list[str] = []
    definition = trigger.definition

    m = re.search(r"\$[A-Za-z_0-9]*\$\s*(?:BEGIN\s+)?(.*?)\s*RETURN\s+\w+;\s*END\s*;\s*\$[A-Za-z_0-9]*\$", definition, re.IGNORECASE | re.DOTALL)
    body = m.group(1) if m else None
    if body is None:
        m2 = re.search(r"\$[A-Za-z_0-9]*\$(.*?)\$[A-Za-z_0-9]*\$", definition, re.IGNORECASE | re.DOTALL)
        body = m2.group(1) if m2 else None
    if body is None:
        return None, ["Could not extract trigger function body"]

    parts = _split_structural(body)
    lines: list[str] = []
    for part in parts:
        upper = part.upper()
        if upper in ("BEGIN",):
            lines.append("    BEGIN")
            continue
        if upper == "END":
            lines.append("    END")
            continue
        if upper == "EXCEPTION WHEN OTHERS THEN":
            lines.append("    END TRY")
            lines.append("    BEGIN CATCH")
            continue
        stmt = _expr_pg_to_tsql(part)
        stmt = re.sub(r"\bNEW\.", "inserted.", stmt)
        stmt = re.sub(r"\bOLD\.", "deleted.", stmt)
        # INSERT ... VALUES (inserted.x, ...) -> INSERT ... SELECT x, ... FROM inserted
        m = re.match(
            r"^(INSERT\s+INTO\s+[^(\s]+\s*\([^)]*\))\s+VALUES\s*\((.*)\)\s*$",
            stmt, re.IGNORECASE | re.DOTALL,
        )
        if m and re.search(r"\b(inserted|deleted)\.", m.group(2), re.IGNORECASE):
            expr_list = ", ".join(re.sub(r"\b(inserted|deleted)\.", "", e, flags=re.IGNORECASE) for e in _split_top(m.group(2)))
            src = "inserted" if "inserted." in m.group(2) else "deleted"
            stmt = f"{m.group(1)} SELECT {expr_list} FROM {src}"
        lines.append("    " + stmt + ";")

    events_tsql = ", ".join(e for e in (trigger.events or ["INSERT"]) if e)
    timing = "INSTEAD OF" if trigger.timing == "INSTEAD OF" else "AFTER"
    create = (
        f"CREATE TRIGGER {trigger.name}\n"
        f"ON {trigger.table}\n"
        f"{timing} {events_tsql}\n"
        f"AS\nBEGIN\n" + "\n".join(lines) + "\nEND"
    )
    return create, warnings
