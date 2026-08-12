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
from engine.translators.procedure_translator import (
    _transform_plpgsql_body,
    _transform_tsql_body,
    _translate_top,
    _split_args_balanced,
)

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
    from engine.translators.sql_builder import _strip_pg_casts, _translate_pg_boolean_literals
    text = re.sub(r'"([\w\s\d_]+)"', r"[\1]", text)
    text = _strip_pg_casts(text)
    text = _translate_pg_boolean_literals(text)
    text = text.replace("||", "+")
    text = re.sub(r"ROW_COUNT\s*\(\s*\)", "@@ROWCOUNT", text, flags=re.IGNORECASE)
    # SQL Server has no equivalent recursion-depth function. Nested triggers
    # can be guarded with TRIGGER_NESTLEVEL().
    text = re.sub(r"pg_trigger_depth\s*\(\s*\)", "TRIGGER_NESTLEVEL()", text, flags=re.IGNORECASE)
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

    pg_timing = "INSTEAD OF" if "INSTEAD" in timing else "AFTER"
    pg_return = "NEW"
    if "INSTEAD" in timing:
        if events == ["DELETE"]:
            # INSTEAD OF DELETE on a base table -> BEFORE DELETE row trigger:
            # PostgreSQL deletes the row after the trigger returns, so the
            # explicit self-DELETE is stripped and the trigger returns OLD.
            pg_timing = "BEFORE"
            pg_return = "OLD"
        else:
            return None, ["INSTEAD OF triggers on base tables have no PostgreSQL equivalent (only views support INSTEAD OF); the trigger was skipped — reimplement its logic as a BEFORE trigger or application-level check"]

    body = _extract_tsql_body(definition)
    if body is None:
        return None, ["Could not extract trigger body (expected AS BEGIN ... END)"]

    lines = _transform_tsql_trigger_body(body, warnings, table=table, return_target=pg_return)

    pg_events = " OR ".join(e.upper() for e in events) or "INSERT"
    fn_name = f"{name}_fn"
    function = (
        f"CREATE OR REPLACE FUNCTION {fn_name}() RETURNS TRIGGER AS $$\n"
        f"BEGIN\n" + "\n".join("    " + ln for ln in lines) + "\n"
        f"    RETURN {pg_return};\n"
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


def _guard_repl(m: re.Match) -> str:
    """``EXISTS (SELECT * FROM inserted|deleted)`` presence guards, which are
    per-statement in T-SQL, map to per-row ``TG_OP`` tests in PostgreSQL.
    (``NEW IS NOT NULL`` would be wrong: for a composite record it is only
    true when every field is non-null.)"""
    if m.group(2).lower() == "inserted":
        return "TG_OP = 'DELETE'" if m.group(1) else "TG_OP IN ('INSERT','UPDATE')"
    return "TG_OP = 'INSERT'" if m.group(1) else "TG_OP IN ('DELETE','UPDATE')"


def _wrap_insdel(text: str) -> str:
    """Rewrite the T-SQL `inserted`/`deleted` pseudo-tables into single-row
    PostgreSQL forms (string literals are preserved).

    - ``EXISTS (SELECT * FROM inserted)`` guards become ``TG_OP``
    - ``FROM|JOIN inserted [alias]`` becomes ``FROM (SELECT (NEW).*) [alias]``
      so column references resolve against the trigger's row (same for OLD)."""
    parts = re.split(r"('(?:[^']|'')*')", text)
    for i, p in enumerate(parts):
        if p.startswith("'"):
            continue
        p = re.sub(
            r"(NOT\s+)?EXISTS\s*\(\s*SELECT\s+\*\s+FROM\s+(inserted|deleted)\s*\)",
            _guard_repl,
            p, flags=re.IGNORECASE,
        )
        p = re.sub(
            r"\b(FROM|JOIN)\s+(inserted|deleted)\b",
            lambda m: f"{m.group(1)} (SELECT ({'NEW' if m.group(2).lower() == 'inserted' else 'OLD'}).*)",
            p, flags=re.IGNORECASE,
        )
        parts[i] = p
    return "".join(parts)


def _sub_insdel(text: str) -> str:
    """Substitute ``inserted`` -> ``NEW`` and ``deleted`` -> ``OLD`` outside
    string literals (so IF predicates and expressions pick them up without
    corrupting RAISERROR messages)."""
    parts = re.split(r"('(?:[^']|'')*')", text)
    return "".join(
        re.sub(r"\binserted\b", "NEW", re.sub(r"\bdeleted\b", "OLD", p, flags=re.IGNORECASE), flags=re.IGNORECASE)
        if not p.startswith("'") else p
        for p in parts
    )


def _transform_tsql_trigger_body(body: str, warnings: list[str], table: str | None = None,
                                 return_target: str = "NEW") -> list[str]:
    """Transform a trigger body by delegating to the procedure translator's
    block engine (IF/WHILE/BEGIN/END/TRY/CATCH, multi-line statements), with a
    trigger-specific statement rewriter (`_trigger_statement`)."""
    def statement_fn(line, declared, warns, returns_set):
        return _trigger_statement(line, declared, warns, returns_set, table=table, return_target=return_target)

    out, warns, _ = _transform_tsql_body(_wrap_insdel(body), "trigger", False, statement_fn=statement_fn)
    warnings.extend(warns)
    return out


def _translate_raiseerror(line: str) -> str:
    """RAISERROR / THROW -> RAISE EXCEPTION (only the message and optional
    positional values are kept; severity/state are dropped)."""
    m = re.match(r"^RAISERROR\s*\((.*)\)\s*$", line, re.IGNORECASE | re.DOTALL)
    if m:
        args = _split_args_balanced(m.group(1))
        if args:
            msg = args[0].strip()
            if msg.startswith("@"):
                return f"RAISE EXCEPTION '%', {msg[1:]}"
            msg_literal = re.sub(r"%\s*[sdiduoxXcgeEfG]", "%", msg)
            if len(args) > 3:
                extra = ", ".join(a.strip() for a in args[3:])
                return f"RAISE EXCEPTION {msg_literal}, {extra}"
            return f"RAISE EXCEPTION {msg_literal}"
        return "RAISE EXCEPTION 'error'"
    m = re.match(r"^THROW\s+(\d+)\s*,\s*([^,]+)\s*,", line, re.IGNORECASE)
    if m:
        return f"RAISE EXCEPTION {m.group(2).strip()}"
    if line.upper().startswith("THROW"):
        return "RAISE EXCEPTION 'error'"
    return line


def _trigger_statement(line: str, declared: dict[str, str], warnings: list[str], returns_set: bool,
                       table: str | None = None, return_target: str = "NEW") -> str | None:
    """Rewrite a completed trigger-body statement (called via the
    procedure translator's statement_fn hook)."""
    upper = line.upper()
    if upper in ("SET NOCOUNT ON", "SET NOCOUNT OFF"):
        return None
    # transaction control is not supported inside PL/pgSQL trigger functions
    if re.match(r"^BEGIN\s+(TRANSACTION|TRAN|WORK)\b", line, re.IGNORECASE):
        warnings.append("transaction control 'BEGIN TRANSACTION' is not supported inside PL/pgSQL trigger functions and was removed")
        return None
    if re.match(r"^(COMMIT|ROLLBACK)\s+(TRANSACTION|TRAN|WORK)\b", line, re.IGNORECASE):
        warnings.append(f"transaction control '{upper.split()[0]} TRANSACTION' is not supported inside PL/pgSQL trigger functions and was removed")
        return None
    # An INSTEAD OF DELETE converted to a BEFORE DELETE trigger: the explicit
    # DELETE of the trigger's own table is implicit in PostgreSQL and removed.
    guard_self_write = False
    if table:
        bare = re.escape(table.split(".")[-1])
        if re.match(rf"^DELETE\s+FROM\s+{bare}\b", line, re.IGNORECASE) and re.search(
                r"\(SELECT \((NEW|OLD)\)\.\*\)", line, re.IGNORECASE):
            warnings.append("DELETE inside an INSTEAD OF DELETE trigger is implicit in PostgreSQL (BEFORE DELETE) and was removed")
            return None
        # A trigger that writes to its own table would fire itself forever
        # (MSSQL disables recursive triggers by default). Guard the self-write
        # so it only runs for the outermost statement.
        guard_self_write = re.match(rf"^(?:UPDATE\s+{bare}\b|INSERT\s+INTO\s+{bare}\b)", line, re.IGNORECASE) and bool(
            re.search(r"\(SELECT \((NEW|OLD)\)\.\*\)", line, re.IGNORECASE))

    line = _translate_raiseerror(line)
    line = _translate_top(line)
    stmt = _rewrite_insert_select(line)
    if re.search(r"\bFROM\s+(?:inserted|deleted)\b", stmt, re.IGNORECASE):
        warnings.append("Statement still references FROM inserted/deleted — multi-row semantics need manual review")
    stmt = _sub_insdel(stmt)
    stmt = _expr_tsql_to_pg(stmt)
    if guard_self_write:
        return f"IF pg_trigger_depth() = 1 THEN\n{stmt};\nEND IF;"
    # A row-level trigger function must RETURN NEW/OLD; a bare RETURN exits.
    if re.match(r"^RETURN\b", stmt, re.IGNORECASE) and not re.match(rf"^RETURN\s+(NEW|OLD)\b", stmt, re.IGNORECASE):
        stmt = re.sub(r"^RETURN\b", f"RETURN {return_target}", stmt, flags=re.IGNORECASE)
    return stmt + ";"


_RESERVED = {"SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "NULL", "IN", "VALUES",
             "INSERT", "UPDATE", "DELETE", "JOIN", "ON", "AS", "SET", "TOP", "DISTINCT",
             "INSERTED", "DELETED", "GETDATE", "NEWID", "NEWSEQUENTIALID", "SCOPE_IDENTITY",
             "ISNULL", "COUNT", "SUM", "MIN", "MAX", "AVG", "COALESCE", "CASE", "WHEN", "THEN", "ELSE", "END",
             "SYSTEM_USER", "CONCAT", "RETURN", "RAISERROR"}


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

    # Use the routine block parser for PL/pgSQL IF/ELSIF/ELSE/LOOP syntax;
    # treating those tokens as plain statements leaves THEN in generated SQL.
    parts, body_warnings, declared = _transform_plpgsql_body(body)
    warnings.extend(body_warnings)
    lines: list[str] = []
    for var_name, var_type in declared.items():
        dtype = var_type.split(":=", 1)[0].strip()
        from engine.translators.sql_builder import convert_type
        mapped, warn = convert_type(dtype, "postgres", "tsql")
        if warn:
            warnings.append(f"Variable {var_name}: {warn}")
        lines.append(f"    DECLARE @{var_name} {mapped or 'NVARCHAR(MAX)'};")
    for part in parts:
        stmt = _expr_pg_to_tsql(part.rstrip(";"))
        # Bodies produced by our T-SQL -> PG converter use these single-row
        # wrappers. On the return trip they are exactly the pseudo-tables.
        stmt = re.sub(r"\(\s*SELECT\s+\(\s*NEW\s*\)\.\*\s*\)", "inserted", stmt, flags=re.IGNORECASE)
        stmt = re.sub(r"\(\s*SELECT\s+\(\s*OLD\s*\)\.\*\s*\)", "deleted", stmt, flags=re.IGNORECASE)
        stmt = re.sub(r"\bRETURN\s+(?:NEW|OLD)\b", "RETURN", stmt, flags=re.IGNORECASE)
        stmt = _translate_trigger_operation_guards(stmt, trigger.events or [])
        # PostgreSQL LIMIT in scalar subqueries becomes T-SQL TOP.
        stmt = re.sub(r"\(SELECT\s+(.+?)\s+LIMIT\s+1\)", r"(SELECT TOP (1) \1)", stmt,
                      flags=re.IGNORECASE | re.DOTALL)
        # INSERT ... VALUES (inserted.x, ...) -> INSERT ... SELECT x, ... FROM inserted
        stmt = re.sub(r"\bNEW\.", "inserted.", stmt, flags=re.IGNORECASE)
        stmt = re.sub(r"\bOLD\.", "deleted.", stmt, flags=re.IGNORECASE)
        m = re.match(
            r"^(INSERT\s+INTO\s+[^(\s]+\s*\([^)]*\))\s+VALUES\s*\((.*)\)\s*$",
            stmt, re.IGNORECASE | re.DOTALL,
        )
        if m and re.search(r"\b(inserted|deleted)\.", m.group(2), re.IGNORECASE):
            expr_list = ", ".join(re.sub(r"\b(inserted|deleted)\.", "", e, flags=re.IGNORECASE) for e in _split_top(m.group(2)))
            src = "inserted" if "inserted." in m.group(2) else "deleted"
            stmt = f"{m.group(1)} SELECT {expr_list} FROM {src}"
        else:
            # inserted/deleted are pseudo-tables, not row variables. Scalar
            # NEW.x/OLD.x references therefore need a subquery in T-SQL.
            stmt = re.sub(
                r"\b(inserted|deleted)\.([A-Za-z_][A-Za-z0-9_]*)",
                lambda sm: f"(SELECT [{sm.group(2)}] FROM {sm.group(1).lower()})",
                stmt, flags=re.IGNORECASE,
            )
        suffix = "" if stmt.upper() in ("BEGIN", "END", "ELSE", "RETURN") or stmt.upper().startswith(("IF ", "ELSE IF ", "WHILE ")) else ";"
        lines.append("    " + stmt + suffix)

    events_tsql = ", ".join(e for e in (trigger.events or ["INSERT"]) if e)
    timing = "INSTEAD OF" if trigger.timing == "INSTEAD OF" else "AFTER"
    create = (
        f"CREATE TRIGGER {trigger.name}\n"
        f"ON {trigger.table}\n"
        f"{timing} {events_tsql}\n"
        f"AS\nBEGIN\n" + "\n".join(lines) + "\nEND"
    )
    return create, warnings


def _translate_trigger_operation_guards(text: str, events: list[str]) -> str:
    """Map PostgreSQL TG_OP predicates to inserted/deleted presence checks."""
    replacements = {
        "INSERT": "EXISTS (SELECT 1 FROM inserted) AND NOT EXISTS (SELECT 1 FROM deleted)",
        "UPDATE": "EXISTS (SELECT 1 FROM inserted) AND EXISTS (SELECT 1 FROM deleted)",
        "DELETE": "EXISTS (SELECT 1 FROM deleted) AND NOT EXISTS (SELECT 1 FROM inserted)",
    }
    for op, predicate in replacements.items():
        text = re.sub(rf"TG_OP\s*=\s*'{op}'", predicate, text, flags=re.IGNORECASE)
    text = re.sub(
        r"TG_OP\s+IN\s*\(\s*'INSERT'\s*,\s*'UPDATE'\s*\)",
        "EXISTS (SELECT 1 FROM inserted)", text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"TG_OP\s+IN\s*\(\s*'DELETE'\s*,\s*'UPDATE'\s*\)",
        "EXISTS (SELECT 1 FROM deleted)", text, flags=re.IGNORECASE,
    )
    return text
