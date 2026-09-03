# pigagent/metrics/compression.py
"""Per-compression-run collector — a standalone CompressionScope.

Compression runs as a fire-and-forget background task, so it must not touch
the turn-scope contextvar. It builds its own scope and hands it to the
shared exporter on finish (same bounded-queue side channel as turn metrics).
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from metrics import render
from metrics.exporter import enqueue
from metrics.scope import CompressionScope


class CompressionMetrics:
    """Thin collector over one :class:`CompressionScope`. Fire-and-forget."""

    def __init__(self, user_id: str, scenario: str = "free_chat"):
        self._scope = CompressionScope(user_id=user_id, scenario=scenario)
        self._user_id = user_id
        self._scenario = scenario
        self.mark("start")
        logger.info(f"[CompressMetrics] START u={user_id} scenario={scenario}")

    def mark(self, key: str) -> None:
        self._scope.mark(key)

    def set_meta(self, key: str, value: object) -> None:
        self._scope.set_meta(key, value)

    @property
    def segments(self) -> dict[str, float]:
        """Derived per-phase durations (seconds) from the collected marks."""
        return render.compression_segments(self._scope.marks)

    def finish(self) -> None:
        self._scope.mark("end")
        self._log(self.segments)
        self._scope.finish()
        enqueue(self._scope)

    def _log(self, segments: dict[str, float]) -> None:
        parts = "  ".join(f"{k}={v:.2f}s" for k, v in segments.items())
        meta = ", ".join(f"{k}={v}" for k, v in self._scope.meta.items()
                         if k != "scenario")
        logger.info(
            f"[CompressMetrics] DONE u={self._user_id} {self._scenario}  "
            f"{parts}" + (f"  [{meta}]" if meta else "")
        )
