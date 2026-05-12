# agent/components/factory.py
"""
Component factory: creates STT, PigAgent, TTS instances from configuration.

Extracted from main.py to keep the entrypoint slim and testable.
"""

import os

from loguru import logger

from config import get_config
from core.stt import create_stt
from core.tts import create_tts
from core.pigagent import PigAgent, AgentConfig


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

    # Mode 3 requires diarization
    if config.AGENT_MODE == 3 and not config.DEEPGRAM_ENABLE_DIARIZATION:
        errors.append("Mode 3 (Group Discussion) requires DEEPGRAM_ENABLE_DIARIZATION = true")

    if errors:
        logger.error("=" * 70)
        logger.error("Configuration Errors:")
        for error in errors:
            logger.error(f"  - {error}")
        logger.error("=" * 70)
        return False

    return True


def create_agent_components(config=None, persona=None):
    """Create STT, PigAgent, and TTS components based on configuration.

    Args:
        config: AgentConfig instance. If None, loads from get_config().
        persona: Persona instance for TTS voice override. If None, uses config defaults.

    Returns:
        Tuple of (stt, pig_agent, tts) instances.
    """
    if config is None:
        config = get_config()

    # Get API keys from environment variables
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

    # ── LLM Provider + PigAgent ─────────────────────────────────────────

    provider = config.create_provider()

    # Build instructions from persona
    llm_provider_id = config.LLM_PROVIDER.lower()
    if persona is not None and hasattr(persona, "get_full_prompt"):
        instructions = persona.get_full_prompt(llm_provider_id)
    else:
        from config import get_personality_prompt
        instructions = get_personality_prompt(llm_provider_id)

    pig_agent = PigAgent(AgentConfig(
        provider=provider,
        instructions=instructions,
        temperature=config.LLM_TEMPERATURE,
        max_tokens=config.LLM_MAX_TOKENS,
    ))

    logger.info(f"[Factory] PigAgent created with model={provider.model}")

    # ── TTS ────────────────────────────────────────────────────────────

    # Use persona voice if provided, else config default
    tts_voice = config.CARTESIA_TTS_VOICE
    tts_speed = config.CARTESIA_TTS_SPEED
    tts_emotion = None

    if persona is not None:
        if persona.tts_voice:
            tts_voice = persona.tts_voice
        if persona.tts_speed is not None:
            tts_speed = persona.tts_speed
        tts_emotion = persona.tts_emotion

    # Parse emotion from config if persona doesn't provide one
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
