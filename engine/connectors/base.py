"""
Database connector abstraction.

A connector wraps a raw DB-API connection and provides the uniform surface
the extraction and migration pipeline needs: connect/test, DDL execution,
batched reads for data copy, identity handling, and schema extraction.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from engine.schema import Database


class ConnectorError(Exception):
    """Raised when a database cannot be reached, queried, or modified."""


def to_int(value) -> int | None:
    """Coerce a DB-API value to int.

    Tolerates bigint values that some TDS drivers return as raw little-endian
    byte payloads (e.g. b'\\x01\\x00...' == 1) instead of Python ints, plus
    strings and Decimals from other drivers.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        stripped = value.strip()
        try:
            return int(stripped.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return int.from_bytes(stripped, "little", signed=True)
    return int(value)


class DatabaseConnector(ABC):
    """Uniform interface over a source or target database."""

    dialect: str  # "tsql" | "postgres"

    def __init__(self, host: str, port: int, database: str, user: str, password: str):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self._conn = None

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Open the underlying connection; raise ConnectorError on failure."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection if open."""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def test(self) -> str:
        """Return a human-readable server+version string, raising on failure."""
        if self._conn is None:
            self.connect()
        return self._server_version()

    def _ensure_conn(self) -> None:
        """Open the connection on first use (execution or introspection)."""
        if self._conn is None:
            self.connect()

    def extract_schema(self) -> Database:
        """Introspect the connected database into a normalized Database.

        Auto-connects first, mirroring ``test()``, so callers don't have to
        remember to open the connection before extracting.
        """
        if self._conn is None:
            self.connect()
        return self._extract_schema()

    # -- execution ----------------------------------------------------------

    @abstractmethod
    def execute(self, sql: str, params: tuple | list | None = None) -> None:
        """Run SQL that returns no rows (DDL/DML). May contain several statements."""

    @abstractmethod
    def execute_many(self, sql: str, params_list: list) -> None:
        """Run one parameterized statement against many rows of parameters."""

    @abstractmethod
    def fetch(self, sql: str, params: tuple | list | None = None) -> list[tuple]:
        """Run a query and return all rows."""

    @abstractmethod
    def fetchone(self, sql: str, params: tuple | list | None = None) -> tuple | None:
        """Run a query and return the first row."""

    @abstractmethod
    def _server_version(self) -> str:
        """Human-readable server name + version."""

    # -- schema -------------------------------------------------------------

    @abstractmethod
    def _extract_schema(self) -> Database:
        """Introspect the connected database into a normalized Database."""

    # -- data migration -----------------------------------------------------

    @abstractmethod
    def iter_table_rows(self, table_name: str, columns: list[str], order_columns: list[str], batch_size: int, int_columns: list[str] | None = None) -> Iterator[list[tuple]]:
        """Yield batches of rows from `table_name` for the given columns.

        Ordering by the PK is what makes batched SELECT ... WHERE pk > last
        pagination possible; callers fall back to OFFSET when no order columns
        exist. `int_columns` names any integer columns whose values some TDS
        drivers return as raw bytes instead of Python ints, so they can be
        normalized before pagination and downstream inserts.
        """

    @abstractmethod
    def count_rows(self, table_name: str) -> int:
        """Return the number of rows in a table."""

    @abstractmethod
    def set_identity_insert(self, table_name: str, on: bool) -> None:
        """Allow/forbid explicit values into identity columns (no-op if n/a)."""

    @abstractmethod
    def max_value(self, table_name: str, column: str) -> int | None:
        """Return MAX(column) — used to seed target sequences after data load."""

    @abstractmethod
    def seed_identity(self, table_name: str, column: str) -> None:
        """Re-point the table's identity/sequence so future inserts don't collide."""

    # -- helpers ------------------------------------------------------------

    @abstractmethod
    def quote_ident(self, name: str) -> str:
        """Quote a (possibly schema-qualified) identifier for this dialect."""
