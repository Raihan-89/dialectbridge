"""Read-only, live source/target database comparison for the verification UI."""
from __future__ import annotations

from typing import Callable

from .migration_service import connector_for


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
    """Match migrated objects despite dbo/public and identifier-case changes."""
    return name.rsplit(".", 1)[-1].strip('[]"').casefold()


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
    source = connector_for(source_connection)
    target = connector_for(target_connection)
    try:
        source_db = source.extract_schema()
        target_db = target.extract_schema()

        if section == "rows":
            source_items = {
                _key(t.name): {"name": t.name, "rows": source.count_rows(t.name)}
                for t in source_db.tables
            }
            target_items = {
                _key(t.name): {"name": t.name, "rows": target.count_rows(t.name)}
                for t in target_db.tables
            }
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
    finally:
        source.close()
        target.close()
