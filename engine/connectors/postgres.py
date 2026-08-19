"""
PostgreSQL connector built on psycopg2.
"""
from __future__ import annotations

from typing import Iterator
import io
from uuid import UUID

import psycopg2
import psycopg2.extras
import psycopg2.extensions

from engine.connectors.base import ConnectorError, DatabaseConnector, to_int
from engine.extractors.postgres import extract_schema


def _adapt_uuid(uuid_obj: UUID) -> psycopg2.extensions.AsIs:
    return psycopg2.extensions.AsIs("'%s'::uuid" % (str(uuid_obj),))


psycopg2.extensions.register_adapter(UUID, _adapt_uuid)


class PostgresConnector(DatabaseConnector):
    dialect = "postgres"

    def connect(self) -> None:
        try:
            self._conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                dbname=self.database,
                user=self.user,
                password=self.password,
                connect_timeout=10,
            )
            self._conn.autocommit = True
        except psycopg2.Error as exc:
            raise ConnectorError(f"Cannot connect to PostgreSQL {self.host}:{self.port}/{self.database}: {exc}") from exc

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
        except psycopg2.Error as exc:
            raise ConnectorError(f"PostgreSQL execute failed: {exc}") from exc

    def execute_many(self, sql: str, params_list: list) -> None:
        self._ensure_conn()
        try:
            with self._conn.cursor() as cur:
                cur.executemany(sql, params_list)
        except psycopg2.Error as exc:
            raise ConnectorError(f"PostgreSQL batch execute failed: {exc}") from exc

    def fetch(self, sql: str, params=None) -> list[tuple]:
        self._ensure_conn()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except psycopg2.Error as exc:
            raise ConnectorError(f"PostgreSQL query failed: {exc}") from exc

    def fetchone(self, sql: str, params=None) -> tuple | None:
        self._ensure_conn()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        except psycopg2.Error as exc:
            raise ConnectorError(f"PostgreSQL query failed: {exc}") from exc

    def _server_version(self) -> str:
        row = self.fetchone("SHOW server_version")
        return f"PostgreSQL {self.host}:{self.port}/{self.database} — v{row[0]}"

    def _extract_schema(self):
        return extract_schema(self)

    def iter_table_rows(self, table_name, columns, order_columns, batch_size=5000, int_columns=None):
        qident = ", ".join(f'"{c}"' for c in columns)
        order_clause = ""
        if order_columns:
            order_clause = " ORDER BY " + ", ".join(f'"{c}"' for c in order_columns)
        tbl = self.quote_ident(table_name)

        if not order_columns:
            # Keep one cursor open and stream the complete result. OFFSET-based
            # pagination is neither stable nor safe for keyless tables, and the
            # former single LIMIT silently omitted every row after batch one.
            self._ensure_conn()
            try:
                with self._conn.cursor() as cur:
                    cur.execute(f"SELECT {qident} FROM {tbl}")
                    while True:
                        rows = cur.fetchmany(batch_size)
                        if not rows:
                            break
                        yield rows
            except psycopg2.Error as exc:
                raise ConnectorError(f"PostgreSQL query failed: {exc}") from exc
            return

        key_indexes = [columns.index(column) for column in order_columns]
        last_key: tuple | None = None
        while True:
            if last_key is not None:
                quoted_keys = ", ".join(f'"{column}"' for column in order_columns)
                placeholders = ", ".join(["%s"] * len(order_columns))
                conds = f"({quoted_keys}) > ({placeholders})"
                sql = (
                    f"SELECT {qident} FROM {tbl} WHERE {conds} {order_clause} "
                    f"LIMIT {batch_size}"
                )
                rows = self.fetch(sql, tuple(last_key))
            else:
                sql = f"SELECT {qident} FROM {tbl} {order_clause} LIMIT {batch_size}"
                rows = self.fetch(sql)
            if not rows:
                break
            yield rows
            if len(rows) < batch_size:
                break
            last_key = tuple(rows[-1][i] for i in key_indexes)

    def count_rows(self, table_name: str) -> int:
        row = self.fetchone(f"SELECT COUNT(*) FROM {self.quote_ident(table_name)}")
        return to_int(row[0]) if row else 0

    def set_identity_insert(self, table_name: str, on: bool) -> None:
        # No explicit toggle needed: our converted identity columns are
        # GENERATED BY DEFAULT, so explicit values are always accepted.
        pass

    def max_value(self, table_name: str, column: str) -> int | None:
        row = self.fetchone(f'SELECT MAX("{column}") FROM {self.quote_ident(table_name)}')
        return to_int(row[0]) if row and row[0] is not None else None

    def seed_identity(self, table_name: str, column: str) -> None:
        # pg_get_serial_sequence parses its text argument as a possibly
        # schema-qualified, quoted identifier — quote each part so the
        # original casing (e.g. "dbo"."Customers") resolves correctly.
        qualified = ".".join(f'"{part}"' for part in table_name.split("."))
        seq = self.fetchone("SELECT pg_get_serial_sequence(%s, %s)", (qualified, column))
        if seq and seq[0]:
            self.execute(
                f"SELECT setval('{seq[0]}', (SELECT COALESCE(MAX(\"{column}\"), 0) FROM {qualified}) + 1, false)"
            )

    def quote_ident(self, name: str) -> str:
        if "." in name:
            return ".".join(f'"{part}"' for part in name.split("."))
        return f'"{name}"'

    def copy_to_table(self, table_name: str, columns: list[str], rows: Iterator) -> int:
        """Bulk-load rows via COPY ... FROM STDIN.  Returns rows copied."""
        self._ensure_conn()
        tbl = self.quote_ident(table_name)
        col_list = ", ".join(f'"{c}"' for c in columns)
        total = 0
        try:
            with self._conn.cursor() as cur:
                buf = io.StringIO()
                for batch in rows:
                    for row in batch:
                        line = "\t".join(
                            "\\N" if v is None else str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
                            for v in row
                        )
                        buf.write(line + "\n")
                        total += 1
                buf.seek(0)
                cur.copy_expert(
                    f"COPY {tbl} ({col_list}) FROM STDIN",
                    buf,
                )
        except psycopg2.Error as exc:
            raise ConnectorError(f"PostgreSQL COPY failed: {exc}") from exc
        return total

    def drop_indexes(self, table_name: str) -> list[str]:
        """Drop non-unique, non-PK indexes on *table_name*.  Returns DDL to recreate them."""
        self._ensure_conn()
        tbl = table_name.split(".")[-1]
        rows = self.fetch(
            "SELECT indexdef, schemaname, indexname "
            "FROM pg_indexes "
            "WHERE tablename = %s "
            "AND indexdef NOT LIKE '%%UNIQUE%%' "
            "AND indexdef NOT LIKE '%%PRIMARY%%'",
            (tbl,),
        )
        recreate: list[str] = []
        for indexdef, _schema, indexname in rows:
            self.execute(f"DROP INDEX IF EXISTS {self.quote_ident(indexname)}")
            recreate.append(indexdef.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS"))
        return recreate

    def recreate_indexes(self, ddl_list: list[str]) -> None:
        """Recreate previously dropped indexes."""
        for ddl in ddl_list:
            try:
                self.execute(ddl)
            except Exception:
                pass
