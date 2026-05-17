"""
玩法分类器 — 分析特朗普新帖，生成游戏场景 prompt 并写入 roast_scenarios 表。

每条新帖调用一次 LLM，返回该帖适合的所有 mode + prompt。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.llm import Message, get_llm

logger = logging.getLogger(__name__)

MODE_ABBREV = {
    "poison_opinion": "poison",
    "debate": "debate",
    "prediction": "predict",
    "breaking_bomb": "bomb",
}

_markdown_template = """你是一个内容分类器。下面是一条特朗普在 {platform} 上的社交媒体帖子。

帖子内容：{content}
发布时间：{created_at}
标签：{tags}

请判断这条帖适合 Pigugu 的哪些游戏模式，并为每个适合的模式生成游戏场景文本（prompt）。

四种模式：
- poison_opinion：帖子有争议性或槽点 → 生成毒观点场景
  场景文本必须包含：帖文内容 + 争议角度标签（如 TRUMP_POLL_BRAG / TARIFF_BLAME / DIPLOMACY_THREAT / ELECTION_FRAUD_HINT / PERSONAL_ATTACK 等）+ 矛盾钩子（帖子最站不住脚的那个点）

- debate：帖子包含明确主张/观点 → 生成来辩场景
  场景文本必须包含：帖文内容 + 核心主张 + Pigugu 挑衅立场（选用户最可能不同意的角度）+ 帖子本身的论据强处 + 帖子本身的论据弱处

- prediction：帖子包含可验证预测/截止日期 → 生成预测场景
  场景文本必须包含：帖文内容 + 预测目标 + 截止时间 + 揭晓标准

- breaking_bomb：帖子是重大突发事件 → 生成突发场景
  场景文本必须包含：帖文内容 + 紧急原因。is_urgent 仅当涉及战争/军事/重大灾难时为 true

对每条适合的模式，生成一个对象，包含：
- roast_id: "{{mode_abbrev}}_{{date}}_{{3位序号}}"（date 用帖子日期的 YYYY-MM-DD，序号从 001 开始）
- game_mode: 模式名
- prompt: 自然语言游戏场景描述，≤500 tokens，用中文写，便于 Agent 直接引用
- expires_at: ISO 8601 格式的过期时间
  - poison_opinion / debate: 帖子发布时间 + 48h
  - prediction: 截止时间
  - breaking_bomb: 帖子发布时间 + 2h

返回 JSON。只返回适合的模式，不适合的不返回：
{{
  "modes": [
    {{
      "roast_id": "poison_2026-05-17_001",
      "game_mode": "poison_opinion",
      "prompt": "[毒观点场景]\\n特朗普刚刚在 Truth Social...",
      "expires_at": "2026-05-19T01:36:21Z"
    }}
  ]
}}"""


async def classify_and_store(
    post: dict,
    *,
    model: str = "deepseek-chat",
    temperature: float = 0.1,
) -> list[dict]:
    """Classify a single post and store results into roast_scenarios.

    Returns the list of stored scenario dicts (roast_id, game_mode, prompt).
    """
    content = _build_classifier_prompt(post)
    llm = get_llm(model)

    try:
        resp = await llm.chat(
            messages=[Message.user(content)],
            model=model,
            response_format={"type": "json_object"},
            temperature=temperature,
        )
        data = json.loads(resp.content)
        modes = data.get("modes", [])
    except (json.JSONDecodeError, KeyError) as e:
        logger.error("Classifier LLM response parse error: %s", e)
        # Fallback: generate a poison_opinion scenario from template
        modes = _fallback_poison(post)
    except Exception:
        logger.exception("Classifier LLM call failed, using fallback")
        modes = _fallback_poison(post)

    if not modes:
        logger.info("No modes matched for post %s", post.get("post_id"))
        return []

    stored = await _store_scenarios(modes, news_id=str(post.get("id", "")))
    logger.info(
        "Classified post %s → %d modes: %s",
        post.get("post_id"),
        len(stored),
        [s["game_mode"] for s in stored],
    )
    return stored


def _build_classifier_prompt(post: dict) -> str:
    return _markdown_template.format(
        platform=post.get("platform", "unknown"),
        content=post.get("content", ""),
        created_at=post.get("created_at", ""),
        tags=json.dumps(post.get("tags", []), ensure_ascii=False),
    )


def _fallback_poison(post: dict) -> list[dict]:
    """Template-generated poison_opinion fallback (no LLM needed)."""
    content = post.get("content", "")
    created_at = post.get("created_at", "")
    date_str = _extract_date(created_at)
    expires = _add_hours(created_at, 48)

    roast_id = f"poison_{date_str}_fallback"
    prompt = (
        f"[毒观点场景]\n"
        f"特朗普刚刚发帖：\"{content}\"\n"
        f"争议角度：GENERIC — 帖子内容有潜在争议性。\n"
        f"游戏钩子：引导玩家讨论——你对这条帖怎么看？"
    )
    return [
        dict(
            roast_id=roast_id,
            game_mode="poison_opinion",
            prompt=prompt,
            expires_at=expires,
        )
    ]


async def _store_scenarios(
    modes: list[dict],
    news_id: str,
) -> list[dict]:
    """Insert into roast_scenarios, skipping duplicates (by roast_id PK)."""
    stored = []
    async with AsyncSessionLocal() as session:
        for m in modes:
            roast_id = m.get("roast_id", "")
            game_mode = m.get("game_mode", "")
            prompt = m.get("prompt", "")
            expires_at = _parse_dt(m.get("expires_at"))

            if not roast_id or not game_mode or not prompt:
                logger.warning("Skipping incomplete mode entry: %s", m)
                continue

            # Ensure roast_id uniqueness by appending a counter if needed
            roast_id = await _deduplicate_roast_id(session, roast_id)

            try:
                await session.execute(
                    text(
                        "INSERT INTO roast_scenarios "
                        "(roast_id, game_mode, prompt, news_id, expires_at) "
                        "VALUES (:roast_id, :game_mode, :prompt, :news_id, :expires_at)"
                    ),
                    dict(
                        roast_id=roast_id,
                        game_mode=game_mode,
                        prompt=prompt,
                        news_id=news_id,
                        expires_at=expires_at,
                    ),
                )
                stored.append(m)
            except Exception:
                logger.exception("Failed to insert roast_scenario %s", roast_id)

        await session.commit()
    return stored


async def _deduplicate_roast_id(session, roast_id: str) -> str:
    """Check if roast_id exists; if so, append a counter suffix."""
    result = await session.execute(
        text("SELECT 1 FROM roast_scenarios WHERE roast_id = :rid"),
        dict(rid=roast_id),
    )
    if result.fetchone() is None:
        return roast_id

    for i in range(1, 100):
        candidate = f"{roast_id}_{i}"
        result = await session.execute(
            text("SELECT 1 FROM roast_scenarios WHERE roast_id = :rid"),
            dict(rid=candidate),
        )
        if result.fetchone() is None:
            return candidate

    # After 99 attempts, fall back to timestamp suffix
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    return f"{roast_id}_{ts}"


def _extract_date(iso_string: str) -> str:
    """Extract YYYY-MM-DD from an ISO 8601 string."""
    try:
        return iso_string[:10]
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _add_hours(iso_string: str, hours: int) -> Optional[str]:
    """Add N hours to an ISO 8601 string, return ISO 8601 string."""
    dt = _parse_dt(iso_string)
    if dt is None:
        return None
    from datetime import timedelta

    return (dt + timedelta(hours=hours)).isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
