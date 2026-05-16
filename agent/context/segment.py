# agent/context/segment.py
"""Segment end detection — rules-first, LLM fallback.

Two layers:
  Layer 1 — Automatic (deterministic, no LLM):
    1. New roast_id → previous auto-ends
    2. Idle > 30 min → auto-ends

  Layer 2 — LLM judge (fire-and-forget):
    Intent detection: disengaged, goodbye, game over, topic drift.

Usage:
    from context.segment import detect_end

    ended, reason = await detect_end(messages, metadata={
        "roast_id_changed": False,
        "time_gap_minutes": 2.0,
    })
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from core.llm import get_llm, Message

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# ── Layer 1: Automatic Rules (deterministic only) ───────────────────────

_IDLE_THRESHOLD_MINUTES = 30


def _auto_end(
    roast_id_changed: bool,
    time_gap_minutes: float,
) -> tuple[bool, str]:
    """Check deterministic end conditions. Returns (ended, reason).

    Only handles determinable cases. Intent-guessing (disengaged,
    goodbye, game over) is the LLM's job.
    """

    if roast_id_changed:
        return True, "roast_id_changed"

    if time_gap_minutes > _IDLE_THRESHOLD_MINUTES:
        return True, f"idle_{time_gap_minutes:.0f}min"

    return False, ""


# ── Layer 2: LLM Judge ──────────────────────────────────────────────────

def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning(f"Prompt file not found: {path}")
    return ""


async def _llm_end(messages: list[Message], model: str) -> bool:
    """LLM judge for segment end detection."""
    try:
        llm = get_llm(model)

        prompt = _load_prompt("segment_end.j2")
        llm_messages = [Message.system(prompt)]
        for m in reversed(messages):
            llm_messages.append(Message(role=m.role, content=m.content))

        resp = await llm.chat(
            messages=llm_messages,
            model=model,
            max_tokens=10,
        )

        ended = resp.content.strip().upper().startswith("END")
        if ended:
            logger.debug("[Segment] End detected by LLM")
        return ended

    except Exception as e:
        logger.warning(f"[Segment] LLM end detection failed: {e}")
        return False


# ── Main Entry Point ────────────────────────────────────────────────────

async def detect_end(
    messages: list[Message],
    *,
    metadata: dict | None = None,
    model: str = "qwen3.6-flash",
) -> tuple[bool, str]:
    """Judge whether the current roast segment has ended.

    Layer 1 (deterministic) then Layer 2 (LLM). Returns (ended, reason).

    metadata keys:
        roast_id_changed: bool — new roast_id introduced
        time_gap_minutes: float — minutes since last turn

    Returns:
        (ended, reason) — reason: roast_id_changed | idle_Nmin | llm | ""
    """
    if not messages:
        return False, ""

    meta = metadata or {}

    # Layer 1: Automatic rules (deterministic only)
    ended, reason = _auto_end(
        roast_id_changed=meta.get("roast_id_changed", False),
        time_gap_minutes=meta.get("time_gap_minutes", 0.0),
    )
    if ended:
        logger.info(f"[Segment] Auto end: {reason}")
        return True, reason

    # Layer 2: LLM judge
    if await _llm_end(messages, model):
        return True, "llm"

    return False, ""
