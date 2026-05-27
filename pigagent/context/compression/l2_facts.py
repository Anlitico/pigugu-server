# pigagent/context/compression/l2_facts.py
"""L2 user fact extraction and profile summarization."""

from __future__ import annotations

from pathlib import Path

from config import get_config

_cfg = get_config()

from core.llm.types import Message

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load(name: str) -> str:
    path = _PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def extract_facts(turns: list[Message], model: str = "qwen-plus-us") -> list[dict]:
    if not turns:
        return []

    turns_text = "\n".join(f"[{t.role}]: {t.content}" for t in turns)
    prompt = _load("l2_extract_facts.j2").replace("{{turns_text}}", turns_text)

    from core.llm import get_llm, Message as M
    import json
    try:
        llm = get_llm(model)
        resp = await llm.chat(messages=[M.user(prompt)], model=model)
        content = resp.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("\n```", 1)[0]
        return json.loads(content).get("facts", [])
    except Exception:
        return []


async def summarize_profile(facts: list[str], *, existing: str = "", model: str = "qwen-plus-us") -> str:
    if not facts and not existing:
        return ""

    from core.llm import get_llm, Message as M

    if existing:
        new_facts_text = "\n".join(f"- {f}" for f in facts)
        prompt = _load("l2_profile_merge.j2").format(
            existing_profile=existing, new_facts=new_facts_text,
            max_words=_cfg.CONTEXT_L2_PROFILE_MAX_WORDS,
        )
    else:
        facts_text = "\n".join(f"- {f}" for f in facts)
        prompt = _load("l2_profile_initial.j2").format(
            facts_text=facts_text, max_words=_cfg.CONTEXT_L2_PROFILE_MAX_WORDS,
        )

    try:
        llm = get_llm(model)
        resp = await llm.chat(messages=[M.user(prompt)], model=model)
        return resp.content.strip()
    except Exception:
        return existing
