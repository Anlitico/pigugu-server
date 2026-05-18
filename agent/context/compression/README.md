# Compression Pipeline

## Overview

When a user's conversation exceeds the token budget (default 200K), the compression pipeline reduces context size by converting raw turns into structured summaries — without losing information.

```
raw turns (200K+ tokens)
        │
        ▼
┌───────────────────────────────────┐
│  L2: User Facts → PG             │  extract preferences, traits, facts
│  L3: Session Summary → Redis     │  recursive conversation summary
│  L4: Roast Summary → Redis       │  game-state-aware compression (optional)
└───────────────────────────────────┘
        │
        ▼
LLM context (~20K tokens)
```

## Architecture

```
compression/
├── compressor.py    # Pipeline orchestrator — decides what to compress
├── l2_facts.py      # Extract user facts from conversation → profile
├── l3_session.py    # Recursive conversation summary (compress / merge)
├── l4_roast.py      # Roast (game) segment compression
└── README.md
```

Supporting modules:
- `context/snapshot.py` — `ContextSnapshot` wraps record list with token counting and segment analysis
- `context/roast.py` — `RoastState` for roast lifecycle (assignment, staleness, queries)
- `context/storage/redis.py` — Redis I/O (turns, summaries, roast data, compression lock)
- `context/storage/pg.py` — PostgreSQL I/O (facts, profile persistence)

## Trigger

Compression is triggered **asynchronously** during `ContextManager.assemble()`, never blocking the current request:

```
assemble()
  → is_compressing() == False?
  → new_records = get_hot_turns(500, after_anchor=anchor)
  → ContextCompressor.run(records=new_records, existing_summary=...)
     → ContextSnapshot.should_compress()
        ├── total_tokens (summary + records) > 200K  ← primary
        └── len(records) > 400                        ← backup (prevent Redis trim)
```

- `compressing` lock (Redis, 5min TTL) prevents concurrent compression for the same user
- Only new turns since last anchor are passed (incremental, not full history)

## Two Scenarios

### free_chat

No active roast. All records → L2 + L3.

```
Phase 1 (concurrent LLM)
  extract_facts(records)        → user facts
  compress_l3(summary, records) → updated summary

Phase 2 (sequential writes)
  PG:    facts → user_facts table
  PG:    facts+existing → user_profile (LLM summarize)
  Redis: profile → user_memory hash
  Redis: summary → summary key (with end_turn anchor)
```

### roast

Active roast (records[-1].roast_id is set). Records split at roast boundary:

```
records: [pre_roast...] [roast...]
             ↓              ↓
            L2+L3          L4? (if roast tokens > 10K)
```

```
Phase 1 (concurrent LLM)
  extract_facts(pre_roast)              → user facts
  compress_l3(summary, pre_roast)       → updated session summary
  compress_roast(roast, prompt, exist)  → updated roast summary

Phase 2 (sequential writes)
  Same as free_chat + roast summary → Redis roast:summary key
```

**L4 compression trigger**: roast tokens > max(200K × 5%, 1000) = 10K tokens. Short roast sessions stay raw.

## Key Design Decisions

### Incremental compression

Only new turns (since last anchor) are sent to the LLM. The existing summary carries all prior history. This keeps LLM input bounded regardless of total conversation length.

### Single recursive L3 summary

No global/recent dual summary. One summary, merged with new turns each compression cycle. Prompt prioritizes recent information to prevent detail loss.

### Anchor-based position tracking

`SummaryRecord.end_turn` marks the last turn covered by the summary. In `assemble()`, `get_hot_turns(after_anchor=anchor)` returns only turns NOT yet summarized. This naturally handles async compression — the latest turn is always raw because compression hasn't caught up yet.

### Roast prompt preservation

L4 output format:
```
{roast_prompt verbatim}
---
{gameplay summary}
```

The roast prompt (game rules) is never compressed — it's included verbatim at the top of every L4 summary.

### Token-based triggers, not turn-based

- Compression: `total_tokens > 200K` (not "every N turns")
- L4: `roast_tokens > 10K` (not "roast > N turns")

Token count is the metric that actually matters for LLM context windows.

### Fault tolerance

- `asyncio.gather(return_exceptions=True)` — any LLM call failure doesn't cascade
- L3 failure → keep old summary (no data loss)
- L2 failure → skip fact extraction (profile unchanged)
- L4 failure → skip roast compression (roast stays raw)
- Exception handler always releases `compressing` lock

## Config Reference

| Key | Default | Description |
|-----|---------|-------------|
| `CONTEXT_TOKEN_BUDGET_CAP` | 200,000 | Token budget for LLM context |
| `CONTEXT_HOT_WINDOW_SIZE` | 500 | Max turns stored in Redis |
| `CONTEXT_MAX_TURNS` | 400 | Force compression above this many new turns |
| `CONTEXT_ROAST_COMPRESSION_RATIO` | 0.05 | L4 threshold as fraction of budget |
| `CONTEXT_ROAST_COMPRESSION_MIN_TOKENS` | 1,000 | L4 threshold floor |

## Data Flow (end to end)

```
add_turn()                           assemble()
   │                                     │
   │  ConversationRecord                 │  read summary → anchor
   │  assign roast_id                    │  read raw turns > anchor
   │  RPUSH to Redis                     │  load roast context
   │  async flush to PG                  │  build WorkingContext
   │                                     │  fire compression task
   │                                     │
   ▼                                     ▼
┌─────────────────────────────────────────────────────┐
│                    Redis                            │
│  ctx:u1:turns       [t1, t2, ..., t_n]  (RPUSH)   │
│  ctx:u1:summary     {text, end_turn}               │
│  ctx:u1:compressing "1" (lock, TTL 5min)           │
│  ctx:u1:roast:*     prompt / summary               │
│  pigugu:user:u1:memory  profile / stats            │
└─────────────────────────────────────────────────────┘
   │                                     │
   │  persist_facts                      │  read_profile
   │  upsert_profile                     │  read_new_facts
   ▼                                     ▼
┌─────────────────────────────────────────────────────┐
│                    PostgreSQL                       │
│  agent_conversations   (full turn archive)         │
│  user_facts            (extracted facts)            │
│  user_memory           (profile summary)            │
└─────────────────────────────────────────────────────┘
```
