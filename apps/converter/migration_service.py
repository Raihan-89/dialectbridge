"""
Bridge between Django models and the migration engine.

Builds live connectors from saved DatabaseConnection records, runs the
MigrationOrchestrator, and turns its report into plain dicts for storage
and the API.
"""
import logging
import os

from engine.connectors.base import ConnectorError
from engine.connectors import build_connector
from engine.migration.orchestrator import MigrationOrchestrator
from engine.translators.sql_builder import build_database_ddl
from .models import DatabaseConnection, MigrationError, MigrationJob

logger = logging.getLogger("dialectbridge.migration")


def connector_for(connection: DatabaseConnection):
    """Instantiate a connector for a saved DatabaseConnection (not connected)."""
    return build_connector(
        dialect=connection.engine,
        host=connection.host,
        port=connection.effective_port(),
        database=connection.database,
        user=connection.username,
        password=connection.get_password(),
    )


def test_connection(connection: DatabaseConnection) -> dict:
    """Return server/version info, or raise ConnectorError on failure."""
    connector = connector_for(connection)
    try:
        version = connector.test()
        return {"ok": True, "server": version}
    finally:
        connector.close()


def _default_copy_workers() -> int:
    """How many copy units (whole tables, or slices of a big one) run at once.

    Row building and COPY formatting are CPU-bound Python, so worker processes
    scale nearly linearly while threads do not. Half the cores capped at 4 was
    chosen to leave headroom for the two database servers, which usually share
    the box — but on a 6-core machine that is 3 workers, and a worker spends a
    good part of its time blocked on the source read rather than burning CPU.
    Leaving two cores for the servers uses the box better without starving
    them. ``DIALECTBRIDGE_COPY_WORKERS`` overrides this entirely; lower it if
    the database servers are the bottleneck, raise it if they are remote.
    """
    override = os.environ.get("DIALECTBRIDGE_COPY_WORKERS")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            logger.warning("Ignoring invalid DIALECTBRIDGE_COPY_WORKERS=%r", override)
    return max(2, min(8, (os.cpu_count() or 4) - 2))


def run_migration(source: DatabaseConnection, target: DatabaseConnection,
                  copy_data: bool = True, reset_target: bool = False,
                  progress_callback=None, cancel_check=None,
                  parallel_workers: int | None = None) -> dict:
    """Run a full migration and return the serialized report dict.

    ``cancel_check`` is a zero-arg callable returning True when the user
    wants the migration to stop as soon as possible.
    """
    source_conn = connector_for(source)
    target_conn = connector_for(target)
    workers = _default_copy_workers() if parallel_workers is None else parallel_workers
    try:
        report = MigrationOrchestrator(
            source_conn, target_conn, copy_data=copy_data, reset_target=reset_target,
            progress_callback=progress_callback, cancel_check=cancel_check,
            parallel_workers=workers,
        ).run()
        return report.to_dict()
    finally:
        source_conn.close()
        target_conn.close()


def assess_migration(source: DatabaseConnection, target: DatabaseConnection) -> dict:
    """Perform a read-only compatibility assessment and generate target DDL."""
    source_conn = connector_for(source)
    target_conn = connector_for(target)
    try:
        source_schema = source_conn.extract_schema()
        target_version = target_conn.test()
        dialect = "tsql" if target.engine == DatabaseConnection.Engine.MSSQL else "postgres"
        statements, conversion_warnings = build_database_ddl(source_schema, dialect)
        counts = {
            name: len(getattr(source_schema, name))
            for name in ("tables", "views", "functions", "procedures", "triggers", "sequences", "types", "synonyms")
        }
        blockers = [warning for warning in source_schema.warnings + conversion_warnings
                    if "no clean" in warning.lower() or "skipped" in warning.lower() or "could not" in warning.lower()]
        return {
            "source_database": source.database,
            "target_database": target.database,
            "target_version": target_version,
            "counts": counts,
            "statement_count": len(statements),
            "warnings": source_schema.warnings + conversion_warnings,
            "blockers": blockers,
            "readiness": "blocked" if blockers else ("review" if source_schema.warnings or conversion_warnings else "ready"),
            "ddl": "\n\n".join(statement.rstrip(";") + ";" for statement in statements),
        }
    finally:
        source_conn.close()
        target_conn.close()


def record_migration_errors(job: MigrationJob, report: dict) -> int:
    """Persist every failed object from a migration report as a MigrationError.

    Returns the number of errors recorded. Re-running is idempotent per job
    (existing rows are replaced) so a re-run never duplicates rows.
    """
    job.errors.all().delete()

    created = 0
    for result in report.get("schema_results", []) + report.get("data_results", []):
        if result.get("status") != "failed":
            continue
        detail = result.get("detail") or ""
        kind = result.get("kind") or "object"
        if kind not in {c[0] for c in MigrationError.ObjectKind.choices}:
            kind = MigrationError.ObjectKind.OTHER
        if result.get("rows_failed") and result.get("rows_failed") > 0:
            error_type = "data_copy"
        else:
            error_type = "sql_error"
        MigrationError.objects.create(
            job=job,
            object_kind=kind,
            object_name=result.get("name") or "",
            error_type=error_type,
            message=detail[:2000] or "Object failed without a detail message.",
            detail=detail[:5000],
        )
        created += 1
    return created
