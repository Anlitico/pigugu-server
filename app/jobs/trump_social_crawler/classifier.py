"""
玩法分类器 — 分析特朗普新帖，生成游戏场景 prompt 并写入 roast_scenarios 表。

每条新帖调用一次 LLM，返回该帖适合的所有 mode + headline + teaser + prompt。
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

_markdown_template = """You are a content classifier. Below is a Trump social media post on {platform}.

Post content: {content}
Posted at: {created_at}
Tags: {tags}

Determine which Pigugu game modes this post fits, and for each match generate a game scenario in English.

Four modes:
- poison_opinion: The post has controversy or a hot-take angle → generate a poison scenario.
  The prompt MUST include: the post content + a controversy angle tag (one of: TRUMP_POLL_BRAG / TARIFF_BLAME / DIPLOMACY_THREAT / ELECTION_FRAUD_HINT / PERSONAL_ATTACK) + a hook (the single weakest, most questionable point in the post).

- debate: The post makes a clear claim/argument → generate a debate scenario.
  The prompt MUST include: the post content + the core claim + Pigugu's provocative stance (pick the angle the user is MOST likely to disagree with) + the post's argument strength + the post's argument weakness.

- prediction: The post contains a verifiable prediction or deadline → generate a prediction scenario.
  The prompt MUST include: the post content + the prediction target + the deadline + the resolution criteria (how to judge correct/wrong when the deadline arrives).

- breaking_bomb: The post is a major breaking event → generate a breaking scenario.
  The prompt MUST include: the post content + the urgency reason. is_urgent is true ONLY for war/military/major disaster.

For each matching mode, generate an object with:
- roast_id: "{{mode_abbrev}}_{{date}}_{{3-digit-seq}}" (date = YYYY-MM-DD from post date, seq starts at 001)
- game_mode: the mode name
- headline: a short display title (the core news, <=120 chars) for the app card
- teaser: Pigugu's provocative teaser line (<=150 chars) — sarcastic, hooks the user to tap the card and start the game
- tags: array of classification tags for this scenario. For poison_opinion, include the controversy angle tag (TRUMP_POLL_BRAG / TARIFF_BLAME / DIPLOMACY_THREAT / ELECTION_FRAUD_HINT / PERSONAL_ATTACK). For other modes, include relevant keyword tags.
- prompt: natural language game scenario description in English, <=500 tokens, formatted for direct Agent consumption
- expires_at: ISO 8601 expiry time
  - poison_opinion / debate: post time + 48h
  - prediction: the deadline
  - breaking_bomb: post time + 2h

Return JSON. Only return modes that actually fit — skip unfit modes:
{{
  "modes": [
    {{
      "roast_id": "poison_2026-05-17_001",
      "game_mode": "poison_opinion",
      "headline": "Trump Boasts About Poll Numbers",
      "teaser": "Excellent by what metric exactly? Tap in if you think this is all hot air.",
      "tags": ["TRUMP_POLL_BRAG"],
      "prompt": "[POISON SCENARIO]\\nTrump just posted on Truth Social: ...",
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

    Returns the list of stored scenario dicts.
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
        modes = _fallback_poison(post)
    except Exception:
        logger.exception("Classifier LLM call failed, using fallback")
        modes = _fallback_poison(post)

    if not modes:
        logger.info("No modes matched for post %s", post.get("post_id"))
        return []

    stored = await _store_scenarios(modes, post=post)
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

    headline = (content[:117] + "...") if len(content) > 120 else content
    roast_id = f"poison_{date_str}_fallback"

    return [
        dict(
            roast_id=roast_id,
            game_mode="poison_opinion",
            headline=headline,
            teaser="Trump just posted. What do you make of this?",
            tags=["GENERIC"],
            prompt=(
                f"[POISON SCENARIO]\n"
                f"Trump just posted: \"{content}\"\n"
                f"Angle: GENERIC — the post content is potentially controversial.\n"
                f"Hook: prompt the player — what do you make of this?"
            ),
            expires_at=expires,
        )
    ]


async def _store_scenarios(
    modes: list[dict],
    post: dict,
) -> list[dict]:
    """Insert into roast_scenarios, skipping duplicates (by roast_id PK)."""
    source = post.get("platform", "")
    source_url = post.get("url", "")
    news_id = str(post.get("id", ""))

    stored = []
    async with AsyncSessionLocal() as session:
        for m in modes:
            roast_id = m.get("roast_id", "")
            game_mode = m.get("game_mode", "")
            prompt = m.get("prompt", "")
            headline = m.get("headline", "")
            teaser = m.get("teaser", "")
            tags = m.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            is_urgent = bool(m.get("is_urgent", False))
            expires_at = _parse_dt(m.get("expires_at"))

            if not roast_id or not game_mode or not prompt:
                logger.warning("Skipping incomplete mode entry: %s", m)
                continue

            roast_id = await _deduplicate_roast_id(session, roast_id)

            try:
                await session.execute(
                    text(
                        "INSERT INTO roast_scenarios "
                        "(roast_id, game_mode, headline, source, source_url, "
                        "teaser, tags, is_urgent, prompt, news_id, expires_at) "
                        "VALUES (:roast_id, :game_mode, :headline, :source, :source_url, "
                        ":teaser, CAST(:tags AS jsonb), :is_urgent, :prompt, :news_id, :expires_at)"
                    ),
                    dict(
                        roast_id=roast_id,
                        game_mode=game_mode,
                        headline=headline,
                        source=source,
                        source_url=source_url,
                        teaser=teaser,
                        tags=json.dumps(tags),
                        is_urgent=is_urgent,
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
