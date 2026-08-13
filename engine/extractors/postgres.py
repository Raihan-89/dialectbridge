"""
PostgreSQL schema extraction into the normalized Database model.

Uses pg_catalog for type fidelity (format_type) plus catalog queries for
PK/FK/index/trigger/function definitions. All per-table queries address the
table by OID, so identifiers with spaces/uppercase never break parsing.

Beyond the plain relational surface it also extracts domains/enums/composite
types, standalone sequences (excluding identity/serial-owned ones), materialized
views, event triggers, partitioned tables (parents + child bounds), and a basic
security model (roles, memberships, object grants) so the reverse migration to
SQL Server can recreate them.
"""
from __future__ import annotations

import re

from engine.schema import (
    CheckConstraint, Column, Constraint, Database, ForeignKey, Index, Permission,
    PartitionChild, Principal, Routine, Sequence, Table, Trigger, UserType, View,
)

_SYSTEM_SCHEMAS = (
    "('pg_catalog', 'information_schema', 'pg_toast', 'pg_temp_1', 'pg_catalog_fts_config')"
)

_TABLES_SQL = f"""
SELECT c.oid, n.nspname, c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
    AND n.nspname NOT LIKE 'pg_temp_%'
    AND n.nspname NOT LIKE 'pg_toast_temp_%'
    AND c.relpartbound IS NULL
ORDER BY n.nspname, c.relname
"""

_PARTITIONED_TABLES_SQL = f"""
SELECT c.oid, n.nspname, c.relname, pg_get_partkeydef(c.oid)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'p'
    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
    AND n.nspname NOT LIKE 'pg_temp_%'
ORDER BY n.nspname, c.relname
"""

_PARTITION_CHILDREN_SQL = """
SELECT
    p_ns.nspname || '.' || p.relname AS parent,
    c_ns.nspname || '.' || c.relname AS child,
    pg_get_expr(c.relpartbound, c.oid) AS bounds
FROM pg_inherits ih
JOIN pg_class p ON p.oid = ih.inhparent
JOIN pg_namespace p_ns ON p_ns.oid = p.relnamespace
JOIN pg_class c ON c.oid = ih.inhrelid
JOIN pg_namespace c_ns ON c_ns.oid = c.relnamespace
WHERE p.relkind = 'p'
ORDER BY p.relname, c.relname
"""

_COLUMNS_SQL = """
SELECT
    a.attname,
    format_type(a.atttypid, a.atttypmod) AS data_type,
    NOT a.attnotnull AS nullable,
    pg_get_expr(d.adbin, d.adrelid) AS default_expr,
    a.attidentity,
    a.attgenerated,
    a.attnum,
    c.collname AS collation_name
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
LEFT JOIN pg_collation c ON c.oid = a.attcollation
WHERE a.attrelid = %s
    AND a.attnum > 0
    AND NOT a.attisdropped
ORDER BY a.attnum
"""

_PK_SQL = """
SELECT con.conname, a.attname, k.ord
FROM pg_constraint con
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
WHERE con.conrelid = %s AND con.contype = 'p'
ORDER BY k.ord
"""

_UNIQUE_SQL = """
SELECT con.conname, a.attname, k.ord
FROM pg_constraint con
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
WHERE con.conrelid = %s AND con.contype = 'u'
ORDER BY con.conname, k.ord
"""

_FK_SQL = """
SELECT
    con.conname,
    a.attname,
    n.nspname || '.' || rc.relname AS ref_table,
    ra.attname AS ref_attname,
    con.confupdtype,
    con.confdeltype
FROM pg_constraint con
JOIN pg_class rc ON rc.oid = con.confrelid
JOIN pg_namespace n ON n.oid = rc.relnamespace
CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(pattnum, fattnum, ord)
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.pattnum
JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = k.fattnum
WHERE con.conrelid = %s AND con.contype = 'f'
ORDER BY con.conname, k.ord
"""

_INDEX_SQL = """
SELECT
    i.relname AS index_name,
    a.attname AS col_name,
    ix.indisunique,
    pg_get_expr(ix.indpred, ix.indrelid) AS filter_def,
    ix.indisclustered,
    k.ord > ix.indnkeyatts AS is_included,
    ix.indam::regclass::text AS index_method
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class t ON t.oid = ix.indrelid
CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord)
LEFT JOIN pg_attribute a ON a.attrelid = ix.indrelid AND a.attnum = k.attnum
WHERE t.oid = %s
    AND NOT ix.indisprimary
    AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = ix.indexrelid AND c.contype = 'u')
    AND a.attnum IS NOT NULL
ORDER BY i.relname, k.ord
"""

_EXPRESSION_INDEX_SQL = """
SELECT i.relname
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class t ON t.oid = ix.indrelid
WHERE t.oid = %s AND 0 = ANY(ix.indkey)
ORDER BY i.relname
"""

_NON_BTREE_INDEX_SQL = """
SELECT i.relname, ix.indam::regclass::text AS method
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class t ON t.oid = ix.indrelid
WHERE t.oid = %s AND ix.indam::regclass::text <> 'btree'
ORDER BY i.relname
"""

_CHECK_SQL = """
SELECT con.conname, pg_get_constraintdef(con.oid)
FROM pg_constraint con
WHERE con.conrelid = %s AND con.contype = 'c'
ORDER BY con.conname
"""

_VIEWS_SQL = f"""
SELECT n.nspname || '.' || c.relname, pg_get_viewdef(c.oid, true)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'v'
    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
ORDER BY n.nspname, c.relname
"""

_MATVIEWS_SQL = f"""
SELECT n.nspname || '.' || c.relname, pg_get_viewdef(c.oid, true)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'm'
    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
ORDER BY n.nspname, c.relname
"""

_FUNCTIONS_SQL = f"""
SELECT n.nspname || '.' || p.proname, pg_get_functiondef(p.oid)
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN {_SYSTEM_SCHEMAS}
    AND p.prokind IN ('f', 'w')
    AND p.prorettype NOT IN (SELECT oid FROM pg_type WHERE typname IN ('trigger', 'event_trigger'))
ORDER BY n.nspname, p.proname
"""

_PROCEDURES_SQL = f"""
SELECT n.nspname || '.' || p.proname, pg_get_functiondef(p.oid)
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN {_SYSTEM_SCHEMAS}
    AND p.prokind = 'p'
ORDER BY n.nspname, p.proname
"""

_TRIGGERS_SQL = f"""
SELECT
    t.tgname,
    n.nspname || '.' || c.relname AS table_name,
    pg_get_triggerdef(t.oid, true),
    COALESCE(pg_get_functiondef(t.tgfoid), '')
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal
    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
ORDER BY n.nspname, t.tgname
"""

_EVENT_TRIGGERS_SQL = f"""
SELECT ev.evtname, ev.evtevent, COALESCE(pg_get_functiondef(ev.evtfoid), '')
FROM pg_event_trigger ev
WHERE ev.evtname NOT LIKE 'pg_%'
ORDER BY ev.evtname
"""

_IDENTITY_SEQ_SQL = f"""
SELECT
    a.attrelid::regclass::text AS table_name,
    a.attname,
    pg_get_serial_sequence(a.attrelid::regclass::text, a.attname) AS seq_name,
    seq.seqstart,
    seq.seqincrement
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_sequence seq
    ON seq.seqrelid = (pg_get_serial_sequence(a.attrelid::regclass::text, a.attname))::regclass
WHERE a.attidentity <> ''
    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
ORDER BY n.nspname, c.relname
"""

_STANDALONE_SEQ_SQL = f"""
SELECT
    n.nspname || '.' || c.relname,
    s.seqstart,
    s.seqincrement,
    s.seqcycle,
    s.last_value,
    format_type(s.seqtypid, NULL)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_sequence s ON s.seqrelid = c.oid
WHERE c.relkind = 'S'
    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
    AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.objid = c.oid
            AND d.classid = 'pg_class'::regclass
            AND d.deptype IN ('a', 'i')
    )
ORDER BY n.nspname, c.relname
"""

_DOMAINS_SQL = f"""
SELECT n.nspname || '.' || t.typname, t.oid,
       format_type(t.typbasetype, t.typtypmod), t.typnotnull
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'd'
    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
ORDER BY n.nspname, t.typname
"""

_DOMAIN_DEFAULT_SQL = """
SELECT pg_get_expr(d.adbin, d.adrelid)
FROM pg_attrdef d
WHERE d.adrelid = %s AND d.adnum = 0
"""

_DOMAIN_CHECKS_SQL = """
SELECT pg_get_constraintdef(c.oid)
FROM pg_constraint c
WHERE c.contypid = %s AND c.contype = 'c'
ORDER BY c.conname
"""

_ENUMS_SQL = f"""
SELECT n.nspname || '.' || t.typname, t.oid
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'e'
    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
ORDER BY n.nspname, t.typname
"""

_ENUM_VALUES_SQL = """
SELECT e.enumlabel
FROM pg_enum e
WHERE e.enumtypid = %s
ORDER BY e.enumsortorder
"""

_COMPOSITES_SQL = f"""
SELECT n.nspname || '.' || t.typname
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'c'
    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
    AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = t.typrelid AND c.relkind IN ('r', 'p', 'v', 'm', 'S'))
ORDER BY n.nspname, t.typname
"""

_ROLES_SQL = """
SELECT rolname, rolcanlogin, rolsuper
FROM pg_roles
WHERE rolname NOT LIKE 'pg_%'
    AND rolname NOT IN ('postgres', 'public')
    AND rolsuper = false
ORDER BY rolname
"""

_ROLE_MEMBERS_SQL = """
SELECT member.rolname, role.rolname
FROM pg_auth_members am
JOIN pg_roles member ON member.oid = am.member
JOIN pg_roles role ON role.oid = am.roleid
WHERE member.rolname NOT LIKE 'pg_%' AND role.rolname NOT LIKE 'pg_%'
ORDER BY member.rolname, role.rolname
"""

_OBJECT_GRANTS_SQL = """
SELECT
    n.nspname || '.' || c.relname,
    c.relkind,
    u.rolname,
    p.privilege_type,
    p.is_grantable
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) p
JOIN pg_roles u ON u.oid = p.grantee
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND u.rolname NOT LIKE 'pg_%'
    AND p.grantee <> 0
ORDER BY n.nspname, c.relname, u.rolname, p.privilege_type
"""

_ROUTINE_GRANTS_SQL = """
SELECT
    n.nspname || '.' || p.proname,
    p.prokind,
    u.rolname,
    q.privilege_type,
    q.is_grantable
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) q
JOIN pg_roles u ON u.oid = q.grantee
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND u.rolname NOT LIKE 'pg_%'
    AND q.grantee <> 0
ORDER BY n.nspname, p.proname, u.rolname, q.privilege_type
"""


def extract_schema(conn) -> Database:
    database = Database(
        name=conn.database,
        dialect="postgres",
        collation=_database_collation(conn),
    )

    for oid, schema, name in conn.fetch(_TABLES_SQL):
        database.tables.append(_extract_table(conn, oid, f"{schema}.{name}"))

    _extract_partitioned_tables(conn, database)

    database.views = [
        View(name=row[0], definition=_wrap_view(row[0], row[1]))
        for row in conn.fetch(_VIEWS_SQL)
    ]
    database.views += _extract_matviews(conn)

    database.functions = [
        Routine(name=row[0], kind="function", definition=row[1])
        for row in conn.fetch(_FUNCTIONS_SQL)
    ]
    database.procedures = [
        Routine(name=row[0], kind="procedure", definition=row[1])
        for row in conn.fetch(_PROCEDURES_SQL)
    ]
    database.triggers = _extract_triggers(conn)
    database.sequences = [
        Sequence(
            name=row[0],
            start_value=int(row[1]),
            increment=int(row[2]),
            is_cycling=bool(row[3]),
            current_value=int(row[4]) if row[4] is not None else None,
            data_type=row[5],
        )
        for row in conn.fetch(_STANDALONE_SEQ_SQL)
    ]
    database.types = _extract_types(conn)
    _extract_security(conn, database)
    return database


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------

def _extract_table(conn, oid: int, qualified: str) -> Table:
    columns = []
    for row in conn.fetch(_COLUMNS_SQL, (oid,)):
        name, data_type, nullable, default_expr, attidentity, attgenerated, _, collation = row
        is_identity = attidentity in ("a", "d")
        identity_seed = identity_increment = None
        if is_identity:
            schema, tbl = qualified.rsplit(".", 1)
            seq = conn.fetchone(
                "SELECT seqstart, seqincrement FROM pg_sequence "
                "WHERE seqrelid = pg_get_serial_sequence(%s, %s)::regclass",
                (f"{conn.quote_ident(schema)}.{conn.quote_ident(tbl)}", name),
            )
            if seq:
                identity_seed, identity_increment = int(seq[0]), int(seq[1])
        elif default_expr and re.match(r"^nextval\(", default_expr.strip()):
            # serial / bigserial columns: surfaced as identity so the reverse
            # migration recreates them with IDENTITY(seed, increment).
            is_identity = True
            seq = _serial_sequence_info(conn, qualified, name)
            if seq:
                identity_seed, identity_increment = seq
        columns.append(
            Column(
                name=name,
                data_type=data_type,
                nullable=nullable,
                default=_clean_default(default_expr),
                is_identity=is_identity,
                identity_seed=identity_seed,
                identity_increment=identity_increment,
                is_computed=attgenerated != "",
                computed_definition=default_expr if attgenerated else None,
                collation=None if collation in (None, "default", "C", "POSIX") else collation,
            )
        )

    pk = None
    pk_rows = conn.fetch(_PK_SQL, (oid,))
    if pk_rows:
        pk = Constraint(name=pk_rows[0][0], columns=[r[1] for r in pk_rows])

    unique_constraints: list[Constraint] = []
    current = None
    for cname, ccol, _ in conn.fetch(_UNIQUE_SQL, (oid,)):
        if current is None or current.name != cname:
            current = Constraint(name=cname, columns=[])
            unique_constraints.append(current)
        current.columns.append(ccol)

    foreign_keys: list[ForeignKey] = []
    current_fk = None
    for fk_name, col_name, ref_table, ref_col, upd, dele in conn.fetch(_FK_SQL, (oid,)):
        if current_fk is None or current_fk.name != fk_name:
            current_fk = ForeignKey(
                name=fk_name, columns=[], ref_table=ref_table, ref_columns=[],
                on_update=_fk_action(upd), on_delete=_fk_action(dele),
            )
            foreign_keys.append(current_fk)
        current_fk.columns.append(col_name)
        current_fk.ref_columns.append(ref_col)

    indexes = _extract_indexes(conn, oid)

    checks = [
        CheckConstraint(name=row[0], definition=row[1])
        for row in conn.fetch(_CHECK_SQL, (oid,))
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


def _extract_indexes(conn, oid: int, warnings: list[str] | None = None) -> list[Index]:
    warnings = warnings if warnings is not None else []
    indexes: list[Index] = []
    current_idx = None
    for idx_name, col_name, is_unique, filter_def, clustered, is_included, method in conn.fetch(
            _INDEX_SQL, (oid,)):
        if current_idx is None or current_idx.name != idx_name:
            current_idx = Index(
                name=idx_name, columns=[], unique=bool(is_unique), where=filter_def,
                is_clustered=bool(clustered),
            )
            indexes.append(current_idx)
        if is_included:
            current_idx.included_columns.append(col_name)
        else:
            current_idx.columns.append(col_name)

    for idx_name in [r[0] for r in conn.fetch(_EXPRESSION_INDEX_SQL, (oid,))]:
        warnings.append(
            f"Index '{idx_name}' is a functional (expression) index with no direct SQL Server "
            f"equivalent — it was not migrated"
        )
    for idx_name, method in conn.fetch(_NON_BTREE_INDEX_SQL, (oid,)):
        warnings.append(
            f"Index '{idx_name}' uses the {method} index method, which has no SQL Server equivalent "
            f"— it was not migrated"
        )
    return indexes


def _extract_partitioned_tables(conn, database: Database) -> None:
    children_by_parent: dict[str, list[PartitionChild]] = {}
    for parent, child, bounds in conn.fetch(_PARTITION_CHILDREN_SQL):
        children_by_parent.setdefault(parent.lower(), []).append(
            PartitionChild(name=child, bounds=bounds.replace("FOR VALUES ", ""))
        )

    by_name = {t.name.lower(): t for t in database.tables}
    for oid, schema, name, partkeydef in conn.fetch(_PARTITIONED_TABLES_SQL):
        qualified = f"{schema}.{name}"
        table = _extract_table(conn, oid, qualified)
        m = re.search(r"^(RANGE|LIST)\s*\((.*)\)", partkeydef, re.IGNORECASE)
        if not m:
            database.warnings.append(
                f"Partitioned table '{qualified}' has an unrecognized partition key "
                f"'{partkeydef}' — migrated as a regular table"
            )
            database.tables.append(table)
            continue
        table.is_partitioned = True
        table.partition_type = m.group(1).lower()
        table.partition_key = m.group(2).strip()
        table.partition_children = children_by_parent.get(qualified.lower(), [])
        database.tables.append(table)


# ---------------------------------------------------------------------------
# views / triggers / types
# ---------------------------------------------------------------------------

def _extract_matviews(conn) -> list[View]:
    matviews: list[View] = []
    for oid, schema, name in conn.fetch(
            f"""SELECT c.oid, n.nspname, c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'm'
                    AND n.nspname NOT IN {_SYSTEM_SCHEMAS}
                ORDER BY n.nspname, c.relname"""):
        qualified = f"{schema}.{name}"
        body = conn.fetchone("SELECT pg_get_viewdef(%s::regclass, true)", (qualified,))
        if not body or not body[0]:
            continue
        view = View(
            name=qualified,
            definition=_wrap_view(qualified, body[0]),
            is_materialized=True,
        )
        view.indexes = _extract_indexes(conn, oid)
        matviews.append(view)
    return matviews


def _extract_triggers(conn) -> list[Trigger]:
    triggers: list[Trigger] = []
    for row in conn.fetch(_TRIGGERS_SQL):
        triggers.append(
            Trigger(
                name=row[0],
                table=row[1],
                timing=_trigger_timing(row[2]),
                events=_trigger_events(row[2]),
                definition=row[2] + ("\n\n" + row[3] if row[3] else ""),
            )
        )
    for name, event, function_def in conn.fetch(_EVENT_TRIGGERS_SQL):
        triggers.append(
            Trigger(
                name=name,
                table=None,
                timing="AFTER",
                events=[event],
                definition=function_def or "",
                is_ddl=True,
                ddl_scope="database",
            )
        )
    return triggers


def _extract_types(conn) -> list[UserType]:
    types: list[UserType] = []

    for name, oid, base_type, notnull in conn.fetch(_DOMAINS_SQL):
        default = None
        row = conn.fetchone(_DOMAIN_DEFAULT_SQL, (oid,))
        if row and row[0]:
            default = _clean_default(row[0])
        checks = [r[0] for r in conn.fetch(_DOMAIN_CHECKS_SQL, (oid,))]
        types.append(
            UserType(
                name=name, kind="domain", base_type=base_type,
                default=default, nullable=not notnotnull, constraints=checks,
            )
        )

    for name, oid in conn.fetch(_ENUMS_SQL):
        values = [r[0] for r in conn.fetch(_ENUM_VALUES_SQL, (oid,))]
        types.append(UserType(name=name, kind="enum", values=values))

    for (name,) in conn.fetch(_COMPOSITES_SQL):
        types.append(UserType(name=name, kind="composite"))

    return types


# ---------------------------------------------------------------------------
# security
# ---------------------------------------------------------------------------

def _extract_security(conn, database: Database) -> None:
    by_name: dict[str, Principal] = {}
    for rolname, canlogin, is_super in conn.fetch(_ROLES_SQL):
        principal = Principal(name=rolname, kind="user" if canlogin else "role")
        by_name[rolname] = principal
        (database.users if canlogin else database.roles).append(principal)

    for member, role in conn.fetch(_ROLE_MEMBERS_SQL):
        principal = by_name.get(member)
        if principal is not None and role in by_name:
            principal.member_of.append(role)

    securable_map = {"r": "table", "p": "view", "v": "view", "m": "view", "S": "sequence"}
    action_map = {
        "SELECT": "SELECT", "INSERT": "INSERT", "UPDATE": "UPDATE", "DELETE": "DELETE",
        "EXECUTE": "EXECUTE", "REFERENCES": "REFERENCES", "TRIGGER": None,
        "TRUNCATE": None, "USAGE": None, "MAINTAIN": None,
    }
    seen: set[tuple] = set()
    for obj_name, relkind, rolname, priv, grantable in conn.fetch(_OBJECT_GRANTS_SQL):
        action = action_map.get(priv)
        if action is None or rolname not in by_name:
            if action is None:
                database.warnings.append(
                    f"PostgreSQL privilege '{priv}' on '{obj_name}' has no SQL Server equivalent "
                    f"and was not migrated"
                )
            continue
        securable = securable_map.get(relkind, "object")
        key = (rolname, securable, obj_name, action)
        if key in seen:
            continue
        seen.add(key)
        database.permissions.append(
            Permission(
                principal=rolname, securable=securable, object_name=obj_name,
                action=action, grant_type="GRANT", with_grant=bool(grantable),
            )
        )

    routine_map = {"f": "function", "w": "function", "p": "procedure"}
    for obj_name, prokind, rolname, priv, grantable in conn.fetch(_ROUTINE_GRANTS_SQL):
        action = action_map.get(priv)
        if action is None or rolname not in by_name:
            continue
        key = (rolname, routine_map.get(prokind, "function"), obj_name, action)
        if key in seen:
            continue
        seen.add(key)
        database.permissions.append(
            Permission(
                principal=rolname, securable=routine_map.get(prokind, "function"),
                object_name=obj_name, action=action, grant_type="GRANT",
                with_grant=bool(grantable),
            )
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _database_collation(conn) -> str | None:
    row = conn.fetchone("SELECT datcollate FROM pg_database WHERE datname = current_database()")
    return row[0] if row else None


def _serial_sequence_info(conn, qualified: str, column: str) -> tuple[int, int] | None:
    try:
        schema, tbl = qualified.rsplit(".", 1)
        seq = conn.fetchone(
            "SELECT seqstart, seqincrement FROM pg_sequence "
            "WHERE seqrelid = pg_get_serial_sequence(%s, %s)::regclass",
            (f"{conn.quote_ident(schema)}.{conn.quote_ident(tbl)}", column),
        )
        if seq:
            return int(seq[0]), int(seq[1])
    except Exception:
        return None
    return None


def _clean_default(default) -> str | None:
    if not default:
        return None
    default = default.strip()
    if default.startswith("nextval("):
        return None  # serial/identity columns — represented as identity on the target
    return default


def _fk_action(code: str) -> str:
    mapping = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
    return mapping.get(code, "NO ACTION")


def _wrap_view(name: str, body: str) -> str:
    body = body.strip().rstrip(";")
    return f"CREATE VIEW {name} AS {body}"


def _trigger_timing(definition: str) -> str:
    upper = definition.upper()
    if "INSTEAD OF" in upper:
        return "INSTEAD OF"
    if "BEFORE" in upper:
        return "BEFORE"
    return "AFTER"


def _trigger_events(definition: str) -> list[str]:
    upper = definition.upper()
    header = upper.split("EXECUTE", 1)[0]
    return [e for e in ("INSERT", "UPDATE", "DELETE") if e in header]
