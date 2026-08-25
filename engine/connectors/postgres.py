"""
PostgreSQL connector built on psycopg2.
"""
from __future__ import annotations

from typing import Iterator
import io
import re
from uuid import UUID

import psycopg2
import psycopg2.extras
import psycopg2.extensions

from engine.connectors.base import ConnectorError, DatabaseConnector, to_int
from engine.extractors.postgres import extract_schema


def _adapt_uuid(uuid_obj: UUID) -> psycopg2.extensions.AsIs:
    return psycopg2.extensions.AsIs("'%s'::uuid" % (str(uuid_obj),))


psycopg2.extensions.register_adapter(UUID, _adapt_uuid)


# COPY TEXT reserves these four characters; everything else goes out verbatim.
_COPY_SPECIALS = re.compile(r"[\\\t\n\r]")


class _CopyTextStream(io.TextIOBase):
    """Expose row batches as a bounded text stream for psycopg2 COPY.

    ``copy_expert`` pulls small chunks through ``read()``.  Keeping only the
    unread tail here avoids assembling a whole (potentially multi-hundred-GB)
    table in a ``StringIO`` before PostgreSQL starts receiving it.

    A whole batch is rendered at once: formatting used to cost one Python
    function call per *value* plus a generator step and a buffer refill per
    *row*, which dominated the migration (8M calls for a 1M-row table). Now the
    per-row work is a single list comprehension and the buffer is refilled once
    per batch.
    """

    def __init__(self, batches: Iterator, escape=None):
        self._batches = iter(batches)
        self._escape = escape or PostgresConnector._copy_escape
        self._pending = ""
        self._offset = 0
        self.rows_read = 0

    def readable(self) -> bool:
        return True

    def _render(self, batch) -> str:
        """Format one batch of rows into COPY TEXT lines."""
        specials = _COPY_SPECIALS.search
        lines = []
        for row in batch:
            cells = []
            for value in row:
                if value is None:
                    cells.append("\\N")
                elif type(value) is str:
                    # The common case: only pay for escaping when a reserved
                    # character is actually present.
                    cells.append(
                        value if specials(value) is None else
                        value.replace("\\", "\\\\").replace("\t", "\\t")
                             .replace("\n", "\\n").replace("\r", "\\r")
                    )
                elif type(value) is int or type(value) is float:
                    cells.append(str(value))
                elif isinstance(value, (bytes, bytearray, memoryview)):
                    # COPY TEXT consumes one escaping layer before BYTEA parses
                    # the value.  Send two backslashes so BYTEA receives its
                    # required ``\x<hex>`` form instead of COPY decoding
                    # ``\xff`` into an invalid raw UTF-8 byte.
                    cells.append("\\\\x" + bytes(value).hex())
                else:
                    text = str(value)
                    cells.append(
                        text if specials(text) is None else
                        text.replace("\\", "\\\\").replace("\t", "\\t")
                            .replace("\n", "\\n").replace("\r", "\\r")
                    )
            lines.append("\t".join(cells))
        self.rows_read += len(batch)
        lines.append("")            # trailing newline for the final row
        return "\n".join(lines)

    def _next_chunk(self) -> str:
        """Render batches until there is something to hand back."""
        for batch in self._batches:
            if batch:
                return self._render(batch)
        return ""

    def read(self, size: int = -1) -> str:
        if size == 0:
            return ""
        if size < 0:
            chunks = [self._pending[self._offset:]]
            self._pending = ""
            self._offset = 0
            while True:
                chunk = self._next_chunk()
                if not chunk:
                    break
                chunks.append(chunk)
            return "".join(chunks)

        chunks = []
        remaining = size
        while remaining:
            available = len(self._pending) - self._offset
            if available:
                take = min(remaining, available)
                chunks.append(self._pending[self._offset:self._offset + take])
                self._offset += take
                remaining -= take
                if self._offset < len(self._pending):
                    break
                self._pending = ""
                self._offset = 0
                continue
            chunk = self._next_chunk()
            if not chunk:
                break
            self._pending = chunk
            self._offset = 0
        return "".join(chunks)


def _key_range_predicate(key_range) -> tuple[str, list]:
    """Render a half-open ``[lo, hi)`` bound on one key column."""
    if not key_range:
        return "", []
    column, low, high = key_range
    conds, params = [], []
    if low is not None:
        conds.append(f'"{column}" >= %s')
        params.append(low)
    if high is not None:
        conds.append(f'"{column}" < %s')
        params.append(high)
    return " AND ".join(conds), params


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

    def iter_table_rows(self, table_name, columns, order_columns, batch_size=5000,
                        int_columns=None, key_range=None):
        qident = ", ".join(f'"{c}"' for c in columns)
        order_clause = ""
        if order_columns:
            order_clause = " ORDER BY " + ", ".join(f'"{c}"' for c in order_columns)
        tbl = self.quote_ident(table_name)
        # See MSSQLConnector.iter_table_rows — one half-open slice of the key so
        # several workers can read disjoint parts of one large table at once.
        range_sql, range_params = _key_range_predicate(key_range)

        if not order_columns:
            # Keep one cursor open and stream the complete result. OFFSET-based
            # pagination is neither stable nor safe for keyless tables, and the
            # former single LIMIT silently omitted every row after batch one.
            self._ensure_conn()
            try:
                with self._conn.cursor() as cur:
                    if range_sql:
                        cur.execute(f"SELECT {qident} FROM {tbl} WHERE {range_sql}",
                                    tuple(range_params))
                    else:
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
                params = list(last_key)
                if range_sql:
                    conds = f"{conds} AND {range_sql}"
                    params.extend(range_params)
                sql = (
                    f"SELECT {qident} FROM {tbl} WHERE {conds} {order_clause} "
                    f"LIMIT {batch_size}"
                )
                rows = self.fetch(sql, tuple(params))
            else:
                if range_sql:
                    sql = (f"SELECT {qident} FROM {tbl} WHERE {range_sql} "
                           f"{order_clause} LIMIT {batch_size}")
                    rows = self.fetch(sql, tuple(range_params))
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

    def approx_table_stats(self) -> dict[str, tuple]:
        """``{table: (rows, bytes)}`` planner estimates — see the MSSQL twin."""
        rows = self.fetch(
            "SELECT n.nspname, c.relname, c.reltuples::bigint, "
            "pg_total_relation_size(c.oid) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p')"
        )
        return {f"{schema}.{table}".lower(): (max(0, to_int(count or 0)), to_int(size or 0))
                for schema, table, count, size in rows}

    def key_bounds(self, table_name: str, column: str) -> tuple | None:
        row = self.fetchone(
            f'SELECT MIN("{column}"), MAX("{column}") FROM {self.quote_ident(table_name)}'
        )
        if not row or row[0] is None or row[1] is None:
            return None
        return to_int(row[0]), to_int(row[1])

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
        stream = _CopyTextStream(rows, self._copy_escape)
        try:
            with self._conn.cursor() as cur:
                cur.copy_expert(
                    f"COPY {tbl} ({col_list}) FROM STDIN",
                    stream,
                    size=1024 * 1024,
                )
        except psycopg2.Error as exc:
            raise ConnectorError(f"PostgreSQL COPY failed: {exc}") from exc
        return stream.rows_read

    @staticmethod
    def _copy_escape(v):
        """Escape a value for PostgreSQL COPY TEXT format."""
        if v is None:
            return "\\N"
        if isinstance(v, (bytes, bytearray, memoryview)):
            # COPY TEXT consumes one escaping layer before BYTEA parses the
            # value.  Send two backslashes so BYTEA receives its required
            # ``\x<hex>`` representation instead of COPY decoding ``\xff``
            # into an invalid raw UTF-8 byte.
            return "\\\\x" + bytes(v).hex()
        s = str(v)
        return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")

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
