# pigagent/core/llm/registry.py
"""ModelRegistry runtime index + Provider config + Model loading"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from .types import ModelInfo, ModelCapability

_PROVIDER_CONFIG = Path(__file__).parent / "providers.toml"
_MODEL_CONFIG = Path(__file__).parent / "models.toml"


@dataclass
class ProviderConfig:
    base_url: str
    env: str
    default: str


# ═══════════════════════════════════════════════════════════════════════════════
# Provider 配置 — 从 providers.toml 加载
# ═══════════════════════════════════════════════════════════════════════════════

_PROVIDERS: dict[str, ProviderConfig] = {}

if _PROVIDER_CONFIG.exists():
    with open(_PROVIDER_CONFIG, "rb") as f:
        for name, entry in tomllib.load(f).items():
            _PROVIDERS[name] = ProviderConfig(
                base_url=entry["base_url"],
                env=entry["env"],
                default=entry.get("default", ""),
            )
logger.info(f"[Registry] Loaded {len(_PROVIDERS)} providers from {_PROVIDER_CONFIG}")


def get_provider_config(name: str) -> ProviderConfig | None:
    return _PROVIDERS.get(name.lower())


def list_providers() -> list[str]:
    return list(_PROVIDERS.keys())


def resolve_provider(provider: str) -> tuple[str, str, str]:
    """Resolve provider → (base_url, api_key, default_model)."""
    cfg = get_provider_config(provider)
    if cfg is None:
        raise ValueError(
            f"Unknown provider '{provider}'. Known: {list(_PROVIDERS)}"
        )
    api_key = os.getenv(cfg.env, "")
    return cfg.base_url, api_key, cfg.default


# ═══════════════════════════════════════════════════════════════════════════════
# ModelRegistry — 纯运行时索引（数据由 model.load_models() 注入）
# ═══════════════════════════════════════════════════════════════════════════════

class ModelRegistry:
    """Thread-unsafe runtime model index."""

    _models: dict[str, ModelInfo] = {}

    @classmethod
    def register(cls, info: ModelInfo) -> None:
        cls._models[info.model_id] = info

    @classmethod
    def get(cls, model_id: str) -> ModelInfo:
        if model_id in cls._models:
            return cls._models[model_id]
        logger.warning(f"[Registry] Unknown model '{model_id}', fallback")
        return ModelInfo(
            model_id=model_id,
            provider="unknown",
            display_name=model_id,
            capabilities={ModelCapability.TEXT, ModelCapability.STREAMING},
        )

    @classmethod
    def list(
        cls,
        *,
        provider: str | None = None,
        capability: ModelCapability | None = None,
    ) -> list[ModelInfo]:
        result = list(cls._models.values())
        if provider:
            result = [m for m in result if m.provider == provider]
        if capability:
            result = [m for m in result if capability in m.capabilities]
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# Model config loader — read models.toml into ModelRegistry
# ═══════════════════════════════════════════════════════════════════════════════

def load_models(path: str | Path | None = None) -> int:
    target = Path(path) if path else _MODEL_CONFIG

    if not target.exists():
        logger.error(f"[Model] Config not found: {target}")
        return 0

    with open(target, "rb") as f:
        data = tomllib.load(f)

    count = 0
    for entry in data.get("models", []):
        caps = set()
        for c in entry.get("capabilities", []):
            try:
                caps.add(ModelCapability(c))
            except ValueError:
                logger.warning(f"[Model] Unknown capability '{c}' in {entry['id']}")

        info = ModelInfo(
            model_id=entry["id"],
            provider=entry.get("provider", ""),
            display_name=entry.get("display", entry["id"]),
            capabilities=caps,
            context_window=entry.get("context", 0),
            max_output_tokens=entry.get("output", 0),
            thinking=entry.get("thinking", False),
            search=entry.get("search", False),
        )
        ModelRegistry.register(info)
        count += 1

    logger.info(f"[Model] Loaded {count} models from {target}")
    return count
