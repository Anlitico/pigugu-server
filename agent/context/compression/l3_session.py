# agent/context/compression/l3_session.py
"""L3 session compression — recursive summary merge."""

from __future__ import annotations

from pathlib import Path

from core.llm.types import Message

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load(name: str) -> str:
    path = _PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def compress_tier_1(turns: list[Message], model: str = "qwen3.6-plus") -> str:
    """First compression: turns → summary."""
    if not turns:
        return ""
    turns_text = "\n".join(f"[{t.role}]: {t.content}" for t in turns)
    prompt = _load("summarize_tier_1.j2").replace("{{turns_text}}", turns_text)

    from core.llm import get_llm, Message as M
    try:
        llm = get_llm(model)
        resp = await llm.chat(messages=[M.user(prompt)], model=model)
        return resp.content.strip()
    except Exception:
        return ""


async def compress_tier_2(existing_summary: str, new_turns: list[Message], model: str = "qwen3.6-plus") -> str:
    """Merge: existing summary + new turns → updated summary."""
    if not existing_summary and not new_turns:
        return ""
    if not existing_summary:
        return await compress_tier_1(new_turns, model=model)

    new_segment = "\n".join(f"[{t.role}]: {t.content}" for t in new_turns)
    prompt = _load("summarize_tier_2.j2").format(
        existing_summary=existing_summary, new_segment=new_segment,
    )

    from core.llm import get_llm, Message as M
    try:
        llm = get_llm(model)
        resp = await llm.chat(messages=[M.user(prompt)], model=model)
        return resp.content.strip()
    except Exception:
        return existing_summary
