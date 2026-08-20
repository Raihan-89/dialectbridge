"""
DDL trigger translator: T-SQL database DDL triggers <-> PostgreSQL event triggers.

SQL Server database-scoped DDL triggers fire on CREATE/ALTER/DROP events and
abort the offending DDL with ROLLBACK + RAISERROR. PostgreSQL event triggers
fire for a configured tag list and abort by raising an exception from the
PL/pgSQL event-trigger function.

Mapping choices:
  - trigger.events (MSSQL event names, e.g. CREATE_TABLE) -> WHEN TAG IN (...)
    via the DDL event maps in engine.mappers.type_mappings.
  - EVENTDATA() -> TG_TAG (the running event trigger only exposes the tag; the
    rich EVENTDATA() XML is not portable).
  - ROLLBACK/RAISERROR abort patterns -> RAISE EXCEPTION (the PostgreSQL idiom).

The reverse direction is best-effort: the extractor only captures the event
trigger's firing point (ddl_command_start / ddl_command_end / ...), not its
WHEN TAG list, so the tags are reconstructed as a conservative set and the
user is warned to review them.
"""
from __future__ import annotations

import re

from engine.mappers.type_mappings import (
    MSSQL_DDL_EVENT_TO_PG_TAG,
    PG_TAG_TO_MSSQL_DDL_EVENT,
)
from engine.schema import Trigger
from engine.translators.procedure_translator import (
    _expr,
    _expr_rev,
    _extract_plpgsql_body,
    _transform_plpgsql_body,
    _transform_tsql_body,
)
from engine.translators.sql_builder import convert_type
from engine.translators.trigger_translator import _translate_raiseerror

# PG event triggers whose WHEN TAG list is NOT captured by the extractor: map
# the firing point to a conservative MSSQL event set for recreation.
_FIRE_TO_MSSQL_EVENTS = {
    "ddl_command_start": (
        "CREATE_TABLE", "ALTER_TABLE", "DROP_TABLE", "CREATE_VIEW", "ALTER_VIEW",
        "DROP_VIEW", "CREATE_FUNCTION", "ALTER_FUNCTION", "DROP_FUNCTION",
        "CREATE_PROCEDURE", "ALTER_PROCEDURE", "DROP_PROCEDURE", "CREATE_TYPE",
        "CREATE_SCHEMA", "DROP_SCHEMA", "CREATE_INDEX", "DROP_INDEX",
    ),
    "ddl_command_end": (
        "CREATE_TABLE", "ALTER_TABLE", "DROP_TABLE", "CREATE_VIEW", "ALTER_VIEW",
        "DROP_VIEW", "CREATE_FUNCTION", "ALTER_FUNCTION", "DROP_FUNCTION",
        "CREATE_PROCEDURE", "ALTER_PROCEDURE", "DROP_PROCEDURE", "CREATE_TYPE",
        "CREATE_SCHEMA", "DROP_SCHEMA", "CREATE_INDEX", "DROP_INDEX",
    ),
}

_FIRE_NAMES = {
    "ddl_command_start": "ddl_command_start",
    "ddl_command_end": "ddl_command_end",
}


def translate_ddl_trigger(trigger: Trigger, target: str) -> tuple[str | None, list[str]]:
    if target == "postgres":
        return _tsql_ddl_trigger_to_pg(trigger)
    return _pg_ddl_trigger_to_tsql(trigger)


# ---------------------------------------------------------------------------
# T-SQL database DDL trigger -> PostgreSQL event trigger
# ---------------------------------------------------------------------------

def _tsql_ddl_trigger_to_pg(trigger: Trigger) -> tuple[str | None, list[str]]:
    warnings: list[str] = []
    tags: list[str] = []
    for event in trigger.events:
        tag = MSSQL_DDL_EVENT_TO_PG_TAG.get(event.upper())
        if tag:
            if tag not in tags:
                tags.append(tag)
        else:
            warnings.append(
                f"DDL event '{event}' has no PostgreSQL event-trigger equivalent and was dropped"
            )
    if not tags:
        return None, ["No convertible DDL events — trigger skipped"]

    body = _extract_tsql_ddl_body(trigger.definition)
    if body is None:
        from engine.translators.procedure_translator import _routine_error_context
        ctx = _routine_error_context(trigger.definition or "")
        return None, [f"Could not extract DDL trigger body — starts with: {ctx}"]

    def statement_fn(line, declared, warns, returns_set):
        return _ddl_statement(line, declared, warns, returns_set, warnings)

    lines, body_warnings, declared = _transform_tsql_body(
        body, "trigger", False, statement_fn=statement_fn,
    )
    warnings.extend(body_warnings)

    fn_name = f"{trigger.name}_ddl_fn"
    declaration = ""
    if declared:
        declaration = "DECLARE\n" + "\n".join(
            f"    {name} {data_type};" for name, data_type in declared.items()
        ) + "\n"
    function = (
        f"CREATE OR REPLACE FUNCTION {fn_name}() RETURNS event_trigger AS $$\n"
        f"{declaration}BEGIN\n" + "\n".join("    " + ln for ln in lines) + "\n"
        f"END;\n"
        f"$$ LANGUAGE plpgsql;"
    )
    tags_list = ", ".join(f"'{t}'" for t in tags)
    create = (
        f"CREATE EVENT TRIGGER {trigger.name} ON ddl_command_end "
        f"WHEN TAG IN ({tags_list}) EXECUTE FUNCTION {fn_name}();"
    )
    return function + "\n\n" + create, warnings


def _extract_tsql_ddl_body(definition: str) -> str | None:
    from engine.translators.procedure_translator import _strip_sql_comments
    definition = _strip_sql_comments(definition or "")
    if not definition.strip():
        return None
    m = re.search(r"\bAS\s+BEGIN\b", definition, re.IGNORECASE)
    if m:
        tail = definition[m.end():]
        ends = list(re.finditer(r"\bEND\b", tail, re.IGNORECASE))
        if not ends:
            return None
        return tail[: ends[-1].start()]
    # Fallback: body without BEGIN/END (single-statement body)
    m2 = re.search(r"\bAS\b\s+", definition, re.IGNORECASE)
    if m2:
        tail = definition[m2.end():].strip()
        if tail:
            return tail.rstrip(";")
    return None


def _ddl_statement(line: str, declared: dict[str, str], warns: list[str],
                   returns_set: bool, ddl_warnings: list[str]) -> str | None:
    stripped = line.strip()
    upper = stripped.upper()
    # SQL Server metadata often stores the declaration and first SET on one
    # physical line. Pull declarations into the function's DECLARE section
    # before translating the remaining statement.
    while True:
        declaration = re.match(
            r"^DECLARE\s+@?([A-Za-z_]\w*)\s+(?:AS\s+)?([\w.]+(?:\s*\([^;]+?\))?)\s*"
            r"(?:;\s*(.*))?$",
            stripped,
            re.IGNORECASE | re.DOTALL,
        )
        if not declaration:
            break
        data_type, warning = convert_type(declaration.group(2), "tsql", "postgres")
        declared[declaration.group(1)] = data_type or "TEXT"
        if warning:
            warns.append(warning)
        stripped = (declaration.group(3) or "").strip()
        if not stripped:
            return None
    upper = stripped.upper()
    # OBJECT_DEFINITION from replication/system DDL triggers can include
    # several SQL Server session options on the same physical line before the
    # real body statement. They are creation metadata, not executable logic.
    stripped = re.sub(
        r"^(?:SET\s+\w+\s+(?:ON|OFF)\s*;?\s*)+",
        "",
        stripped,
        flags=re.IGNORECASE,
    ).strip()
    if not stripped:
        return None
    upper = stripped.upper()
    if upper in ("SET NOCOUNT ON", "SET NOCOUNT OFF") or re.match(
        r"^SET\s+\w+\s+(?:ON|OFF)$", upper
    ):
        return None
    if re.match(r"^BEGIN\s+(TRANSACTION|TRAN|WORK)\b", stripped, re.IGNORECASE):
        warns.append("transaction control inside a DDL trigger has no PostgreSQL equivalent and was removed")
        return None
    if re.match(r"^(COMMIT|ROLLBACK)\s+(TRANSACTION|TRAN|WORK)\b", stripped, re.IGNORECASE):
        warns.append(
            f"'{upper.split()[0]} TRANSACTION' inside a DDL trigger has no PostgreSQL equivalent "
            f"— PostgreSQL aborts DDL triggers by raising an exception; the statement was removed"
        )
        return None
    stmt = stripped
    # EVENTDATA().value('...','nvarchar(max)') -> TG_TAG (best effort: the
    # event-trigger function only knows the running tag).
    if re.search(r"EVENTDATA\s*\(\s*\)", stmt, re.IGNORECASE):
        ddl_warnings.append(
            "EVENTDATA() has no PostgreSQL equivalent — the trigger body uses TG_TAG "
            "(the running event name) instead; review the converted logic"
        )
    stmt = re.sub(r"EVENTDATA\s*\(\s*\)\.value\s*\([^)]*\)", "TG_TAG", stmt, flags=re.IGNORECASE)
    stmt = re.sub(r"EVENTDATA\s*\(\s*\)", "TG_TAG", stmt, flags=re.IGNORECASE)
    # T-SQL SET assignments are PL/pgSQL assignments.  Leaving SET here makes
    # PostgreSQL interpret the variable name as a configuration parameter.
    stmt = re.sub(
        r"^SET\s+@?([A-Za-z_]\w*)\s*=\s*(.+)$",
        lambda match: f"{match.group(1)} := {match.group(2)}",
        stmt,
        flags=re.IGNORECASE | re.DOTALL,
    )
    stmt = re.sub(r"^PRINT\s+", "RAISE NOTICE ", stmt, flags=re.IGNORECASE)
    stmt = _translate_raiseerror(stmt)
    stmt = _expr(stmt)
    return stmt.rstrip(";") + ";"


# ---------------------------------------------------------------------------
# PostgreSQL event trigger -> T-SQL database DDL trigger
# ---------------------------------------------------------------------------

def _pg_ddl_trigger_to_tsql(trigger: Trigger) -> tuple[str | None, list[str]]:
    warnings: list[str] = []
    fire_point = (trigger.events or ["ddl_command_end"])[0].lower()
    events = list(_FIRE_TO_MSSQL_EVENTS.get(fire_point, ()))
    if not events:
        return None, [f"Event trigger firing point '{fire_point}' cannot be recreated as a SQL Server DDL trigger"]
    warnings.append(
        f"PostgreSQL event trigger '{trigger.name}' fires on '{fire_point}' but its exact tag "
        f"list was not captured — recreated as a database DDL trigger with a conservative "
        f"event set; review and adjust the FOR clause"
    )

    body = _extract_plpgsql_body(trigger.definition)
    if body is None:
        from engine.translators.procedure_translator import _routine_error_context
        ctx = _routine_error_context(trigger.definition or "")
        return None, [f"Could not extract event trigger function body — starts with: {ctx}"]
    parts, body_warnings, declared = _transform_plpgsql_body(body)
    warnings.extend(body_warnings)
    lines: list[str] = []
    for part in parts:
        stmt = _expr_rev(part.rstrip(";"))
        stmt = re.sub(r"\bTG_TAG\b", "EVENTDATA()", stmt, flags=re.IGNORECASE)
        lines.append("    " + stmt + ";")
    events_tsql = ", ".join(events)
    return (
        f"CREATE TRIGGER {trigger.name}\n"
        f"ON DATABASE\n"
        f"FOR {events_tsql}\n"
        f"AS\nBEGIN\n" + "\n".join(lines) + "\nEND"
    ), warnings
