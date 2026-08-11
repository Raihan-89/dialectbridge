"""
Migration orchestrator: the end-to-end pipeline that turns a source database
into a target database.

    extract source schema
        -> convert to target-dialect DDL
        -> apply structural DDL (tables/PK/uniques/indexes)
        -> copy data table-by-table (identity preserved, sequences re-seeded)
        -> apply referential DDL (FKs/checks) + views/functions/procs/triggers
        -> verify: compare row counts between source and target
        -> produce a per-object report

FKs are intentionally applied *after* the data copy so parent/child insert
order never matters. Check constraints are applied after data too, so the
migration doesn't fail on data that already satisfies the constraint in the
source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from engine.connectors.base import ConnectorError
from engine.migration.data_mover import DataMigration
from engine.translators.sql_builder import build_database_ddl

_PRE_DATA_PREFIXES = ("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX",
                      "ALTER TABLE")
_FK_PREFIXES = ("ALTER TABLE",)


@dataclass
class ObjectResult:
    kind: str
    name: str
    status: str = "success"          # success | failed | skipped
    detail: str = ""
    rows_copied: int = 0
    rows_failed: int = 0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "rows_copied": self.rows_copied,
            "rows_failed": self.rows_failed,
        }


@dataclass
class MigrationReport:
    success: bool = True
    started_at: str = ""
    finished_at: str = ""
    source_db: str = ""
    target_db: str = ""
    schema_results: list[ObjectResult] = field(default_factory=list)
    data_results: list[ObjectResult] = field(default_factory=list)
    verification: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "source_db": self.source_db,
            "target_db": self.target_db,
            "schema_results": [r.to_dict() for r in self.schema_results],
            "data_results": [r.to_dict() for r in self.data_results],
            "verification": self.verification,
            "warnings": self.warnings,
            "summary": self.summary,
        }


def _is_structural(stmt: str) -> bool:
    upper = stmt.lstrip().upper()
    return upper.startswith("CREATE TABLE") or upper.startswith("CREATE INDEX") \
        or upper.startswith("CREATE UNIQUE INDEX")


def _is_fk_or_check(stmt: str) -> bool:
    upper = stmt.lstrip().upper()
    return upper.startswith("ALTER TABLE") and (
        "FOREIGN KEY" in upper or "CHECK" in upper
    )


class MigrationOrchestrator:
    def __init__(self, source, target, copy_data: bool = True, reset_target: bool = False):
        self.source = source
        self.target = target
        self.copy_data = copy_data
        self.reset_target = reset_target

    def run(self) -> MigrationReport:
        report = MigrationReport(
            source_db=self.source.database, target_db=self.target.database,
            started_at=_now(),
        )

        # ---- 1. extract ----------------------------------------------------
        try:
            schema = self.source.extract_schema()
        except ConnectorError as exc:
            report.success = False
            report.warnings.append(f"Schema extraction failed: {exc}")
            report.finished_at = _now()
            report.summary = {"status": "failed", "reason": "extraction"}
            return report

        report.warnings.extend(schema.warnings)

        # ---- 2. convert -----------------------------------------------------
        ddl, conv_warnings = build_database_ddl(schema, self.target.dialect)
        report.warnings.extend(conv_warnings)

        # ---- 3. apply structural DDL -----------------------------------------
        schemas = sorted({name.split(".")[0] for t in schema.tables for name in (t.name,)})

        # Optional destructive reset: drop the target schemas we are about to
        # migrate into so a previous run does not collide. The user opts in.
        if self.reset_target and schemas:
            for s in schemas:
                self.target.execute(f"DROP SCHEMA IF EXISTS {self.target.quote_ident(s)} CASCADE")

        for s in schemas:
            self._apply(report, "schema", f"CREATE SCHEMA IF NOT EXISTS {self.target.quote_ident(s)}", object_name=s)

        for table in schema.all_tables_in_dependency_order():
            self._apply_table(report, schema, table)

        structural = [s for s in ddl if _is_structural(s)]
        for stmt in structural:
            if not stmt.lstrip().upper().startswith("CREATE TABLE"):
                self._apply(report, "index", stmt)

        # ---- 4. copy data -----------------------------------------------------
        if self.copy_data:
            for table in schema.all_tables_in_dependency_order():
                self._copy_table(report, table)

        # ---- 5. referential + object DDL ---------------------------------------
        # Views and routines may reference tables by bare name (T-SQL defaults
        # to the dbo schema); point PostgreSQL's search_path at the migrated
        # schemas so those references resolve.
        if self.target.dialect == "postgres" and schemas:
            self.target.execute("SET search_path = " + ", ".join(self.target.quote_ident(s) for s in schemas))

        for stmt in ddl:
            if _is_structural(stmt):
                continue
            kind = "constraint" if _is_fk_or_check(stmt) else _object_kind(stmt)
            self._apply(report, kind, stmt)

        # ---- 6. verify ----------------------------------------------------------
        report.verification = self._verify(schema)

        report.finished_at = _now()
        report.success = not any(r.status == "failed" for r in report.schema_results + report.data_results)
        report.summary = {
            "status": "completed" if report.success else "completed-with-errors",
            "tables": len(schema.tables),
            "views": len(schema.views),
            "functions": len(schema.functions),
            "procedures": len(schema.procedures),
            "triggers": len(schema.triggers),
            "rows_copied": sum(r.rows_copied for r in report.data_results),
            "rows_failed": sum(r.rows_failed for r in report.data_results),
            "schema_failed": sum(1 for r in report.schema_results if r.status == "failed"),
            "data_failed": sum(1 for r in report.data_results if r.status == "failed"),
            "warnings": len(report.warnings),
        }
        return report

    # ------------------------------------------------------------------
    def _apply_table(self, report: MigrationReport, schema, table) -> None:
        from engine.translators.sql_builder import build_table_ddl
        stmts, warnings = build_table_ddl(table, self.target.dialect, schema.dialect)
        report.warnings.extend(warnings)
        for stmt in stmts:
            self._apply(report, "table", stmt, object_name=table.name)

    def _copy_table(self, report: MigrationReport, table) -> None:
        mover = DataMigration(self.source, self.target, table)
        result = mover.run()
        obj = ObjectResult(
            kind="data", name=table.name,
            status="success" if not result["errors"] else "failed",
            rows_copied=result["rows_copied"], rows_failed=result["rows_failed"],
            detail="; ".join(result["errors"][:3]),
        )
        report.data_results.append(obj)

    def _apply(self, report: MigrationReport, kind: str, stmt: str, object_name: str | None = None) -> None:
        name = object_name or _name_from_stmt(stmt)
        try:
            self.target.execute(stmt)
            report.schema_results.append(ObjectResult(kind=kind, name=name))
        except ConnectorError as exc:
            report.schema_results.append(
                ObjectResult(kind=kind, name=name, status="failed", detail=str(exc)[:300])
            )

    # ------------------------------------------------------------------
    def _verify(self, schema) -> list[dict]:
        results = []
        for table in schema.tables:
            try:
                src_count = self.source.count_rows(table.name)
            except ConnectorError:
                src_count = None
            try:
                tgt_count = self.target.count_rows(table.name)
            except ConnectorError:
                tgt_count = None
            results.append({
                "table": table.name,
                "source_rows": src_count,
                "target_rows": tgt_count,
                "match": src_count == tgt_count,
            })
        return results


def _object_kind(stmt: str) -> str:
    upper = stmt.lstrip().upper()
    for kind in ("CREATE OR ALTER VIEW", "CREATE VIEW", "CREATE OR REPLACE FUNCTION",
                 "CREATE FUNCTION", "CREATE OR REPLACE PROCEDURE", "CREATE PROCEDURE",
                 "CREATE OR ALTER PROCEDURE", "CREATE TRIGGER"):
        if upper.startswith(kind):
            return kind.split()[-1].lower()
    return "object"


def _name_from_stmt(stmt: str) -> str:
    # grab the first identifier after CREATE <kind>
    m = __import__("re").match(r"^\s*CREATE(?:\s+\w+){1,2}\s+(?:\"?\[?\w+\]?\"?\.)?\"?\[?([\w\d_]+)", stmt)
    return m.group(1) if m else stmt[:40]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
