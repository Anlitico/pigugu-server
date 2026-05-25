# pigagent/lk/telemetry.py
"""Latency timing instrumentation for the voice pipeline.

Tracks T0 (user stops speaking) through T5 (agent starts speaking)
and logs a detailed breakdown each turn.
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger


class TurnTimer:
    """Per-turn latency tracker. One instance per entrypoint session."""

    def __init__(self):
        self.data: dict[str, Any] = {
            "turn_id": 0,
            "user_stop_speaking": None,     # T0
            "final_transcript": None,        # T1
            "agent_start_thinking": None,    # T2
            "llm_first_token": None,         # T2.5
            "speech_created": None,          # T3
            "llm_response_logged": None,     # T4
            "agent_start_speaking": None,    # T5
        }

    def reset(self) -> None:
        self.data["turn_id"] += 1
        for key in (
            "user_stop_speaking", "final_transcript", "agent_start_thinking",
            "llm_first_token", "speech_created", "llm_response_logged",
            "agent_start_speaking",
        ):
            self.data[key] = None

    def mark(self, key: str) -> None:
        self.data[key] = time.perf_counter()

    def log_summary(self) -> None:
        d = self.data
        t5 = d["agent_start_speaking"]
        if not t5:
            return

        t0 = d["user_stop_speaking"]
        t1 = d["final_transcript"]
        t2 = d["agent_start_thinking"]
        t2_5 = d["llm_first_token"]
        t3 = d["speech_created"]
        t4 = d["llm_response_logged"]

        start = t0 or t1 or t2
        if not start:
            return

        total = t5 - start

        logger.info("=" * 60)
        logger.info(f"  [TIMING] Turn #{d['turn_id']}  -  Total lag: {total:.3f}s")
        if t0 and t1:
            logger.info(f"  T0 -> T1 STT:   {(t1 - t0):+.3f}s")
        if t2 and t2_5:
            logger.info(f"  T2 -> T2.5 TTFT: {(t2_5 - t2):+.3f}s")
        if t2_5 and t4:
            logger.info(f"  T2.5 -> T4 LLM: {(t4 - t2_5):+.3f}s")
        if t3 and t5:
            logger.info(f"  T3 -> T5 TTS:   {(t5 - t3):+.3f}s")

        if total < 2.0:
            logger.info("  VERDICT: EXCELLENT (<2s)")
        elif total < 4.0:
            logger.info("  VERDICT: ACCEPTABLE (2-4s)")
        else:
            logger.info(f"  VERDICT: SLOW ({total:.1f}s)")
        logger.info("=" * 60)
