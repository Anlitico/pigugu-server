"""Full 6-step pipeline dry-run test.

Step 1: Fetch 7-day AP + Reuters, classified by domain
Step 2: Dedup (DB skip) + multi-dimension scoring → Top 10
Step 3: Deep crawl each of 10 topics
Step 4: Select Top 3 with reasoning
Step 5: Decide mode + generate scenarios
Step 6: Validate + print (store skipped)

Uses Anthropic SDK + DeepSeek V4. Saves output to tmp/dry-runs/.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import anthropic

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_AUTH_TOKEN", ""))

from deep_crawl import deep_crawl_topic  # noqa: E402
from fetch import fetch_week_articles  # noqa: E402
from prompt import SYSTEM_PROMPT  # noqa: E402

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "tmp", "dry-runs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Tool definitions (Anthropic format) ────────────────────────────────────

TOOLS = [
    {
        "name": "fetch_week_headlines",
        "description": "Fetch 7 days of AP + Reuters headlines, classified by domain.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "check_existing_topics",
        "description": "Check which topics already exist in DB (past 7 days). Dry run: returns empty.",
        "input_schema": {
            "type": "object",
            "properties": {
                "titles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Article titles or topic strings to check",
                }
            },
            "required": ["titles"],
        },
    },
    {
        "name": "deep_crawl_topic",
        "description": "Deep-crawl a topic for enrichment: related articles, background, roast angles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Short topic name"},
                "headline": {"type": "string", "description": "Original article headline"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "mark_pipeline_complete",
        "description": "Log pipeline completion (dry run — no-op).",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "object",
                    "description": "total_fetched, candidates, deep_crawled, scenarios_generated",
                }
            },
            "required": ["summary"],
        },
    },
]

# ── Tool handlers ──────────────────────────────────────────────────────────


async def handle_tool(name: str, args: dict) -> str:
    if name == "fetch_week_headlines":
        articles = await fetch_week_articles()
        by_domain: dict[str, int] = {}
        for a in articles:
            d = a.get("domain", "Other")
            by_domain[d] = by_domain.get(d, 0) + 1
        summary = {
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
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)

    elif name == "check_existing_topics":
        titles = args.get("titles", [])
        return json.dumps({
            "existing": [],
            "fresh": len(titles),
            "note": "Dry run — DB not available, all topics treated as fresh.",
        })

    elif name == "deep_crawl_topic":
        result = await deep_crawl_topic(
            args.get("topic", ""),
            args.get("headline", ""),
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "mark_pipeline_complete":
        return "OK — dry run complete."

    return json.dumps({"error": f"Unknown tool: {name}"})


# ── Main ──────────────────────────────────────────────────────────────────


async def main():
    client = anthropic.Anthropic(
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"),
        api_key=os.environ["ANTHROPIC_API_KEY"],
    )

    messages = [
        {
            "role": "user",
            "content": (
                "Run the FULL 6-step Pigugu news crawl pipeline.\n\n"
                "Step 1: Call fetch_week_headlines to get 7 days of AP + Reuters articles.\n"
                "Step 2: Collect ALL titles, call check_existing_topics to dedup, then score each "
                "remaining article on 6 dimensions (us_relevance, roast_potential, controversy, "
                "timeliness, social_buzz, trump_related) × domain_weight. Print the scoring grid "
                "with your top 10 candidates. ENSURE topic diversity — at least 5 domains represented.\n"
                "Step 3: Call deep_crawl_topic for EACH of the 10 candidates. Read ALL results.\n"
                "Step 4: From enriched results, select the TOP 3. Print clear reasoning for each.\n"
                "Step 5: For each pick, decide mode (roast_together always; debate if clear "
                "debatable proposition exists). Generate FULL scenarios with ALL required fields.\n"
                "Step 6: Self-validate (headline ≤120c, teaser ≤150c, prompt ≤500w, "
                "tags 3-5, roast_id unique, ≥2 domains covered). Print everything.\n"
                "DO NOT call store_game_scenario (DB not available). "
                "End with mark_pipeline_complete."
            ),
        }
    ]

    print("=" * 70)
    print("  Pigugu News Crawler — 6-Step Dry Run")
    print("=" * 70)

    full_output: list[str] = []
    usage = None

    for turn in range(30):  # more turns for deep crawl loop
        resp = await asyncio.to_thread(
            client.messages.create,
            model=os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro[1m]"),
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": resp.content})
        if hasattr(resp, "usage"):
            usage = resp.usage

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        text_blocks = [b for b in resp.content if b.type == "text"]

        for b in text_blocks:
            print(b.text)
            full_output.append(b.text)

        if not tool_uses:
            print(f"\n{'─'*70}")
            print(f"  Stop reason: {resp.stop_reason}")
            if usage:
                print(f"  Tokens: {usage.input_tokens} in / {usage.output_tokens} out")
            break

        tool_results = []
        for b in tool_uses:
            print(f"\n  🔧 [{b.name}]", end=" ", flush=True)
            result = await handle_tool(b.name, b.input)
            if b.name == "fetch_week_headlines":
                data = json.loads(result)
                print(f"→ {data['total_count']} articles, {len(data['by_domain'])} domains")
            elif b.name == "deep_crawl_topic":
                data = json.loads(result)
                n_articles = len(data.get("articles", []))
                n_angles = len(data.get("angles", []))
                print(f"→ {n_articles} related, {n_angles} angles")
            elif b.name == "check_existing_topics":
                data = json.loads(result)
                print(f"→ {data['fresh']} fresh")
            else:
                print("→ done")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": result[:16000],
            })

        messages.append({"role": "user", "content": tool_results})

    # ── Save outputs ──────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    output_text = "\n".join(full_output)

    md_path = os.path.join(OUTPUT_DIR, f"{ts}-6step-output.md")
    with open(md_path, "w") as f:
        f.write(output_text)
    print(f"\n  📄 Full output → {md_path}")

    if usage:
        summary_path = os.path.join(OUTPUT_DIR, f"{ts}-summary.json")
        with open(summary_path, "w") as f:
            json.dump({
                "timestamp": ts,
                "model": os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro[1m]"),
                "pipeline": "6-step",
                "turns": turn + 1,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }, f, ensure_ascii=False, indent=2)
        print(f"  📄 Summary → {summary_path}")

    print("=" * 70)
    print("  Dry run complete.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
