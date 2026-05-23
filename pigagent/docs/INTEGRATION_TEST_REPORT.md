# LLM Integration Test Report

**Date**: 2026-05-14
**Branch**: feat/agent-architecture-refactor
**Result**: 24/24 passed (107s)

---

## Architecture Under Test

```
models.toml → ModelRegistry → _build_pool() → get_llm(model) → provider.chat(model=..., ...)
```

Provider pool is keyed by backend (`qwen-us`, `volcengine`). Model is selected per-call via the `model=` parameter on `chat()` / `chat_stream()`.

## Model Coverage

| Model | Provider | Context | Max Output | Thinking | Search |
|-------|----------|---------|------------|----------|--------|
| qwen-plus | qwen-us | 1M | 32k | Yes | Yes |
| qwen3.6-flash | qwen-us | 1M | 64k | Yes | Yes |
| qwen3.6-plus | qwen-us | 1M | 64k | Yes | Yes |
| doubao-seed-1-6-251015 | volcengine | 256k | 16k | Yes | No |

## Feature Test Matrix

| Feature | qwen-plus | qwen3.6-flash | qwen3.6-plus | doubao |
|---------|-----------|---------------|--------------|--------|
| Connectivity | Passed | Passed | Passed | Passed |
| Streaming | Passed | Passed | Passed | Passed |
| Thinking (chain-of-thought) | Passed | Passed | Passed | Passed |
| JSON Mode (structured output) | Passed | Passed | Passed | Passed |
| Tool Calling (function calling) | Passed | — | — | — |
| Web Search (built-in) | Passed | Passed | Passed | — |
| Temperature (0.0 vs 1.5) | Passed | — | — | — |
| Stop Sequences | Passed | — | — | — |

## Cross-Model Switching

Same provider instance (`get_llm("qwen3.6-flash")`) used to call two different models:

- `provider.chat(model="qwen3.6-flash", ...)` — Passed
- `provider.chat(model="qwen3.6-plus", ...)` — Passed

Temperature variation on the same model:

- `temperature=0.0` — deterministic output, no errors
- `temperature=1.5` — non-deterministic output, no errors

## Run Command

```bash
pytest tests/integration/test_llm_smoke.py -v --tb=short
```

Requires `DASHSCOPE_US_API_KEY` and `ARK_API_KEY` in root `.env`.
