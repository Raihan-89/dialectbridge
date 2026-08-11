"""
Bridge between Django models and the migration engine.

Builds live connectors from saved DatabaseConnection records, runs the
MigrationOrchestrator, and turns its report into plain dicts for storage
and the API.
"""
from engine.connectors.base import ConnectorError
from engine.connectors import build_connector
from engine.migration.orchestrator import MigrationOrchestrator
from .models import DatabaseConnection


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
