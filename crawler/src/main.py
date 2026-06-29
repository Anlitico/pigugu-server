"""News Crawler Agent — single entry point.

Uses Anthropic SDK → DeepSeek V4 (via ANTHROPIC_BASE_URL).
No Agent SDK, no CLI — just Python.

CronJob:    python -m src.main
Local test: python -m src.main --dry-run
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import anthropic
from loguru import logger

from .db import AsyncSessionLocal
from .deep_crawl import deep_crawl_topic
from .fetch import fetch_week_articles
from .models import RoastScenario
from .prompt import SYSTEM_PROMPT

# ── Tool definitions (Anthropic format) ───────────────────────────────────

TOOLS: list[dict[str, object]] = [
    {
        "name": "fetch_week_headlines",
        "description": "Fetch 7-day AP + Reuters headlines, classified by domain.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_recent_scenarios",
        "description": "Return full text (headline, teaser, prompt) of all scenarios from past 7 days.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "deep_crawl_topic",
        "description": "Deep-crawl one topic for enrichment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "headline": {"type": "string"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "store_game_scenario",
        "description": "Store a game scenario. Hard-validates constraints. Returns errors if invalid.",
        "input_schema": {
            "type": "object",
            "properties": {
                "roast_id": {"type": "string"},
                "game_mode": {"type": "string", "enum": ["roast_together", "debate"]},
                "headline": {"type": "string"},
                "teaser": {"type": "string"},
                "prompt": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string", "enum": ["ap", "reuters"]},
                "source_url": {"type": "string"},
                "article_id": {"type": "string"},
                "expires_at": {"type": "string"},
            },
            "required": [
                "roast_id", "game_mode", "headline", "teaser", "prompt",
                "tags", "source", "source_url", "article_id", "expires_at",
            ],
        },
    },
    {
        "name": "mark_pipeline_complete",
        "description": "Log pipeline completion.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "object"}},
            "required": ["summary"],
        },
    },
]

# ── Tool handlers ───────────────────────────────────────────────────────


async def _handle_fetch_week(_args: dict) -> str:
    articles = await fetch_week_articles()
    by_domain: dict[str, int] = {}
    for a in articles:
        d = a.get("domain", "Other")
        by_domain[d] = by_domain.get(d, 0) + 1
    return json.dumps(
        {
            "total_count": len(articles),
            "by_domain": dict(sorted(by_domain.items(), key=lambda x: -x[1])),
            "articles": [
                {
                    "article_id": a["article_id"],
                    "source": a["source"],
                    "title": a["title"],
                    "summary": a.get("summary", "")[:200],
                    "url": a.get("url", ""),
                    "domain": a.get("domain", "Other"),
                    "published_at": a.get("published_at", ""),
                }
                for a in articles
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


async def _handle_list_recent(_args: dict) -> str:
    from sqlalchemy import select

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(
                    RoastScenario.roast_id,
                    RoastScenario.headline,
                    RoastScenario.teaser,
                    RoastScenario.prompt,
                    RoastScenario.game_mode,
                    RoastScenario.source,
                    RoastScenario.created_at,
                )
                .where(RoastScenario.created_at >= cutoff)
                .order_by(RoastScenario.created_at.desc())
            )
            rows = result.fetchall()

        scenarios = [
            {
                "roast_id": r[0],
                "headline": r[1],
                "teaser": r[2],
                "prompt": (r[3] or "")[:300],
                "game_mode": r[4],
                "source": r[5],
                "created_at": r[6].isoformat() if r[6] else "",
            }
            for r in rows
        ]
        return json.dumps({"count": len(scenarios), "scenarios": scenarios}, ensure_ascii=False, indent=2)
    except Exception:
        logger.warning("DB unavailable — returning empty for dedup")
        return json.dumps({"count": 0, "scenarios": [], "note": "DB unavailable — all topics treated as fresh"})


async def _handle_deep_crawl(args: dict) -> str:
    result = await deep_crawl_topic(args.get("topic", ""), args.get("headline", ""))
    return json.dumps(result, ensure_ascii=False, indent=2)


async def _handle_store(args: dict) -> str:
    """Hard-validate then persist. Returns error message if validation fails."""
    errors: list[str] = []

    headline = args.get("headline", "")
    teaser = args.get("teaser", "")
    tags = args.get("tags", [])
    source = args.get("source", "")
    game_mode = args.get("game_mode", "")
    roast_id = args.get("roast_id", "")
    prompt = args.get("prompt", "")
    source_url = args.get("source_url", "")
    article_id = args.get("article_id", "")
    expires_at = args.get("expires_at", "")

    if len(headline) > 50:
        errors.append(f"headline too long: {len(headline)} chars (max 50)")
    if not headline.strip():
        errors.append("headline empty")
    if len(teaser) > 80:
        errors.append(f"teaser too long: {len(teaser)} chars (max 80)")
    if not teaser.strip():
        errors.append("teaser empty")
    if game_mode not in ("roast_together", "debate"):
        errors.append(f"invalid game_mode: '{game_mode}'")
    if source not in ("ap", "reuters"):
        errors.append(f"invalid source: '{source}'")
    if not isinstance(tags, list) or not (3 <= len(tags) <= 5):
        errors.append(f"tags must be 3-5 items, got {len(tags) if isinstance(tags, list) else '?'}")
    if not roast_id.strip():
        errors.append("roast_id empty")
    if not prompt.strip():
        errors.append("prompt empty")
    if not source_url.strip():
        errors.append("source_url empty")
    if not expires_at:
        errors.append("expires_at missing")

    if errors:
        msg = "VALIDATION FAILED:\n" + "\n".join(f"  - {e}" for e in errors)
        logger.warning("store rejected: {}", errors)
        return json.dumps({"error": msg})

    expires = None
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass

    async with AsyncSessionLocal() as session:
        row = RoastScenario(
            roast_id=roast_id,
            game_mode=game_mode,
            headline=headline,
            teaser=teaser,
            prompt=prompt,
            tags=tags,
            source=source,
            source_url=source_url,
            news_id=article_id,
            status="active",
            expires_at=expires,
        )
        session.add(row)
        await session.commit()

    logger.info("STORED {} ({})", roast_id, game_mode)
    return json.dumps({"stored": roast_id, "mode": game_mode})


async def _handle_mark_complete(args: dict) -> str:
    s = args.get("summary", {})
    logger.info("PIPELINE DONE — fetched={} curated={} stored={}",
                s.get("total_fetched", 0), s.get("candidates", 0), s.get("stored", 0))
    return json.dumps({"status": "complete"})


HANDLERS = {
    "fetch_week_headlines": _handle_fetch_week,
    "list_recent_scenarios": _handle_list_recent,
    "deep_crawl_topic": _handle_deep_crawl,
    "store_game_scenario": _handle_store,
    "mark_pipeline_complete": _handle_mark_complete,
}

# ── Agent loop ──────────────────────────────────────────────────────────


async def run(dry_run: bool = False) -> None:
    client = anthropic.Anthropic(
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"),
        api_key=os.environ["ANTHROPIC_API_KEY"],
    )

    user_prompt = (
        "Run the FULL 6-step Pigugu news crawl pipeline.\n\n"
        "Step 1: fetch_week_headlines. Report domain distribution.\n"
        "Step 2: list_recent_scenarios. Read ALL full text. Semantic dedup.\n"
        "Then multi-dimension score × domain_weight + bonuses. Top 10 ≥5 domains.\n"
        "Step 3: deep_crawl_topic for EACH of 10.\n"
        "Step 4: Select Top 3 from enriched results.\n"
        "Step 5: Q1→Q2→Q3 → ONE mode each. Generate full FACTS+VOICE.\n"
        "Step 6: Self-validate (headline≤50, teaser≤80, tags 3-5, ≥2 domains).\n"
    )

    if dry_run:
        user_prompt += (
            "\nDRY RUN — DO NOT call store_game_scenario. "
            "Just print the 3 scenarios with full FACTS/VOICE. End with mark_pipeline_complete."
        )
    else:
        user_prompt += (
            "\nCall store_game_scenario for each. If validation fails, FIX and retry. "
            "End with mark_pipeline_complete."
        )

    messages: list[dict[str, object]] = [{"role": "user", "content": user_prompt}]

    model = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro[1m]")
    logger.info("Starting pipeline (dry_run={}, model={})", dry_run, model)
    usage = None
    stored = 0

    for turn in range(35):
        with client.messages.stream(
            model=model,
            max_tokens=24000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
        ) as stream:
            final = stream.get_final_message()

        if hasattr(final, "usage"):
            usage = final.usage

        messages.append({"role": "assistant", "content": final.content})

        tool_uses = [b for b in final.content if b.type == "tool_use"]
        text_blocks = [b for b in final.content if b.type == "text"]

        for b in text_blocks:
            logger.info("[AGENT] {}", b.text[:200])

        if not tool_uses:
            logger.info("Pipeline finished — stop_reason={}", final.stop_reason)
            break

        tool_results: list[dict[str, object]] = []
        for b in tool_uses:
            name = b.name
            handler = HANDLERS.get(name)
            if handler is None:
                result = json.dumps({"error": f"Unknown tool: {name}"})
            elif name == "store_game_scenario" and dry_run:
                result = json.dumps({"stored": "dry-run-skip", "mode": "dry-run"})
            else:
                result = await handler(b.input)

            if name == "store_game_scenario":
                d = json.loads(result)
                if "stored" in d:
                    stored += 1

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": result[:16000],
            })

        messages.append({"role": "user", "content": tool_results})

    logger.info("=" * 50)
    logger.info("Pipeline complete — stored={}", stored)
    if usage:
        logger.info("Tokens: {} in / {} out", usage.input_tokens, usage.output_tokens)
    logger.info("=" * 50)


# ── CLI ─────────────────────────────────────────────────────────────────

def main() -> None:
    logger.remove()
    logger.add(sys.stdout, format="{time:HH:mm:ss} [{level}] {message}", level="INFO")

    dry_run = "--dry-run" in sys.argv
    asyncio.run(run(dry_run=dry_run))


if __name__ == "__main__":
    main()
