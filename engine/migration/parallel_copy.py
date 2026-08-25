"""
Process-based parallel table copying.

A migration spends nearly all of its time in two CPU-bound Python stages —
building rows out of the source driver's result set and rendering them into
PostgreSQL's COPY TEXT format. Both hold the GIL, so copying tables on threads
measures *slower* than doing them one at a time (0.44x on a 3-way test). Worker
*processes* sidestep the GIL entirely and measured 2.86x on the same test.

Handing each worker a whole table leaves one limit in place: the migration can
never finish faster than its single biggest table. On the reference database
one table holds 65% of all rows and ran for the entire copy phase while the
other workers sat idle. So a table big enough to be worth it is cut into
disjoint primary-key ranges (see ``table_shards``) and several workers copy it
at once. The once-per-table work — dropping indexes before the load, recreating
them and reseeding the identity afterwards — is lifted out of the workers and
done once around the whole set of slices.

This module is deliberately additive: the orchestrator keeps its original
sequential loop and only reaches for a pool when a caller asks for more than
one worker and the connectors can be rebuilt in a child process. Anything
unexpected falls back to the sequential path rather than failing a migration,
and any table that cannot be split safely is copied whole exactly as before.
"""
from __future__ import annotations

import importlib
import logging
import multiprocessing as mp
import queue as queue_mod
import threading
from time import monotonic

from engine.migration.table_shards import estimates_for, plan_shards

logger = logging.getLogger("dialectbridge.migration")

# Connections are opened once per worker process and reused for every table
# that worker handles.
_WORKER: dict = {}


class ParallelCopyUnavailable(Exception):
    """Raised when the pool cannot be used; the caller falls back to serial."""


def connector_spec(connector) -> tuple:
    """Describe a connector well enough to rebuild it inside a child process."""
    cls = type(connector)
    for attr in ("host", "port", "database", "user", "password"):
        if not hasattr(connector, attr):
            raise ParallelCopyUnavailable(
                f"{cls.__name__} has no '{attr}' — cannot rebuild it in a worker"
            )
    return (cls.__module__, cls.__qualname__, connector.host, connector.port,
            connector.database, connector.user, connector.password)


def _build(spec: tuple):
    module_name, qualname, host, port, database, user, password = spec
    cls = getattr(importlib.import_module(module_name), qualname)
    connector = cls(host=host, port=port, database=database, user=user, password=password)
    connector.connect()
    return connector


def _init_worker(source_spec: tuple, target_spec: tuple, progress: "mp.Queue") -> None:
    _WORKER["source"] = _build(source_spec)
    _WORKER["target"] = _build(target_spec)
    _WORKER["progress"] = progress


def _copy_one(item):
    """Copy one table, or one key range of one table, inside a worker process."""
    from engine.migration.data_mover import DataMigration

    table, shard_id, key_range = item
    progress = _WORKER["progress"]

    def _on_batch(table_name, rows_so_far, batch_rows, total_expected):
        try:
            progress.put_nowait((table_name, shard_id, rows_so_far, batch_rows, total_expected))
        except Exception:
            pass        # progress is best-effort; never fail a copy over it

    try:
        source, target = _WORKER["source"], _WORKER["target"]
        # A slice reports no total of its own — the parent already knows the
        # whole table's count and a per-slice figure would overwrite it.
        if key_range is None:
            try:
                total = source.count_rows(table.name)
            except Exception:
                total = None
            if total is not None:
                _on_batch(table.name, 0, 0, total)
        mover = DataMigration(source, target, table, progress_callback=_on_batch,
                              key_range=key_range, manage_table=key_range is None)
        # Timed here, in the worker, because this is the only place that knows
        # when *this* table's copy actually started. The parent hands every
        # table to the pool at once, so a start time stamped there measures the
        # whole phase and makes a nine-row table look like it took a minute.
        started = monotonic()
        summary = mover.run()
        summary["duration_seconds"] = round(max(0.0, monotonic() - started), 2)
        return table.name, shard_id, summary, None
    except BaseException as exc:                       # reported, never crashes the pool
        return table.name, shard_id, None, f"{type(exc).__name__}: {exc}"


def _merge(summaries: list[dict]) -> dict:
    """Fold one table's slice summaries back into a single table result."""
    merged = {"rows_copied": 0, "rows_failed": 0, "rows_skipped": 0, "errors": [],
              "duration_seconds": 0.0}
    for summary in summaries:
        merged["rows_copied"] += summary.get("rows_copied", 0)
        merged["rows_failed"] += summary.get("rows_failed", 0)
        merged["rows_skipped"] += summary.get("rows_skipped", 0)
        merged["errors"].extend(summary.get("errors", []))
        # The slices ran at the same time, so the table's elapsed time is the
        # longest slice, not the sum of them.
        merged["duration_seconds"] = max(
            merged["duration_seconds"], summary.get("duration_seconds", 0.0) or 0.0
        )
    return merged


def _build_work(source, tables, workers: int) -> tuple[list, dict]:
    """Return the work items to dispatch and the shard plan that produced them."""
    tables = list(tables)
    if workers < 2 or len(tables) == 0:
        return [(table, 0, None) for table in tables], {}
    try:
        plans = plan_shards(source, tables, estimates_for(source))
    except Exception as exc:
        logger.warning("Shard planning failed (%s) — every table copied whole", exc)
        return [(table, 0, None) for table in tables], {}

    items = []
    for table in tables:
        plan = plans.get(table.name)
        if not plan:
            items.append((table, 0, None))
            continue
        key, ranges = plan
        for index, (low, high) in enumerate(ranges):
            items.append((table, index, (key, low, high)))
    # Longest first: a big table's slices should start before the pool fills up
    # with short tables, or the run ends waiting on a slice dispatched last.
    items.sort(key=lambda item: item[2] is None)
    return items, plans


def copy_tables(source, target, tables, workers: int, on_progress, on_table_done):
    """Copy ``tables`` across a worker pool.

    ``on_progress(table_name, rows_so_far, batch_rows, total_expected)`` is
    called on the parent for streamed progress; ``on_table_done(name, result,
    error)`` once per finished table. Raises ParallelCopyUnavailable before any
    work starts if the pool cannot be created.
    """
    tables = list(tables)
    items, plans = _build_work(source, tables, workers)
    by_name = {table.name: table for table in tables}

    source_spec = connector_spec(source)
    target_spec = connector_spec(target)

    # Slices of one table must not each drop the indexes out from under the
    # others, so the drop happens once here, before any slice starts.
    dropped: dict[str, list[str]] = {}
    from engine.migration.data_mover import DataMigration
    for name in plans:
        try:
            dropped[name] = DataMigration(source, target, by_name[name]).drop_table_indexes()
        except Exception as exc:
            logger.warning("Could not drop indexes on %s before a split copy (%s)", name, exc)
            dropped[name] = []

    try:
        context = mp.get_context("spawn")
        progress: "mp.Queue" = context.Queue(maxsize=10_000)
        pool = context.Pool(
            processes=workers, initializer=_init_worker,
            initargs=(source_spec, target_spec, progress),
        )
    except Exception as exc:
        # Nothing has been copied yet, but indexes may already be gone; put
        # them back so the serial fallback starts from an untouched target.
        for name, ddl in dropped.items():
            try:
                DataMigration(source, target, by_name[name]).finish_table(ddl)
            except Exception:
                pass
        raise ParallelCopyUnavailable(str(exc)) from exc

    # A slice cannot report the whole table's size, so the parent counts a
    # split table once up front — otherwise the progress bar for the very
    # biggest table would be the only one with no total.
    for name in plans:
        try:
            on_progress(name, 0, 0, source.count_rows(name))
        except Exception:
            pass

    stop = threading.Event()
    # Slices of one table report their own running totals; the table's progress
    # is their sum, so each slice's latest figure is kept separately.
    shard_rows: dict[str, dict[int, int]] = {}

    def _drain():
        """Forward worker progress to the parent's callback."""
        while not stop.is_set():
            try:
                item = progress.get(timeout=0.2)
            except (queue_mod.Empty, OSError, ValueError):
                continue
            except Exception:
                return
            try:
                table_name, shard_id, rows_so_far, batch_rows, total_expected = item
                counts = shard_rows.setdefault(table_name, {})
                counts[shard_id] = rows_so_far
                on_progress(table_name, sum(counts.values()), batch_rows, total_expected)
            except Exception:
                pass

    pump = threading.Thread(target=_drain, name="dialectbridge-progress", daemon=True)
    pump.start()

    expected = {}
    for _table, _shard, key_range in items:
        expected[_table.name] = expected.get(_table.name, 0) + 1
    collected: dict[str, list[dict]] = {}
    failures: dict[str, str] = {}

    try:
        for name, shard_id, result, error in pool.imap_unordered(_copy_one, items):
            if error is not None:
                failures.setdefault(name, error)
            else:
                collected.setdefault(name, []).append(result)
            expected[name] -= 1
            if expected[name] > 0:
                continue                     # more slices of this table still running
            # Every slice of this table has landed: put its indexes back and
            # reseed its identity before reporting it done.
            if name in plans:
                try:
                    DataMigration(source, target, by_name[name]).finish_table(dropped.get(name, []))
                except Exception as exc:
                    logger.warning("Could not restore indexes on %s (%s)", name, exc)
            on_table_done(name, _merge(collected.get(name, [])), failures.get(name))
        pool.close()
        pool.join()
    except BaseException:
        pool.terminate()
        pool.join()
        raise
    finally:
        stop.set()
        pump.join(timeout=1)
        try:
            progress.close()
        except Exception:
            pass
