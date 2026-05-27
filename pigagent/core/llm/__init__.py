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
from .registry import ModelRegistry, get_provider_config, resolve_provider, load_models, list_providers


# -------------------------------------------------------------------------------
# Provider instance pool  -  pre-built at import time, keyed by provider_id
# -------------------------------------------------------------------------------

_pool: dict[str, LLMProvider] = {}


def _load_class(path: str):
    """Reflectively load a class from a dotted path like 'pkg.mod.Cls'."""
    import importlib
    mod_name, cls_name = path.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)


def _build_pool() -> None:
    """Create one provider instance per backend (not per model).

    Provider class is resolved from the ``backend`` field in providers.toml
    via reflection — no hardcoded class names in code.
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

        _, api_key, _ = resolve_provider(pid)
        cls = _load_class(cfg.backend)
        _pool[pid] = cls(base_url=cfg.base_url, api_key=api_key)


def get_llm(model: str = "qwen-plus-us") -> LLMProvider:
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
    model: str = "qwen-plus-us",
    **kwargs,
) -> LLMProvider:
    """Backward-compat alias for get_llm()."""
    return get_llm(model)


__all__ = [
    "Message", "TokenUsage", "ChatResponse", "ChatDelta", "ToolCall",
    "ModelCapability", "ModelInfo", "ToolSpec",
    "LLMProvider",
    "ModelRegistry", "get_provider_config", "resolve_provider", "load_models", "list_providers",
    "get_llm", "create_llm",
]

# ── Load on first import ────────────────────────────────────────────────────
load_models()
_build_pool()
