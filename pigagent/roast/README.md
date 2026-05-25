# Roast Module

Game mode state machine for Pigugu voice agent. Three PRD-aligned modes, each with triggers, prompts, and async state detection.

## Structure

```
roast/
├── types.py              # Mode, Phase enums
├── base.py               # GameMode ABC + Trigger dataclass + tick()
├── state.py              # RoastState data + Redis/PG persistence
├── pending.py            # Prompt bridge: write (tick) / consume (context)
├── registry.py           # GameModeRegistry + get_game_mode()
├── prompts/              # Jinja2 templates for system and trigger prompts
│   ├── roast_together_*.j2
│   ├── debate_bicker_*.j2
│   └── breaking_bomb_*.j2
└── modes/
    ├── roast_together.py  # 一起吐槽 — energy tracking, best_take, 3 triggers
    ├── debate_bicker.py   # 辩论抬杠 — strong points, fart types, 3 triggers
    └── breaking_bomb.py   # 突发炸弹 — reaction recording, 1 trigger
```

## Public API

```python
from roast import (
    GameMode, Trigger,              # Base types
    GameModeRegistry, get_game_mode, # Registry
    RoastState,                     # Session state
    consume_pending_prompt,         # Context bridge
)
```

## Integration Flow

```
API request (user starts game):
  game_mode = get_game_mode("debate_bicker")
  state = await RoastState.start(user_id, persona_id, news_id, mode,
                                  extra=game_mode.init_extra(), redis=redis)

Each user turn (PigAgent.generate_reply):
  1. prompt = await consume_pending_prompt(state.roast_instance_id, redis)
  2. stream(messages) → LLM reply
  3. asyncio.create_task(game_mode.tick(state, records, redis))

Game end:
  await state.close(redis)
```

## Adding a New Mode

1. Create `roast/modes/<name>.py` extending `GameMode`
2. Define `mode`, `max_turns`, `system_prompt_extension`, `init_extra()`, `triggers`
3. Override `tick()` if mode-specific state updates are needed
4. Add `.j2` templates in `roast/prompts/`
5. Register in `GameModeRegistry.register_defaults()`
6. Add `Mode` enum value in `types.py`

## Storage

| Key | Backend | Purpose |
|-----|---------|---------|
| `roast:state:active:{user_id}` | Redis | Current session (TTL 24h) |
| `roast:{roast_instance_id}:pending_prompt` | Redis | Trigger prompt for next turn |
| `roast_states` table | PG | Historical records (async write) |
