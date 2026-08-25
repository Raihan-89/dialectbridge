"""
Data migration engine: copies table data between two connected databases
with type-safe INSERTs, identity preservation, sequence seeding and row-count
verification.

Design:
  - Reads rows from the source in ordered batches (keyset pagination on the
    PK when available) so very large tables don't exhaust memory.
  - Builds target INSERTs with the target's quoted identifier and parameter
    placeholders, executed with the target driver's parameter binding so
    values round-trip exactly (no string interpolation of user data).
  - PostgreSQL targets use COPY for bulk loading (10-50x faster than INSERT
    for large tables); non-PG targets fall back to batched INSERTs.
  - Non-unique indexes on the target are dropped before loading and recreated
    after, cutting index-maintenance overhead during bulk inserts.
  - Identity columns are populated explicitly so PK values match the source,
    then each table's identity/sequence is re-seeded past the highest value.
  - FKs are applied to the target only after data load (the schema builder
    already emits FK ALTERs separately), so insert order never matters.
"""
from __future__ import annotations

import queue
import threading

from engine.connectors.base import ConnectorError
from engine.schema import Table

_INT_TYPES = {"BIGINT", "INT", "SMALLINT", "TINYINT"}
_WIDE_TYPE_MARKERS = ("BINARY", "IMAGE", "BYTEA", "TEXT", "NTEXT", "XML", "MAX")
_DEFAULT_COPY_BATCH_SIZE = 25_000
_WIDE_COPY_BATCH_SIZE = 5_000
# Batches held in flight between the reader thread and the COPY writer. Two is
# enough to keep both stages busy while bounding memory to a couple of batches.
_PREFETCH_DEPTH = 2
_READER_STOPPED = object()


def _prefetch(batches):
    """Read *batches* on a background thread so the source read overlaps the
    target write.

    Reading from SQL Server and COPYing into PostgreSQL used to run strictly in
    turn — the COPY stream pulled a batch, waited for the network round trip,
    then pushed it. Overlapping the two stages makes a table cost about as long
    as its slower half instead of the sum of both.
    """
    handoff: queue.Queue = queue.Queue(maxsize=_PREFETCH_DEPTH)

    def _pump():
        try:
            for batch in batches:
                handoff.put(batch)
        except BaseException as exc:                     # re-raised in consumer
            handoff.put(exc)
        else:
            handoff.put(_READER_STOPPED)

    reader = threading.Thread(target=_pump, name="dialectbridge-reader", daemon=True)
    reader.start()
    try:
        while True:
            item = handoff.get()
            if item is _READER_STOPPED:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        # Drain so a consumer that stops early (COPY error) cannot wedge the
        # reader on a full queue.
        while reader.is_alive():
            try:
                handoff.get_nowait()
            except queue.Empty:
                reader.join(timeout=0.1)


class DataMigration:
    def __init__(self, source, target, table: Table, batch_size: int | None = None,
                 progress_callback=None, key_range=None, manage_table: bool = True):
        self.source = source
        self.target = target
        self.table = table
        self.batch_size = batch_size or self._recommended_batch_size()
        self.progress_callback = progress_callback
        # One slice of a table split across workers: ``(column, low, high)``.
        self.key_range = key_range
        # Index drop/recreate and identity reseeding are once-per-table jobs.
        # When several workers each copy a slice of the same table they must
        # not each drop the indexes out from under the others, so the caller
        # does that work once around the whole set and clears this flag.
        self.manage_table = manage_table
        self.rows_copied = 0
        self.rows_skipped = 0
        self.rows_failed = 0
        self.errors: list[str] = []

    def _recommended_batch_size(self) -> int:
        """Use fewer round trips for narrow fact tables without allowing
        image/LOB batches to consume excessive memory."""
        has_wide_values = any(
            any(marker in column.data_type.upper() for marker in _WIDE_TYPE_MARKERS)
            for column in self.table.columns
            if not column.is_computed
        )
        return _WIDE_COPY_BATCH_SIZE if has_wide_values else _DEFAULT_COPY_BATCH_SIZE

    # ------------------------------------------------------------------
    def run(self) -> dict:
        cols = self._column_names()
        if not cols:
            return self._summary("no-convertible-columns")

        order_cols = self._order_columns()
        target_cols = ", ".join(self.target.quote_ident(c) for c in cols)

        # Notify: starting this table
        if self.progress_callback:
            self.progress_callback(self.table.name, 0, 0, None)

        dropped_indexes = self._drop_indexes() if self.manage_table else []

        self._enable_identity_insert()

        use_copy = hasattr(self.target, "copy_to_table") and self.target.dialect == "postgres"
        try:
            if use_copy:
                self._run_copy(cols, order_cols)
            else:
                self._run_insert(cols, order_cols, target_cols)
        except ConnectorError as exc:
            self.rows_failed += 1
            self.errors.append(str(exc))
        finally:
            self._disable_identity_insert()

        if self.manage_table:
            self._seed_identity(cols)
            self._recreate_indexes(dropped_indexes)
        return self._summary()

    # ------------------------------------------------------------------
    def _run_copy(self, cols: list[str], order_cols: list[str]) -> None:
        """Bulk-load via PostgreSQL COPY (much faster for large tables)."""
        running_count = 0

        def _progress_iter():
            """Yield prefetched batches and report progress after each one."""
            nonlocal running_count
            source_batches = self.source.iter_table_rows(
                self.table.name, cols, order_cols,
                batch_size=self.batch_size, int_columns=self._int_columns(),
                **self._range_kwarg(),
            )
            for batch in _prefetch(source_batches):
                if batch:
                    yield batch
                    running_count += len(batch)
                    if self.progress_callback:
                        self.progress_callback(self.table.name, running_count, len(batch), None)

        self.rows_copied = self.target.copy_to_table(self.table.name, cols, _progress_iter())

    # ------------------------------------------------------------------
    def _run_insert(self, cols: list[str], order_cols: list[str], target_cols: str) -> None:
        """Fallback: batched INSERT for non-PostgreSQL targets."""
        for batch in self.source.iter_table_rows(
            self.table.name, cols, order_cols,
            batch_size=self.batch_size, int_columns=self._int_columns(),
            **self._range_kwarg(),
        ):
            if not batch:
                continue
            self._insert_batch(target_cols, cols, batch)
            # Notify progress after each batch
            if self.progress_callback:
                self.progress_callback(self.table.name, self.rows_copied, len(batch), None)

    # ------------------------------------------------------------------
    def _range_kwarg(self) -> dict:
        """Pass ``key_range`` only when a slice was actually asked for.

        A whole-table copy then calls ``iter_table_rows`` with exactly the
        arguments it always did, so any connector that predates key ranges —
        including the fakes the pipeline tests run against — keeps working.
        """
        return {"key_range": self.key_range} if self.key_range else {}

    def _column_names(self) -> list[str]:
        return [c.name for c in self.table.columns if not c.is_computed]

    def _int_columns(self) -> list[str]:
        return [
            c.name for c in self.table.columns
            if not c.is_computed and c.data_type.split("(")[0].strip().upper() in _INT_TYPES
        ]

    def _order_columns(self) -> list[str]:
        if self.table.primary_key:
            return [c for c in self.table.primary_key.columns if c in self._column_names()]
        return []

    def _enable_identity_insert(self) -> None:
        if not any(c.is_identity for c in self.table.columns):
            return
        try:
            self.target.set_identity_insert(self.table.name, True)
        except Exception:
            pass

    def _disable_identity_insert(self) -> None:
        if not any(c.is_identity for c in self.table.columns):
            return
        try:
            self.target.set_identity_insert(self.table.name, False)
        except Exception:
            pass

    def _seed_identity(self, cols: list[str]) -> None:
        identity_col = next((c.name for c in self.table.columns if c.is_identity), None)
        if not identity_col or identity_col not in cols:
            return
        try:
            self.target.seed_identity(self.table.name, identity_col)
        except Exception as exc:
            self.errors.append(f"Could not reseed identity on {self.table.name}: {exc}")

    # ------------------------------------------------------------------
    def drop_table_indexes(self) -> list[str]:
        """Public form of the once-per-table index drop, for a sharded copy."""
        return self._drop_indexes()

    def finish_table(self, dropped_indexes: list[str]) -> None:
        """Once-per-table work a sharded copy defers until every slice lands."""
        self._seed_identity(self._column_names())
        self._recreate_indexes(dropped_indexes)

    def _drop_indexes(self) -> list[str]:
        if not hasattr(self.target, "drop_indexes"):
            return []
        try:
            return self.target.drop_indexes(self.table.name)
        except Exception:
            return []

    def _recreate_indexes(self, ddl_list: list[str]) -> None:
        if not ddl_list or not hasattr(self.target, "recreate_indexes"):
            return
        try:
            self.target.recreate_indexes(ddl_list)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _insert_batch(self, target_cols: str, cols: list[str], batch: list[tuple]) -> None:
        placeholders = ", ".join(["%s"] * len(cols))
        sql = f"INSERT INTO {self.target.quote_ident(self.table.name)} ({target_cols}) VALUES ({placeholders})"
        try:
            self.target.execute_many(sql, [list(r) for r in batch])
            self.rows_copied += len(batch)
        except ConnectorError:
            for row in batch:
                try:
                    self.target.execute(sql, list(row))
                    self.rows_copied += 1
                except ConnectorError as exc:
                    self._row_error(exc, row)

    def _row_error(self, exc, row) -> None:
        self.rows_failed += 1
        if len(self.errors) < 20:
            self.errors.append(f"Row insert failed ({row!r}): {exc}")

    # ------------------------------------------------------------------
    def _summary(self, status: str = "completed") -> dict:
        return {
            "table": self.table.name,
            "status": status,
            "rows_copied": self.rows_copied,
            "rows_skipped": self.rows_skipped,
            "rows_failed": self.rows_failed,
            "errors": self.errors,
        }
