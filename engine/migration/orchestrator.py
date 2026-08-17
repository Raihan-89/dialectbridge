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
import logging
from time import monotonic

from engine.connectors.base import ConnectorError
from engine.migration.data_mover import DataMigration
from engine.translators.sql_builder import build_database_ddl

_PRE_DATA_PREFIXES = ("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX",
                      "ALTER TABLE")
_FK_PREFIXES = ("ALTER TABLE",)
_PRE_TABLE_PREFIXES = ("CREATE DOMAIN", "CREATE TYPE", "CREATE SEQUENCE")
logger = logging.getLogger("dialectbridge.migration")


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
        or upper.startswith("CREATE UNIQUE INDEX") \
        or upper.startswith("CREATE CLUSTERED INDEX") \
        or upper.startswith("CREATE UNIQUE CLUSTERED INDEX")


def _is_pre_table(stmt: str) -> bool:
    upper = stmt.lstrip().upper()
    return any(upper.startswith(p) for p in _PRE_TABLE_PREFIXES)


def _is_fk_or_check(stmt: str) -> bool:
    upper = stmt.lstrip().upper()
    return upper.startswith("ALTER TABLE") and (
        "FOREIGN KEY" in upper or "CHECK" in upper
    )


class MigrationOrchestrator:
    def __init__(self, source, target, copy_data: bool = True, reset_target: bool = False,
                 progress_callback=None):
        self.source = source
        self.target = target
        self.copy_data = copy_data
        self.reset_target = reset_target
        self.progress_callback = progress_callback

    def _progress(self, percent: int, stage: str) -> None:
        if self.progress_callback:
            self.progress_callback(percent, stage)

    def run(self) -> MigrationReport:
        started = monotonic()
        logger.info(
            "Migration started source=%s target=%s copy_data=%s reset_target=%s",
            self.source.database, self.target.database, self.copy_data, self.reset_target,
        )
        self._progress(5, "Connecting and extracting source schema")
        report = MigrationReport(
            source_db=self.source.database, target_db=self.target.database,
            started_at=_now(),
        )

        # ---- 1. extract ----------------------------------------------------
        try:
            schema = self.source.extract_schema()
        except ConnectorError as exc:
            logger.error("Schema extraction failed source=%s error=%s", self.source.database, exc)
            report.success = False
            report.warnings.append(f"Schema extraction failed: {exc}")
            report.finished_at = _now()
            report.summary = {"status": "failed", "reason": "extraction"}
            return report

        report.warnings.extend(schema.warnings)
        if schema.warnings:
            logger.warning("Schema extraction completed with %d warning(s)", len(schema.warnings))

        # ---- 2. convert -----------------------------------------------------
        self._progress(20, "Converting schema and database objects")
        ddl, conv_warnings = build_database_ddl(schema, self.target.dialect)
        report.warnings.extend(conv_warnings)
        if conv_warnings:
            logger.warning("Schema conversion completed with %d warning(s)", len(conv_warnings))

        # ---- 3. apply structural DDL -----------------------------------------
        self._progress(35, "Creating schemas, tables, and indexes")
        schemas = sorted({name.split(".")[0] for t in schema.tables for name in (t.name,)})

        # Optional destructive reset: drop the target objects we are about to
        # recreate so a previous run does not collide. The user opts in.
        if self.reset_target and schemas:
            if self.target.dialect == "postgres":
                for s in schemas:
                    self.target.execute(f"DROP SCHEMA IF EXISTS {self.target.quote_ident(s)} CASCADE")
            else:
                self._reset_tsql_target(schemas)

        for s in schemas:
            if self.target.dialect == "postgres":
                stmt = f"CREATE SCHEMA IF NOT EXISTS {self.target.quote_ident(s)}"
            else:
                # SQL Server has no CREATE SCHEMA IF NOT EXISTS; dbo always
                # exists and re-runs must be tolerant.
                stmt = (
                    f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'{s.replace(chr(39), chr(39)+chr(39))}') "
                    f"EXEC(N'CREATE SCHEMA {self.target.quote_ident(s)}')"
                )
            self._apply(report, "schema", stmt, object_name=s)

        # Types and sequences must exist before the tables that reference them.
        for stmt in ddl:
            if _is_pre_table(stmt):
                self._apply(report, _object_kind(stmt), stmt)

        for table in schema.all_tables_in_dependency_order():
            self._apply_table(report, schema, table)

        structural = [s for s in ddl if _is_structural(s)]
        for stmt in structural:
            if not stmt.lstrip().upper().startswith("CREATE TABLE"):
                self._apply(report, "index", stmt)

        # ---- 4. copy data -----------------------------------------------------
        self._progress(55, "Copying table data")
        if self.copy_data:
            for table in schema.all_tables_in_dependency_order():
                self._copy_table(report, table)

        # ---- 5. referential + object DDL ---------------------------------------
        self._progress(75, "Creating constraints, views, routines, and triggers")
        # Views and routines may reference tables by bare name (T-SQL defaults
        # to the dbo schema); point PostgreSQL's search_path at the migrated
        # schemas so those references resolve.
        if self.target.dialect == "postgres" and schemas:
            self.target.execute("SET search_path = " + ", ".join(self.target.quote_ident(s) for s in schemas))

        for stmt in ddl:
            if _is_structural(stmt) or _is_pre_table(stmt):
                continue
            kind = "constraint" if _is_fk_or_check(stmt) else _object_kind(stmt)
            self._apply(report, kind, stmt)

        # ---- 6. verify ----------------------------------------------------------
        self._progress(90, "Verifying migrated row counts")
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
        logger.info(
            "Migration finished status=%s rows_copied=%d rows_failed=%d warnings=%d duration_seconds=%.1f",
            report.summary["status"], report.summary["rows_copied"],
            report.summary["rows_failed"], report.summary["warnings"], monotonic() - started,
        )
        self._progress(98, "Saving migration report")
        return report

    # ------------------------------------------------------------------
    def _reset_tsql_target(self, schemas: list[str]) -> None:
        """Drop user objects in the given T-SQL schemas so a re-run is clean.

        `dbo` (and other owner schemas) cannot themselves be dropped, so the
        objects inside them are removed instead: foreign keys first, then
        tables, then views / functions / procedures / triggers.
        """
        in_list = ", ".join(f"N'{s.replace(chr(39), chr(39) + chr(39))}'" for s in schemas)

        fks = self.target.fetch(
            f"""
            SELECT QUOTENAME(SCHEMA_NAME(t.schema_id)) + '.' + QUOTENAME(t.name),
                   QUOTENAME(fk.name)
            FROM sys.foreign_keys fk
            JOIN sys.tables t ON t.object_id = fk.parent_object_id
            WHERE SCHEMA_NAME(t.schema_id) IN ({in_list})
            """
        )
        for parent_table, fk_name in fks:
            self.target.execute(f"ALTER TABLE {parent_table} DROP CONSTRAINT {fk_name}")

        while True:
            rows = self.target.fetch(
                f"""
                SELECT TOP (1) QUOTENAME(SCHEMA_NAME(t.schema_id)) + '.' + QUOTENAME(t.name)
                FROM sys.tables t
                WHERE SCHEMA_NAME(t.schema_id) IN ({in_list})
                """
            )
            if not rows:
                break
            self.target.execute(f"DROP TABLE {rows[0][0]}")

        for type_code, kind in (("'V'", "view"), ("'P'", "procedure"),
                                ("'FN','IF','TF'", "function"), ("'TR'", "trigger"),
                                ("'SO'", "sequence")):
            rows = self.target.fetch(
                f"""
                SELECT QUOTENAME(SCHEMA_NAME(o.schema_id)) + '.' + QUOTENAME(o.name)
                FROM sys.objects o
                WHERE o.type IN ({type_code})
                    AND SCHEMA_NAME(o.schema_id) IN ({in_list})
                """
            )
            for (obj_name,) in rows:
                self.target.execute(f"DROP {kind.upper()} {obj_name}")

        # user-defined types (alias types / domains recreated by the migration)
        rows = self.target.fetch(
            f"""
            SELECT QUOTENAME(SCHEMA_NAME(t.schema_id)) + '.' + QUOTENAME(t.name)
            FROM sys.types t
            WHERE t.is_user_defined = 1
                AND SCHEMA_NAME(t.schema_id) IN ({in_list})
            """
        )
        for (obj_name,) in rows:
            self.target.execute(f"DROP TYPE {obj_name}")

        # database users (migration recreates them with WITHOUT LOGIN). The
        # current connection's principal is preserved so the run can continue.
        rows = self.target.fetch(
            f"""
            SELECT QUOTENAME(name)
            FROM sys.database_principals
            WHERE type IN ('S', 'U')
                AND name NOT IN ('dbo', 'guest', 'INFORMATION_SCHEMA', 'sys')
                AND name <> USER_NAME()
            """
        )
        for (obj_name,) in rows:
            self.target.execute(f"DROP USER {obj_name}")

        # application/database roles created by the migration. Fixed roles are
        # skipped and members are dropped alongside their users above.
        rows = self.target.fetch(
            f"""
            SELECT QUOTENAME(name)
            FROM sys.database_principals
            WHERE type = 'R'
                AND name NOT IN ('public', 'db_owner', 'db_accessadmin',
                                 'db_securityadmin', 'db_ddladmin',
                                 'db_backupoperator', 'db_datareader',
                                 'db_datawriter', 'db_denydatareader',
                                 'db_denydatawriter')
            """
        )
        for (obj_name,) in rows:
            self.target.execute(f"DROP ROLE {obj_name}")

    def _apply_table(self, report: MigrationReport, schema, table) -> None:
        from engine.translators.sql_builder import build_table_ddl
        stmts, warnings = build_table_ddl(table, self.target.dialect, schema.dialect)
        report.warnings.extend(warnings)
        for stmt in stmts:
            self._apply(report, "table", stmt, object_name=table.name)

    def _copy_table(self, report: MigrationReport, table) -> None:
        started = monotonic()
        mover = DataMigration(self.source, self.target, table)
        result = mover.run()
        obj = ObjectResult(
            kind="data", name=table.name,
            status="success" if not result["errors"] else "failed",
            rows_copied=result["rows_copied"], rows_failed=result["rows_failed"],
            detail="; ".join(result["errors"][:3]),
        )
        report.data_results.append(obj)
        if obj.status == "failed":
            logger.error(
                "Table copy failed table=%s rows_copied=%d rows_failed=%d error=%s",
                table.name, obj.rows_copied, obj.rows_failed, obj.detail,
            )
        else:
            logger.info(
                "Table copied table=%s rows=%d duration_seconds=%.1f",
                table.name, obj.rows_copied, monotonic() - started,
            )

    def _apply(self, report: MigrationReport, kind: str, stmt: str, object_name: str | None = None) -> None:
        name = object_name or _name_from_stmt(stmt)
        try:
            self.target.execute(stmt)
            report.schema_results.append(ObjectResult(kind=kind, name=name))
        except ConnectorError as exc:
            logger.error("Schema object creation failed kind=%s name=%s error=%s", kind, name, exc)
            report.schema_results.append(
                ObjectResult(
                    kind=kind, name=name, status="failed",
                    detail=f"{exc}; Statement: {stmt}"[:4000],
                )
            )

    # ------------------------------------------------------------------
    def _verify(self, schema) -> list[dict]:
        results = []
        for table in schema.tables:
            try:
                src_count = self.source.count_rows(table.name)
            except ConnectorError as exc:
                logger.warning("Source row-count verification failed table=%s error=%s", table.name, exc)
                src_count = None
            try:
                tgt_count = self.target.count_rows(table.name)
            except ConnectorError as exc:
                logger.warning("Target row-count verification failed table=%s error=%s", table.name, exc)
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
    for kind in ("CREATE OR ALTER VIEW", "CREATE OR REPLACE VIEW", "CREATE VIEW",
                 "CREATE MATERIALIZED VIEW", "CREATE OR REPLACE FUNCTION",
                 "CREATE FUNCTION", "CREATE OR REPLACE PROCEDURE", "CREATE PROCEDURE",
                 "CREATE OR ALTER PROCEDURE", "CREATE TRIGGER", "CREATE EVENT TRIGGER"):
        if upper.startswith(kind):
            base = kind.split()[-1].lower()
            if base == "event":
                return "trigger"
            return base
    for kind in ("CREATE DOMAIN", "CREATE TYPE"):
        if upper.startswith(kind):
            return "type"
    for kind in ("CREATE SEQUENCE",):
        if upper.startswith(kind):
            return "sequence"
    if upper.startswith("SELECT SETVAL"):
        return "sequence"
    for kind in ("CREATE ROLE", "CREATE USER", "ALTER ROLE", "GRANT", "ALTER SEQUENCE"):
        if upper.startswith(kind):
            return "grant"
    return "object"


def _name_from_stmt(stmt: str) -> str:
    # grab the first identifier after CREATE <kind>
    m = __import__("re").match(
        r"^\s*CREATE(?:\s+OR\s+(?:ALTER|REPLACE))?\s+"
        r"(?:VIEW|FUNCTION|PROCEDURE|TRIGGER)\s+"
        r"(?:\"?\[?\w+\]?\"?\.)?\"?\[?([\w\d_]+)", stmt,
        flags=__import__("re").IGNORECASE,
    )
    return m.group(1) if m else stmt[:40]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
