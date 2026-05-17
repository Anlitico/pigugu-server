# agent/context/compression/l4_roast.py
"""L4 roast compression — game-aware summary with prompt preservation."""

from __future__ import annotations

from pathlib import Path

from core.llm.types import Message

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load(name: str) -> str:
    path = _PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def compress_roast(
    turns: list[Message],
    *,
    existing_summary: str = "",
    roast_prompt: str = "",
    model: str = "qwen3.6-plus",
) -> str:
    """Compress roast turns. Output includes roast_prompt verbatim at top."""
    result_parts = [roast_prompt] if roast_prompt else []

    if not turns:
        if existing_summary:
            result_parts.append(existing_summary)
        return "\n\n---\n\n".join(p for p in result_parts if p)

    turns_text = "\n".join(f"[{t.role}]: {t.content}" for t in turns)

    if existing_summary:
        existing_body = existing_summary.split("\n---\n", 1)[-1] if "\n---\n" in existing_summary else existing_summary
        merge_prompt = (
            f"Existing game summary:\n{existing_body}\n\n"
            f"New gameplay:\n{turns_text}\n\n"
            f"Merge into a single summary under 250 words. "
            f"Preserve character state, plot points, and game decisions."
        )
    else:
        merge_prompt = _load("summarize_roast.j2").replace("{{turns_text}}", turns_text)

    from core.llm import get_llm, Message as M
    try:
        llm = get_llm(model)
        resp = await llm.chat(messages=[M.user(merge_prompt)], model=model)
        body = resp.content.strip()
    except Exception:
        body = existing_summary.split("\n---\n", 1)[-1] if existing_summary and "\n---\n" in existing_summary else existing_summary

    if body:
        result_parts.append(body)
    return "\n\n---\n\n".join(p for p in result_parts if p)
