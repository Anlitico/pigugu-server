# Core Agent Infrastructure

Generic React agent loop with composable stop conditions, interrupt support, and concurrent tool execution. Zero business dependencies — reusable for any agent project.

## Quick Start

```python
from core.agent import AgentRunner, RunnerConfig, step_count_is, no_tool_calls

runner = AgentRunner(RunnerConfig(
    model="qwen3.6-plus",
    tools=[...],
    tool_handlers={...},
    max_steps=5,
    interrupt_key="agent:session_123",
))

result = await runner.run(
    on_before_step=load_context,      # async () -> list[Message]
    on_after_step=flush_context,      # async (messages, state) -> None
)
```

## Architecture

```
AgentRunner.run()
  ├── on_before_step()        # Load context once (caller injects)
  ├── while not stop:
  │     ├── _run_step()       # LLM call → StepResult
  │     ├── if tool_calls:
  │     │     ├── append assistant msg
  │     │     └── executor.run()  # Concurrent tool execution
  │     └── else: break
  └── on_after_step()         # Flush results once (finally block)
```

**Design**: Load once, loop in memory, flush once. No Redis I/O inside the loop. `finally` guarantees flush on interrupt, error, or normal completion.

## Modules

| File | Purpose |
|------|---------|
| `runner.py` | `AgentRunner` — main loop, stop conditions, interrupt-guarded execution |
| `executor.py` | `ToolExecutor` — concurrent tool execution with timeout and error isolation |
| `state.py` | `AgentState` — per-request state machine (RUNNING → SUCCESS/ERROR/INTERRUPTED) |
| `interrupt.py` | `InterruptManager` — `asyncio.Event`-based interrupt + `@check_interrupt` decorator |
| `stop.py` | `StepResult` dataclass + composable stop conditions (`step_count_is`, `no_tool_calls`) |
| `sanitize.py` | `validate_tool_calls` — remove dangling tool calls before LLM input |

## AgentRunner

Generic loop runner. Does NOT hold context — all context operations are injected via hooks.

```python
class AgentRunner:
    def __init__(self, config: RunnerConfig): ...
    async def run(self, *, on_before_step, on_after_step) -> StepResult: ...
```

### RunnerConfig

| Field | Default | Description |
|-------|---------|-------------|
| `model` | `"qwen3.6-plus"` | LLM model ID |
| `tools` | `[]` | List of `ToolSpec` for LLM tool calling |
| `tool_handlers` | `{}` | `{name: handler}` — functions that execute tools |
| `tool_timeout` | `60.0` | Per-tool timeout in seconds |
| `max_tool_concurrency` | `10` | Max simultaneous tool executions |
| `max_steps` | `5` | Max loop iterations (safety net) |
| `stop_when` | `[]` | Extra stop conditions (default: `[step_count_is, no_tool_calls]`) |
| `temperature` | `0.6` | LLM temperature |
| `interrupt_key` | `None` | InterruptManager event key; when set, enables interrupt-guarded loop |

### Hooks

```python
BeforeStepHook = Callable[[], Awaitable[list[Message]]]
# Called once before the loop. Returns initial messages including system prompt.

AfterStepHook = Callable[[list[Message], AgentState], Awaitable[None]]
# Called once after the loop (in finally). Receives all messages and final state.
```

### Stop Conditions

Composable — stop when any returns `True`:

```python
step_count_is(n)       # Stop after n iterations
no_tool_calls(runner)  # Stop when LLM returns no tool calls
```

Can be extended with custom conditions: any `Callable[[AgentRunner], bool]`.

### Interrupt Flow

```
InterruptManager.trigger(key)
  → asyncio.Event.set()
  → AgentRunner._run_guarded: asyncio.wait race
  → loop_task cancelled
  → state = INTERRUPTED
  → on_after_step() called (finally)
```

## ToolExecutor

Concurrent tool execution with timeout and error isolation.

```python
executor = ToolExecutor(handlers={"search": search_handler})
result = await executor.run(tool_calls)  # ToolExecutionResult
```

| Feature | Implementation |
|---------|---------------|
| Concurrency | `asyncio.gather` + `Semaphore` |
| Timeout | `asyncio.wait_for` per tool |
| Error isolation | One failing tool doesn't kill others |
| Cancel safety | `BaseException` handler cancels all child tasks |

### Handler Signature

```python
ToolHandler = Callable[[dict], Any]
# Receives parsed JSON arguments, returns str or dict.
# Can be sync or async (auto-detected).
```

## AgentState

Per-request state machine:

```
RUNNING ──→ SUCCESS       (normal completion)
RUNNING ──→ ERROR         (unexpected exception)
RUNNING ──→ INTERRUPTED   (user VAD / explicit cancel)
```

One `AgentState` per agent loop invocation.

## InterruptManager

Local `asyncio.Event`-based interrupt system.

```python
mgr = get_interrupt_manager()
mgr.create("agent:room_abc")     # create event
await mgr.trigger("agent:room_abc")  # trigger interrupt
mgr.is_set("agent:room_abc")     # check status
mgr.cleanup("agent:room_abc")    # remove event
```

- Thread-safe via `threading.Lock`
- Auto-cleanup of expired events (background task, default 30min TTL)
- Memory leak warning at 100+ events

### `@check_interrupt` Decorator

Race ANY async function against an interrupt event:

```python
@check_interrupt(key="request:123")
async def my_task(state: AgentState):
    await long_running_work()
    return result
# → asyncio.wait([task, event.wait()]) — whichever finishes first wins
```

Supports: regular async functions, async generators, lifecycle callbacks (`on_start`, `on_success`, `on_error`, `on_interrupt`, `on_finally`).

## Integration with PigAgent

```python
class PigAgent:
    def __init__(self, ctx: ContextManager, config: PigAgentConfig):
        self.runner = AgentRunner(RunnerConfig(...))

    async def run(self, *, user_id, interrupt_key=None):
        async def _load():
            context = await self.ctx.load(user_id)
            prompt = get_system_prompt(self.config.system_prompt_id)
            if prompt:
                context.insert(0, Message.system(prompt))
            return context

        async def _flush(messages, state):
            for msg in new_messages:
                await self.ctx.add_turn(...)

        return await self.runner.run(
            on_before_step=_load,
            on_after_step=_flush,
        )
```

## Testing

```bash
pytest tests/unit/core/agent/ -q   # 61 tests
```
