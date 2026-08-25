"""
MSSQL (SQL Server) connector built on pymssql.
"""
from __future__ import annotations

from typing import Iterator

import pymssql

from engine.connectors.base import ConnectorError, DatabaseConnector, to_int
from engine.extractors.mssql import extract_schema


def _key_range_predicate(key_range) -> tuple[str, list]:
    """Render a half-open ``[lo, hi)`` bound on one key column."""
    if not key_range:
        return "", []
    column, low, high = key_range
    conds, params = [], []
    if low is not None:
        conds.append(f"[{column}] >= %s")
        params.append(low)
    if high is not None:
        conds.append(f"[{column}] < %s")
        params.append(high)
    return " AND ".join(conds), params


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

    def iter_table_rows(self, table_name, columns, order_columns, batch_size=5000,
                        int_columns=None, key_range=None):
        qident = ", ".join(f"[{c}]" for c in columns)
        order_clause = ""
        if order_columns:
            order_clause = " ORDER BY " + ", ".join(f"[{c}]" for c in order_columns)
        tbl = self.quote_ident(table_name)
        # ``key_range`` restricts this reader to one half-open slice of the
        # table's key, so several workers can copy disjoint slices of one very
        # large table at the same time. It ANDs onto the keyset predicate below
        # and is None for every ordinary whole-table read.
        range_sql, range_params = _key_range_predicate(key_range)
        wanted = {c.lower() for c in (int_columns or [])}
        # Resolve the integer columns to positions once. The old per-value
        # variant ran an isinstance + set lookup for every cell of every row —
        # ~8% of total read time on a wide fact table — even though only these
        # few columns can ever need repair.
        int_positions = [i for i, c in enumerate(columns) if c.lower() in wanted]
        last_key: tuple | None = None

        def _convert(rows):
            """Repair bigint columns some TDS drivers return as raw bytes.

            When no column can need repair the driver's rows are handed on
            untouched — rebuilding every row into a fresh list cost one
            allocation per row for no benefit.
            """
            if not int_positions:
                return rows
            out = []
            for row in rows:
                row = list(row)
                for i in int_positions:
                    if type(row[i]) is bytes:
                        row[i] = to_int(row[i])
                out.append(row)
            return out

        if not order_columns:
            # A single live cursor streams all rows without requiring a key.
            # The former TOP query returned only the first batch.
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
                        yield _convert(rows)
            except Exception as exc:
                raise ConnectorError(f"SQL Server query failed: {exc}") from exc
            return

        key_indexes = [columns.index(column) for column in order_columns]

        while True:
            if order_columns and last_key is not None:
                # Lexicographic keyset predicate for composite primary keys:
                # a > ? OR (a = ? AND b > ?) OR (... AND c > ?).
                branches, params = [], []
                for index, column in enumerate(order_columns):
                    equal = [f"[{prior}] = %s" for prior in order_columns[:index]]
                    branches.append("(" + " AND ".join(equal + [f"[{column}] > %s"]) + ")")
                    params.extend(last_key[:index])
                    params.append(last_key[index])
                conds = " OR ".join(branches)
                if range_sql:
                    conds = f"({conds}) AND {range_sql}"
                    params.extend(range_params)
                sql = (
                    f"SELECT TOP {batch_size} {qident} FROM {tbl} "
                    f"WHERE {conds} {order_clause}"
                )
                rows = self.fetch(sql, tuple(params))
            elif order_columns:
                if range_sql:
                    sql = f"SELECT TOP {batch_size} {qident} FROM {tbl} WHERE {range_sql} {order_clause}"
                    rows = self.fetch(sql, tuple(range_params))
                else:
                    sql = f"SELECT TOP {batch_size} {qident} FROM {tbl} {order_clause}"
                    rows = self.fetch(sql)
            if not rows:
                break
            rows = _convert(rows)
            exhausted = len(rows) < batch_size
            # Capture the next keyset cursor *before* handing the batch to the
            # consumer, which is free to mutate or retain it.
            if not exhausted:
                last_key = tuple(rows[-1][i] for i in key_indexes)
            yield rows
            if exhausted:
                break

    def count_rows(self, table_name: str) -> int:
        row = self.fetchone(f"SELECT COUNT_BIG(*) FROM {self.quote_ident(table_name)}")
        return to_int(row[0]) if row else 0

    def approx_row_counts(self) -> dict[str, int]:
        """Row-count estimates for every table, from the catalog.

        Used only to decide which tables are big enough to be worth splitting
        across workers. An exact COUNT of every table would itself cost a full
        scan of the largest ones — the very thing this is meant to speed up.
        """
        rows = self.fetch(
            "SELECT s.name, t.name, SUM(p.row_count) "
            "FROM sys.dm_db_partition_stats p "
            "JOIN sys.tables t ON t.object_id = p.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE p.index_id IN (0, 1) "
            "GROUP BY s.name, t.name"
        )
        return {f"{schema}.{table}".lower(): to_int(count or 0) for schema, table, count in rows}

    def key_bounds(self, table_name: str, column: str) -> tuple | None:
        row = self.fetchone(
            f"SELECT MIN([{column}]), MAX([{column}]) FROM {self.quote_ident(table_name)}"
        )
        if not row or row[0] is None or row[1] is None:
            return None
        return to_int(row[0]), to_int(row[1])

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
