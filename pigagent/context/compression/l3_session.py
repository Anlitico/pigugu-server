# pigagent/context/compression/l3_session.py
"""L3 session compression  -  recursive summary merge."""

from __future__ import annotations

from pathlib import Path

from config import get_config

_cfg = get_config()

from core.llm.types import Message

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load(name: str) -> str:
    path = _PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def compress_turns(turns: list[Message], model: str = "qwen3.6-plus") -> str:
    """First compression: turns  ->  summary."""
    if not turns:
        return ""
    turns_text = "\n".join(f"[{t.role}]: {t.content}" for t in turns)
    prompt = _load("l3_summary_initial.j2").replace("{{turns_text}}", turns_text).replace("{{max_words}}", str(_cfg.CONTEXT_L3_COMPRESS_MAX_WORDS))

    from core.llm import get_llm, Message as M
    try:
        llm = get_llm(model)
        resp = await llm.chat(messages=[M.user(prompt)], model=model)
        return resp.content.strip()
    except Exception:
        return ""


async def merge_summary(existing_summary: str, new_turns: list[Message], model: str = "qwen3.6-plus") -> str:
    """Merge: existing summary + new turns  ->  updated summary."""
    if not existing_summary and not new_turns:
        return ""
    if not existing_summary:
        return await compress_turns(new_turns, model=model)

    new_segment = "\n".join(f"[{t.role}]: {t.content}" for t in new_turns)
    prompt = _load("l3_summary_merge.j2").format(
        existing_summary=existing_summary, new_segment=new_segment,
        max_words=_cfg.CONTEXT_L3_MERGE_MAX_WORDS,
    )

    from core.llm import get_llm, Message as M
    try:
        llm = get_llm(model)
        resp = await llm.chat(messages=[M.user(prompt)], model=model)
        return resp.content.strip()
    except Exception:
        return existing_summary
