# pigagent/bootstrap/factory.py
"""
Component factory: creates STT, TTS instances per session. PigAgent is a
module-level singleton — created once and reused across all connections.
"""

import os

from loguru import logger

from config import get_config
from core.audio.stt import create_stt
from core.audio.tts import create_tts
from pigagent import PigAgent
from core.agent import ToolRegistry
from tools import create_web_search_tool, volume_tool
from tools.search import TavilyProvider

# ── Global singleton ───────────────────────────────────────────────────────

_pig_agent: PigAgent | None = None


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
    llm_provider_id = config.LLM_PROVIDER.lower()

    from personas import PersonaRegistry
    PersonaRegistry.register_defaults()
    prompts = PersonaRegistry.build_prompt_cache(llm_provider_id)
    logger.info(f"[Factory] Prompt cache built: {list(prompts.keys())}")

    search_provider = TavilyProvider()
    web_search_tool = create_web_search_tool(search_provider)

    registry = ToolRegistry()
    registry.register_many([web_search_tool, volume_tool])

    pig_agent = PigAgent(
        None,  # ctx (wired when ContextManager is available)
        model=model,
        prompts=prompts,
        tools=registry.tools,
        tool_handlers=registry.tool_handlers,
        temperature=config.LLM_TEMPERATURE,
        max_tokens=config.LLM_MAX_TOKENS,
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

    # Check STT provider API key
    stt_provider = config.STT_PROVIDER.lower()
    if stt_provider == "deepgram":
        if not os.getenv("DEEPGRAM_API_KEY"):
            errors.append("DEEPGRAM_API_KEY required in .env file for Deepgram STT")
    elif stt_provider == "cartesia":
        if not os.getenv("CARTESIA_API_KEY"):
            errors.append("CARTESIA_API_KEY required in .env file for Cartesia STT")

    # Check Cartesia API key for TTS (always needed for TTS)
    if not os.getenv("CARTESIA_API_KEY"):
        errors.append("CARTESIA_API_KEY required in .env file for Cartesia TTS")

    # Check LLM API key based on provider
    from core.llm.registry import get_provider_config

    llm_provider = config.LLM_PROVIDER.lower()
    cfg = get_provider_config(llm_provider)
    if cfg:
        if not os.getenv(cfg.env):
            errors.append(f"{cfg.env} required in .env file for {llm_provider} LLM")
    else:
        errors.append(f"Unknown LLM provider: {llm_provider}")

    # Check LiveKit credentials
    if not os.getenv("LIVEKIT_API_KEY"):
        errors.append("LIVEKIT_API_KEY required in .env file")

    if not os.getenv("LIVEKIT_API_SECRET"):
        errors.append("LIVEKIT_API_SECRET required in .env file")

    # Check Perplexity API key when using Perplexity search backend
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
    """Create STT and TTS instances per session.

    PigAgent is a global singleton — returned from get_pig_agent().

    Args:
        config: AgentConfig instance. If None, loads from get_config().
        persona: Persona instance for TTS voice override.

    Returns:
        Tuple of (stt, pig_agent, tts) instances.
    """
    if config is None:
        config = get_config()

    deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")
    cartesia_api_key = os.getenv("CARTESIA_API_KEY")

    # ── STT ────────────────────────────────────────────────────────────

    stt_provider = config.STT_PROVIDER.lower()
    if stt_provider == "deepgram":
        stt = create_stt(
            provider="deepgram",
            model=config.DEEPGRAM_STT_MODEL,
            language=config.DEEPGRAM_STT_LANGUAGE,
            sample_rate=config.DEEPGRAM_STT_SAMPLE_RATE,
            enable_diarization=config.DEEPGRAM_ENABLE_DIARIZATION,
            api_key=deepgram_api_key,
        )
    elif stt_provider == "cartesia":
        stt = create_stt(
            provider="cartesia",
            model=config.CARTESIA_STT_MODEL,
            language=config.CARTESIA_STT_LANGUAGE,
            encoding=config.CARTESIA_STT_ENCODING,
            sample_rate=config.CARTESIA_STT_SAMPLE_RATE,
            api_key=cartesia_api_key,
            base_url=config.CARTESIA_STT_BASE_URL,
        )
    else:
        raise ValueError(f"Unknown STT provider: {stt_provider}")

    # ── PigAgent (global singleton) ─────────────────────────────────────

    pig_agent = get_pig_agent()

    # ── TTS ────────────────────────────────────────────────────────────

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

    return stt, pig_agent, tts
