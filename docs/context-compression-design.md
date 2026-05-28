# Context Compression & Assembly Design

## 1. Unified Context Format

All context layers are flattened into a single `list[ConversationRecord]` with `turn_number`:

```
turn  -3: [system] L2 profile          virtual (from summaries)
turn  -2: [system] L3 summary          virtual (from summaries)
turn  51: [user]   roast prompt        real turn (from raw turns)
turn  -1: [user]   L4 roast summary    virtual (from summaries)
turn  61: [user]   raw turn            real turn
turn  62: [assistant] raw turn         real turn
turn  81: [user]   2nd roast prompt    real turn (different rid)
turn  82: [user]   raw turn            real turn
```

- **Virtual turns**: negative `turn_number`, generated from summaries
- **Real turns**: positive `turn_number`, from Redis `ctx:{id}:turns` or PG `agent_conversations`
- L2/L3 = `system` role (AI-generated metadata)
- Roast prompt / L4 / raw = `user` / `assistant` / `tool` roles (conversation content)

## 2. Assembly (assemble → to_records)

1. Read summaries from Redis (`ctx:{id}:summaries`) or PG (`context_summaries`)
2. Generate virtual records: L2 (-3), L3 (-2), roast_prompt (real turn), L4 (-1)
3. Read raw turns: `turn_number > end_turn`
4. Merge into one sorted list (virtual first, then real by turn_number)

`to_messages()` converts this list to LLM input.

## 3. Compression

### Trigger Conditions
- Real turn count > `CONTEXT_MAX_TURNS` (100) **OR**
- Total tokens > `CONTEXT_TOKEN_BUDGET_CAP` (200K)

### Steps

**Step 1: Find Roast Prompt Boundary**

```
if summaries.roast_id == current_rid:
    prompt_turn = summaries.roast_prompt_turn   (same roast, use stored)
else:
    prompt_turn = _find_roast_prompt(records)   (new/first roast, from raw turns)
```

`_find_roast_prompt`: find latest `roast_instance_id` from bottom → first matching record from top.

**Step 2: Split by Layer**

| Layer | Input |
|-------|-------|
| L2 facts | Full context minus L2 itself (L3 + prompt + L4 + all raw) |
| L3 session | L3 virtual turn + real turns where `turn < prompt_turn` and no `roast_instance_id` |
| L4 roast | L4 virtual turn + real turns where `turn > prompt_turn` and same `roast_instance_id` |

**Step 3: Per-layer Trigger**

| Layer | Condition |
|-------|-----------|
| L2 | New fact turns exist |
| L3 | New real turns in L3 range |
| L4 | New real turns in L4 range |

**Step 4: Compress**

| Layer | Method |
|-------|--------|
| L2 | Extract facts → LLM incremental profile update |
| L3 | `existing_summary` + new turns → merge summary |
| L4 | `existing_roast` + new turns → merge summary |

**Step 5: Persist**

Write new summaries to Redis (`ctx:{id}:summaries`) and PG (`context_summaries`):

- `end_turn` = last compressed real turn number
- `roast_prompt_turn` = prompt's turn_number (unchanged for same roast)
- `roast_id` = current roast instance ID

## 4. Scenarios

### A. First Compression (single roast)
```
raw: turn 1-50 (no rid), turn 51 (prompt rid=A), turn 52-80 (rid=A)
summaries: empty
→ prompt from raw: turn 51
→ L3: turn 1-50
→ L4: turn 52-80
→ end_turn=80, roast_prompt_turn=51, roast_id=A
```

### B. Same Roast Incremental
```
raw: turn 81-100 (rid=A, from end_turn+1)
summaries: end_turn=80, roast_id=A, roast_prompt_turn=51
→ roast_id matches → prompt_turn=51 (from summaries)
→ L3: no new turns (1-80 already compressed)
→ L4: turn 81-100
→ end_turn=100, roast_prompt_turn=51 (unchanged)
```

### C. New Roast
```
raw: turn 81-100 (old, may be stale), turn 101 (prompt rid=B), turn 102-120
summaries: end_turn=80, roast_id=A, roast_prompt_turn=51
→ roast_id A ≠ B → prompt from raw: turn 101
→ L3: turn 81-100 (< 101, regardless of rid)
→ L4: turn 102-120 (> 101, rid=B)
→ end_turn=120, roast_prompt_turn=101, roast_id=B
```

## 5. Data Structures

### Redis: `ctx:{id}:summaries` (JSON)
```json
{
  "end_turn": 80,
  "l2_profile": "...",
  "l3_session": "...",
  "roast_id": "rid-A",
  "roast_prompt": "...",
  "roast_prompt_turn": 51,
  "l4_roast": "..."
}
```

### PG: `context_summaries`
| Column | Type | Note |
|--------|------|------|
| `user_id` | TEXT PK | |
| `end_turn` | INTEGER PK | last compressed real turn |
| `l2_profile` | TEXT | L2 user profile |
| `l3_session` | TEXT | L3 session summary |
| `roast_id` | TEXT | active roast ID |
| `roast_prompt` | TEXT | roast game rules, never compressed |
| `roast_prompt_turn` | INTEGER | **NEW** prompt turn number (split anchor) |
| `l4_roast` | TEXT | L4 roast summary |
| `model_used` | TEXT | |
| `created_at` | TIMESTAMPTZ | |

### Redis: `ctx:{id}:turns` (List)
Raw conversation turns (`ConversationRecord` JSON), LTRIM to `CONTEXT_HOT_WINDOW_SIZE`.

## 6. Key Principles

1. **Single split anchor**: `roast_prompt_turn` is the only boundary for L3/L4/raw splitting
2. **No heterogeneous queries**: compressor receives the full unified list, no Redis/PG reads inside
3. **Same logic for all scenarios**: first compression, same-roast incremental, new roast — same code path
4. **Virtual turns for summaries**: negative turn_number distinguishes summary records from real turns, but both participate in the unified list
