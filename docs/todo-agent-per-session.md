# TODO: PigAgent per-session refactor

## Current State

`PigAgent` is a process-level singleton created once in `bootstrap/factory.py`:

```python
ctx = ContextManager(...)
pig_agent = PigAgent(ctx, ...)
```

All LiveKit sessions share the same `PigAgent` instance → same `ContextManager` → same `_MEMORY`.

## Problem

Shared state across sessions creates coupling:

- `ContextManager._last_wc` needs per-user dict to avoid cross-user races
- `_MEMORY` is a global dict keyed by `user_id` — works correctly but means session shutdown can't clean up memory without affecting other users
- `ContextManager._turn_lock` serializes `add_turn()` globally — turns from different users block each other
- Hard to reason about lifecycle: when does a user's in-memory state get evicted? (currently TTL-based, not session-bound)

## Proposal

One `PigAgent` (and `ContextManager`) per LiveKit session:

```
session.connect() → PigAgent(...) → ContextManager(...)
session.disconnect() → PigAgent teardown → MemoryStore eviction
```

### Benefits
- Natural lifecycle: session open = agent live, session close = agent destroyed
- No cross-user state sharing, no per-user dicts needed
- `_turn_lock` scoped to single user, no global serialization
- Memory cleanup is explicit (session close) rather than TTL-based

### Risks / Open Questions
1. **Resource cost** — LLM runner, Redis connections, PG pool: can these be shared across PigAgent instances while keeping context state per-instance?
2. **LiveKit reconnection** — LiveKit sessions can drop and reconnect rapidly. Is `PigAgent` init fast enough to not add latency?
3. **Context warm-up** — losing `_MEMORY` on disconnect means every reconnect hits Redis/PG. Is that acceptable, or should memory state survive short disconnects?
4. **Compression** — `ContextCompressor` runs as background task per user. With per-session agents, compression needs its own lifecycle management.
5. **Shared resources** — LLM runner, Redis client, PG pool should remain singletons shared across sessions; only context state (MemoryStore, ContextManager) should be per-session.

## Related Code

| File | Concern |
|------|---------|
| `pigagent/bootstrap/factory.py:136` | Singleton PigAgent creation |
| `pigagent/context/manager.py:35-40` | ContextManager init, `_last_wc` per-user workaround |
| `pigagent/context/storage/memory.py:20-23` | Global `_MEMORY` dict, `_TTL_SECONDS` eviction |
| `pigagent/lk/bridge.py:22-41` | `PigAgentVoiceBridge` holds `self._pig` reference |
| `pigagent/lk/session.py` | Session lifecycle, could trigger agent init/teardown |
