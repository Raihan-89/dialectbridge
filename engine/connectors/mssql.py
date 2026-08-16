"""
MSSQL (SQL Server) connector built on pymssql.
"""
from __future__ import annotations

from typing import Iterator

import pymssql

from engine.connectors.base import ConnectorError, DatabaseConnector, to_int
from engine.extractors.mssql import extract_schema


class MSSQLConnector(DatabaseConnector):
    dialect = "tsql"

    def connect(self) -> None:
        try:
            self._conn = pymssql.connect(
                server=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
                login_timeout=10,
                as_dict=False,
                # pymssql otherwise binds Python datetime values using the
                # legacy DATETIME representation, losing PostgreSQL's
                # microseconds even when the target column is DATETIME2.
                use_datetime2=True,
            )
            self._conn.autocommit(True)
        except Exception as exc:  # pymssql raises various exceptions on failure
            raise ConnectorError(f"Cannot connect to SQL Server {self.host}:{self.port}/{self.database}: {exc}") from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def execute(self, sql: str, params=None) -> None:
        self._ensure_conn()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
        except Exception as exc:
            raise ConnectorError(f"SQL Server execute failed: {exc}") from exc

    def execute_many(self, sql: str, params_list: list) -> None:
        self._ensure_conn()
        try:
            with self._conn.cursor() as cur:
                cur.executemany(sql, params_list)
        except Exception as exc:
            raise ConnectorError(f"SQL Server batch execute failed: {exc}") from exc

    def fetch(self, sql: str, params=None) -> list[tuple]:
        self._ensure_conn()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except Exception as exc:
            raise ConnectorError(f"SQL Server query failed: {exc}") from exc

    def fetchone(self, sql: str, params=None) -> tuple | None:
        self._ensure_conn()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        except Exception as exc:
            raise ConnectorError(f"SQL Server query failed: {exc}") from exc

    def _server_version(self) -> str:
        row = self.fetchone("SELECT @@VERSION")
        version = row[0] if row else "unknown"
        return f"SQL Server {self.host}:{self.port} — {version.splitlines()[0]}"

    def _extract_schema(self):
        return extract_schema(self)

    def iter_table_rows(self, table_name, columns, order_columns, batch_size=5000, int_columns=None):
        qident = ", ".join(f"[{c}]" for c in columns)
        order_clause = ""
        if order_columns:
            order_clause = " ORDER BY " + ", ".join(f"[{c}]" for c in order_columns)
        tbl = self.quote_ident(table_name)
        int_columns = {c.lower() for c in (int_columns or [])}
        last_key: tuple | None = None

        def _norm(value, col_name):
            # Some TDS drivers hand back bigint values as raw little-endian
            # bytes; normalize only the columns we know are integers.
            if isinstance(value, bytes) and col_name.lower() in int_columns:
                return to_int(value)
            return value

        while True:
            if order_columns and last_key is not None:
                # keyset pagination on the PK
                conds = " AND ".join(
                    f"([{c}] > %s)" if i == 0 else f"([{c}] = %s)" for i, c in enumerate(order_columns)
                )
                sql = (
                    f"SELECT TOP {batch_size} {qident} FROM {tbl} "
                    f"WHERE {conds} {order_clause}"
                )
                rows = self.fetch(sql, tuple(last_key))
            elif order_columns:
                sql = f"SELECT TOP {batch_size} {qident} FROM {tbl} {order_clause}"
                rows = self.fetch(sql)
            else:
                sql = f"SELECT TOP {batch_size} {qident} FROM {tbl}"
                rows = self.fetch(sql)
                if not rows:
                    break
                yield [[_norm(v, c) for v, c in zip(row, columns)] for row in rows]
                # no ordering column — can't paginate; stop after first batch
                break

            if not rows:
                break
            yield [[_norm(v, c) for v, c in zip(row, columns)] for row in rows]
            if len(rows) < batch_size:
                break
            last_key = tuple(
                _norm(rows[-1][i], order_columns[i]) for i in range(len(order_columns))
            )

    def count_rows(self, table_name: str) -> int:
        row = self.fetchone(f"SELECT COUNT_BIG(*) FROM {self.quote_ident(table_name)}")
        return to_int(row[0]) if row else 0

    def set_identity_insert(self, table_name: str, on: bool) -> None:
        # Only the column-list form avoids needing the table owner in scope.
        self.execute(f"SET IDENTITY_INSERT {self.quote_ident(table_name)} {'ON' if on else 'OFF'}")

    def max_value(self, table_name: str, column: str) -> int | None:
        row = self.fetchone(f"SELECT MAX([{column}]) FROM {self.quote_ident(table_name)}")
        return to_int(row[0]) if row and row[0] is not None else None

    def seed_identity(self, table_name: str, column: str) -> None:
        # Advance the identity counter past the highest loaded value.
        self.execute(f"DBCC CHECKIDENT ('{self.quote_ident(table_name)}', RESEED)")

    def quote_ident(self, name: str) -> str:
        if "." in name:
            return ".".join(f"[{part}]" for part in name.split("."))
        return f"[{name}]"
