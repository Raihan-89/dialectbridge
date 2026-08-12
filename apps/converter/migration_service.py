"""
Bridge between Django models and the migration engine.

Builds live connectors from saved DatabaseConnection records, runs the
MigrationOrchestrator, and turns its report into plain dicts for storage
and the API.
"""
from engine.connectors.base import ConnectorError
from engine.connectors import build_connector
from engine.migration.orchestrator import MigrationOrchestrator
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
                  copy_data: bool = True, reset_target: bool = False) -> dict:
    """Run a full migration and return the serialized report dict."""
    source_conn = connector_for(source)
    target_conn = connector_for(target)
    try:
        report = MigrationOrchestrator(
            source_conn, target_conn, copy_data=copy_data, reset_target=reset_target
        ).run()
        return report.to_dict()
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
