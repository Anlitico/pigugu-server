# LLM Package

Provider-agnostic LLM abstraction for Pigugu Agent. Supports Qwen (DashScope), DeepSeek, and Volcengine (Doubao) via OpenAI-compatible API.

## Architecture

```
llm/
├── __init__.py          # Public API, instance pool, get_llm() / create_llm()
├── provider.py          # LLMProvider abstract base class
├── types.py             # Message, ChatResponse, ChatDelta, ModelInfo, etc.
├── registry.py          # ModelRegistry + provider config + model loader
├── models.toml          # Model definitions (context window, capabilities, etc.)
├── providers.toml       # Provider endpoints and credentials
├── providers/
│   ├── qwen.py          # QwenProvider — DashScope US endpoint
│   ├── volcengine.py    # VolcengineProvider — Ark international endpoint
│   └── openai.py        # OpenAIChatProvider — generic OpenAI-compatible
└── README.md
```

## Quick Start

```python
from core.llm import get_llm, Message

# Get a pre-built provider instance (no new connections created)
provider = get_llm("qwen-plus")

# Chat
resp = await provider.chat(messages=[Message.user("Hello")])
print(resp.content)

# Streaming
async for delta in provider.chat_stream(messages=[Message.user("Hi")]):
    print(delta.content)
```

## Instance Pool

All provider instances are created once at import time. Switching models returns a different instance from the same pool — no new HTTP connections or client objects.

```python
from core.llm import get_llm

qwen_plus  = get_llm("qwen-plus")      # QwenProvider instance
qwen_turbo = get_llm("qwen-turbo")     # Different QwenProvider instance
ds_chat    = get_llm("deepseek-chat")  # OpenAIChatProvider instance

# All six instances share the same import-time pool
for name in ["qwen-plus", "qwen-turbo", "qwen-max", "qwen-long",
             "deepseek-chat", "deepseek-reasoner"]:
    p = get_llm(name)
    resp = await p.chat(messages=[Message.user("Hello")])
```

`create_llm()` is a backward-compatible alias for `get_llm()`. Both look up the pre-built pool; neither creates new connections.

## Common API

All providers share the same interface:

```python
# Non-streaming
resp: ChatResponse = await provider.chat(
    messages: list[Message],
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    thinking: dict | None = None,   # {"enabled": True, "budget": 4096}
    search: dict | None = None,     # {"enabled": True, "force": False}
    response_format: dict | None = None,
)

# Streaming
async for delta in provider.chat_stream(messages=[...]):
    print(delta.content)           # text chunk
    print(delta.reasoning_content) # thinking tokens
    print(delta.tool_calls)        # tool calls (final chunk)
```

## Provider Capabilities

| Feature | Qwen | Volcengine |
|---|---|---|
| **Thinking** | `enable_thinking` + `thinking_budget` | `thinking: {type: "enabled"}` + `budget_tokens` |
| **Reasoning effort** | N/A | `reasoning_effort`: minimal/low/medium/high (Seed 2.0+) |
| **Web Search** | `enable_search` via extra_body | Tool-based plugin |
| **Continuation** | `partial: True` on message | `prefix: True` on message |
| **Tool use** | Function calling | Function calling |
| **Structured output** | `response_format` | `response_format` |

## Pre-request Validation

Both `QwenProvider` and `VolcengineProvider` validate requested features against model capabilities before sending the API request. If a model does not support a requested feature (e.g., thinking on qwen-turbo), a `ValueError` is raised with a list of compatible models.

## Adding a Model

1. Add a `[[models]]` block to `models.toml`
2. The instance pool auto-builds one instance per model at next import

## Adding a Provider

1. Create `providers/<name>.py` implementing `LLMProvider`
2. Add provider entry to `providers.toml`
3. Add routing in `_build_pool()` in `__init__.py`

## Querying Models

```python
from core.llm import ModelRegistry, ModelCapability

ModelRegistry.get("qwen-plus")
ModelRegistry.list(provider="qwen-us")
ModelRegistry.list(capability=ModelCapability.TOOL_USE)
```
