"""The only metrics sink: drains finished scopes to ClickHouse.

Design (see docs voice-latency-metrics-design §10): business code never
touches this module directly. A task that owns a finished scope hands it to
:func:`enqueue`; the scope becomes one INSERT row, batched by a single
background task on the running event loop.

Non-blocking guarantees:
- ``enqueue`` is synchronous, thread-safe and never raises; it appends to a
  bounded deque (drop-oldest when full) — a slow/stuck ClickHouse can make us
  drop stale observations, never stall the pipeline or grow memory.
- One exporter task per process; one asynch connection lifecycle per batch;
  every insert has a timeout and swallows+counts failures.
- Gated by ``ENABLE_METRICS_EXPORT`` (default off): unconfigured -> no-op.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections import deque
from typing import Any
from urllib.parse import quote

from loguru import logger

_TICK_S = 0.2
_BATCH_MAX = 200
_QUEUE_MAX = 2000
_INSERT_TIMEOUT_S = 5.0


def _build_dsn() -> str:
    host = os.getenv("CLICKHOUSE_HOST", "clickhouse").strip()
    port = os.getenv("CLICKHOUSE_PORT", "9000").strip()
    user = os.getenv("CLICKHOUSE_USER", "default").strip()
    db = os.getenv("CLICKHOUSE_DATABASE", "voice").strip()
    password = os.getenv("CLICKHOUSE_PASSWORD", "")
    return (
        f"clickhouse://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{db}"
    )


def is_export_enabled() -> bool:
    return os.getenv("ENABLE_METRICS_EXPORT", "false").lower() in (
        "1", "true", "yes", "on",
    )


class MetricsExporter:
    """Bounded in-process queue + single async CH writer."""

    def __init__(
        self,
        *,
        enabled: bool,
        dsn: str,
        queue_max: int = _QUEUE_MAX,
    ) -> None:
        self._enabled = enabled
        self._dsn = dsn
        self._queue_max = queue_max
        self._pending: deque[tuple[str, tuple[str, ...], tuple[Any, ...]]] = deque()
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._started_on_loop: asyncio.AbstractEventLoop | None = None
        # counters (metrics about the metrics)
        self.enqueued = 0
        self.dropped = 0
        self.written = 0
        self.failed = 0

    # ── emit side (any thread, sync, never raises) ──────────────────

    def submit(self, row: tuple[str, tuple[str, ...], tuple[Any, ...]]) -> bool:
        """Queue one fully-rendered (table, columns, values) row. Returns False
        when disabled. Never blocks and never raises."""
        if not self._enabled:
            return False
        try:
            with self._lock:
                if len(self._pending) >= self._queue_max:
                    self._pending.popleft()
                    self.dropped += 1
                self._pending.append(row)
                self.enqueued += 1
        except Exception:
            # Enqueue must never surface to the pipeline.
            self.dropped += 1
            return False
        try:
            self._ensure_task()
        except Exception:
            # Loop is closing / no loop this tick — the row stays queued
            # (bounded) and drains when a loop returns. Never raise to callers.
            pass
        return True

    def _ensure_task(self) -> None:
        if self._task is not None:
            return
        try:
            cur = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop running in this thread — rows stay buffered
            # (bounded) until a loop starts the consumer, or are dropped once
            # full.
            cur = None
        if self._started_on_loop is None:
            if cur is None:
                return
            self._started_on_loop = cur
        if cur is self._started_on_loop:
            self._create_consumer()
        else:
            # submit() from a foreign thread/loop: schedule consumer creation
            # on the owner loop. ``loop.create_task`` is not thread-safe.
            self._started_on_loop.call_soon_threadsafe(self._create_consumer)

    def _create_consumer(self) -> None:
        # Runs on the exporter's own loop (single task even under racing
        # producers from several threads/loops).
        if self._task is not None:
            return
        self._task = self._started_on_loop.create_task(self._run())
        logger.info(f"[MetricsExporter] started (dsn host={self._dsn.split('@')[-1]})")

    # ── consumer (single task, on the exporter loop) ─────────────────

    def _drain(self) -> list[tuple[str, tuple[str, ...], tuple[Any, ...]]]:
        with self._lock:
            if not self._pending:
                return []
            rows = list(self._pending)
            self._pending.clear()
        return rows

    async def _run(self) -> None:
        while True:
            rows = self._drain()
            if not rows:
                await asyncio.sleep(_TICK_S)
                continue
            # Group by table so one INSERT covers all rows of a table.
            by_table: dict[str, list[tuple[tuple[str, ...], tuple[Any, ...]]]] = {}
            for table, columns, values in rows:
                by_table.setdefault(table, []).append((columns, values))
            for table, items in by_table.items():
                if not items:
                    continue
                columns = items[0][0]
                await self._insert(table, columns, [v for _, v in items])
            await asyncio.sleep(0)

    async def _insert(self, table: str, columns: tuple[str, ...], rows: list) -> None:
        from asynch import connect as ch_connect  # type: ignore[import-not-found]

        col_sql = ", ".join(columns)

        async def _do() -> None:
            # Connection + cursor inside the timeout too: a TCP connect that
            # hangs (network black-hole) must not stall the single consumer
            # task forever — otherwise the queue never drains and every
            # subsequent submit drops the oldest row.
            conn = ch_connect(self._dsn)
            async with conn:
                async with conn.cursor() as cur:
                    # asynch streams native blocks — query ends with bare VALUES.
                    await cur.execute(
                        f"INSERT INTO {table} ({col_sql}) VALUES",
                        rows,
                    )

        try:
            await asyncio.wait_for(_do(), timeout=_INSERT_TIMEOUT_S)
            self.written += len(rows)
        except Exception as e:
            self.failed += 1
            logger.warning(
                f"[MetricsExporter] insert failed table={table} rows={len(rows)} "
                f"err={e!r} (written={self.written} dropped={self.dropped})"
            )

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


# ── module singleton ────────────────────────────────────────────────

exporter = MetricsExporter(
    enabled=is_export_enabled(),
    dsn=_build_dsn(),
)


def enqueue(scope) -> bool:
    """Log + enqueue a finished scope. Sync, safe from any thread/loop.

    The human [METRIC] log line is unconditional (matches the pgsql-era
    behaviour — dev machines run with export disabled but still want the
    per-turn log); only the ClickHouse submit is gated by ``ENABLE_METRICS_EXPORT``.
    """
    if getattr(scope, "enqueued", False):
        # Already handed off — a racing owner task must not double-submit the
        # same scope (would duplicate the CH row).
        return True
    line = None
    try:
        line = scope.log_line() if hasattr(scope, "log_line") else None
    except Exception:
        # A log line must never fail the emit path.
        line = None
    if line:
        logger.info(line)
    if not exporter._enabled:
        return False
    try:
        row = scope.ch_row()
    except Exception as e:
        logger.warning(f"[MetricsExporter] render failed {e!r}")
        return False
    scope.enqueued = True
    return exporter.submit(row)


def stop() -> None:
    exporter.stop()
