"""
SQL Server schema extraction into the normalized Database model.

Uses INFORMATION_SCHEMA for the portable bits (columns, PK/UK key columns)
and sys.* catalogs for things INFORMATION_SCHEMA doesn't expose well:
identity seed/increment, computed columns, full view/proc/function/trigger
definitions, FK actions, index definitions, sequences, user-defined types,
synonyms, DDL triggers, partitioning, temporal/graph flags and database
settings.

Everything that has no clean PostgreSQL equivalent (columnstore / spatial /
XML indexes, full-text catalogs, CDC, Service Broker, Query Store, TDE,
certificates/keys, DENY permissions) is surfaced as a warning instead of
being silently dropped.
"""
from __future__ import annotations

import re

from engine.connectors.base import to_int
from engine.schema import (
    CheckConstraint, Column, Constraint, Database, ForeignKey, Index, Permission,
    PartitionChild, Principal, Routine, Sequence, Synonym, Table, Trigger, UserType, View,
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

# Only the relational btree index types are convertible. Columnstore (5,6),
# XML (3) and spatial (4) indexes are flagged separately for manual review.
_INDEX_SQL = """
SELECT
    i.name AS index_name,
    col.name AS col_name,
    i.is_unique,
    i.filter_definition,
    ic.is_included_column,
    CASE WHEN i.type = 1 THEN 1 ELSE 0 END AS is_clustered,
    ic.key_ordinal
FROM sys.indexes i
JOIN sys.index_columns ic
    ON i.object_id = ic.object_id AND i.index_id = ic.index_id
JOIN sys.columns col
    ON ic.object_id = col.object_id AND ic.column_id = col.column_id
WHERE OBJECT_SCHEMA_NAME(i.object_id) = %s AND OBJECT_NAME(i.object_id) = %s
    AND i.is_primary_key = 0
    AND i.is_unique_constraint = 0
    AND i.type IN (1, 2)
    AND i.is_hypothetical = 0
    AND ((ic.key_ordinal > 0 AND ic.is_included_column = 0) OR ic.is_included_column = 1)
ORDER BY i.name, ic.key_ordinal
"""

_NON_RELATIONAL_INDEX_SQL = """
SELECT i.name, i.type
FROM sys.indexes i
WHERE OBJECT_SCHEMA_NAME(i.object_id) = %s AND OBJECT_NAME(i.object_id) = %s
    AND i.is_primary_key = 0 AND i.is_unique_constraint = 0
    AND i.is_hypothetical = 0
    AND i.type IN (3, 4, 5, 6)
ORDER BY i.name
"""

_INDEX_TYPE_NAMES = {3: "XML", 4: "spatial", 5: "columnstore", 6: "columnstore"}

_CHECK_SQL = """
SELECT cc.name, cc.definition
FROM sys.check_constraints cc
WHERE OBJECT_SCHEMA_NAME(cc.parent_object_id) = %s AND OBJECT_NAME(cc.parent_object_id) = %s
ORDER BY cc.name
"""

# is_ms_shipped objects are SQL Server's own (replication catalog views such as
# sysextendedarticlesview, CDC helpers). They reference engine-internal tables
# that do not exist in PostgreSQL and must never be migrated.
_VIEWS_SQL = """
SELECT OBJECT_SCHEMA_NAME(v.object_id) + '.' + v.name AS name, OBJECT_DEFINITION(v.object_id)
FROM sys.views v
WHERE v.is_ms_shipped = 0
ORDER BY OBJECT_SCHEMA_NAME(v.object_id), v.name
"""

_INDEXED_VIEWS_SQL = """
SELECT OBJECT_SCHEMA_NAME(v.object_id) + '.' + v.name
FROM sys.views v
WHERE EXISTS (SELECT 1 FROM sys.indexes i WHERE i.object_id = v.object_id AND i.type IN (1, 2))
ORDER BY v.name
"""

_VIEW_INDEX_SQL = """
SELECT i.name, col.name, i.is_unique,
       CASE WHEN i.type = 1 THEN 1 ELSE 0 END AS is_clustered,
       ic.is_included_column
FROM sys.indexes i
JOIN sys.index_columns ic
    ON i.object_id = ic.object_id AND i.index_id = ic.index_id
JOIN sys.columns col
    ON ic.object_id = col.object_id AND ic.column_id = col.column_id
WHERE i.object_id = OBJECT_ID(%s)
    AND i.type IN (1, 2)
    AND i.is_hypothetical = 0
ORDER BY i.name, ic.key_ordinal
"""

_PROCEDURES_SQL = """
SELECT OBJECT_SCHEMA_NAME(object_id) + '.' + name, OBJECT_DEFINITION(object_id)
FROM sys.procedures
WHERE is_ms_shipped = 0
ORDER BY name
"""

_FUNCTIONS_SQL = """
SELECT
    OBJECT_SCHEMA_NAME(object_id) + '.' + name,
    OBJECT_DEFINITION(object_id),
    CASE type WHEN 'FN' THEN 'scalar' WHEN 'IF' THEN 'inline_table' WHEN 'TF' THEN 'table' END
FROM sys.objects
WHERE type IN ('FN', 'IF', 'TF')
    AND is_ms_shipped = 0
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
    AND tr.is_ms_shipped = 0
ORDER BY tr.name
"""

# Database-scoped DDL triggers (parent_class = 0). SQL Server's own replication
# triggers (tr_MStran_*) call sys.sp_MStran_ddlrepl and are excluded.
_DDL_TRIGGERS_SQL = """
SELECT
    tr.name,
    te.type_desc,
    OBJECT_DEFINITION(tr.object_id)
FROM sys.triggers tr
JOIN sys.trigger_events te ON te.object_id = tr.object_id
WHERE tr.parent_class = 0
    AND tr.is_ms_shipped = 0
ORDER BY tr.name, te.type_desc
"""

_SEQUENCES_SQL = """
SELECT
    OBJECT_SCHEMA_NAME(s.object_id) + '.' + s.name,
    s.start_value,
    s.increment,
    s.is_cycling,
    s.current_value,
    TYPE_NAME(s.user_type_id),
    s.precision,
    s.scale
FROM sys.sequences s
ORDER BY s.object_id
"""

_TYPES_SQL = """
SELECT
    SCHEMA_NAME(t.schema_id) + '.' + t.name,
    t.is_table_type,
    t.is_assembly_type,
    TYPE_NAME(t.system_type_id),
    t.max_length,
    t.precision,
    t.scale,
    t.is_nullable,
    OBJECT_DEFINITION(t.default_object_id)
FROM sys.types t
WHERE t.is_user_defined = 1
ORDER BY t.name
"""

_TABLE_TYPE_COLUMNS_SQL = """
SELECT
    c.name,
    TYPE_NAME(c.user_type_id),
    c.max_length,
    c.precision,
    c.scale,
    c.is_nullable,
    OBJECT_DEFINITION(c.default_object_id)
FROM sys.table_types tt
JOIN sys.columns c ON c.object_id = tt.type_table_object_id
WHERE SCHEMA_NAME(tt.schema_id) + '.' + tt.name = %s
ORDER BY c.column_id
"""

_SYNONYMS_SQL = """
SELECT SCHEMA_NAME(schema_id) + '.' + name, base_object_name
FROM sys.synonyms
ORDER BY name
"""

_SYNONYM_KIND_SQL = """
SELECT o.type
FROM sys.objects o
WHERE o.object_id = OBJECT_ID(%s)
"""

_PARTITIONED_TABLES_SQL = """
SELECT
    OBJECT_SCHEMA_NAME(t.object_id) + '.' + t.name,
    pf.name,
    pf.type_desc,
    col.name AS partition_col
FROM sys.tables t
JOIN sys.indexes i ON i.object_id = t.object_id AND i.index_id IN (0, 1)
JOIN sys.partition_schemes ps ON i.data_space_id = ps.data_space_id
JOIN sys.partition_functions pf ON pf.function_id = ps.function_id
JOIN sys.index_columns ic
    ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.partition_ordinal > 0
JOIN sys.columns col ON col.object_id = ic.object_id AND col.column_id = ic.column_id
ORDER BY t.name
"""

_PARTITION_BOUNDARIES_SQL = """
SELECT pf.name, pv.value
FROM sys.partition_functions pf
JOIN sys.partition_range_values pv ON pv.function_id = pf.function_id
ORDER BY pf.name, pv.boundary_id
"""

_TEMPORAL_TABLES_SQL = """
SELECT
    OBJECT_SCHEMA_NAME(t.object_id) + '.' + t.name,
    OBJECT_SCHEMA_NAME(t.history_table_id) + '.' + OBJECT_NAME(t.history_table_id)
FROM sys.tables t
WHERE t.temporal_type = 2
ORDER BY t.name
"""

_GRAPH_TABLES_SQL = """
SELECT OBJECT_SCHEMA_NAME(object_id) + '.' + name, is_node, is_edge
FROM sys.tables
WHERE is_node = 1 OR is_edge = 1
ORDER BY name
"""

_SECURITY_PRINCIPALS_SQL = """
SELECT name, type, is_fixed_role
FROM sys.database_principals
WHERE type IN ('S', 'U', 'G', 'R')
    AND name NOT IN ('dbo', 'guest', 'sys', 'INFORMATION_SCHEMA')
    AND name NOT LIKE '##%'
    AND name NOT LIKE 'NT AUTHORITY%'
    AND name NOT LIKE 'NT SERVICE%'
    AND name <> 'public'
ORDER BY name
"""

_ROLE_MEMBERSHIP_SQL = """
SELECT member.name, role.name
FROM sys.database_role_members rm
JOIN sys.database_principals member ON member.principal_id = rm.member_principal_id
JOIN sys.database_principals role ON role.principal_id = rm.role_principal_id
WHERE role.is_fixed_role = 0
    AND member.name <> 'public'
    AND role.name <> 'public'
ORDER BY member.name, role.name
"""

_PERMISSIONS_SQL = """
SELECT
    pr.name,
    perm.permission_name,
    perm.state_desc,
    CASE perm.class
        WHEN 1 THEN OBJECT_SCHEMA_NAME(perm.major_id) + '.' + OBJECT_NAME(perm.major_id)
        WHEN 3 THEN SCHEMA_NAME(perm.major_id)
        ELSE CAST(perm.major_id AS NVARCHAR(200))
    END AS object_name,
    perm.class_desc,
    obj.type
FROM sys.database_permissions perm
JOIN sys.database_principals pr ON pr.principal_id = perm.grantee_principal_id
LEFT JOIN sys.objects obj ON obj.object_id = perm.major_id
WHERE perm.class IN (1, 3)
    AND pr.name NOT IN ('dbo', 'guest', 'sys', 'INFORMATION_SCHEMA')
    AND pr.type IN ('S', 'U', 'G', 'R')
    AND perm.major_id > 0
ORDER BY pr.name, perm.permission_name
"""


def extract_schema(conn) -> Database:
    database = Database(
        name=conn.database,
        dialect="tsql",
        collation=_database_collation(conn),
    )

    partitions = _partition_map(conn, database)
    boundaries = _partition_boundaries(conn, database)
    temporal = _temporal_map(conn, database)
    graph = _graph_map(conn, database)

    for schema, name in conn.fetch(_BASE_TABLES_SQL):
        qualified = f"{schema}.{name}"
        table, warnings = _extract_table(conn, schema, name)
        _apply_special_flags(table, qualified, partitions, boundaries, temporal, graph, warnings)
        database.warnings.extend(warnings)
        database.tables.append(table)

    database.views = _extract_views(conn, database)
    database.procedures = [
        Routine(name=row[0], kind="procedure", definition=row[1] or "")
        for row in conn.fetch(_PROCEDURES_SQL)
    ]
    database.functions = [
        Routine(name=row[0], kind=row[2], definition=row[1] or "")
        for row in conn.fetch(_FUNCTIONS_SQL)
    ]
    database.triggers = _extract_triggers(conn)
    database.sequences = [
        Sequence(
            name=row[0],
            start_value=to_int(row[1]) or 0,
            increment=to_int(row[2]) or 1,
            is_cycling=bool(row[3]),
            current_value=to_int(row[4]),
            data_type=_rebuild_sequence_type(row[5], row[6], row[7]),
        )
        for row in _fetch_or_warn(conn, _SEQUENCES_SQL, database,
                                  "Standalone sequences (sys.sequences)")
    ]
    database.types = _extract_types(conn, database)
    database.synonyms = _extract_synonyms(conn, database)
    _extract_security(conn, database)
    _add_settings_warnings(conn, database)
    return database


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------

def _extract_table(conn, schema: str, name: str) -> tuple[Table, list[str]]:
    qualified = f"{schema}.{name}"
    warnings: list[str] = []

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
                identity_seed=to_int(row[9]),
                identity_increment=to_int(row[10]),
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

    indexes: list[Index] = []
    current_idx = None
    try:
        index_rows = conn.fetch(_INDEX_SQL, (schema, name))
    except Exception as exc:
        warnings.append(
            f"Indexes on {qualified} could not be inspected (query failed: {exc}) — "
            f"indexes will not be migrated"
        )
        index_rows = []
    for idx_name, col_name, is_unique, filter_def, included, clustered, key_ord in index_rows:
        if current_idx is None or current_idx.name != idx_name:
            current_idx = Index(
                name=idx_name, columns=[], unique=bool(is_unique), where=filter_def,
                is_clustered=bool(clustered),
            )
            indexes.append(current_idx)
        if included:
            current_idx.included_columns.append(col_name)
        else:
            current_idx.columns.append(col_name)

    try:
        non_relational_rows = conn.fetch(_NON_RELATIONAL_INDEX_SQL, (schema, name))
    except Exception as exc:
        warnings.append(
            f"Specialized index detection on {qualified} could not be inspected "
            f"(query failed: {exc}) — specialized indexes will not be migrated"
        )
        non_relational_rows = []
    for idx_name, idx_type in non_relational_rows:
        warnings.append(
            f"Index '{idx_name}' on {qualified} is a {_INDEX_TYPE_NAMES.get(idx_type, 'specialized')} "
            f"index with no PostgreSQL equivalent and was not migrated"
        )

    checks = [
        CheckConstraint(name=row[0], definition=row[1])
        for row in conn.fetch(_CHECK_SQL, (schema, name))
    ]

    table = Table(
        name=qualified,
        columns=columns,
        primary_key=pk,
        foreign_keys=foreign_keys,
        unique_constraints=unique_constraints,
        indexes=indexes,
        check_constraints=checks,
    )
    return table, warnings


def _apply_special_flags(table: Table, qualified: str, partitions: dict, boundaries: dict,
                         temporal: dict, graph: dict, warnings: list[str]) -> None:
    if qualified in partitions:
        func_name, range_type, key = partitions[qualified]
        if range_type != "RANGE RIGHT" or func_name not in boundaries:
            warnings.append(
                f"Table '{qualified}' uses partition function '{func_name}' with {range_type} "
                f"semantics (boundary values belong to the lower partition) — PostgreSQL RANGE "
                f"partitions are [lower, upper); the table was migrated as a non-partitioned table"
            )
        else:
            table.is_partitioned = True
            table.partition_type = "range"
            table.partition_key = key
            table.partition_children = _range_children(table.name, boundaries[func_name])

    if qualified in temporal:
        table.is_temporal = True
        table.temporal_history_table = temporal[qualified]
        warnings.append(
            f"Table '{qualified}' is a system-versioned temporal table — PostgreSQL has no native "
            f"temporal support; it was migrated as a regular table (period columns kept, history "
            f"mechanism lost)"
        )

    if qualified in graph:
        table.is_graph = True
        table.graph_kind = "node" if graph[qualified] == "node" else "edge"
        warnings.append(
            f"Table '{qualified}' is a SQL Server graph {'node' if graph[qualified] == 'node' else 'edge'} "
            f"table — PostgreSQL has no native graph support; it was migrated as a regular table"
        )


def _range_children(parent: str, bounds: list[str]) -> list[PartitionChild]:
    """Build PG-style child partitions for RANGE RIGHT boundaries b1..bn.

    MSSQL RANGE RIGHT gives: p1 < b1, p2 in [b1,b2), ..., p_{n+1} >= bn.
    PostgreSQL needs MINVALUE/MAXVALUE sentinels to represent the unbounded ends.
    """
    schema = parent.rsplit(".", 1)[0]
    base = parent.rsplit(".", 1)[1]
    children: list[PartitionChild] = []
    lower = "MINVALUE"
    for i, b in enumerate(bounds):
        name = f"{schema}.{base}_p{i + 1}"
        children.append(PartitionChild(name=name, bounds=f"FROM ({lower}) TO ({b})"))
        lower = b
    name = f"{schema}.{base}_p{len(bounds) + 1}"
    children.append(PartitionChild(name=name, bounds=f"FROM ({lower}) TO (MAXVALUE)"))
    return children


# ---------------------------------------------------------------------------
# views / triggers / sequences / types / synonyms
# ---------------------------------------------------------------------------

def _extract_views(conn, database: Database) -> list[View]:
    try:
        indexed_names = {row[0].lower() for row in conn.fetch(_INDEXED_VIEWS_SQL)}
    except Exception as exc:
        database.warnings.append(
            f"Indexed views could not be detected on this SQL Server (query failed: {exc}) — "
            f"views will be migrated without their indexes"
        )
        indexed_names = set()
    views: list[View] = []
    for name, definition in conn.fetch(_VIEWS_SQL):
        view = View(name=name, definition=definition or "")
        if name.lower() in indexed_names:
            try:
                index_rows = conn.fetch(_VIEW_INDEX_SQL, (name,))
            except Exception as exc:
                database.warnings.append(
                    f"Indexes on view '{name}' could not be inspected (query failed: {exc}) — "
                    f"the view was migrated without its indexes"
                )
                index_rows = []
            by_name: dict[str, Index] = {}
            for idx_name, col_name, is_unique, clustered, included in index_rows:
                idx = by_name.setdefault(
                    idx_name,
                    Index(name=idx_name, columns=[], unique=bool(is_unique),
                          is_clustered=bool(clustered)),
                )
                if included:
                    idx.included_columns.append(col_name)
                else:
                    idx.columns.append(col_name)
            view.indexes = list(by_name.values())
        views.append(view)
    return views


def _extract_triggers(conn) -> list[Trigger]:
    triggers: list[Trigger] = []
    for row in conn.fetch(_TRIGGERS_SQL):
        triggers.append(
            Trigger(
                name=row[0],
                table=row[1],
                timing="INSTEAD OF" if row[2] else "AFTER",
                events=_events_from_definition(row[3] or ""),
                definition=row[3] or "",
            )
        )
    by_name: dict[str, Trigger] = {}
    for name, event, definition in conn.fetch(_DDL_TRIGGERS_SQL):
        if name not in by_name:
            by_name[name] = Trigger(
                name=name, table=None, timing="AFTER", events=[],
                definition=definition or "", is_ddl=True, ddl_scope="database",
            )
        by_name[name].events.append(event)
    triggers.extend(by_name.values())
    return triggers


def _extract_types(conn, database: Database) -> list[UserType]:
    types: list[UserType] = []
    try:
        type_rows = conn.fetch(_TYPES_SQL)
    except Exception as exc:
        database.warnings.append(
            f"User-defined types could not be inspected on this SQL Server (query failed: {exc}) — "
            f"user-defined types will not be migrated"
        )
        type_rows = []
    for row in type_rows:
        name, is_table_type, is_assembly_type, base, length, precision, scale = row[:7]
        nullable, default = row[7], row[8]
        if is_table_type:
            columns = []
            try:
                column_rows = conn.fetch(_TABLE_TYPE_COLUMNS_SQL, (name,))
            except Exception as exc:
                database.warnings.append(
                    f"Columns for table type '{name}' could not be inspected: {exc}"
                )
                column_rows = []
            for col_name, col_type, col_len, col_precision, col_scale, col_nullable, col_default in column_rows:
                columns.append(Column(
                    name=col_name,
                    data_type=_rebuild_type(col_type, col_len, col_precision, col_scale, None),
                    nullable=bool(col_nullable),
                    default=_clean_default(col_default),
                ))
            types.append(UserType(name=name, kind="table_type", columns=columns))
            continue
        if is_assembly_type:
            database.warnings.append(
                f"CLR user-defined type '{name}' is .NET code and cannot be ported to PostgreSQL — "
                f"type not migrated"
            )
            types.append(UserType(name=name, kind="clr"))
            continue
        types.append(
            UserType(
                name=name,
                kind="alias",
                base_type=_rebuild_type(base, length, precision, scale, None),
                nullable=bool(nullable),
                default=_clean_default(default),
            )
        )
    return types


def _extract_synonyms(conn, database: Database) -> list[Synonym]:
    kind_map = {"U": "table", "V": "view", "P": "procedure", "FN": "function", "IF": "function",
                "TF": "function", "SN": "synonym"}
    synonyms: list[Synonym] = []
    try:
        synonym_rows = conn.fetch(_SYNONYMS_SQL)
    except Exception as exc:
        database.warnings.append(
            f"Synonyms could not be inspected on this SQL Server (query failed: {exc}) — "
            f"synonyms will not be migrated"
        )
        return synonyms
    for name, target in synonym_rows:
        kind = "table"
        try:
            rows = conn.fetch(_SYNONYM_KIND_SQL, (target,))
        except Exception:
            rows = []
        if rows:
            kind = kind_map.get(rows[0][0], "table")
        synonyms.append(Synonym(name=name, target_object=target, target_kind=kind))
    return synonyms


# ---------------------------------------------------------------------------
# partitioning
# ---------------------------------------------------------------------------

def _partition_map(conn, database) -> dict[str, tuple[str, str, str]]:
    """qualified table name -> (partition function name, RANGE LEFT/RIGHT, partition column)."""
    result: dict[str, tuple[str, str, str]] = {}
    for table_name, func_name, range_type, part_col in _fetch_or_warn(
            conn, _PARTITIONED_TABLES_SQL, database, "Partitioned tables"):
        result[table_name] = (func_name, range_type, part_col)
    return result


def _partition_boundaries(conn, database) -> dict[str, list[str]]:
    boundaries: dict[str, list[str]] = {}
    for func_name, value in _fetch_or_warn(conn, _PARTITION_BOUNDARIES_SQL, database,
                                           "Partition boundaries"):
        rendered = _render_partition_value(value)
        if rendered is None:
            return {}  # unconvertible boundary — caller falls back to non-partitioned
        boundaries.setdefault(func_name, []).append(rendered)
    return boundaries


def _render_partition_value(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-8")
        except Exception:
            return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if hasattr(v, "isoformat"):
        return f"'{v.isoformat()}'"
    s = str(v).strip()
    if re.match(r"^-?[\d.e+-]+$", s):
        return s
    return f"'{s.replace(chr(39), chr(39) + chr(39))}'"


# ---------------------------------------------------------------------------
# temporal / graph
# ---------------------------------------------------------------------------

def _temporal_map(conn, database) -> dict[str, str]:
    rows = _fetch_or_warn(conn, _TEMPORAL_TABLES_SQL, database,
                          "Temporal (system-versioned) tables")
    return {row[0]: row[1] for row in rows}


def _graph_map(conn, database) -> dict[str, str]:
    rows = _fetch_or_warn(conn, _GRAPH_TABLES_SQL, database,
                          "Graph (node/edge) tables")
    result = {}
    for name, is_node, is_edge in rows:
        result[name] = "node" if is_node else "edge"
    return result


# ---------------------------------------------------------------------------
# security
# ---------------------------------------------------------------------------

def _extract_security(conn, database: Database) -> None:
    by_name: dict[str, Principal] = {}
    try:
        principal_rows = conn.fetch(_SECURITY_PRINCIPALS_SQL)
    except Exception as exc:
        database.warnings.append(
            f"Database principals could not be inspected on this SQL Server (query failed: {exc}) — "
            f"roles, users, memberships and grants will not be migrated"
        )
        return
    for name, ptype, is_fixed_role in principal_rows:
        kind = "role" if ptype == "R" else "user"
        if is_fixed_role:
            continue
        by_name[name] = Principal(name=name, kind=kind)
        (database.roles if kind == "role" else database.users).append(by_name[name])

    try:
        membership_rows = conn.fetch(_ROLE_MEMBERSHIP_SQL)
    except Exception as exc:
        database.warnings.append(
            f"Role memberships could not be inspected on this SQL Server (query failed: {exc}) — "
            f"role memberships will not be migrated"
        )
        membership_rows = []
    for member, role in membership_rows:
        principal = by_name.get(member)
        if principal is not None and role in by_name:
            principal.member_of.append(role)

    permission_map = {
        "SELECT": "SELECT", "INSERT": "INSERT", "UPDATE": "UPDATE", "DELETE": "DELETE",
        "EXECUTE": "EXECUTE", "REFERENCES": "REFERENCES", "ALTER": "ALTER",
        "VIEW DEFINITION": None, "CONTROL": None,
    }
    securable_map = {"U": "table", "V": "view", "P": "procedure", "FN": "function",
                     "IF": "function", "TF": "function"}
    known_names = {
        "table": {t.name.lower() for t in database.tables},
        "view": {v.name.lower() for v in database.views},
        "function": {r.name.lower() for r in database.functions},
        "procedure": {r.name.lower() for r in database.procedures},
    }
    seen: set[tuple] = set()
    try:
        permission_rows = conn.fetch(_PERMISSIONS_SQL)
    except Exception as exc:
        database.warnings.append(
            f"Permissions could not be inspected on this SQL Server (query failed: {exc}) — "
            f"grants will not be migrated"
        )
        permission_rows = []
    for principal, perm_name, state_desc, obj_name, class_desc, obj_type in permission_rows:
        action = permission_map.get(perm_name)
        if action is None:
            database.warnings.append(
                f"Permission '{perm_name}' on '{obj_name}' for '{principal}' cannot be ported to "
                f"PostgreSQL and was not migrated"
            )
            continue
        if class_desc == "SCHEMA" or class_desc == "SCHEMA_CATALOG":
            securable = "schema"
        else:
            securable = securable_map.get(obj_type or "")
            if securable is None:
                # Legacy servers sometimes cannot join sys.objects (or the
                # object type comes back empty) — resolve the securable by
                # matching the object name against the extracted schema.
                key = (obj_name or "").lower()
                securable = (
                    "table" if key in known_names["table"]
                    else "view" if key in known_names["view"]
                    else "function" if key in known_names["function"]
                    else "procedure" if key in known_names["procedure"]
                    else "table"
                )
        grant_type = "DENY" if state_desc == "DENY" else "GRANT"
        if state_desc == "DENY":
            database.warnings.append(
                f"DENY permission '{perm_name}' on '{obj_name}' for '{principal}' has no PostgreSQL "
                f"equivalent (REVOKE is not DENY) and was not migrated"
            )
            continue
        key = (principal, securable, obj_name, action)
        if key in seen:
            continue
        seen.add(key)
        database.permissions.append(
            Permission(
                principal=principal, securable=securable, object_name=obj_name,
                action=action, grant_type=grant_type,
                with_grant=state_desc == "GRANT_WITH_GRANT_OPTION",
            )
        )


# ---------------------------------------------------------------------------
# database-level settings (mostly surfaced as warnings)
# ---------------------------------------------------------------------------

def _database_collation(conn) -> str | None:
    row = conn.fetchone("SELECT CONVERT(varchar(200), DATABASEPROPERTYEX(DB_NAME(), 'Collation'))")
    return row[0] if row and row[0] else None


def _fetch_or_warn(conn, sql: str, database: Database, feature: str, params=None) -> list:
    """Run an optional feature probe and never let it fail the whole extraction.

    Some catalog features only exist on newer SQL Server versions (e.g.
    temporal columns are 2016+, sys.sequences is 2012+, and graph columns are
    2017+). When a probe fails the feature is skipped and a
    warning is recorded instead of aborting the migration."""
    try:
        if params is None:
            return conn.fetch(sql)
        return conn.fetch(sql, params)
    except Exception as exc:
        database.warnings.append(
            f"{feature} could not be inspected on this SQL Server (query failed: {exc}) — "
            f"this feature will not be migrated"
        )
        return []


def _add_settings_warnings(conn, database: Database) -> None:
    def _safe_fetch(sql):
        try:
            return conn.fetch(sql)
        except Exception:
            return []

    if _safe_fetch("SELECT name FROM sys.fulltext_catalogs"):
        database.warnings.append(
            f"{len(_safe_fetch('SELECT name FROM sys.fulltext_catalogs'))} full-text catalog(s) exist — "
            f"full-text catalogs have no PostgreSQL equivalent and were not migrated"
        )
    if _safe_fetch("SELECT name FROM sys.fulltext_indexes"):
        database.warnings.append(
            "Full-text indexes exist on tables — they were not migrated (PostgreSQL full-text search "
            "requires tsvector expressions and is not a drop-in equivalent)"
        )
    if _safe_fetch("SELECT is_tracked_by_cdc FROM sys.tables WHERE is_tracked_by_cdc = 1"):
        database.warnings.append(
            "Change Data Capture (CDC) is enabled — CDC capture tables and jobs have no PostgreSQL "
            "equivalent and were not migrated"
        )
    row = _safe_fetch("SELECT is_change_tracking_enabled FROM sys.databases WHERE name = DB_NAME()")
    if row and row[0][0]:
        database.warnings.append(
            "Change Tracking is enabled — it has no PostgreSQL equivalent and was not migrated"
        )
    row = _safe_fetch("SELECT is_broker_enabled FROM sys.databases WHERE name = DB_NAME()")
    if row and row[0][0]:
        database.warnings.append(
            "Service Broker is enabled — queues, contracts and services have no PostgreSQL equivalent "
            "and were not migrated"
        )
    row = _safe_fetch("SELECT desired_state_desc FROM sys.database_query_store_options")
    if row and row[0][0] != "OFF":
        database.warnings.append(
            "Query Store is enabled — its runtime statistics and execution plans have no PostgreSQL "
            "equivalent and were not migrated"
        )
    row = _safe_fetch("SELECT encryption_state FROM sys.dm_database_encryption_keys WHERE database_id = DB_ID()")
    if row and row[0][0] and row[0][0] != 0:
        database.warnings.append(
            "Transparent Data Encryption (TDE) is enabled — the database encryption key cannot be ported "
            "between engines; the target database must be encrypted separately"
        )
    for catalog, label in (("sys.certificates", "certificates"),
                           ("sys.symmetric_keys", "symmetric keys"),
                           ("sys.asymmetric_keys", "asymmetric keys")):
        rows = _safe_fetch(f"SELECT name FROM {catalog}")
        if rows:
            database.warnings.append(
                f"{len(rows)} {label} exist — encryption keys and certificates are engine-specific and "
                f"cannot be migrated"
            )
    row = _safe_fetch("SELECT compatibility_level, recovery_model_desc FROM sys.databases WHERE name = DB_NAME()")
    if row:
        database.warnings.append(
            f"Database compatibility level {row[0][0]} and recovery model '{row[0][1]}' are "
            f"SQL Server-specific settings with no PostgreSQL equivalent and were not migrated"
        )
    rows = _safe_fetch("SELECT name FROM sys.database_scoped_configurations")
    if rows:
        database.warnings.append(
            "Database scoped configurations (Max DOP, parameter sniffing, query optimizer hotfixes) "
            "are SQL Server-specific and were not migrated"
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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


def _rebuild_sequence_type(type_name, precision, scale) -> str | None:
    if not type_name:
        return None
    upper = type_name.upper()
    if upper in ("DECIMAL", "NUMERIC") and precision is not None:
        return f"{upper}({precision},{scale if scale is not None else 0})"
    return upper


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
    # e.g. AFTER INSERT, UPDATE   |  FOR DELETE, INSERT   |  INSTEAD OF UPDATE
    match = re.search(
        r"\b(?:FOR|AFTER|INSTEAD\s+OF)\b\s*([A-Z,\s]+)", upper, flags=re.IGNORECASE
    )
    return bool(match) and event in match.group(1).split(",")
