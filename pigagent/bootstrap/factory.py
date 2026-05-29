# pigagent/bootstrap/factory.py
"""
Component factory: STT/TTS per session, PigAgent + storage as global singletons.

Storage (Redis + PG pool) is initialized at module load time. If either fails,
the process exits  -  there is no fallback.
"""

import os

from loguru import logger

from config import get_config
from core.audio.stt import create_stt
from core.audio.tts import create_tts
from agent import PigAgent


# ── Global singletons ──────────────────────────────────────────────────────

_pig_agent: PigAgent | None = None
_redis = None
_pg_pool = None
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


def _init_pg_pool():
    """Initialize asyncpg pool. Fails fast if not configured."""
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool

    database_url = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is required. Set it in .env, "
            "e.g. postgresql://user:pass@localhost:5432/pigugu"
        )

    import asyncpg  # type: ignore[reportMissingImports]
    import asyncio

    async def _create():
        return await asyncpg.create_pool(database_url, min_size=2, max_size=10)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _pg_pool = asyncio.run(_create())
    else:
        # Running loop exists  -  schedule but can't await here.
        # Defer to first use via lazy init pattern.
        _pg_pool = database_url  # store URL, create pool on first access
        logger.info("[Factory] PG pool deferred (event loop already running)")

    if not isinstance(_pg_pool, str):
        logger.info(f"[Factory] PG pool created")
    return _pg_pool


def get_redis():
    """Return the global Redis client."""
    global _redis
    if _redis is None:
        _init_redis()
    return _redis


def get_pg_pool():
    """Return the global PG DSN string (not a pool — connections are created per-operation)."""
    global _pg_pool
    if _pg_pool is None:
        _init_pg_pool()
    if isinstance(_pg_pool, str):
        _pg_pool = _pg_pool.replace("+asyncpg", "")
    return _pg_pool


def get_pig_agent() -> PigAgent:
    """Return the global PigAgent singleton."""
    global _pig_agent
    if _pig_agent is None:
        _pig_agent = _build_pig_agent()
    return _pig_agent


def _build_pig_agent(config=None) -> PigAgent:
    """Build the PigAgent singleton (called once at first use)."""
    if config is None:
        config = get_config()

    model = config.resolve_model()

    from system_prompts import PersonaRegistry
    PersonaRegistry.register_defaults()
    prompts = PersonaRegistry.build_prompt_cache()
    logger.info(f"[Factory] Prompt cache built: {list(prompts.keys())}")

    from roast import GameModeRegistry
    GameModeRegistry.register_defaults()
    game_modes = GameModeRegistry.build_cache()
    logger.info(f"[Factory] Game mode cache built: {list(game_modes.keys())}")

    redis = get_redis()
    pg_pool = get_pg_pool()

    from context.manager import ContextManager
    ctx = ContextManager(redis_client=redis, pg_pool=pg_pool)
    logger.info("[Factory] ContextManager created")

    pig_agent = PigAgent(
        ctx,
        redis=redis,
        pg_pool=pg_pool,
        model=model,
        prompts=prompts,
        game_modes=game_modes,
        temperature=config.LLM_TEMPERATURE,
        max_tokens=config.LLM_MAX_TOKENS,
        max_iterations=config.AGENT_MAX_STEPS,
    )
    logger.info(f"[Factory] PigAgent singleton created with model={model}")
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


def create_agent_components(config=None, persona=None):
    """Create TTS per session. STT, PigAgent, VAD are global singletons.

    Args:
        config: AgentConfig instance. If None, loads from get_config().
        persona: Persona instance for TTS voice override.

    Returns:
        Tuple of (stt, pig_agent, tts) instances.
    """
    if config is None:
        config = get_config()

    # ── STT (global singleton) ─────────────────────────────────────────

    stt = get_stt()

    # ── PigAgent (global singleton) ─────────────────────────────────────

    pig_agent = get_pig_agent()

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

    tts_lang = config.CARTESIA_TTS_LANGUAGE or "en"
    tts = create_tts(
        model=config.CARTESIA_TTS_MODEL,
        language=tts_lang,
        voice=tts_voice,
        speed=tts_speed,
        emotion=tts_emotion,
        volume=config.CARTESIA_TTS_VOLUME,
        sample_rate=config.CARTESIA_TTS_SAMPLE_RATE,
        api_key=cartesia_api_key,
        base_url=config.CARTESIA_TTS_BASE_URL,
    )

    return stt, pig_agent, tts
