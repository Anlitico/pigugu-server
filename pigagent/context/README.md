# Context Module

4-layer agent context architecture with data-driven roast lifecycle, token-budget-triggered compression, and structured summarization (inspired by Claude Code's compaction pipeline).

## Quick Start

```python
from context import ContextManager

ctx = ContextManager(
    redis_client=redis,
    pg_pool=pg,
)

# Before each LLM call
messages = await ctx.load(user_id="u1")
response = await llm.chat(messages)

# After each LLM call
await ctx.add_turn(
    user_id="u1", role="assistant", content=response,
    tool_calls=[...],  # optional
)

# Game state
await ctx.write_game_state(user_id="u1", state={"score": "100"})
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      ContextManager                             │
│  add_turn()  load()  assemble()  end_roast()  write_game_state()│
└──────────────┬──────────────────────────────────────────────────┘
               │
    ┌──────────┼──────────┐
    ▼          ▼          ▼
┌─────────┐ ┌───────┐ ┌──────────────┐
│ Redis   │ │  PG   │ │ Compression  │
│ (hot)   │ │(cold) │ │ Pipeline     │
└─────────┘ └───────┘ └──────────────┘
```

### 4 Context Layers (LLM-visible)

```
L1 — System Prompt      ~3-5K tokens   (prefix-cached, persona_prompt)
L2 — User Profile       ~1-2K tokens   (prefix-cached, structured profile)
L3 — Session Summary    variable        (recursive summary, anchor-based)
L4 — Roast Context      variable        (game rules + gameplay summary)
Raw Turns               0-100 messages  (uncompressed, after anchor)
```

### Key Types

| Type | File | Purpose |
|------|------|---------|
| `ContextManager` | [manager.py](pigagent/context/manager.py) | Entry point — add turns, assemble context, trigger compression |
| `ConversationRecord` | [schema.py](pigagent/context/schema.py) | Stored turn — full Message fidelity including tool_calls |
| `SummaryRecord` | [schema.py](pigagent/context/schema.py) | Compressed summary with `end_turn` anchor |
| `WorkingContext` | [schema.py](pigagent/context/schema.py) | Assembled LLM-visible context with `to_messages()` |
| `ContextSnapshot` | [snapshot.py](pigagent/context/snapshot.py) | Token counting, segment splitting, compression triggers |
| `RoastState` | [roast.py](pigagent/context/roast.py) | Pure functions for roast lifecycle (assignment, staleness, queries) |
| `ContextCompressor` | [compression/compressor.py](pigagent/context/compression/compressor.py) | Unified compression pipeline (L2+L3+L4) |

### Storage

| Store | What | Key Pattern |
|-------|------|-------------|
| Redis | Raw turns | `ctx:{uid}:turns` (RPUSH, LRANGE) |
| Redis | L3 summary | `ctx:{uid}:summary` |
| Redis | L4 roast prompt | `ctx:{uid}:roast:prompt` |
| Redis | L4 roast summary | `ctx:{uid}:roast:summary` |
| Redis | Game state | `ctx:{uid}:game_state` (hash) |
| Redis | User memory | `pigugu:user:{uid}:memory` (hash) |
| Redis | Compression lock | `ctx:{uid}:compressing` (TTL 5min) |
| PG | All turns | `agent_conversations` |
| PG | User facts | `user_facts` |
| PG | User profile | `user_memory` |

## Compression Pipeline

Triggered **asynchronously** during `assemble()` — never blocks the current request.

```
Compression trigger:
  Primary: total tokens (summary + records) > 200K
  Backup:  len(records) > 400 turns

Two scenarios:
  free_chat  → L2 extract + L3 merge
  roast      → L2 extract + L3 merge + L4 roast compress

Execution:
  Phase 1 — Concurrent LLM calls (asyncio.gather)
  Phase 2 — Sequential writes (Redis first, then PG)
```

### L2 — User Fact Extraction

Extracts durable facts from conversation → merges into structured user profile.

```
extract_facts(turns) → [{fact, category}, ...]
summarize_profile(facts, existing) → structured profile text
```

Prompt: [l2_extract_facts.j2](pigagent/context/prompts/l2_extract_facts.j2)

### L3 — Recursive Session Summary

Single recursive summary. First call compresses turns into summary. Subsequent calls merge existing summary with new turns.

```
compress_turns(turns) → summary
merge_summary(existing, new_turns) → updated summary
```

Prompts: [l3_summary_initial.j2](pigagent/context/prompts/l3_summary_initial.j2), [l3_summary_merge.j2](pigagent/context/prompts/l3_summary_merge.j2)

Both use Claude Code-style chain-of-thought (`STEP 1 ANALYZE → discard, STEP 2 OUTPUT → keep`) with structured sections:

- `[Topics & Themes]` / `[User Signals]` / `[Decisions & Outcomes]` / `[Recent Conversation]` / `[Pending]`

### L4 — Roast Compression

Roast prompt (game rules) is **never passed to the LLM** — preserved verbatim by code.

```
Output format:
  {roast_prompt}
  ---
  {LLM-compressed gameplay summary}
```

Merge works the same way: strip prompt header → LLM merges gameplay body → prepend prompt.

Prompts: [l4_roast_initial.j2](pigagent/context/prompts/l4_roast_initial.j2), [l4_merge_roast.j2](pigagent/context/prompts/l4_merge_roast.j2)

### Token Budgets

| Layer | Source | ~Tokens |
|-------|--------|---------|
| L1 | `persona_prompt` | 3-5K |
| L2 | User profile | 1-2K |
| L3 | Session summary | 6-10K |
| L4 | Roast summary | 6K |
| Raw | Uncompressed turns | variable |

Total cap: 200K (config `CONTEXT_TOKEN_BUDGET_CAP`).

## Roast Lifecycle

Data-driven — no separate state flags in Redis.

```
Roast starts:  add_turn() → assign_roast_id() sets roast_id on new record
Roast active:  records[-1].roast_id is set → L4 compression runs
Roast ends:    24h staleness OR new roast_id appears
               → next message inherits None
               → compression scenario switches to free_chat
               → L4 content naturally merges into L3 over time
```

No explicit end-of-roast cleanup needed. `end_roast()` is a logging-only no-op.

## Anchor Mechanism

`SummaryRecord.end_turn` marks the last turn covered by the summary.

```
assemble():
  anchor = summary.end_turn  (or 0 if no summary)
  raw_records = get_hot_turns(100, after_anchor=anchor)
  → turns ≤ anchor are in summary
  → turns > anchor are raw
```

After compression, anchor moves forward. The latest turn is always raw because compression is async (hasn't caught up yet).

## Fault Tolerance

- `asyncio.gather(return_exceptions=True)` — any LLM failure doesn't cascade
- L3 LLM failure → keep old summary (no data loss)
- L2 LLM failure → skip fact extraction (profile unchanged)
- L4 LLM failure → keep old roast summary
- Exception handler always releases `compressing` lock (5min TTL backup)
- Redis down → graceful degradation (empty records, no crash)
- PG down → async flush silently fails, Redis hot path continues

## Config Reference

| Config | Default | Description |
|--------|---------|-------------|
| `CONTEXT_TOKEN_BUDGET_CAP` | 200,000 | Token budget trigger |
| `CONTEXT_HOT_WINDOW_SIZE` | 500 | Max turns in Redis |
| `CONTEXT_MAX_TURNS` | 400 | Force compression above this |
| `CONTEXT_ROAST_COMPRESSION_RATIO` | 0.05 | L4 threshold (% of budget) |
| `CONTEXT_ROAST_COMPRESSION_MIN_TOKENS` | 1,000 | L4 threshold floor |
| `CONTEXT_L3_COMPRESS_MAX_WORDS` | 5,000 | L3 initial summary size |
| `CONTEXT_L3_MERGE_MAX_WORDS` | 8,000 | L3 merge summary size |
| `CONTEXT_L4_ROAST_MAX_WORDS` | 5,000 | L4 roast summary size |
| `CONTEXT_L2_PROFILE_MAX_WORDS` | 1,500 | L2 profile size |

## Directory Structure

```
context/
├── __init__.py          # Public exports
├── schema.py            # Data structures (ConversationRecord, SummaryRecord, WorkingContext, ...)
├── snapshot.py          # ContextSnapshot — token counting + segment analysis
├── roast.py             # RoastState — pure roast lifecycle functions
├── manager.py           # ContextManager — entry point + orchestrator
├── README.md
│
├── storage/
│   ├── redis.py         # RedisStorage — turns, summaries, roast data, locks
│   └── pg.py            # PgStorage — turn archive, facts, profile persistence
│
├── compression/
│   ├── compressor.py    # ContextCompressor — pipeline orchestrator
│   ├── l2_facts.py      # Fact extraction + profile summarization
│   ├── l3_session.py    # Recursive session summary (compress + merge)
│   ├── l4_roast.py      # Roast compression (prompt preservation)
│   └── README.md        # Compression pipeline documentation
│
└── prompts/
    ├── l2_extract_facts.j2
    ├── l2_profile_initial.j2
    ├── l2_profile_merge.j2
    ├── l3_summary_initial.j2
    ├── l3_summary_merge.j2
    ├── l4_roast_initial.j2
    └── l4_merge_roast.j2

Tests live in `tests/unit/context/`, one file per module:

```
tests/unit/context/
├── test_schema.py       # ConversationRecord, SummaryRecord, TokenBudget, UserMemory,
│                          RoastContext, WorkingContext
├── test_storage.py      # RedisKeys, RedisStorage (happy + exception), PgStorage
├── test_manager.py      # ContextManager — constructor, add_turn, assemble, load
├── test_snapshot.py     # ContextSnapshot — scenario detection, segment splitting
├── test_roast.py        # RoastState — roast_id assignment, staleness, queries
├── test_compression.py  # L2/L3/L4 edge cases (empty turns, no LLM calls)
└── test_context.py      # Core types: Message serialization, tool call validation, constants
```

Run with `pytest tests/unit/context/ -m "not integration"` (integration tests hit real LLM APIs).
```
