"""
SQL Server schema extraction into the normalized Database model.

Uses INFORMATION_SCHEMA for the portable bits (columns, PK/UK key columns)
and sys.* catalogs for things INFORMATION_SCHEMA doesn't expose well:
identity seed/increment, computed columns, full view/proc/function/trigger
definitions, FK actions, and index definitions.
"""
from __future__ import annotations

from engine.schema import (
    CheckConstraint, Column, Constraint, Database, ForeignKey, Index, Routine, Table, Trigger, View,
)

_BASE_TABLES_SQL = """
SELECT TABLE_SCHEMA, TABLE_NAME
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_SCHEMA, TABLE_NAME
"""

_COLUMNS_SQL = """
SELECT
    c.COLUMN_NAME,
    c.DATA_TYPE,
    c.CHARACTER_MAXIMUM_LENGTH,
    c.NUMERIC_PRECISION,
    c.NUMERIC_SCALE,
    c.DATETIME_PRECISION,
    c.IS_NULLABLE,
    c.COLUMN_DEFAULT,
    ic.is_identity,
    CONVERT(bigint, ic.seed_value),
    CONVERT(bigint, ic.increment_value),
    cc.definition AS computed_definition,
    c.COLLATION_NAME
FROM INFORMATION_SCHEMA.COLUMNS c
LEFT JOIN sys.identity_columns ic
    ON OBJECT_ID(c.TABLE_SCHEMA + '.' + c.TABLE_NAME) = ic.object_id
    AND c.COLUMN_NAME = ic.name
LEFT JOIN sys.computed_columns cc
    ON OBJECT_ID(c.TABLE_SCHEMA + '.' + c.TABLE_NAME) = cc.object_id
    AND c.COLUMN_NAME = cc.name
WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s
ORDER BY c.ORDINAL_POSITION
"""

_PK_SQL = """
SELECT kc.CONSTRAINT_NAME, kc.COLUMN_NAME
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kc
    ON tc.CONSTRAINT_NAME = kc.CONSTRAINT_NAME
    AND tc.TABLE_SCHEMA = kc.TABLE_SCHEMA
WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
    AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
ORDER BY kc.ORDINAL_POSITION
"""

_UNIQUE_SQL = """
SELECT kc.CONSTRAINT_NAME, kc.COLUMN_NAME
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kc
    ON tc.CONSTRAINT_NAME = kc.CONSTRAINT_NAME
    AND tc.TABLE_SCHEMA = kc.TABLE_SCHEMA
WHERE tc.CONSTRAINT_TYPE = 'UNIQUE'
    AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
ORDER BY kc.CONSTRAINT_NAME, kc.ORDINAL_POSITION
"""

_FK_SQL = """
SELECT
    fk.name AS fk_name,
    col.name AS col_name,
    OBJECT_SCHEMA_NAME(fk.referenced_object_id) + '.' + OBJECT_NAME(fk.referenced_object_id) AS ref_table,
    ref_col.name AS ref_col_name,
    fk.update_referential_action_desc,
    fk.delete_referential_action_desc
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc
    ON fkc.constraint_object_id = fk.object_id
JOIN sys.columns col
    ON fkc.parent_object_id = col.object_id AND fkc.parent_column_id = col.column_id
JOIN sys.columns ref_col
    ON fkc.referenced_object_id = ref_col.object_id AND fkc.referenced_column_id = ref_col.column_id
WHERE OBJECT_SCHEMA_NAME(fk.parent_object_id) = %s AND OBJECT_NAME(fk.parent_object_id) = %s
ORDER BY fk.name, fkc.constraint_column_id
"""

_INDEX_SQL = """
SELECT
    i.name AS index_name,
    col.name AS col_name,
    i.is_unique,
    i.filter_definition,
    ic.is_included_column
FROM sys.indexes i
JOIN sys.index_columns ic
    ON i.object_id = ic.object_id AND i.index_id = ic.index_id
JOIN sys.columns col
    ON ic.object_id = col.object_id AND ic.column_id = col.column_id
WHERE OBJECT_SCHEMA_NAME(i.object_id) = %s AND OBJECT_NAME(i.object_id) = %s
    AND i.is_primary_key = 0
    AND i.is_unique_constraint = 0
    AND i.type > 0
    AND i.is_hypothetical = 0
    AND ic.is_included_column = 0
    AND ic.key_ordinal > 0
ORDER BY i.name, ic.key_ordinal
"""

_CHECK_SQL = """
SELECT cc.name, cc.definition
FROM sys.check_constraints cc
WHERE OBJECT_SCHEMA_NAME(cc.parent_object_id) = %s AND OBJECT_NAME(cc.parent_object_id) = %s
ORDER BY cc.name
"""

_VIEWS_SQL = """
SELECT TABLE_SCHEMA + '.' + TABLE_NAME AS name, OBJECT_DEFINITION(OBJECT_ID(TABLE_SCHEMA + '.' + TABLE_NAME))
FROM INFORMATION_SCHEMA.VIEWS
ORDER BY TABLE_SCHEMA, TABLE_NAME
"""

_PROCEDURES_SQL = """
SELECT OBJECT_SCHEMA_NAME(object_id) + '.' + name, OBJECT_DEFINITION(object_id)
FROM sys.procedures
ORDER BY name
"""

_FUNCTIONS_SQL = """
SELECT
    OBJECT_SCHEMA_NAME(object_id) + '.' + name,
    OBJECT_DEFINITION(object_id),
    CASE type WHEN 'FN' THEN 'scalar' WHEN 'IF' THEN 'inline_table' WHEN 'TF' THEN 'table' END
FROM sys.objects
WHERE type IN ('FN', 'IF', 'TF')
ORDER BY name
"""

_TRIGGERS_SQL = """
SELECT
    tr.name,
    OBJECT_SCHEMA_NAME(tr.parent_id) + '.' + OBJECT_NAME(tr.parent_id) AS table_name,
    tr.is_instead_of_trigger,
    OBJECT_DEFINITION(tr.object_id)
FROM sys.triggers tr
WHERE tr.parent_class = 1
ORDER BY tr.name
"""


def extract_schema(conn) -> Database:
    database = Database(name=conn.database, dialect="tsql")

    for schema, name in conn.fetch(_BASE_TABLES_SQL):
        qualified = f"{schema}.{name}"
        table = _extract_table(conn, schema, name)
        database.tables.append(table)

    database.views = [
        View(name=row[0], definition=row[1] or "")
        for row in conn.fetch(_VIEWS_SQL)
    ]
    database.procedures = [
        Routine(name=row[0], kind="procedure", definition=row[1] or "")
        for row in conn.fetch(_PROCEDURES_SQL)
    ]
    database.functions = [
        Routine(name=row[0], kind=row[2], definition=row[1] or "")
        for row in conn.fetch(_FUNCTIONS_SQL)
    ]
    database.triggers = [
        Trigger(
            name=row[0],
            table=row[1],
            timing="INSTEAD OF" if row[2] else "AFTER",
            events=_events_from_definition(row[3] or ""),
            definition=row[3] or "",
        )
        for row in conn.fetch(_TRIGGERS_SQL)
    ]
    return database


def _extract_table(conn, schema: str, name: str) -> Table:
    qualified = f"{schema}.{name}"

    columns = []
    for row in conn.fetch(_COLUMNS_SQL, (schema, name)):
        data_type, length, precision, scale = row[1], row[2], row[3], row[4]
        columns.append(
            Column(
                name=row[0],
                data_type=_rebuild_type(data_type, length, precision, scale, row[5]),
                nullable=row[6] == "YES",
                default=_clean_default(row[7]),
                is_identity=bool(row[8]),
                identity_seed=int(row[9]) if row[9] is not None else None,
                identity_increment=int(row[10]) if row[10] is not None else None,
                is_computed=row[11] is not None,
                computed_definition=row[11],
                collation=row[12],
            )
        )

    pk = None
    pk_rows = conn.fetch(_PK_SQL, (schema, name))
    if pk_rows:
        pk = Constraint(name=pk_rows[0][0], columns=[r[1] for r in pk_rows])

    unique_constraints: list[Constraint] = []
    current = None
    for cname, ccol in conn.fetch(_UNIQUE_SQL, (schema, name)):
        if current is None or current.name != cname:
            current = Constraint(name=cname, columns=[])
            unique_constraints.append(current)
        current.columns.append(ccol)

    foreign_keys: list[ForeignKey] = []
    current_fk = None
    for fk_name, col_name, ref_table, ref_col, upd, dele in conn.fetch(_FK_SQL, (schema, name)):
        if current_fk is None or current_fk.name != fk_name:
            current_fk = ForeignKey(
                name=fk_name, columns=[], ref_table=ref_table, ref_columns=[],
                on_update=_fk_action(upd), on_delete=_fk_action(dele),
            )
            foreign_keys.append(current_fk)
        current_fk.columns.append(col_name)
        current_fk.ref_columns.append(ref_col)

    indexes = []
    current_idx = None
    for idx_name, col_name, is_unique, filter_def, included in conn.fetch(_INDEX_SQL, (schema, name)):
        if current_idx is None or current_idx.name != idx_name:
            current_idx = Index(name=idx_name, columns=[], unique=bool(is_unique), where=filter_def)
            indexes.append(current_idx)
        current_idx.columns.append(col_name)

    checks = [
        CheckConstraint(name=row[0], definition=row[1])
        for row in conn.fetch(_CHECK_SQL, (schema, name))
    ]

    return Table(
        name=qualified,
        columns=columns,
        primary_key=pk,
        foreign_keys=foreign_keys,
        unique_constraints=unique_constraints,
        indexes=indexes,
        check_constraints=checks,
    )


def _rebuild_type(data_type: str, length, precision, scale, datetime_precision) -> str:
    data_type = data_type.upper()
    if data_type in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "BINARY", "VARBINARY"):
        if length is None:
            return data_type
        if length == -1:
            return f"{data_type}(MAX)"
        return f"{data_type}({length})"
    if data_type in ("DECIMAL", "NUMERIC"):
        if precision is not None:
            return f"{data_type}({precision},{scale if scale is not None else 0})"
        return data_type
    if data_type in ("DATETIME2", "DATETIMEOFFSET", "TIME"):
        if datetime_precision is not None and datetime_precision != 7:
            return f"{data_type}({datetime_precision})"
        return data_type
    return data_type


def _clean_default(default) -> str | None:
    if not default:
        return None
    default = default.strip()
    # INFORMATION_SCHEMA wraps literals in parentheses: (0) -> 0, ((newid())) -> NEWID()
    while default.startswith("(") and default.endswith(")"):
        default = default[1:-1].strip()
    return default


def _fk_action(action: str) -> str:
    mapping = {
        "NO_ACTION": "NO ACTION",
        "CASCADE": "CASCADE",
        "SET_NULL": "SET NULL",
        "SET_DEFAULT": "SET DEFAULT",
    }
    return mapping.get(action, "NO ACTION")


def _events_from_definition(definition: str) -> list[str]:
    upper = definition.upper()
    events = []
    for event in ("INSERT", "UPDATE", "DELETE"):
        # match the event list right after FOR/AFTER/INSTEAD OF
        if _event_in_header(upper, event):
            events.append(event)
    return events


def _event_in_header(upper: str, event: str) -> bool:
    import re
    # e.g. AFTER INSERT, UPDATE   |  FOR DELETE, INSERT   |  INSTEAD OF UPDATE
    match = re.search(
        r"\b(?:FOR|AFTER|INSTEAD\s+OF)\b\s*([A-Z,\s]+)", upper, flags=re.IGNORECASE
    )
    return bool(match) and event in match.group(1).split(",")
