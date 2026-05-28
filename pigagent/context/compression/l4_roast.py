# pigagent/context/compression/l4_roast.py
"""L4 roast compression  -  game-aware summary with prompt preservation."""

from __future__ import annotations

from pathlib import Path

from config import get_config

_cfg = get_config()

from core.llm.types import Message

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load(name: str) -> str:
    path = _PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def compress_roast(
    turns: list[Message],
    *,
    existing_summary: str = "",
    model: str = "qwen-plus-us",
) -> str:
    """Compress roast turns into a game-aware summary."""
    if not turns:
        return existing_summary

    turns_text = "\n".join(f"[{t.role}]: {t.content}" for t in turns)

    if existing_summary:
        existing_body = existing_summary.split("\n---\n", 1)[-1] if "\n---\n" in existing_summary else existing_summary
        merge_prompt = (
            _load("l4_merge_roast.j2")
            .replace("{{existing_summary}}", existing_body)
            .replace("{{turns_text}}", turns_text)
            .replace("{{max_words}}", str(_cfg.CONTEXT_L4_ROAST_MAX_WORDS))
        )
    else:
        merge_prompt = (
            _load("l4_roast_initial.j2")
            .replace("{{turns_text}}", turns_text)
            .replace("{{max_words}}", str(_cfg.CONTEXT_L4_ROAST_MAX_WORDS))
        )

    from core.llm import get_llm, Message as M
    try:
        llm = get_llm(model)
        resp = await llm.chat(messages=[M.user(merge_prompt)], model=model)
        body = resp.content.strip()
    except Exception:
        body = existing_summary.split("\n---\n", 1)[-1] if existing_summary and "\n---\n" in existing_summary else existing_summary

    return body or existing_summary
