# LLM Package

Provider-agnostic LLM abstraction for Pigugu Agent. Supports Qwen (DashScope) and Volcengine (Doubao) via OpenAI-compatible API.

## Architecture

```
llm/
├── __init__.py          # Public API, instance pool (keyed by provider), get_llm()
├── provider.py          # LLMProvider abstract base class
├── types.py             # Message, ChatResponse, ChatDelta, ModelInfo, etc.
├── registry.py          # ModelRegistry + provider config + model loader
├── models.toml          # Model definitions (capabilities only, no runtime params)
├── providers.toml       # Provider endpoints and credentials
├── providers/
│   ├── qwen.py          # QwenProvider — DashScope US endpoint
│   └── volcengine.py    # VolcengineProvider — Ark endpoint
└── README.md
```

## Quick Start

```python
from core.llm import get_llm, Message

# One provider instance per backend (not per model)
provider = get_llm("qwen3.6-plus")

# Model is selected per-call
resp = await provider.chat(
    messages=[Message.user("Hello")],
    model="qwen3.6-flash",
)
print(resp.content)

# Same provider, different model — no new connections
resp2 = await provider.chat(
    messages=[Message.user("Hello again")],
    model="qwen3.6-plus",
    temperature=0.8,
)

# Streaming
async for delta in provider.chat_stream(
    messages=[Message.user("Count to 3")],
    model="qwen3.6-flash",
):
    if delta.content:
        print(delta.content)
```

## Instance Pool

Providers are stateless connection objects — one instance per backend. **Model is selected per-call** via the `model=` parameter.

```python
from core.llm import get_llm

# All three return the SAME instance (same qwen-us backend)
a = get_llm("qwen-plus")
b = get_llm("qwen3.6-flash")
c = get_llm("qwen3.6-plus")
assert a is b is c

# Different backend → different instance
d = get_llm("doubao-seed-1-6-251015")
assert a is not d
```

`get_llm(model_id)` resolves `model_id → provider` via `ModelRegistry`, then returns the pre-built instance for that backend.

## Common API

All providers share the same interface. `model` is required on every call.

```python
# Non-streaming
resp: ChatResponse = await provider.chat(
    messages: list[Message],
    *,
    model: str,                           # REQUIRED — e.g. "qwen3.6-flash"
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    parallel_tool_calls: bool = True,
    temperature: float | None = None,     # default 0.6
    top_p: float | None = None,
    max_tokens: int | None = None,
    stop: list[str] | None = None,
    seed: int | None = None,
    thinking: dict | None = None,         # {"enabled": True, "budget": 4096}
    search: dict | None = None,           # {"enabled": True, "force": False}
    response_format: dict | None = None,
)

# Streaming
async for delta in provider.chat_stream(
    messages=[...],
    model="qwen3.6-flash",
    ...
):
    print(delta.content)           # text chunk
    print(delta.reasoning_content) # thinking tokens
    print(delta.tool_calls)        # tool calls (final chunk)
```

## Registered Models

| Model | Provider | Context | Max Output | Thinking | Search |
|-------|----------|---------|------------|----------|--------|
| qwen-plus | qwen-us | 1M | 32k | yes | yes |
| qwen3.6-flash | qwen-us | 1M | 64k | yes | yes |
| qwen3.6-plus | qwen-us | 1M | 64k | yes | yes |
| doubao-seed-1-6-251015 | volcengine | 256k | 16k | yes | no |

## Provider Capabilities

| Feature | Qwen | Volcengine |
|---|---|---|
| **Thinking** | `enable_thinking` + `thinking_budget` | `thinking: {type: "enabled"}` + `budget_tokens` |
| **Reasoning effort** | N/A | `reasoning_effort`: minimal/low/medium/high |
| **Web Search** | `enable_search` via extra_body | Tool-based plugin |
| **Continuation** | `partial: True` on message | `prefix: True` on message |
| **Tool use** | Function calling | Function calling |
| **Structured output** | `response_format` | `response_format` |

## Pre-request Validation

Both `QwenProvider` and `VolcengineProvider` validate requested features against `ModelRegistry` before sending the API request. If a model does not support a requested feature, a `ValueError` is raised with a list of compatible models.

## Adding a Model

1. Add a `[[models]]` block to `models.toml` (capabilities only — no temperature, no runtime params)
2. Pool auto-detects the provider backend at next import

## Adding a Provider

1. Create `providers/<name>.py` implementing `LLMProvider`
2. Add provider entry to `providers.toml`
3. Add routing in `_build_pool()` in `__init__.py`

## Token Counting

Two methods per provider — fast (offline) for runtime, accurate (API) for background:

```python
provider = get_llm("qwen3.6-flash")

# Fast — tiktoken cl100k_base, < 1ms, for hot path / budget enforcement
tokens = provider.count_tokens("hello world 你好世界")

# Accurate — provider's native tokenizer API, for background compression
tokens = await provider.count_tokens_async("hello world 你好世界", model="qwen3.6-flash")
```

| Method | Scenario | QwenProvider | VolcengineProvider |
|--------|----------|-------------|-------------------|
| `count_tokens(text)` | Runtime hot path | tiktoken `cl100k_base` | tiktoken `cl100k_base` |
| `count_tokens_async(text, model)` | Background compression | DashScope tokenizer API | Ark tokenizer API |

Both providers use `cl100k_base` offline (Qwen's tokenizer is GPT-4-family). API calls fall back to offline tiktoken on failure. Override `_get_encoding()` in new providers to swap the encoding.

```python
class MyProvider(LLMProvider):
    def _get_encoding(self):
        import tiktoken
        return tiktoken.get_encoding("o200k_base")  # newer models
```

## Querying Models

```python
from core.llm import ModelRegistry, ModelCapability

# Look up model metadata
info = ModelRegistry.get("qwen3.6-flash")
print(info.context_window)    # 1000000
print(info.max_output_tokens) # 65536

# Filter by provider or capability
ModelRegistry.list(provider="qwen-us")
ModelRegistry.list(capability=ModelCapability.WEB_SEARCH)
```
