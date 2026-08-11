from engine.connectors.base import ConnectorError, DatabaseConnector
from engine.connectors.mssql import MSSQLConnector
from engine.connectors.postgres import PostgresConnector

__all__ = ["ConnectorError", "DatabaseConnector", "MSSQLConnector", "PostgresConnector", "build_connector"]


def build_connector(dialect: str, host: str, port: int, database: str, user: str, password: str) -> DatabaseConnector:
    """Instantiate the right connector for a dialect name.

    Accepts both the engine names ("mssql"/"postgres") and the internal
    dialect names ("tsql"/"postgres").
    """
    if dialect in ("mssql", "tsql"):
        return MSSQLConnector(host, port or 1433, database, user, password)
    if dialect in ("postgres",):
        return PostgresConnector(host, port or 5432, database, user, password)
    raise ConnectorError(f"Unknown database dialect: {dialect!r}")
