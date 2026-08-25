"""
Splitting one very large table across several copy workers.

``parallel_copy`` gives every worker a whole table, which makes the migration
no faster than its biggest table. On the reference database one table holds 65%
of all rows and single-handedly spans the entire copy phase: the other workers
finish everything else and then sit idle waiting for it. Table-level
parallelism cannot fix that — the fix is to cut the big table itself into
disjoint key ranges that several workers copy at once.

A table is only split when the split is provably safe and worthwhile:

  * it has a single-column primary key,
  * that key is an integer type (so ranges are arithmetic, not collation-
    dependent), and
  * the catalog's row estimate is large enough that the extra round trips are
    worth it.

Anything else — composite keys, UUID/text keys, no key at all, a catalog that
will not answer — copies exactly as it always has, in one piece.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("dialectbridge.migration")

# Below this the per-shard setup costs more than the parallelism saves.
SHARD_MIN_ROWS = 1_000_000
# Target rows per shard. Small enough to give the pool something to balance
# with, large enough that each shard is still a long sequential read.
ROWS_PER_SHARD = 1_500_000
# More shards than this stops helping and starts adding round trips.
MAX_SHARDS = 16

# Rows are a poor proxy for how long a table takes to copy. A measured run had
# 2,451 attachment rows take 91s (27 rows/s) while 18.9M narrow rows took 42s
# (443,000 rows/s) — a blob table is slow per *row* because each row carries
# megabytes. Splitting on row count alone left every one of those tables whole
# and they became the critical path. So a table is also split once it is simply
# large on disk, whatever its row count.
SHARD_MIN_BYTES = 256 * 1024 * 1024
BYTES_PER_SHARD = 192 * 1024 * 1024

_INT_KEY_TYPES = {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT",
                  "INT2", "INT4", "INT8", "SERIAL", "BIGSERIAL", "SMALLSERIAL"}


def _integer_key(table) -> str | None:
    """Return the table's single integer primary-key column, if it has one."""
    pk = table.primary_key
    if not pk or len(pk.columns) != 1:
        return None
    name = pk.columns[0]
    column = next((c for c in table.columns if c.name.lower() == name.lower()), None)
    if column is None or column.is_computed:
        return None
    base = column.data_type.split("(")[0].strip().upper()
    return name if base in _INT_KEY_TYPES else None


def shard_count(rows: int, size_bytes: int = 0) -> int:
    """How many slices to cut a table into.

    Sized both ways — by rows and by bytes — and the larger wins, so a narrow
    fact table and a fat blob table each get enough slices to keep the pool
    busy without either being cut into needless fragments.
    """
    by_rows = -(-rows // ROWS_PER_SHARD) if rows else 0
    by_bytes = -(-size_bytes // BYTES_PER_SHARD) if size_bytes else 0
    return max(2, min(MAX_SHARDS, max(by_rows, by_bytes)))


def worth_splitting(rows: int, size_bytes: int) -> bool:
    return rows >= SHARD_MIN_ROWS or size_bytes >= SHARD_MIN_BYTES


def split_range(low: int, high: int, shards: int) -> list[tuple]:
    """Cut ``[low, high]`` into ``shards`` half-open ``[lo, hi)`` slices.

    The first slice has no lower bound and the last no upper bound, so the
    slices together cover every row including any that arrive outside the
    sampled bounds while the migration runs.
    """
    span = high - low + 1
    if span < shards:
        return []
    width = span // shards
    bounds = []
    for index in range(shards):
        lo = None if index == 0 else low + index * width
        hi = None if index == shards - 1 else low + (index + 1) * width
        bounds.append((lo, hi))
    return bounds


def plan_shards(source, tables, estimates: dict) -> dict:
    """Return ``{table_name: (key_column, [(lo, hi), ...])}`` for big tables.

    ``estimates`` maps a lower-cased table name to ``(rows, bytes)``.
    """
    plans = {}
    for table in tables:
        rows, size_bytes = estimates.get(table.name.lower(), (0, 0))
        if not worth_splitting(rows, size_bytes):
            continue
        key = _integer_key(table)
        if key is None:
            continue
        try:
            bounds = source.key_bounds(table.name, key)
        except Exception as exc:
            logger.warning("Cannot read key bounds for %s (%s) — copying whole",
                           table.name, exc)
            continue
        if not bounds:
            continue
        ranges = split_range(bounds[0], bounds[1], shard_count(rows, size_bytes))
        if len(ranges) < 2:
            continue
        plans[table.name] = (key, ranges)
        logger.info("Table %s (~%d rows, ~%d MB) split into %d key ranges on %s",
                    table.name, rows, size_bytes // (1024 * 1024), len(ranges), key)
    return plans


def estimates_for(source) -> dict:
    """Catalog ``(rows, bytes)`` estimates, or an empty dict when unavailable."""
    if not hasattr(source, "approx_table_stats"):
        return {}
    try:
        return source.approx_table_stats()
    except Exception as exc:
        logger.warning("Table estimates unavailable (%s) — no table will be split", exc)
        return {}
