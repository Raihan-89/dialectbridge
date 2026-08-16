"""
Bridge between Django models and the migration engine.

Builds live connectors from saved DatabaseConnection records, runs the
MigrationOrchestrator, and turns its report into plain dicts for storage
and the API.
"""
from engine.connectors.base import ConnectorError
from engine.connectors import build_connector
from engine.migration.orchestrator import MigrationOrchestrator
from engine.translators.sql_builder import build_database_ddl
from .models import DatabaseConnection, MigrationError, MigrationJob


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


def run_migration(source: DatabaseConnection, target: DatabaseConnection,
                  copy_data: bool = True, reset_target: bool = False,
                  progress_callback=None) -> dict:
    """Run a full migration and return the serialized report dict."""
    source_conn = connector_for(source)
    target_conn = connector_for(target)
    try:
        report = MigrationOrchestrator(
            source_conn, target_conn, copy_data=copy_data, reset_target=reset_target,
            progress_callback=progress_callback,
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
