# pigagent/metrics/compression.py
"""Per-compression-run timing collector. Standalone — no global state.

Compression runs as a fire-and-forget background task, so it can't share
TurnMetrics' global state. This creates a local dict and writes directly
to PG on finish.
"""

from __future__ import annotations

import os
import time
from typing import Any

from loguru import logger

_PG_DSN: str = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")


class CompressionMetrics:
    """Local dict-based collector for one compression run."""

    def __init__(self, user_id: str, scenario: str = "free_chat"):
        self._user_id = user_id
        self._scenario = scenario
        self._marks: dict[str, float] = {}
        self._meta: dict[str, Any] = {}
        self.mark("start")
        logger.info(f"[CompressMetrics] START u={user_id} scenario={scenario}")

    def mark(self, key: str) -> None:
        self._marks[key] = time.monotonic()

    def set_meta(self, key: str, value: object) -> None:
        self._meta[key] = value

    def finish(self) -> None:
        self.mark("end")
        segments = self._compute_segments()
        self._log(segments)
        if _PG_DSN:
            self._flush_pg(segments)

    def _compute_segments(self) -> dict[str, float]:
        segs: dict[str, float] = {}
        phases = [
            ("check", "start", "check_done"),
            ("llm", "check_done", "llm_done"),
            ("profile", "llm_done", "profile_done"),
            ("total", "start", "end"),
        ]
        for label, a, b in phases:
            va, vb = self._marks.get(a), self._marks.get(b)
            if va is not None and vb is not None:
                segs[label] = round(vb - va, 2)
        return segs

    def _log(self, segments: dict[str, float]) -> None:
        parts = "  ".join(f"{k}={v:.2f}s" for k, v in segments.items())
        meta = ", ".join(f"{k}={v}" for k, v in self._meta.items())
        logger.info(
            f"[CompressMetrics] DONE u={self._user_id} {self._scenario}  "
            f"{parts}" + (f"  [{meta}]" if meta else "")
        )

    def _flush_pg(self, segments: dict[str, float]) -> None:
        import asyncio
        import json as _json

        async def _write():
            try:
                import asyncpg  # type: ignore[import-untyped]
                conn = await asyncpg.connect(_PG_DSN)
                try:
                    await conn.execute(
                        """INSERT INTO compression_metrics
                           (user_id, scenario, segments, meta)
                           VALUES ($1, $2, $3::jsonb, $4::jsonb)""",
                        self._user_id,
                        self._scenario,
                        _json.dumps(segments),
                        _json.dumps(self._meta),
                    )
                finally:
                    await conn.close()
            except Exception:
                pass

        try:
            asyncio.ensure_future(_write())
        except RuntimeError:
            pass
