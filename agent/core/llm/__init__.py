# agent/core/llm/__init__.py
"""LLM package — Types, Provider, Registry"""

from .types import (
    Message,
    TokenUsage,
    ChatResponse,
    ChatDelta,
    ToolCall,
    ModelCapability,
    ModelInfo,
    ToolSpec,
)
from .provider import LLMProvider
from .providers.openai import OpenAIChatProvider
from .providers.qwen import QwenProvider
from .providers.volcengine import VolcengineProvider
from .registry import ModelRegistry, get_provider_config, resolve_provider, load_models, list_providers


# ═══════════════════════════════════════════════════════════════════════════════
# Provider instance pool — pre-built at import time, keyed by model_id
# ═══════════════════════════════════════════════════════════════════════════════

_pool: dict[str, LLMProvider] = {}


def _build_pool() -> None:
    """Create one provider instance per model from models.toml."""
    for info in ModelRegistry.list():
        cfg = get_provider_config(info.provider)
        if cfg is None:
            continue

        provider_id = info.provider
        model_id = info.model_id

        if provider_id in ("qwen", "qwen-us"):
            _pool[model_id] = QwenProvider(
                model=model_id,
                base_url=cfg.base_url,
                temperature=info.temperature,
                max_tokens=info.max_output_tokens or None,
            )
        elif provider_id == "volcengine":
            _pool[model_id] = VolcengineProvider(
                model=model_id,
                base_url=cfg.base_url,
                temperature=info.temperature,
                max_tokens=info.max_output_tokens or None,
            )
        else:
            _, api_key, _ = resolve_provider(provider_id)
            _pool[model_id] = OpenAIChatProvider(
                model=model_id,
                api_key=api_key,
                base_url=cfg.base_url,
                temperature=info.temperature,
                max_tokens=info.max_output_tokens or None,
            )


def get_llm(model: str = "qwen-plus") -> LLMProvider:
    """Return a pre-built provider instance for the given model.

    All instances are created once at import time. Switching models
    returns a different instance from the same pool — no new connections.
    """
    if model not in _pool:
        raise KeyError(
            f"Model '{model}' not found in provider pool. "
            f"Available: {list(_pool)}"
        )
    return _pool[model]


def create_llm(
    model: str = "qwen-plus",
    **kwargs,
) -> LLMProvider:
    """Backward-compat alias for get_llm()."""
    return get_llm(model)


__all__ = [
    "Message", "TokenUsage", "ChatResponse", "ChatDelta", "ToolCall",
    "ModelCapability", "ModelInfo", "ToolSpec",
    "LLMProvider",
    "OpenAIChatProvider",
    "QwenProvider",
    "VolcengineProvider",
    "ModelRegistry", "get_provider_config", "resolve_provider", "load_models", "list_providers",
    "get_llm", "create_llm",
]

# ── Load on first import ────────────────────────────────────────────────────
load_models()
_build_pool()
