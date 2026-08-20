"""
Process-based parallel table copying.

A migration spends nearly all of its time in two CPU-bound Python stages —
building rows out of the source driver's result set and rendering them into
PostgreSQL's COPY TEXT format. Both hold the GIL, so copying tables on threads
measures *slower* than doing them one at a time (0.44x on a 3-way test). Worker
*processes* sidestep the GIL entirely and measured 2.86x on the same test.

This module is deliberately additive: the orchestrator keeps its original
sequential loop and only reaches for a pool when a caller asks for more than
one worker and the connectors can be rebuilt in a child process. Anything
unexpected falls back to the sequential path rather than failing a migration.
"""
from __future__ import annotations

import importlib
import logging
import multiprocessing as mp
import queue as queue_mod
import threading

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


def _copy_one(table):
    """Copy a single table inside a worker process."""
    from engine.migration.data_mover import DataMigration

    progress = _WORKER["progress"]

    def _on_batch(table_name, rows_so_far, batch_rows, total_expected):
        try:
            progress.put_nowait((table_name, rows_so_far, batch_rows, total_expected))
        except Exception:
            pass        # progress is best-effort; never fail a copy over it

    try:
        source, target = _WORKER["source"], _WORKER["target"]
        try:
            total = source.count_rows(table.name)
        except Exception:
            total = None
        if total is not None:
            _on_batch(table.name, 0, 0, total)
        mover = DataMigration(source, target, table, progress_callback=_on_batch)
        return table.name, mover.run(), None
    except BaseException as exc:                       # reported, never crashes the pool
        return table.name, None, f"{type(exc).__name__}: {exc}"


def copy_tables(source, target, tables, workers: int, on_progress, on_table_done):
    """Copy ``tables`` across a worker pool.

    ``on_progress(table_name, rows_so_far, batch_rows, total_expected)`` is
    called on the parent for streamed progress; ``on_table_done(name, result,
    error)`` once per finished table. Raises ParallelCopyUnavailable before any
    work starts if the pool cannot be created.
    """
    source_spec = connector_spec(source)
    target_spec = connector_spec(target)
    try:
        context = mp.get_context("spawn")
        progress: "mp.Queue" = context.Queue(maxsize=10_000)
        pool = context.Pool(
            processes=workers, initializer=_init_worker,
            initargs=(source_spec, target_spec, progress),
        )
    except Exception as exc:
        raise ParallelCopyUnavailable(str(exc)) from exc

    stop = threading.Event()

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
                on_progress(*item)
            except Exception:
                pass

    pump = threading.Thread(target=_drain, name="dialectbridge-progress", daemon=True)
    pump.start()
    try:
        for name, result, error in pool.imap_unordered(_copy_one, list(tables)):
            on_table_done(name, result, error)
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
