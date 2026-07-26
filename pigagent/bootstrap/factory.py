# pigagent/bootstrap/factory.py
"""
Component factory: PigAgent + ContextManager per user.

Storage (Redis + PG pool) and caches (prompts, game modes) are initialized
at module load time. If Redis or PG fails, the process exits — there is no
fallback.

PigAgent and ContextManager are created per session/user, not as global
singletons. Shared resources (Redis, PG pool, LLM provider, prompt cache,
game mode cache) remain singletons.

STT and TTS are called directly via Deepgram HTTP API and Cartesia SSE API
(in ws/handler.py), not through LiveKit plugins.
"""

import os

from loguru import logger

from agent_config import get_config
from agent import PigAgent


# ── Shared caches (built once) ────────────────────────────────────────────

_game_modes_cache: dict | None = None


def _ensure_game_mode_cache():
    """Build game mode cache once. Idempotent."""
    global _game_modes_cache
    if _game_modes_cache is None:
        from roast import GameModeRegistry
        GameModeRegistry.register_defaults()
        _game_modes_cache = GameModeRegistry.build_cache()
        logger.info(f"[Factory] Game mode cache built: {list(_game_modes_cache.keys())}")
    return _game_modes_cache


def get_game_modes() -> dict:
    """Return the shared game modes cache (built once)."""
    return _ensure_game_mode_cache()


# ── Global singletons ──────────────────────────────────────────────────────

_redis = None


def _init_redis():
    """Initialize Redis async client. Fails fast if not configured."""
    global _redis
    if _redis is not None:
        return _redis

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError(
            "REDIS_URL is required. Set it in .env, e.g. redis://localhost:6379/0"
        )

    import redis.asyncio as aioredis
    _redis = aioredis.from_url(redis_url, decode_responses=True)
    logger.info(f"[Factory] Redis connected: {redis_url}")
    return _redis


def get_redis():
    """Return the global Redis client."""
    global _redis
    if _redis is None:
        _init_redis()
    return _redis


async def get_pg_pool():
    """Return the global asyncpg connection pool (lazy singleton)."""
    from context.storage.pg import _ensure_pg_pool
    return await _ensure_pg_pool()


async def create_pig_agent(user_id: str, config=None, *, hw_id: str = "") -> PigAgent:
    """Create a new PigAgent instance for a specific user/session.

    Each call creates a fresh PigAgent + ContextManager + PromptStore.
    Shared resources (Redis, PG pool, game modes, model config) are reused.

    hw_id is the hardware_id of the connected device, used by tools
    (e.g. volume_control) to send C2D MQTT messages.
    """
    if config is None:
        config = get_config()

    model = config.resolve_model()
    game_modes = _ensure_game_mode_cache()

    redis = get_redis()
    pg_pool = await get_pg_pool()

    from prompts import PromptStore
    prompt_store = PromptStore(pg_pool)

    from context.manager import ContextManager
    ctx = ContextManager(user_id, redis_client=redis, pg_pool=pg_pool)
    logger.info(f"[Factory] ContextManager created for user={user_id}")

    pig_agent = PigAgent(
        user_id,
        ctx,
        redis=redis,
        pg_pool=pg_pool,
        model=model,
        prompt_store=prompt_store,
        game_modes=game_modes,
        temperature=config.LLM_TEMPERATURE,
        max_tokens=config.LLM_MAX_TOKENS,
        max_iterations=config.AGENT_MAX_STEPS,
        hw_id=hw_id,
    )
    logger.info("[Factory] PigAgent created for user=%s hw_id=%s model=%s", user_id, hw_id, model)
    return pig_agent


def validate_configuration(config=None):
    """Validate that required API keys are set in environment variables.

    Args:
        config: AgentConfig instance. If None, loads from get_config().

    Returns:
        True if valid, False otherwise.
    """
    if config is None:
        config = get_config()

    errors = []

    if not os.getenv("DEEPGRAM_API_KEY"):
        errors.append("DEEPGRAM_API_KEY required in .env file for Deepgram STT")

    if not os.getenv("CARTESIA_API_KEY"):
        errors.append("CARTESIA_API_KEY required in .env file for Cartesia TTS")

    from core.llm.registry import ModelRegistry, get_provider_config

    model = config.resolve_model()
    info = ModelRegistry.get(model)
    llm_provider = info.provider
    if llm_provider == "unknown":
        errors.append(f"Unknown model: {model}")
    else:
        cfg = get_provider_config(llm_provider)
        if cfg:
            if not os.getenv(cfg.env):
                errors.append(f"{cfg.env} required in .env file for {llm_provider} LLM")
        else:
            errors.append(f"Unknown LLM provider: {llm_provider}")

    if config.ENABLE_POLICY_SEARCH and config.POLICY_SEARCH_BACKEND == "perplexity":
        if not os.getenv("PERPLEXITY_API_KEY") and not os.getenv("OPENROUTER_API_KEY"):
            errors.append(
                "PERPLEXITY_API_KEY or OPENROUTER_API_KEY required in .env file "
                "when POLICY_SEARCH_BACKEND=perplexity"
            )

    if errors:
        logger.error("=" * 70)
        logger.error("Configuration Errors:")
        for error in errors:
            logger.error(f"  - {error}")
        logger.error("=" * 70)
        return False

    return True
