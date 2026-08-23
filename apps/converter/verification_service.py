"""Read-only, live source/target database comparison for the verification UI."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from datetime import date, datetime, time
from decimal import Decimal
from itertools import zip_longest
from typing import Callable
from uuid import UUID

from engine.translators.sql_builder import pg_ident

from .migration_service import connector_for


# ---------------------------------------------------------------------------
# Schema cache – avoids re-extracting the full schema on every section click.
# Keyed by (connection_pk, updated_at) so the cache auto-invalidates when the
# connection is edited.
# ---------------------------------------------------------------------------
_SCHEMA_CACHE: dict[tuple, object] = {}
_SCHEMA_CACHE_LOCK = threading.Lock()
_SCHEMA_CACHE_MAX = 20  # max entries before oldest-evict


def _cache_key(connection) -> tuple:
    return (connection.pk, connection.updated_at.timestamp())


def _get_schema(connection):
    key = _cache_key(connection)
    with _SCHEMA_CACHE_LOCK:
        cached = _SCHEMA_CACHE.get(key)
        if cached is not None:
            return cached
    # Connect outside the lock so slow I/O doesn't block other threads.
    connector = connector_for(connection)
    try:
        schema = connector.extract_schema()
    finally:
        connector.close()
    with _SCHEMA_CACHE_LOCK:
        if len(_SCHEMA_CACHE) >= _SCHEMA_CACHE_MAX:
            _SCHEMA_CACHE.clear()
        _SCHEMA_CACHE[key] = schema
    return schema


SECTION_LABELS = {
    "overview": "Overview",
    "tables": "Tables",
    "columns": "Columns",
    "views": "Views",
    "functions": "Functions",
    "procedures": "Procedures",
    "triggers": "Triggers",
    "indexes": "Indexes",
    "constraints": "Constraints",
    "sequences": "Sequences",
    "types": "User-defined types",
    "synonyms": "Synonyms",
    "security": "Users & permissions",
    "rows": "Row counts",
}


def _key(name: str) -> str:
    """Match migrated objects despite dbo/public and identifier-case changes.

    PostgreSQL truncates identifiers at 63 bytes, so a longer SQL Server name
    is created there under a shortened, hash-suffixed form. Folding both sides
    through the same truncation keeps that object paired instead of reporting
    it as missing on one side and unexpected on the other.
    """
    return pg_ident(name.rsplit(".", 1)[-1].strip('[]"').strip()).casefold()


def _simple(items, detail: Callable) -> dict:
    return {_key(item.name): {"name": item.name, **detail(item)} for item in items}


def _objects(database, section: str) -> dict:
    if section == "tables":
        return _simple(database.tables, lambda t: {"detail": f"{len(t.columns)} columns"})
    if section == "columns":
        result = {}
        for table in database.tables:
            for column in table.columns:
                result[f"{_key(table.name)}.{_key(column.name)}"] = {
                    "name": f"{table.name}.{column.name}",
                    "detail": column.data_type,
                    "nullable": column.nullable,
                    "identity": column.is_identity,
                    "default": column.default,
                }
        return result
    if section in {"views", "functions", "procedures", "sequences"}:
        items = getattr(database, section)
        if section == "views":
            return _simple(items, lambda v: {"detail": "Materialized" if v.is_materialized else "View", "definition": v.definition})
        if section in {"functions", "procedures"}:
            return _simple(items, lambda r: {"detail": r.parameters or "No parameters", "returns": r.returns, "definition": r.definition})
        return _simple(items, lambda s: {"detail": f"start {s.start_value}, increment {s.increment}", "current": s.current_value})
    if section == "triggers":
        return _simple(database.triggers, lambda t: {
            "detail": f"{t.timing} {' / '.join(t.events)}", "table": t.table, "definition": t.definition,
        })
    if section == "types":
        return _simple(database.types, lambda t: {
            "detail": f"{t.kind}: " + (
                t.base_type or ", ".join(t.values)
                or ", ".join(f"{c.name} {c.data_type}" for c in t.columns)
            )
        })
    if section == "synonyms":
        return _simple(database.synonyms, lambda s: {"detail": f"{s.target_kind} → {s.target_object}"})
    if section == "security":
        result = {}
        for principal in database.users + database.roles:
            result[f"principal.{_key(principal.name)}"] = {
                "name": principal.name, "detail": principal.kind,
                "memberships": sorted(x.casefold() for x in principal.member_of),
            }
        for permission in database.permissions:
            key = ".".join(("permission", _key(permission.principal), _key(permission.object_name), permission.action.casefold()))
            result[key] = {
                "name": f"{permission.principal}: {permission.action}",
                "detail": f"{permission.securable} {permission.object_name}",
                "grant_type": permission.grant_type, "with_grant": permission.with_grant,
            }
        return result
    if section == "indexes":
        result = {}
        for table in database.tables:
            for index in table.indexes:
                result[f"{_key(table.name)}.{_key(index.name)}"] = {
                    "name": index.name, "table": table.name,
                    "detail": ", ".join(index.columns), "unique": index.unique,
                }
        return result
    if section == "constraints":
        result = {}
        for table in database.tables:
            constraints = []
            if table.primary_key:
                constraints.append(("Primary key", table.primary_key.name, table.primary_key.columns))
            constraints.extend(("Unique", c.name, c.columns) for c in table.unique_constraints)
            constraints.extend(("Foreign key", c.name, c.columns) for c in table.foreign_keys)
            constraints.extend(("Check", c.name, [c.definition]) for c in table.check_constraints)
            for kind, name, values in constraints:
                result[f"{_key(table.name)}.{_key(name)}"] = {
                    "name": name, "table": table.name, "detail": f"{kind}: {', '.join(values)}",
                }
        return result
    raise ValueError(f"Unknown verification section: {section}")


def _comparable(value: dict | None) -> dict | None:
    if value is None:
        return None
    # Names and schema-qualified table names legitimately change in migration.
    return {k: v for k, v in value.items() if k not in {"name", "table", "detail", "current", "definition"}}


def _pair(source: dict, target: dict) -> list[dict]:
    rows = []
    for key in sorted(set(source) | set(target)):
        left, right = source.get(key), target.get(key)
        if left is None:
            status = "target_only"
        elif right is None:
            status = "source_only"
        elif _comparable(left) == _comparable(right):
            status = "match"
        else:
            status = "different"
        rows.append({"key": key, "source": left, "target": right, "status": status})
    return rows


def compare_live(source_connection, target_connection, section: str) -> dict:
    """Connect to both databases and return one JSON-safe comparison section."""
    if section not in SECTION_LABELS:
        raise ValueError(f"Unknown verification section: {section}")
    source_db = _get_schema(source_connection)
    target_db = _get_schema(target_connection)

    if section == "rows":
        source = connector_for(source_connection)
        target = connector_for(target_connection)
        try:
            source_items = {
                _key(t.name): {"name": t.name, "rows": source.count_rows(t.name)}
                for t in source_db.tables
            }
            target_items = {
                _key(t.name): {"name": t.name, "rows": target.count_rows(t.name)}
                for t in target_db.tables
            }
        finally:
            source.close()
            target.close()
    elif section == "overview":
        names = ("tables", "views", "functions", "procedures", "triggers", "sequences", "types", "synonyms")
        source_items = {n: {"name": n.title(), "count": len(getattr(source_db, n))} for n in names}
        target_items = {n: {"name": n.title(), "count": len(getattr(target_db, n))} for n in names}
    else:
        source_items = _objects(source_db, section)
        target_items = _objects(target_db, section)

    rows = _pair(source_items, target_items)
    counts = {status: sum(r["status"] == status for r in rows)
              for status in ("match", "different", "source_only", "target_only")}
    return {
        "section": section,
        "label": SECTION_LABELS[section],
        "rows": rows,
        "counts": counts,
        "total": len(rows),
        "all_match": counts["different"] == counts["source_only"] == counts["target_only"] == 0,
    }


def _table_map(database) -> dict:
    return {_key(table.name): table for table in database.tables}


def list_live_data_tables(source_connection, target_connection) -> dict:
    """List live tables on both ends without reading application data."""
    source_tables = _table_map(_get_schema(source_connection))
    target_tables = _table_map(_get_schema(target_connection))
    tables = []
    for key in sorted(set(source_tables) | set(target_tables)):
        left, right = source_tables.get(key), target_tables.get(key)
        tables.append({
            "key": key,
            "source_name": left.name if left else None,
            "target_name": right.name if right else None,
            "source_columns": len(left.columns) if left else 0,
            "target_columns": len(right.columns) if right else 0,
            "status": "both" if left and right else ("source_only" if left else "target_only"),
        })
    return {"tables": tables}


def _json_value(value):
    """Produce a stable, JSON-safe representation for cross-driver comparison."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "base64:" + base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def _fetch_page(connector, table, columns, order_columns, page, page_size):
    selected = ", ".join(connector.quote_ident(column) for column in columns)
    order = ", ".join(connector.quote_ident(column) for column in order_columns)
    offset = (page - 1) * page_size
    table_name = connector.quote_ident(table.name)
    if connector.dialect == "tsql":
        order_clause = order or "(SELECT NULL)"
        sql = (
            f"SELECT {selected} FROM {table_name} ORDER BY {order_clause} "
            f"OFFSET {offset} ROWS FETCH NEXT {page_size} ROWS ONLY"
        )
    else:
        order_clause = f" ORDER BY {order}" if order else ""
        sql = f"SELECT {selected} FROM {table_name}{order_clause} LIMIT {page_size} OFFSET {offset}"
    return [tuple(_json_value(value) for value in row) for row in connector.fetch(sql)]


def compare_live_table_data(source_connection, target_connection, table_key: str, page=1, page_size=50) -> dict:
    """Read and compare one bounded page of source/target table data."""
    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    source_tables = _table_map(_get_schema(source_connection))
    target_tables = _table_map(_get_schema(target_connection))
    source_table, target_table = source_tables.get(table_key), target_tables.get(table_key)
    if not source_table or not target_table:
        raise ValueError("This table is not present in both databases.")

    source = connector_for(source_connection)
    target = connector_for(target_connection)
    try:

        source_columns = {_key(c.name): c.name for c in source_table.columns}
        target_columns = {_key(c.name): c.name for c in target_table.columns}
        common_keys = [k for k in source_columns if k in target_columns]
        if not common_keys:
            raise ValueError("This table has no comparable columns.")

        source_pk = [_key(c) for c in (source_table.primary_key.columns if source_table.primary_key else [])]
        target_pk = {_key(c) for c in (target_table.primary_key.columns if target_table.primary_key else [])}
        key_columns = [k for k in source_pk if k in target_pk and k in common_keys]
        has_shared_pk = bool(key_columns) and len(key_columns) == len(source_pk)
        source_order = [source_columns[k] for k in key_columns] if has_shared_pk else []
        target_order = [target_columns[k] for k in key_columns] if has_shared_pk else []
        source_rows = _fetch_page(source, source_table, [source_columns[k] for k in common_keys], source_order, page, page_size)
        target_rows = _fetch_page(target, target_table, [target_columns[k] for k in common_keys], target_order, page, page_size)

        def row_dict(row):
            return {key: value for key, value in zip(common_keys, row)}

        paired = []
        if has_shared_pk:
            source_by_key = {tuple(row[common_keys.index(k)] for k in key_columns): row for row in source_rows}
            target_by_key = {tuple(row[common_keys.index(k)] for k in key_columns): row for row in target_rows}
            pairs = ((key, source_by_key.get(key), target_by_key.get(key)) for key in sorted(set(source_by_key) | set(target_by_key), key=str))
        else:
            pairs = ((index + 1 + (page - 1) * page_size, left, right)
                     for index, (left, right) in enumerate(zip_longest(source_rows, target_rows)))
        for row_key, left, right in pairs:
            status = "source_only" if right is None else "target_only" if left is None else "match" if left == right else "different"
            paired.append({
                "key": list(row_key) if isinstance(row_key, tuple) else row_key,
                "source": row_dict(left) if left is not None else None,
                "target": row_dict(right) if right is not None else None,
                "status": status,
            })

        source_count = source.count_rows(source_table.name)
        target_count = target.count_rows(target_table.name)
        counts = {status: sum(row["status"] == status for row in paired)
                  for status in ("match", "different", "source_only", "target_only")}
        return {
            "table": table_key,
            "source_name": source_table.name,
            "target_name": target_table.name,
            "columns": [{"key": key, "source": source_columns[key], "target": target_columns[key]} for key in common_keys],
            "source_only_columns": [c.name for c in source_table.columns if _key(c.name) not in target_columns],
            "target_only_columns": [c.name for c in target_table.columns if _key(c.name) not in source_columns],
            "key_columns": key_columns,
            "has_shared_pk": has_shared_pk,
            "page": page,
            "page_size": page_size,
            "source_count": source_count,
            "target_count": target_count,
            "counts": counts,
            "rows": paired,
            "has_previous": page > 1,
            "has_next": page * page_size < max(source_count, target_count),
        }
    finally:
        source.close()
        target.close()


def _table_fingerprint(connector, table, columns, order_columns) -> dict:
    """Stream a complete table into an order-independent multiset digest."""
    total = 0
    digest_sum = 0
    digest_xor = 0
    int_columns = [
        column.name for column in table.columns
        if _key(column.name) in {_key(name) for name in columns}
        and column.data_type.split("(", 1)[0].strip().upper() in {"BIGINT", "INT", "INTEGER", "SMALLINT", "TINYINT"}
    ]
    for batch in connector.iter_table_rows(
        table.name, columns, order_columns, batch_size=5000, int_columns=int_columns,
    ):
        for row in batch:
            normalized = [_json_value(value) for value in row]
            payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            value = int.from_bytes(hashlib.sha256(payload).digest(), "big")
            digest_sum = (digest_sum + value) % (1 << 256)
            digest_xor ^= value
            total += 1
    combined = hashlib.sha256(
        total.to_bytes(16, "big") + digest_sum.to_bytes(32, "big") + digest_xor.to_bytes(32, "big")
    ).hexdigest()
    return {"rows": total, "sha256": combined}


def verify_live_table_checksum(source_connection, target_connection, table_key: str) -> dict:
    """Exhaustively stream and fingerprint every shared value in a table."""
    source_tables = _table_map(_get_schema(source_connection))
    target_tables = _table_map(_get_schema(target_connection))
    source_table, target_table = source_tables.get(table_key), target_tables.get(table_key)
    if not source_table or not target_table:
        raise ValueError("This table is not present in both databases.")
    source_columns = {_key(c.name): c.name for c in source_table.columns}
    target_columns = {_key(c.name): c.name for c in target_table.columns}
    common = [key for key in source_columns if key in target_columns]
    if not common:
        raise ValueError("This table has no comparable columns.")
    source_only = [c.name for c in source_table.columns if _key(c.name) not in target_columns]
    target_only = [c.name for c in target_table.columns if _key(c.name) not in source_columns]
    source_pk = [_key(c) for c in (source_table.primary_key.columns if source_table.primary_key else [])]
    target_pk = {_key(c) for c in (target_table.primary_key.columns if target_table.primary_key else [])}
    key_columns = [key for key in source_pk if key in target_pk and key in common]
    shared_pk = bool(key_columns) and len(key_columns) == len(source_pk)
    source = connector_for(source_connection)
    target = connector_for(target_connection)
    try:
        left = _table_fingerprint(
            source, source_table, [source_columns[key] for key in common],
            [source_columns[key] for key in key_columns] if shared_pk else [],
        )
        right = _table_fingerprint(
            target, target_table, [target_columns[key] for key in common],
            [target_columns[key] for key in key_columns] if shared_pk else [],
        )
    finally:
        source.close()
        target.close()
    exact = not source_only and not target_only and left == right
    return {
        "table": table_key, "source_name": source_table.name, "target_name": target_table.name,
        "columns_checked": len(common), "source_only_columns": source_only,
        "target_only_columns": target_only, "source": left, "target": right,
        "exact_match": exact, "algorithm": "canonical-row-multiset-sha256-v1",
    }
