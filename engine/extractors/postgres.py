"""
PostgreSQL schema extraction into the normalized Database model.

Uses pg_catalog for type fidelity (format_type) plus catalog queries for
PK/FK/index/trigger/function definitions. All per-table queries address the
table by OID, so identifiers with spaces/uppercase never break parsing.
"""
from __future__ import annotations

from engine.schema import (
    Column, Constraint, Database, ForeignKey, Index, Routine, Sequence, Table,
    Trigger, View,
)

_TABLES_SQL = """
SELECT c.oid, n.nspname, c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
    AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast', 'pg_temp_1')
    AND n.nspname NOT LIKE 'pg_temp_%'
    AND n.nspname NOT LIKE 'pg_toast_temp_%'
ORDER BY n.nspname, c.relname
"""

_COLUMNS_SQL = """
SELECT
    a.attname,
    format_type(a.atttypid, a.atttypmod) AS data_type,
    NOT a.attnotnull AS nullable,
    pg_get_expr(d.adbin, d.adrelid) AS default_expr,
    a.attidentity,
    a.attgenerated,
    a.attnum
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
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
    pg_get_expr(ix.indpred, ix.indrelid) AS filter_def
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

_CHECK_SQL = """
SELECT con.conname, pg_get_constraintdef(con.oid)
FROM pg_constraint con
WHERE con.conrelid = %s AND con.contype = 'c'
ORDER BY con.conname
"""

_VIEWS_SQL = """
SELECT n.nspname || '.' || c.relname, pg_get_viewdef(c.oid, true)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'v'
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY n.nspname, c.relname
"""

_FUNCTIONS_SQL = """
SELECT n.nspname || '.' || p.proname, pg_get_functiondef(p.oid)
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND p.prokind IN ('f', 'w')
    AND p.prorettype NOT IN (SELECT oid FROM pg_type WHERE typname IN ('trigger', 'event_trigger'))
ORDER BY n.nspname, p.proname
"""

_PROCEDURES_SQL = """
SELECT n.nspname || '.' || p.proname, pg_get_functiondef(p.oid)
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND p.prokind = 'p'
ORDER BY n.nspname, p.proname
"""

_TRIGGERS_SQL = """
SELECT
    t.tgname,
    n.nspname || '.' || c.relname AS table_name,
    pg_get_triggerdef(t.oid, true),
    COALESCE(pg_get_functiondef(t.tgfoid), '')
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY n.nspname, t.tgname
"""

_IDENTITY_SEQ_SQL = """
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
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY n.nspname, c.relname
"""


def extract_schema(conn) -> Database:
    database = Database(name=conn.database, dialect="postgres")

    for oid, schema, name in conn.fetch(_TABLES_SQL):
        table = _extract_table(conn, oid, f"{schema}.{name}")
        database.tables.append(table)

    database.views = [
        View(name=row[0], definition=_wrap_view(row[0], row[1]))
        for row in conn.fetch(_VIEWS_SQL)
    ]
    database.functions = [
        Routine(name=row[0], kind="function", definition=row[1])
        for row in conn.fetch(_FUNCTIONS_SQL)
    ]
    database.procedures = [
        Routine(name=row[0], kind="procedure", definition=row[1])
        for row in conn.fetch(_PROCEDURES_SQL)
    ]
    database.triggers = [
        Trigger(
            name=row[0],
            table=row[1],
            timing=_trigger_timing(row[2]),
            events=_trigger_events(row[2]),
            definition=row[2] + ("\n\n" + row[3] if row[3] else ""),
        )
        for row in conn.fetch(_TRIGGERS_SQL)
    ]

    for table_name, col_name, seq_name, start, inc in conn.fetch(_IDENTITY_SEQ_SQL):
        database.sequences.append(
            Sequence(
                name=seq_name,
                start_value=int(start) if start is not None else 1,
                increment=int(inc) if inc is not None else 1,
                owned_by=f"{table_name}.{col_name}",
            )
        )

    return database


def _extract_table(conn, oid: int, qualified: str) -> Table:
    columns = []
    for row in conn.fetch(_COLUMNS_SQL, (oid,)):
        name, data_type, nullable, default_expr, attidentity, attgenerated, _ = row
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

    indexes = []
    current_idx = None
    for idx_name, col_name, is_unique, filter_def in conn.fetch(_INDEX_SQL, (oid,)):
        if current_idx is None or current_idx.name != idx_name:
            current_idx = Index(name=idx_name, columns=[], unique=bool(is_unique), where=filter_def)
            indexes.append(current_idx)
        current_idx.columns.append(col_name)

    checks = [row[1] for row in conn.fetch(_CHECK_SQL, (oid,))]

    return Table(
        name=qualified,
        columns=columns,
        primary_key=pk,
        foreign_keys=foreign_keys,
        unique_constraints=unique_constraints,
        indexes=indexes,
        check_constraints=checks,
    )


def _clean_default(default) -> str | None:
    if not default:
        return None
    default = default.strip()
    if default.startswith("nextval("):
        return None  # serial columns — represented as identity on the target
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
