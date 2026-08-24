"""
Best-effort translation of common built-in function calls and small
expressions between T-SQL and PostgreSQL. Used for view bodies, index
predicates, computed columns and defaults.

Strategy: locate function calls with a lightweight balanced-paren scanner
(so we never split on commas inside nested calls), translate the arguments
recursively, then rewrite the function name and/or shape. Anything that
can't be translated safely is left as-is (never silently changed).
"""
from __future__ import annotations

import re

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


def _find_calls(text: str):
    """Yield (start, end, name, arg_string) for every NAME(...) call.

    Balanced-paren aware; returns outermost calls only. Parentheses inside
    string literals are skipped: counting them desynchronised the scanner, so
    a call such as ``ISNULL(x, '(')`` was matched with the wrong end offset and
    ``_rewrite_calls`` spliced its replacement over unrelated SQL, silently
    deleting text (an unterminated literal further along the statement).

    Delimited identifiers are skipped whole for the same reason: an apostrophe
    inside ``[Beneficiary's name]`` / ``"Beneficiary's name"`` is part of the
    name, not the start of a literal, and reading it as one made the scanner
    run to the next unrelated quote and miss every call in between.
    """
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            i = _skip_string(text, i)
            continue
        if ch == '"' or ch == "[":
            i = _skip_delimited(text, i)
            continue
        m = re.match(_IDENT, text[i:])
        if m:
            ident = m.group(0)
            j = i + len(ident)
            # skip whitespace
            k = j
            while k < n and text[k] == " ":
                k += 1
            if k < n and text[k] == "(":
                depth = 0
                end = k
                while end < n:
                    c = text[end]
                    if c == "'":
                        end = _skip_string(text, end)
                        continue
                    if c == '"' or c == "[":
                        end = _skip_delimited(text, end)
                        continue
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    end += 1
                if depth == 0 and end < n:
                    yield i, end + 1, ident, text[k + 1:end]
                    i = end + 1
                    continue
            i = j
        else:
            i += 1


def _skip_delimited(text: str, i: int) -> int:
    """Return the index just past the delimited identifier starting at ``i``.

    ``[Order's Total]`` and ``"Order's Total"`` are one identifier each, so the
    apostrophe they contain must not be treated as a string delimiter.
    """
    closing = "]" if text[i] == "[" else '"'
    end = text.find(closing, i + 1)
    return len(text) if end == -1 else end + 1


def _skip_string(text: str, i: int) -> int:
    """Return the index just past the single-quoted literal starting at ``i``."""
    n = len(text)
    i += 1
    while i < n:
        if text[i] == "'":
            if i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _split_args(arg_string: str) -> list[str]:
    """Split a comma-separated argument list, respecting nesting and strings."""
    parts = []
    depth = 0
    cur = []
    in_str = False
    i = 0
    while i < len(arg_string):
        ch = arg_string[i]
        if in_str:
            cur.append(ch)
            if ch == "'":
                in_str = False
        elif ch == "'":
            in_str = True
            cur.append(ch)
        elif ch == '"' or ch == "[":
            # One delimited identifier: an apostrophe inside it is part of the
            # name and must not open a literal.
            end = _skip_delimited(arg_string, i)
            cur.append(arg_string[i:end])
            i = end
            continue
        elif ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        parts.append("".join(cur).strip())
    return parts


def _rewrite_calls(text: str, rules: dict) -> str:
    """Apply rules to every call, innermost-first. Recurses into the
    arguments of every call (even ones with no rule) so nested calls
    such as GETDATE() inside a VALUES(...) list still get translated."""
    calls = list(_find_calls(text))
    if not calls:
        return text
    result = text
    offset = 0
    for start, end, name, args in calls:
        translated_args = _rewrite_calls(args, rules)
        replacement = None
        if name.upper() in rules:
            replacement = rules[name.upper()](translated_args)
        if replacement is None and translated_args != args:
            # keep the call, but swap in translated arguments
            replacement = name + "(" + translated_args + ")"
        if replacement is None:
            continue
        result = result[: start + offset] + replacement + result[end + offset:]
        offset += len(replacement) - (end - start)
    return result


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

def _iif_to_case(args: str) -> str:
    parts = _split_args(args)
    if len(parts) != 3:
        return None
    cond, yes, no = parts
    return f"CASE WHEN {cond} THEN {yes} ELSE {no} END"


def _dateadd_to_interval(args: str) -> str | None:
    parts = _split_args(args)
    if len(parts) != 3:
        return None
    datepart, number, date = parts
    units = {
        "YEAR": "year", "YY": "year", "YYYY": "year",
        "QUARTER": "quarter", "QQ": "quarter", "Q": "quarter",
        "MONTH": "month", "MM": "month", "M": "month",
        "WEEK": "week", "WK": "week", "WW": "week",
        "DAY": "day", "DD": "day", "D": "day",
        "HOUR": "hour", "HH": "hour",
        "MINUTE": "minute", "MI": "minute", "N": "minute",
        "SECOND": "second", "SS": "second", "S": "second",
    }
    unit = units.get(parts[0].strip().strip("'").upper())
    if not unit:
        return None
    return f"({date} + ({number} * INTERVAL '1 {unit}'))"


def _datediff_to_expr(args: str) -> str | None:
    parts = _split_args(args)
    if len(parts) != 3:
        return None
    datepart, start, end = parts
    dp = datepart.strip().strip("'").upper()
    d = {
        "YEAR": "EXTRACT(YEAR FROM ({b})) - EXTRACT(YEAR FROM ({a}))",
        "MONTH": "(EXTRACT(YEAR FROM ({b})) - EXTRACT(YEAR FROM ({a}))) * 12 + (EXTRACT(MONTH FROM ({b})) - EXTRACT(MONTH FROM ({a})))",
        "DAY": "EXTRACT(EPOCH FROM (({b})::timestamp - ({a})::timestamp)) / 86400.0",
        "HOUR": "EXTRACT(EPOCH FROM (({b})::timestamp - ({a})::timestamp)) / 3600.0",
        "MINUTE": "EXTRACT(EPOCH FROM (({b})::timestamp - ({a})::timestamp)) / 60.0",
        "SECOND": "EXTRACT(EPOCH FROM (({b})::timestamp - ({a})::timestamp))",
    }
    template = d.get(dp)
    if not template:
        return None
    expr = template.replace("{a}", start).replace("{b}", end)
    # wrap in integer cast to mimic DATEPART semantics
    return f"CAST({expr} AS INTEGER)"


def _datepart_to_expr(args: str) -> str | None:
    parts = _split_args(args)
    if len(parts) != 2:
        return None
    datepart, date = parts
    dp = datepart.strip().strip("'").upper()
    d = {
        "YEAR": "EXTRACT(YEAR FROM {d})",
        "MONTH": "EXTRACT(MONTH FROM {d})",
        "DAY": "EXTRACT(DAY FROM {d})",
        "HOUR": "EXTRACT(HOUR FROM {d})",
        "MINUTE": "EXTRACT(MINUTE FROM {d})",
        "SECOND": "EXTRACT(SECOND FROM {d})",
        "DOW": "EXTRACT(DOW FROM {d}) + 1",
        "DW": "EXTRACT(DOW FROM {d}) + 1",
        "WEEKDAY": "EXTRACT(DOW FROM {d}) + 1",
        "QUARTER": "EXTRACT(QUARTER FROM {d})",
        "WEEK": "EXTRACT(WEEK FROM {d})",
        "YEARDAY": "EXTRACT(DOY FROM {d})",
    }
    template = d.get(dp)
    if not template:
        return None
    return template.replace("{d}", date)


def _patindex_to_position(args: str) -> str:
    parts = _split_args(args)
    if len(parts) != 2:
        return None
    pattern, text = parts
    # '%x%' -> 'x'
    pat = pattern.strip()
    if len(pat) >= 2 and pat.startswith("'%") and pat.endswith("%'"):
        pat = "'" + pat[2:-2] + "'"
    return f"{pat} IN {text}"


def _stuff_to_overlay(args: str) -> str | None:
    parts = _split_args(args)
    if len(parts) != 4:
        return None
    text, start, length, repl = parts
    # `STUFF((SELECT sep + col FROM t FOR XML PATH('')), 1, n, '')` is T-SQL's
    # string-aggregation idiom, not a real STUFF: FOR XML concatenates and the
    # STUFF trims the leading separator. PostgreSQL spells that string_agg.
    aggregated = _xml_path_to_string_agg(text, start, repl)
    if aggregated is not None:
        return aggregated
    if _FOR_XML_RE.search(text):
        # Some other FOR XML shape — leave it untranslated so the routine-level
        # guard reports it instead of emitting SQL that cannot compile.
        return None
    return f"OVERLAY({text} PLACING {repl} FROM {start} FOR {length})"


_FOR_XML_RE = re.compile(r"\bFOR\s+XML\b", re.IGNORECASE)
_XML_PATH_SELECT_RE = re.compile(
    r"^\(\s*SELECT\s+(?P<expr>.*?)\s+FROM\s+(?P<rest>.*?)"
    r"\s+FOR\s+XML\s+PATH\s*\(\s*(?:N?'')\s*\)\s*\)$",
    re.IGNORECASE | re.DOTALL,
)
_CONCAT_CALL_RE = re.compile(r"^\s*CONCAT\s*\((?P<args>.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_LEADING_LITERAL_RE = re.compile(
    r"^\s*(?P<sep>N?'(?:[^']|'')*'(?:\s*\+\s*[A-Za-z_]\w*\s*\([^()]*\))*)\s*\+\s*(?P<value>.+)$",
    re.DOTALL,
)


def _xml_path_to_string_agg(text: str, start: str, replacement: str) -> str | None:
    """Rewrite the FOR XML PATH('') concatenation idiom as string_agg()."""
    match = _XML_PATH_SELECT_RE.match(text.strip())
    if not match:
        return None
    # Only the "strip the leading separator" form maps cleanly.
    if start.strip() != "1" or replacement.strip().lstrip("Nn") != "''":
        return None
    separator, value = _split_aggregate_expression(match.group("expr"))
    if separator is None:
        return None
    return f"(SELECT string_agg({value}, {separator}) FROM {match.group('rest')})"


def _split_aggregate_expression(expr: str) -> tuple[str | None, str | None]:
    """Split `sep + col` / `CONCAT(sep, col)` into (separator, value)."""
    concat = _CONCAT_CALL_RE.match(expr)
    if concat:
        parts = _split_args(concat.group("args"))
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        if len(parts) > 2:
            # CONCAT(sep, a, b, ...) — the separator is still the first
            # argument; everything after it is the value being aggregated.
            # Requiring exactly two arguments rejected the common real-world
            # shape and left `FOR XML` untranslated.
            return parts[0].strip(), "concat(" + ", ".join(p.strip() for p in parts[1:]) + ")"
        return None, None
    literal = _LEADING_LITERAL_RE.match(expr)
    if literal:
        return literal.group("sep").strip(), literal.group("value").strip()
    return None, None


def _convert_to_cast(args: str) -> str | None:
    parts = _split_args(args)
    # CONVERT(type, expr [, style]) or CONVERT(expr, type [, style])
    if len(parts) < 2:
        return None

    def _type_token(tok: str) -> str | None:
        # Bracketed type tokens (CONVERT([nvarchar], ...)) are common in
        # OBJECT_DEFINITION output and must be matched like their bare form.
        tok = tok.strip()
        # OBJECT_DEFINITION commonly renders parameterized system types as
        # [nvarchar](10), not [nvarchar(10)].
        tok = re.sub(r"^\[([A-Za-z_][A-Za-z0-9_]*)\](\s*\([^)]*\))$", r"\1\2", tok)
        if tok.startswith("[") and tok.endswith("]"):
            tok = tok[1:-1].strip()
        if tok.upper() in _TYPE_KEYWORDS or re.match(r"^[A-Z]+(\s*\(\s*\d+(\s*,\s*\d+)?\s*\))?$", tok, re.I):
            return tok
        return None

    t0 = _type_token(parts[0])
    if t0 is not None:
        return f"CAST({parts[1]} AS {_pg_type(t0)})"
    t1 = _type_token(parts[1])
    if t1 is not None:
        return f"CAST({parts[0]} AS {_pg_type(t1)})"
    return None


_TYPE_KEYWORDS = {"INT", "BIGINT", "SMALLINT", "TINYINT", "BIT", "MONEY", "DECIMAL", "NUMERIC",
                  "FLOAT", "REAL", "DATE", "TIME", "DATETIME", "DATETIME2", "SMALLDATETIME",
                  "DATETIMEOFFSET", "CHAR", "NCHAR", "VARCHAR", "NVARCHAR", "TEXT", "NTEXT",
                  "BINARY", "VARBINARY", "IMAGE", "UNIQUEIDENTIFIER", "XML", "SYSNAME"}


def _pg_type(t: str) -> str:
    from engine.mappers.type_mappings import MSSQL_TO_POSTGRES_TYPE_OVERRIDES
    t = t.strip()
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1].strip()
    m = re.match(r"^([A-Z]+)(\(\d+(,\d+)?\))?$", t, re.I)
    base = m.group(1).upper() if m else t.upper()
    params = m.group(2) if m else ""
    mapped = MSSQL_TO_POSTGRES_TYPE_OVERRIDES.get(base)
    if mapped:
        return mapped
    simple = {
        "INT": "INTEGER", "TINYINT": "SMALLINT", "BIT": "BOOLEAN",
        "DATETIME": "TIMESTAMP", "DATETIME2": "TIMESTAMP", "SMALLDATETIME": "TIMESTAMP",
        "DATETIMEOFFSET": "TIMESTAMPTZ", "UNIQUEIDENTIFIER": "UUID",
        "BINARY": "BYTEA", "VARBINARY": "BYTEA", "IMAGE": "BYTEA",
        "NTEXT": "TEXT", "TEXT": "TEXT", "MONEY": "NUMERIC(19,4)",
        "DECIMAL": "NUMERIC", "NUMERIC": "NUMERIC",
    }
    if base in simple:
        return simple[base]
    if base in ("VARCHAR", "CHAR", "NVARCHAR", "NCHAR"):
        if params is None or params.strip("()").upper() == "MAX":
            return "TEXT"
        return f"{base[1:] if base.startswith('N') else base}{params}"
    return f"{base}{params}"


def _pg_date_part_reverse(args: str) -> str | None:
    parts = _split_args(args)
    if len(parts) != 2:
        return None
    unit, date = parts
    u = unit.strip().strip("'").upper()
    d = {
        "YEAR": "YEAR", "MONTH": "MONTH", "DAY": "DAY", "HOUR": "HOUR",
        "MINUTE": "MINUTE", "SECOND": "SECOND", "DOW": "WEEKDAY", "ISODOW": "WEEKDAY",
    }
    mapped = d.get(u)
    if not mapped:
        return None
    return f"{mapped}({date})"


def _overlay_to_stuff(args: str) -> str | None:
    # OVERLAY(text PLACING repl FROM start [FOR length])
    m = re.match(r"^(.*?)\s+PLACING\s+(.*?)\s+FROM\s+(.*?)\s+FOR\s+(.*)$", args, re.I | re.S)
    if not m:
        m = re.match(r"^(.*?)\s+PLACING\s+(.*?)\s+FROM\s+(.*)$", args, re.I | re.S)
        if not m:
            return None
        text, repl, start = m.group(1), m.group(2), m.group(3)
        length = "LEN(%s)" % text
    else:
        text, repl, start, length = m.group(1), m.group(2), m.group(3), m.group(4)
    return f"STUFF({text}, {start}, {length}, {repl})"


# T-SQL CHAR(n) returns the character with code n; PostgreSQL spells that
# chr(n). CHAR is also a *type* name, so the rewrite only fires where a value
# is expected — never after AS/:: (a cast) or after an identifier (a column
# declaration such as `b CHAR(1)`).
_CHAR_CALL_RE = re.compile(r"\bN?CHAR\s*\(\s*(\d+)\s*\)", re.IGNORECASE)
_VALUE_CONTEXT_WORDS = frozenset(
    "select then else return when and or not like set values by case "
    "concat coalesce isnull print raise exception notice union all".split()
)


def _translate_char_calls(text: str) -> str:
    def _replace(match):
        prefix = text[:match.start()].rstrip()
        if not prefix:
            return f"chr({match.group(1)})"
        if prefix.endswith("::"):
            return match.group(0)
        if prefix[-1] in "(,+|=<>*/%-":
            return f"chr({match.group(1)})"
        word = re.search(r"([A-Za-z_]\w*)$", prefix)
        if word and word.group(1).lower() in _VALUE_CONTEXT_WORDS:
            return f"chr({match.group(1)})"
        return match.group(0)

    return _CHAR_CALL_RE.sub(_replace, text)


def translate_functions(text: str, source: str, target: str) -> str:
    """Translate a small expression (no full statements) between dialects."""
    if source == "tsql" and target == "postgres":
        text = _rewrite_calls(text, {
            "GETDATE": lambda a: "CURRENT_TIMESTAMP",
            "SYSDATETIME": lambda a: "CURRENT_TIMESTAMP",
            "GETUTCDATE": lambda a: "CURRENT_TIMESTAMP AT TIME ZONE 'UTC'",
            "SYSUTCDATETIME": lambda a: "CURRENT_TIMESTAMP AT TIME ZONE 'UTC'",
            "CURRENT_TIMESTAMP": lambda a: "CURRENT_TIMESTAMP",
            "NEWID": lambda a: "gen_random_uuid()",
            "NEWSEQUENTIALID": lambda a: "gen_random_uuid()",
            "ISNULL": lambda a: f"COALESCE({a})",
            "IIF": _iif_to_case,
            "CEILING": lambda a: f"CEIL({a})",
            "RAND": lambda a: "RANDOM()",
            "SQUARE": lambda a: f"POWER({a}, 2)",
            "DATEADD": _dateadd_to_interval,
            "DATEDIFF": _datediff_to_expr,
            "DATEPART": _datepart_to_expr,
            "YEAR": lambda a: f"EXTRACT(YEAR FROM {a})",
            "MONTH": lambda a: f"EXTRACT(MONTH FROM {a})",
            "DAY": lambda a: f"EXTRACT(DAY FROM {a})",
            "LEN": lambda a: f"LENGTH({a})",
            "CHARINDEX": lambda a: f"POSITION({_patindex_to_position2(a)})",
            "STUFF": _stuff_to_overlay,
            "CONVERT": _convert_to_cast,
            "GETUTCDATE2": lambda a: "CURRENT_TIMESTAMP",
            "STR": _str_to_pg,
            "SPACE": lambda a: f"REPEAT(' ', {a})",
        })
        # operator-level fixes
        text = re.sub(r"\bLEN\s*\(", "LENGTH(", text, flags=re.I)
        text = re.sub(r"\bCURRENT_TIMESTAMP\b", "CURRENT_TIMESTAMP", text, flags=re.I)
        text = re.sub(r"\bGETUTCDATE\s*\(\s*\)", "CURRENT_TIMESTAMP AT TIME ZONE 'UTC'", text, flags=re.I)
        text = re.sub(r"\bSYSTEM_USER\b", "CURRENT_USER", text, flags=re.I)
        text = _translate_char_calls(text)
        return text
    elif source == "postgres" and target == "tsql":
        text = _rewrite_calls(text, {
            "NOW": lambda a: "GETDATE()",
            "CURRENT_TIMESTAMP": lambda a: "GETDATE()",
            "GEN_RANDOM_UUID": lambda a: "NEWID()",
            "CEIL": lambda a: f"CEILING({a})",
            "LENGTH": lambda a: f"LEN({a})",
            "RANDOM": lambda a: "RAND()",
            "DATE_PART": _pg_date_part_reverse,
            "EXTRACT": _pg_extract_reverse,
            "OVERLAY": _overlay_to_stuff,
        })
        text = re.sub(r"\bCURRENT_TIMESTAMP\b", "GETDATE()", text, flags=re.I)
        text = re.sub(r"\bPOSITION\((\S+)\s+IN\s+(\S+)\)", r"CHARINDEX(\1, \2)", text, flags=re.I)
        text = re.sub(r"\bCAST\s*\(([^)]+)\s+AS\s+timestamp without time zone\)", r"CAST(\1 AS DATETIME2)", text, flags=re.I)
        return text
    return text


def _str_to_pg(args: str) -> str | None:
    parts = _split_args(args)
    if not parts:
        return None
    expr = parts[0]
    if len(parts) == 1:
        return f"CAST({expr} AS TEXT)"
    length = parts[1].strip()
    if len(parts) == 2:
        return f"RPAD(CAST({expr} AS TEXT), {length}, ' ')"
    decimal = parts[2].strip()
    return f"RPAD(TO_CHAR({expr}, 'FM' || RPAD('', GREATEST({length} - {decimal} - 1, 1), '9') || '.' || RPAD('', {decimal}, '9')), {length}, ' ')"


def _patindex_to_position2(args: str) -> str:
    parts = _split_args(args)
    if len(parts) < 2:
        return None
    needle, hay = parts[0], parts[1]
    return f"{needle} IN {hay}"


def _pg_extract_reverse(args: str) -> str | None:
    # EXTRACT(unit FROM expr)
    m = re.match(r"^(.*?)\s+FROM\s+(.*)$", args, re.I | re.S)
    if not m:
        return None
    unit, expr = m.group(1).strip(), m.group(2).strip()
    u = unit.upper()
    d = {
        "YEAR": "YEAR", "MONTH": "MONTH", "DAY": "DAY", "HOUR": "HOUR",
        "MINUTE": "MINUTE", "SECOND": "SECOND", "DOW": "WEEKDAY", "ISODOW": "WEEKDAY",
        "QUARTER": "DATEPART(QUARTER", "WEEK": "DATEPART(WEEK", "DOY": "DATEPART(YEARDAY",
    }
    if u in d:
        return f"{d[u]}, {expr})" if u in ("QUARTER", "WEEK", "DOY") else f"{d[u]}({expr})"
    return None
