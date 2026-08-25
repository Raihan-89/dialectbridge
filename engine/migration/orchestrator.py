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
import re
from time import monotonic

from engine.connectors.base import ConnectorError
from engine.migration.data_mover import DataMigration
from engine.translators.sql_builder import build_database_ddl, pg_ident

_PRE_DATA_PREFIXES = ("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX",
                      "ALTER TABLE")
_FK_PREFIXES = ("ALTER TABLE",)
_PRE_TABLE_PREFIXES = ("CREATE DOMAIN", "CREATE TYPE", "CREATE SEQUENCE")
logger = logging.getLogger("dialectbridge.migration")
_MISSING_RELATION_RE = re.compile(
    r'relation "([^"]+)" does not exist', re.IGNORECASE,
)


def format_duration(seconds: float | None) -> str:
    """Render a copy duration for the report tables ('—' when unknown)."""
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {rest:02d}s"


class MigrationCancelledError(Exception):
    """Raised when a cancellation was requested while the migration runs."""


@dataclass
class ObjectResult:
    kind: str
    name: str
    status: str = "success"          # success | failed | skipped
    detail: str = ""
    rows_copied: int = 0
    rows_failed: int = 0
    duration_seconds: float | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "rows_copied": self.rows_copied,
            "rows_failed": self.rows_failed,
            "duration_seconds": self.duration_seconds,
            "duration_display": format_duration(self.duration_seconds),
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
                 progress_callback=None, cancel_check=None, parallel_workers: int = 1):
        self.source = source
        self.target = target
        self.copy_data = copy_data
        self.reset_target = reset_target
        self.progress_callback = progress_callback
        self.cancel_check = cancel_check
        # 1 keeps the original single-connection loop. Higher values copy that
        # many tables at once in worker processes; the caller decides because
        # only it knows whether the connectors can be rebuilt in a child.
        self.parallel_workers = max(1, int(parallel_workers or 1))
        self._table_progress: dict = {}
        self._source_objects: set[str] = set()
        self._visibility_cache: dict[str, bool] = {}

    def _check_cancelled(self) -> None:
        if self.cancel_check is not None and self.cancel_check():
            raise MigrationCancelledError("Migration cancelled by user")

    # --- relative progress helpers -------------------------------------------

    def _progress(self, percent: int, stage: str) -> None:
        if self.progress_callback:
            self.progress_callback(percent, stage)

    @staticmethod
    def _stamp_elapsed(table_progress: dict) -> None:
        """Attach each table's copy duration in seconds.

        ``table_started``/``table_finished`` come from ``monotonic()``, which is
        the right base for measuring a duration but is not a wall clock. The UI
        cannot subtract the browser's epoch time from it — doing so reported the
        running table as ~56 years old. Only this side knows the monotonic base,
        so the elapsed time is computed here and sent ready to display.
        """
        now = monotonic()
        for entry in table_progress.values():
            started = entry.get("table_started")
            if started is None:
                continue
            finished = entry.get("table_finished")
            end = finished if (entry.get("done") and finished is not None) else now
            entry["elapsed_seconds"] = round(max(0.0, end - started), 3)

    def _progress_phase(self, base: float, weight: float, stage: str, **extra) -> None:
        """Emit progress as a weighted mix across the overall pipeline."""
        pct = min(100, max(0, int(base + weight)))
        if self.progress_callback:
            table_progress = extra.get("table_progress")
            if table_progress:
                self._stamp_elapsed(table_progress)
            data = {"percent": pct, "stage": stage, **extra}
            self.progress_callback(pct, stage, data=data)

    # --------------------------------------------------------------------------
    def run(self) -> MigrationReport:
        started = monotonic()
        logger.info(
            "Migration started source=%s target=%s copy_data=%s reset_target=%s",
            self.source.database, self.target.database, self.copy_data, self.reset_target,
        )
        self._progress(2, "Connecting to source database")
        report = MigrationReport(
            source_db=self.source.database, target_db=self.target.database,
            started_at=_now(),
        )

        # ---- 1. extract ----------------------------------------------------
        self._progress(5, "Extracting source schema")
        try:
            schema = self.source.extract_schema()
        except ConnectorError as exc:
            logger.error("Schema extraction failed source=%s error=%s", self.source.database, exc)
            report.success = False
            report.warnings.append(f"Schema extraction failed: {exc}")
            report.finished_at = _now()
            report.summary = {"status": "failed", "reason": "extraction"}
            return report

        self._source_objects = {
            obj.name.rsplit(".", 1)[-1].lower()
            for group in (schema.tables, schema.views, schema.functions,
                          schema.procedures, schema.sequences, schema.synonyms)
            for obj in group
        }

        report.warnings.extend(schema.warnings)
        if schema.warnings:
            logger.warning("Schema conversion completed with %d warning(s)", len(schema.warnings))

        total_tables = len(schema.tables)
        total_views = len(schema.views)
        total_funcs = len(schema.functions)
        total_procs = len(schema.procedures)
        total_triggers = len(schema.triggers)
        total_sequences = len(schema.sequences)
        total_types = len(schema.types)

        # ---- 2. convert -----------------------------------------------------
        self._check_cancelled()
        self._progress_phase(10, 10, "Converting schema and database objects",
                             total_tables=total_tables, total_views=total_views)
        ddl, conv_warnings = build_database_ddl(schema, self.target.dialect)
        report.warnings.extend(conv_warnings)
        if conv_warnings:
            logger.warning("Schema conversion completed with %d warning(s)", len(conv_warnings))

        # ---- 3. apply structural DDL -----------------------------------------
        self._progress_phase(20, 10, "Creating schemas and tables",
                             total_tables=total_tables)
        schemas = sorted({name.split(".")[0] for t in schema.tables for name in (t.name,)})

        # Optional destructive reset: drop the target objects we are about to
        # recreate so a previous run does not collide. The user opts in.
        if self.reset_target and schemas:
            self._progress_phase(20, 10, "Dropping existing target objects",
                                 total_tables=total_tables)
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

        for idx, table in enumerate(schema.all_tables_in_dependency_order(), 1):
            self._check_cancelled()
            self._progress_phase(20, 10 + (idx / max(total_tables, 1)) * 15,
                                 f"Creating table {idx}/{total_tables}: {table.name}",
                                 tables_created=idx, total_tables=total_tables)
            self._apply_table(report, schema, table)

        # Indexes are deliberately deferred until after COPY. Building them on
        # empty tables and then dropping/recreating them around the load adds
        # work and makes large migrations slower without improving safety.
        deferred_indexes = [
            stmt for stmt in ddl
            if _is_structural(stmt) and not stmt.lstrip().upper().startswith("CREATE TABLE")
        ]

        # ---- 4. copy data -----------------------------------------------------
        self._table_progress = {}
        copy_started = monotonic()
        copy_elapsed = 0.0
        if self.copy_data:
            tables_to_copy = schema.all_tables_in_dependency_order()
            total_to_copy = len(tables_to_copy)

            def _on_table_batch(table_name: str, rows_so_far: int, batch_rows: int, total_expected: int | None) -> None:
                self._check_cancelled()
                tp = self._table_progress.get(table_name, {})
                tp["rows_copied"] = rows_so_far
                tp["current_batch"] = batch_rows
                if total_expected is not None:
                    tp["total_rows"] = total_expected
                self._table_progress[table_name] = tp

                copied_tables = sum(1 for v in self._table_progress.values() if v.get("done"))
                base = 35
                weight = (copied_tables / max(total_to_copy, 1)) * 40
                self._progress_phase(base, weight,
                                     f"Copying data: {table_name} ({rows_so_far} rows)",
                                     tables_copied=copied_tables, total_tables=total_to_copy,
                                     current_table=table_name, current_table_rows=rows_so_far,
                                     current_table_total=total_expected,
                                     table_progress=self._table_progress)

            if self.parallel_workers > 1 and total_to_copy > 1 and self._copy_tables_parallel(
                report, tables_to_copy, _on_table_batch
            ):
                tables_to_copy = []          # handled by the worker pool

            for idx, table in enumerate(tables_to_copy, 1):
                self._check_cancelled()
                self._table_progress[table.name] = {"index": idx, "rows_copied": 0, "done": False, "table_started": monotonic()}
                try:
                    row_count = self.source.count_rows(table.name)
                    self._table_progress[table.name]["total_rows"] = row_count
                except Exception:
                    self._table_progress[table.name]["total_rows"] = None

                self._progress_phase(35, ((idx - 1) / max(total_to_copy, 1)) * 40,
                                     f"Copying data: {table.name} (0 rows)",
                                     tables_copied=idx - 1, total_tables=total_to_copy,
                                     current_table=table.name, current_table_rows=0,
                                     current_table_total=self._table_progress[table.name].get("total_rows"),
                                     table_progress=self._table_progress)

                self._copy_table(report, table, on_batch=_on_table_batch)

                self._table_progress[table.name]["done"] = True
                self._table_progress[table.name]["rows_copied"] = (
                    report.data_results[-1].rows_copied if report.data_results else 0
                )
                self._table_progress[table.name]["table_finished"] = monotonic()

                self._progress_phase(35, (idx / max(total_to_copy, 1)) * 40,
                                     f"Copied {idx}/{total_to_copy} tables",
                                     tables_copied=idx, total_tables=total_to_copy,
                                     current_table=table.name,
                                     current_table_rows=self._table_progress[table.name]["rows_copied"],
                                     table_progress=self._table_progress)

            copy_elapsed = monotonic() - copy_started

        # Build each index once, after bulk data loading.
        if deferred_indexes:
            self._progress_phase(75, 4, "Creating indexes after data load",
                                 total_indexes=len(deferred_indexes))
            for idx, stmt in enumerate(deferred_indexes, 1):
                self._check_cancelled()
                self._apply(report, "index", stmt)
                if idx % 10 == 0 or idx == len(deferred_indexes):
                    self._progress_phase(
                        75, (idx / len(deferred_indexes)) * 4,
                        f"Creating indexes {idx}/{len(deferred_indexes)}",
                        indexes_created=idx, total_indexes=len(deferred_indexes),
                    )

        # ---- 5. referential + object DDL ---------------------------------------
        self._progress_phase(75, 8, "Creating constraints, views, routines, and triggers",
                             total_tables=total_tables)
        # Views and routines may reference tables by bare name (T-SQL defaults
        # to the dbo schema); point PostgreSQL's search_path at the migrated
        # schemas so those references resolve.
        if self.target.dialect == "postgres" and schemas:
            self.target.execute("SET search_path = " + ", ".join(self.target.quote_ident(s) for s in schemas))

        remaining_stmts = [s for s in ddl if not _is_structural(s) and not _is_pre_table(s)]
        for idx, stmt in enumerate(remaining_stmts, 1):
            self._check_cancelled()
            kind = "constraint" if _is_fk_or_check(stmt) else _object_kind(stmt)
            self._apply(report, kind, stmt)
            if idx % 10 == 0 or idx == len(remaining_stmts):
                self._progress_phase(75, 8 + (idx / max(len(remaining_stmts), 1)) * 7,
                                     f"Applying objects {idx}/{len(remaining_stmts)}",
                                     objects_applied=idx, total_objects=len(remaining_stmts))

        # ---- 5b. account for every source object --------------------------------
        # A routine the translator cannot convert produces a warning and *no*
        # statement, so it never reached _apply and never appeared in the report:
        # the object silently vanished, the run showed zero failures, and the
        # target was quietly missing objects the user expected. Reconcile the
        # source inventory against what was actually attempted so nothing can go
        # missing without saying so.
        self._record_unattempted(report, schema)

        # ---- 6. verify ----------------------------------------------------------
        # The per-table copy time is only known here; the live progress payload
        # that carried it is gone once the job finishes, so it is folded into
        # the stored report and stays visible on the report page afterwards.
        copy_times = {r.name: r.duration_seconds for r in report.data_results}
        total_verify = len(schema.tables)
        for idx, table in enumerate(schema.tables, 1):
            self._check_cancelled()
            self._progress_phase(90, (idx / max(total_verify, 1)) * 8,
                                 f"Verifying {idx}/{total_verify}: {table.name}",
                                 verify_index=idx, verify_total=total_verify)
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
            copied = copy_times.get(table.name)
            report.verification.append({
                "table": table.name,
                "source_rows": src_count,
                "target_rows": tgt_count,
                "match": src_count == tgt_count,
                "duration_seconds": copied,
                "duration_display": format_duration(copied),
            })

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
            # Wall-clock span of the copy phase. Summing the per-table times
            # would overcount by the worker count when tables copy in parallel.
            "data_seconds": round(copy_elapsed, 2),
            "data_duration": format_duration(copy_elapsed) if self.copy_data else "—",
            "schema_failed": sum(1 for r in report.schema_results if r.status == "failed"),
            # Skipped objects are not failures, but they are absent from the
            # target. Counting them keeps a clean "0 failures" from reading as
            # "everything migrated".
            "schema_skipped": sum(1 for r in report.schema_results if r.status == "skipped"),
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
                                 'db_backupoperator', 'db_datareader', 'db_datawriter',
                                 'db_denydatareader', 'db_denydatawriter')
            """
        )
        for (obj_name,) in rows:
            self.target.execute(f"DROP ROLE {obj_name}")

    def _apply_table(self, report: MigrationReport, schema, table) -> None:
        from engine.translators.sql_builder import build_table_ddl
        stmts, warnings = build_table_ddl(table, self.target.dialect, schema.dialect, schema)
        report.warnings.extend(warnings)
        for stmt in stmts:
            self._apply(report, "table", stmt, object_name=table.name)
            last_result = report.schema_results[-1] if report.schema_results else None
            if (last_result and last_result.status == "failed"
                    and last_result.kind == "table"
                    and "generation expression is not immutable" in last_result.detail):
                retry_stmts, retry_warnings = build_table_ddl(
                    table, self.target.dialect, schema.dialect, schema,
                    downgrade_computed=True,
                )
                report.warnings.extend(retry_warnings)
                report.warnings.append(
                    f"Table '{table.name}' has non-immutable computed columns — "
                    f"recreated with computed columns as regular columns"
                )
                report.schema_results.pop()
                for retry_stmt in retry_stmts:
                    self._apply(report, "table", retry_stmt, object_name=table.name)

    def _copy_tables_parallel(self, report: MigrationReport, tables, on_batch) -> bool:
        """Copy every table across a process pool.

        Returns True when the pool handled the tables, False when the caller
        should fall back to the sequential loop. A failure to *start* the pool
        is never fatal — the migration simply proceeds as it always did.
        """
        from engine.migration.parallel_copy import (
            ParallelCopyUnavailable, copy_tables,
        )

        total = len(tables)
        for index, table in enumerate(tables, 1):
            self._table_progress[table.name] = {
                "index": index, "rows_copied": 0, "done": False,
                "table_started": monotonic(),
            }

        def _progress(table_name, rows_so_far, batch_rows, total_expected):
            entry = self._table_progress.setdefault(table_name, {})
            entry["rows_copied"] = rows_so_far
            entry["current_batch"] = batch_rows
            if total_expected is not None:
                entry["total_rows"] = total_expected

        def _done(table_name, result, error):
            self._check_cancelled()
            entry = self._table_progress.setdefault(table_name, {})
            entry["done"] = True
            entry["table_finished"] = monotonic()
            if error is not None:
                result = {"rows_copied": 0, "rows_failed": 0, "errors": [error]}
            entry["rows_copied"] = result["rows_copied"]
            self._record_copy_result(report, table_name, result,
                                     entry.get("table_started", monotonic()))
            finished = sum(1 for v in self._table_progress.values() if v.get("done"))
            self._progress_phase(
                35, (finished / max(total, 1)) * 40,
                f"Copied {finished}/{total} tables",
                tables_copied=finished, total_tables=total,
                current_table=table_name, current_table_rows=entry["rows_copied"],
                table_progress=self._table_progress,
            )

        try:
            copy_tables(self.source, self.target, tables,
                        self.parallel_workers, _progress, _done)
        except ParallelCopyUnavailable as exc:
            logger.warning("Parallel copy unavailable (%s) — using a single connection", exc)
            for entry in self._table_progress.values():
                entry["done"] = False
            return False
        logger.info("Parallel copy finished tables=%d workers=%d", total, self.parallel_workers)
        return True

    def _record_copy_result(self, report: MigrationReport, table_name: str,
                            result: dict, started: float) -> None:
        """Fold one table's copy summary into the report (shared by both paths).

        A parallel worker measures its own table and reports it in the summary;
        that figure wins. The parent only knows when the whole phase began, so
        deriving the duration here would charge every table for the time it
        spent queued behind the others.
        """
        measured = result.get("duration_seconds")
        elapsed = (round(max(0.0, measured), 2) if measured is not None
                   else round(max(0.0, monotonic() - started), 2))
        obj = ObjectResult(
            kind="data", name=table_name,
            status="success" if not result["errors"] else "failed",
            rows_copied=result["rows_copied"], rows_failed=result["rows_failed"],
            detail="; ".join(result["errors"][:3]),
            duration_seconds=elapsed,
        )
        report.data_results.append(obj)
        if obj.status == "failed":
            logger.error(
                "Table copy failed table=%s rows_copied=%d rows_failed=%d error=%s",
                table_name, obj.rows_copied, obj.rows_failed, obj.detail,
            )
        else:
            logger.info(
                "Table copied table=%s rows=%d duration_seconds=%.1f",
                table_name, obj.rows_copied, elapsed,
            )

    def _copy_table(self, report: MigrationReport, table, on_batch=None) -> None:
        started = monotonic()
        mover = DataMigration(self.source, self.target, table, progress_callback=on_batch)
        self._record_copy_result(report, table.name, mover.run(), started)

    @staticmethod
    def _bare(name: str) -> str:
        return name.rsplit(".", 1)[-1].strip('"[]').lower()

    @classmethod
    def _name_aliases(cls, name: str) -> set[str]:
        """Every name the target may have used for this source object.

        Currently that is the name itself plus the pg_ident() truncation used
        for identifiers past PostgreSQL's 63-byte limit. pg_ident() is a no-op
        for shorter names, so this collapses to a single alias for almost every
        object.
        """
        raw = name.rsplit(".", 1)[-1].strip('"[]')
        return {raw.lower(), pg_ident(raw).lower()}

    @classmethod
    def _mentions(cls, warning: str, name: str) -> bool:
        """True when *warning* is about this object rather than a longer name
        that merely starts with it.

        A plain substring test filed every warning for
        ``getAttendanceReportNewReportMultipleWages...`` under the shorter
        ``getAttendanceReport`` as well, so one object's report row carried a
        dozen other objects' reasons.
        """
        lowered = warning.lower()
        return any(
            re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", lowered)
            for alias in cls._name_aliases(name)
        )

    def _record_unattempted(self, report: MigrationReport, schema) -> None:
        """Record every source object that produced no target DDL at all.

        Objects are only in the report because a statement was executed for
        them. When conversion fails the builder returns no statement, so the
        object left no trace: not a success, not a failure, just absent. Each
        one is added here as ``skipped`` carrying the warning that explains why,
        so the report's object list always matches the source inventory.
        """
        attempted = {self._bare(r.name) for r in report.schema_results if r.name}
        groups = (
            ("view", schema.views), ("function", schema.functions),
            ("procedure", schema.procedures), ("trigger", schema.triggers),
            ("sequence", schema.sequences), ("type", schema.types),
            ("object", schema.synonyms),
        )
        for kind, objects in groups:
            for obj in objects:
                bare = self._bare(obj.name)
                # A routine whose name is past PostgreSQL's 63-byte limit is
                # created under the pg_ident()-shortened name, so the report
                # row carries the short name while the source inventory still
                # holds the long one. Without the alias the object looked
                # unattempted and was reported "skipped" even though it had
                # migrated successfully.
                if attempted.intersection(self._name_aliases(obj.name)):
                    continue
                reasons = [w for w in report.warnings if self._mentions(w, obj.name)]
                detail = (
                    " ".join(reasons) if reasons else
                    "No target DDL could be generated for this object, so it was "
                    "never created on the target."
                )
                logger.warning(
                    "Schema object never attempted kind=%s name=%s reason=%s",
                    kind, obj.name, detail,
                )
                report.schema_results.append(
                    ObjectResult(kind=kind, name=obj.name, status="skipped",
                                 detail=detail[:4000])
                )
                attempted.add(bare)

    def _missing_source_dependency(self, message: str) -> tuple | None:
        """Classify a failure caused by a relation the target does not have.

        Returns ``(reference, hidden)``. ``hidden`` is True when the source
        server can still see the object even though it never reached the
        extracted schema — which means it was not dropped at all, the migration
        login simply cannot read it. That distinction matters: a dropped table
        is the user's dead metadata and nothing can be done, while a hidden one
        is a permissions problem they can fix and re-run.
        """
        match = _MISSING_RELATION_RE.search(message)
        if not match:
            return None
        reference = match.group(1)
        if reference.rsplit(".", 1)[-1].lower() in self._source_objects:
            return None
        return reference, self._visible_in_source(reference)

    def _visible_in_source(self, reference: str) -> bool:
        """Ask the source server whether it can see this object at all."""
        if not hasattr(self.source, "object_exists"):
            return False
        if reference in self._visibility_cache:
            return self._visibility_cache[reference]
        try:
            visible = bool(self.source.object_exists(reference))
        except Exception:
            visible = False
        self._visibility_cache[reference] = visible
        return visible

    def _apply(self, report: MigrationReport, kind: str, stmt: str, object_name: str | None = None) -> None:
        name = object_name or _name_from_stmt(stmt)
        try:
            self.target.execute(stmt)
            report.schema_results.append(ObjectResult(kind=kind, name=name))
        except ConnectorError as exc:
            missing = self._missing_source_dependency(str(exc))
            if missing:
                reference, hidden = missing
                if hidden:
                    detail = (
                        f"References '{reference}', which exists in the source but was "
                        f"not extracted — the migration login cannot read it. Grant it "
                        f"access to that object and re-run; the object was skipped"
                    )
                else:
                    detail = (
                        f"References '{reference}', which does not exist in the source "
                        f"database — the object was skipped"
                    )
                logger.warning("Schema object skipped kind=%s name=%s reason=%s", kind, name, detail)
                report.schema_results.append(
                    ObjectResult(kind=kind, name=name, status="skipped", detail=detail)
                )
                report.warnings.append(f"{kind.title()} '{name}': {detail}")
                return
            logger.error("Schema object creation failed kind=%s name=%s error=%s", kind, name, exc)
            report.schema_results.append(
                ObjectResult(
                    kind=kind, name=name, status="failed",
                    detail=f"{exc}; Statement: {stmt}"[:4000],
                )
            )


_PG_TRIGGER_BUNDLE_RE = re.compile(
    r"RETURNS\s+TRIGGER\b.*?\bCREATE\s+TRIGGER\b", re.IGNORECASE | re.DOTALL
)
_PG_TRIGGER_NAME_RE = re.compile(
    r"\bCREATE\s+TRIGGER\s+(?:\"([^\"]+)\"|\[([^\]]+)\]|([\w$]+))", re.IGNORECASE
)


def _object_kind(stmt: str) -> str:
    upper = stmt.lstrip().upper()
    # A migrated DML trigger is emitted as one statement holding both the
    # trigger function and the CREATE TRIGGER that binds it. It starts with
    # CREATE OR REPLACE FUNCTION, so matching on the leading keyword alone
    # filed every trigger under "function" and the report showed no triggers
    # at all — the objects migrated, but looked missing.
    if _PG_TRIGGER_BUNDLE_RE.search(stmt):
        return "trigger"
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
    # For the function+CREATE TRIGGER bundle, report the trigger's own name
    # rather than the generated `<trigger>_fn` helper the reader never asked for.
    if _PG_TRIGGER_BUNDLE_RE.search(stmt):
        match = _PG_TRIGGER_NAME_RE.search(stmt)
        if match:
            return next(g for g in match.groups() if g)
    # grab the first identifier after CREATE <kind>
    m = __import__("re").match(
        r"^\s*CREATE(?:\s+OR\s+(?:ALTER|REPLACE))?\s+"
        r"(?:MATERIALIZED\s+VIEW|VIEW|FUNCTION|PROCEDURE|TRIGGER"
        r"|SEQUENCE|DOMAIN|TYPE)\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:\"?\[?\w+\]?\"?\.)?\"?\[?([\w\d_]+)",
        stmt,
        flags=__import__("re").IGNORECASE,
    )
    return m.group(1) if m else stmt[:40]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
