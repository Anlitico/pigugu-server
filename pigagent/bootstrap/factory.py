# pigagent/bootstrap/factory.py
"""
Component factory: STT/TTS per session, PigAgent + ContextManager per user.

Storage (Redis + PG pool) and caches (prompts, game modes) are initialized
at module load time. If Redis or PG fails, the process exits — there is no
fallback.

PigAgent and ContextManager are created per session/user, not as global
singletons. Shared resources (Redis, PG pool, LLM provider, STT, VAD,
prompt cache, game mode cache) remain singletons.
"""

import os

from loguru import logger

from agent_config import get_config
from core.audio.stt import create_stt
from core.audio.tts import create_tts
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
_vad = None
_stt = None


def get_stt():
    """Return the global STT plugin singleton."""
    global _stt
    if _stt is not None:
        return _stt
    config = get_config()
    stt_provider = config.STT_PROVIDER.lower()
    if stt_provider == "deepgram":
        _stt = create_stt(
            provider="deepgram",
            model=config.DEEPGRAM_STT_MODEL,
            language=config.DEEPGRAM_STT_LANGUAGE,
            sample_rate=config.DEEPGRAM_STT_SAMPLE_RATE,
            enable_diarization=config.DEEPGRAM_ENABLE_DIARIZATION,
            endpointing_ms=int(config.ENDPOINTING_DELAY * 1000),
            api_key=os.getenv("DEEPGRAM_API_KEY"),
        )
    else:
        _stt = create_stt(
            provider="cartesia",
            model=config.CARTESIA_STT_MODEL,
            language=config.CARTESIA_STT_LANGUAGE,
            encoding=config.CARTESIA_STT_ENCODING,
            sample_rate=config.CARTESIA_STT_SAMPLE_RATE,
            api_key=os.getenv("CARTESIA_API_KEY"),
            base_url=config.CARTESIA_STT_BASE_URL,
        )
    logger.info(f"[Factory] STT loaded: {_stt.model}")
    return _stt


def get_vad():
    """Return the global VAD (Voice Activity Detection) singleton."""
    global _vad
    if _vad is None:
        try:
            from livekit.plugins import silero  # type: ignore[reportAttributeAccessIssue]
            _vad = silero.VAD.load()
            logger.info("[Factory] VAD loaded")
        except RuntimeError as e:
            logger.warning(f"[Factory] VAD unavailable: {e}")
            return None
    return _vad


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

    PromptStore is per-agent — its cache starts empty, and prompts are
    lazily loaded from PG on first access. When a prompt is updated in
    PG, restarting the session (new PigAgent → new PromptStore) picks
    up the change without a redeploy.

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

    stt_provider = config.STT_PROVIDER.lower()
    if stt_provider == "deepgram":
        if not os.getenv("DEEPGRAM_API_KEY"):
            errors.append("DEEPGRAM_API_KEY required in .env file for Deepgram STT")
    elif stt_provider == "cartesia":
        if not os.getenv("CARTESIA_API_KEY"):
            errors.append("CARTESIA_API_KEY required in .env file for Cartesia STT")

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

    if not os.getenv("LIVEKIT_API_KEY"):
        errors.append("LIVEKIT_API_KEY required in .env file")

    if not os.getenv("LIVEKIT_API_SECRET"):
        errors.append("LIVEKIT_API_SECRET required in .env file")

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


async def create_agent_components(config=None, persona=None):
    """Create TTS per session. STT and VAD are global singletons.

    PigAgent is NOT created here — it's per-session and requires user_id,
    which is resolved after the LiveKit session starts. Callers should
    use create_pig_agent(user_id) separately once user_id is known.

    Args:
        config: AgentConfig instance. If None, loads from get_config().
        persona: Persona instance for TTS voice override.

    Returns:
        Tuple of (stt, tts) instances.
    """
    if config is None:
        config = get_config()

    # ── STT (global singleton) ─────────────────────────────────────────

    stt = get_stt()

    from metrics.session import ColdStartMetrics
    ColdStartMetrics.mark("stt_init")

    # ── TTS (per session  -  persona voice/speed/emotion) ────────────────

    cartesia_api_key = os.getenv("CARTESIA_API_KEY")

    tts_voice = config.CARTESIA_TTS_VOICE
    tts_speed = config.CARTESIA_TTS_SPEED
    tts_emotion = None

    if persona is not None:
        if persona.tts_voice:
            tts_voice = persona.tts_voice
        if persona.tts_speed is not None:
            tts_speed = persona.tts_speed
        tts_emotion = persona.tts_emotion

    if tts_emotion is None and config.CARTESIA_TTS_EMOTION:
        tts_emotion = [e.strip() for e in config.CARTESIA_TTS_EMOTION.split(",")]

    tts = create_tts(
        model=config.CARTESIA_TTS_MODEL,
        language=config.CARTESIA_TTS_LANGUAGE,
        encoding=config.CARTESIA_TTS_ENCODING,
        voice=tts_voice,
        speed=tts_speed,
        emotion=tts_emotion,
        volume=config.CARTESIA_TTS_VOLUME,
        sample_rate=config.CARTESIA_TTS_SAMPLE_RATE,
        word_timestamps=config.CARTESIA_TTS_WORD_TIMESTAMPS,
        api_key=cartesia_api_key,
        base_url=config.CARTESIA_TTS_BASE_URL,
    )

    ColdStartMetrics.mark("tts_init")

    return stt, tts
