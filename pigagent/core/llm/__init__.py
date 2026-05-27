# pigagent/core/llm/__init__.py
"""LLM package  -  Types, Provider, Registry"""

from loguru import logger

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
from .providers.qwen import QwenProvider
from .providers.volcengine import VolcengineProvider
from .registry import ModelRegistry, get_provider_config, resolve_provider, load_models, list_providers


# -------------------------------------------------------------------------------
# Provider instance pool  -  pre-built at import time, keyed by provider_id
# -------------------------------------------------------------------------------

_pool: dict[str, LLMProvider] = {}


def _build_pool() -> None:
    """Create one provider instance per backend (not per model).

    Models are selected per-call via the ``model`` parameter on chat/chat_stream.
    """
    seen: set[str] = set()
    for info in ModelRegistry.list():
        pid = info.provider
        if pid in seen:
            continue
        seen.add(pid)

        cfg = get_provider_config(pid)
        if cfg is None:
            continue

        if pid in ("qwen", "qwen-us"):
            _pool[pid] = QwenProvider(base_url=cfg.base_url)
        elif pid == "volcengine":
            _pool[pid] = VolcengineProvider(base_url=cfg.base_url)
        else:
            _, api_key, _ = resolve_provider(pid)
            _pool[pid] = QwenProvider(base_url=cfg.base_url)
            logger.warning(f"[Pool] Unknown provider '{pid}', using QwenProvider as fallback")


def get_llm(model: str = "qwen-plus") -> LLMProvider:
    """Return the provider instance for the given model.

    Resolves model  ->  provider backend via ModelRegistry, then returns
    the pre-built instance for that backend.
    """
    info = ModelRegistry.get(model)
    pid = info.provider
    if pid not in _pool:
        raise KeyError(
            f"Provider '{pid}' (for model '{model}') not found in pool. "
            f"Available: {list(_pool)}"
        )
    return _pool[pid]


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
    "QwenProvider",
    "VolcengineProvider",
    "ModelRegistry", "get_provider_config", "resolve_provider", "load_models", "list_providers",
    "get_llm", "create_llm",
]

# ── Load on first import ────────────────────────────────────────────────────
load_models()
_build_pool()
