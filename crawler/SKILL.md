---
name: news-crawler
description: Daily news crawl pipeline — fetch AP + Reuters headlines, curate roast-worthy stories, generate game scenarios.
---

# News Crawler Agent

Daily automated pipeline that fetches headlines from AP and Reuters, curates the
most roast-worthy stories for American audiences, and generates `poison_opinion` +
`debate` game scenarios for the Pigugu app.

## How It Runs

Invoked daily at 09:00 UTC by K8s CronJob. Inside the container:

```bash
python -m src.main
```

The Claude Agent SDK runs an autonomous agent loop — no human approval needed
(`permission_mode: bypassPermissions`).

## Architecture

```
┌──────────────────────────────────────────────┐
│  Anthropic SDK → DeepSeek V4                 │
│  (tool-use loop in src/main.py)              │
│                                              │
│  System Prompt ← src/prompt.py               │
│  Tools         ← 5 Python handlers in main   │
│                                              │
│  1. fetch_week_headlines    → AP+Reuters RSS│
│  2. list_recent_scenarios   → semantic dedup│
│  3. deep_crawl_topic        → enrichment    │
│  4. store_game_scenario     → roast_scenarios│
│  5. mark_pipeline_complete                  │
└──────────────────────────────────────────────┘
```

## Curation Criteria

The agent selects ≤3 articles based on:
1. **US Citizen Relevance** (primary) — does this affect ordinary Americans?
2. **Roast Potential** — is there absurdity, irony, hypocrisy?
3. **Timeliness** — is it today's news?
4. **Topic Diversity** — don't pick 3 about the same subject

## Scenario Output

- **poison_opinion** (always): controversy angle + weak point to roast
- **debate** (optional): only if article has a clear debatable claim

## Database

Writes to two tables shared with `pigugu-server`:
- `raw_articles` — original headlines with curator summaries + scores
- `roast_scenarios` — game scenarios consumed by the frontend

## Deployment

```bash
# Build
docker build -t pigugu-crawler:latest -f crawler/Dockerfile crawler/

# Run locally (test)
docker run -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
           -e DATABASE_URL=$DATABASE_URL \
           pigugu-crawler:latest

# Deploy CronJob
kubectl apply -f crawler/deploy/cronjob.yaml
```

## Env Vars

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API key (Agent SDK) |
| `DATABASE_URL` | PostgreSQL connection (shared with pigugu-server) |
| `AGENT_MODEL` | (optional) model override, default `claude-haiku-4-5-20251001` |
