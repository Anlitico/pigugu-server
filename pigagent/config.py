# pigagent/config.py
"""
Configuration for AI Agent

Configuration is loaded from environment variables first, then .config.

API Keys (MUST be provided by environment variables, usually .env locally):
- LIVEKIT_API_KEY
- LIVEKIT_API_SECRET
- DEEPGRAM_API_KEY (if using Deepgram STT)
- CARTESIA_API_KEY (if using Cartesia STT/TTS)
- DASHSCOPE_API_KEY
- XAI_API_KEY (if using Grok LLM)
"""

import os
import tomllib
from pathlib import Path
from typing import Optional, Dict, Any
from pydantic_settings import BaseSettings
from pydantic import Field
from loguru import logger as config_logger


def load_config_file() -> Dict[str, Any]:
    """
    Load configuration from .config file (flat TOML format)
    
    Returns:
        Dictionary of configuration values
    """
    config_path = Path(__file__).parent / ".config"
    
    if not config_path.exists():
        config_logger.warning(f"Config file not found: {config_path}")
        return {}
    
    try:
        with open(config_path, "rb") as f:
            config_data = tomllib.load(f)
        
        config_logger.info(f"Loaded configuration from .config file")
        return config_data
    
    except Exception as e:
        config_logger.error(f"Error loading .config file: {e}")
        return {}


CONFIG_FILE_DATA = load_config_file()


def get_config_value(key: str, default: Any = None) -> Any:
    """
    Get configuration value from environment variables, then .config.
    """
    env_value = os.getenv(key)
    if env_value is not None and env_value != "":
        config_logger.debug(f"Config {key}: from environment")
        return env_value

    config_value = CONFIG_FILE_DATA.get(key)
    if config_value is not None and config_value != "":
        config_logger.debug(f"Config {key}: from .config file")
        return config_value

    config_logger.debug(f"Config {key}: using default={default}")
    return default


def get_bool_config_value(key: str, default: bool = False) -> bool:
    value = get_config_value(key, default)
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


class AgentConfig(BaseSettings):
    """
    Agent configuration

    Loads from environment variables first, then flat .config TOML.
    API keys MUST be provided via environment variables.
    """
    
    # LiveKit Configuration
    LIVEKIT_URL: str = Field(default_factory=lambda: get_config_value("LIVEKIT_URL", "ws://localhost:8002"))
    
    # STT Provider Selection - "deepgram" or "cartesia"
    STT_PROVIDER: str = Field(default_factory=lambda: get_config_value("STT_PROVIDER", "deepgram"))
    
    # STT Configuration (Deepgram)
    DEEPGRAM_STT_MODEL: str = Field(default_factory=lambda: get_config_value("DEEPGRAM_STT_MODEL", "nova-3"))
    DEEPGRAM_STT_LANGUAGE: str = Field(default_factory=lambda: get_config_value("DEEPGRAM_STT_LANGUAGE", "en"))
    DEEPGRAM_STT_SAMPLE_RATE: int = Field(default_factory=lambda: int(get_config_value("DEEPGRAM_STT_SAMPLE_RATE", 16000)))
    DEEPGRAM_ENABLE_DIARIZATION: bool = Field(default_factory=lambda: get_bool_config_value("DEEPGRAM_ENABLE_DIARIZATION", False))
    
    # STT Configuration (Cartesia)
    CARTESIA_STT_MODEL: str = Field(default_factory=lambda: get_config_value("CARTESIA_STT_MODEL", "ink-whisper"))
    CARTESIA_STT_LANGUAGE: str = Field(default_factory=lambda: get_config_value("CARTESIA_STT_LANGUAGE", "en"))
    CARTESIA_STT_ENCODING: str = Field(default_factory=lambda: get_config_value("CARTESIA_STT_ENCODING", "pcm_s16le"))
    CARTESIA_STT_SAMPLE_RATE: int = Field(default_factory=lambda: int(get_config_value("CARTESIA_STT_SAMPLE_RATE", 16000)))
    CARTESIA_STT_BASE_URL: str = Field(default_factory=lambda: get_config_value("CARTESIA_STT_BASE_URL", "https://api.cartesia.ai"))
    
    # TTS Configuration (Cartesia)
    CARTESIA_TTS_MODEL: str = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_MODEL", "sonic-2"))
    CARTESIA_TTS_VOICE: str = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_VOICE", "a0e99841-438c-4a64-b679-ae501e7d6091"))
    CARTESIA_TTS_LANGUAGE: Optional[str] = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_LANGUAGE", "en"))
    CARTESIA_TTS_ENCODING: str = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_ENCODING", "pcm_s16le"))
    CARTESIA_TTS_SAMPLE_RATE: int = Field(default_factory=lambda: int(get_config_value("CARTESIA_TTS_SAMPLE_RATE", 24000)))
    CARTESIA_TTS_SPEED: Optional[float] = Field(default_factory=lambda: float(get_config_value("CARTESIA_TTS_SPEED")) if get_config_value("CARTESIA_TTS_SPEED") else None)
    CARTESIA_TTS_EMOTION: Optional[str] = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_EMOTION"))
    CARTESIA_TTS_VOLUME: float = Field(default_factory=lambda: float(get_config_value("CARTESIA_TTS_VOLUME", 1.0)))
    CARTESIA_TTS_WORD_TIMESTAMPS: bool = Field(default_factory=lambda: get_bool_config_value("CARTESIA_TTS_WORD_TIMESTAMPS", True))
    CARTESIA_TTS_BASE_URL: str = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_BASE_URL", "https://api.cartesia.ai"))
    
    # LLM Configuration
    # LLM_PROVIDER: provider ID used to resolve api_key / base_url
    #   ("qwen", "qwen-us", "grok", "xai", "deepseek", etc.)
    LLM_PROVIDER: str = Field(default_factory=lambda: get_config_value("LLM_PROVIDER", "qwen"))

    # LLM_MODEL: unified model field  -  when set, takes priority over QWEN_MODEL/GROK_MODEL
    LLM_MODEL: str = Field(default_factory=lambda: get_config_value("LLM_MODEL", ""))

    # Legacy per-provider model fields (still supported)
    QWEN_MODEL: str = Field(default_factory=lambda: get_config_value("QWEN_MODEL", "qwen-plus"))
    GROK_MODEL: str = Field(default_factory=lambda: get_config_value("GROK_MODEL", "grok-4-fast-reasoning"))

    # LLM Settings
    # Lightweight model for background tasks (segment end detection, compression)
    SEGMENT_DETECT_MODEL: str = Field(default_factory=lambda: get_config_value("SEGMENT_DETECT_MODEL", "qwen3.6-flash"))

    LLM_TEMPERATURE: float = Field(default_factory=lambda: float(get_config_value("LLM_TEMPERATURE", 0.6)))
    LLM_MAX_TOKENS: Optional[int] = Field(default_factory=lambda: int(get_config_value("LLM_MAX_TOKENS")) if get_config_value("LLM_MAX_TOKENS") else None)

    def resolve_model(self) -> str:
        """Resolve the effective model name.

        Priority: LLM_MODEL > provider-specific field (QWEN_MODEL / GROK_MODEL)
        """
        if self.LLM_MODEL:
            return self.LLM_MODEL
        provider = self.LLM_PROVIDER.lower()
        if provider in ("grok", "xai"):
            return self.GROK_MODEL
        return self.QWEN_MODEL

    def create_provider(self):
        """Return a pre-built LLMProvider from the pool.

        All provider instances are created once at startup via core.llm._build_pool().
        Model is selected per-call via chat(model=...).
        """
        from core.llm import get_llm

        model = self.resolve_model()
        config_logger.info(f"Getting LLM provider for model={model}")
        return get_llm(model)

    def get_llm_config(self) -> dict:
        """Backward-compat wrapper. New code should use create_provider()."""
        provider = self.LLM_PROVIDER.lower()
        model = self.resolve_model()

        from core.llm.registry import resolve_provider
        base_url, api_key, _ = resolve_provider(provider)

        return {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "provider": provider,
        }
    
    # Agent Settings
    AGENT_WORKERS: int = Field(default_factory=lambda: int(get_config_value("AGENT_WORKERS", 2)))
    AGENT_LOG_CONVERSATIONS: bool = Field(default_factory=lambda: get_bool_config_value("AGENT_LOG_CONVERSATIONS", True))
    ENABLE_INTERRUPTIONS: bool = Field(default_factory=lambda: get_bool_config_value("ENABLE_INTERRUPTIONS", True))
    SILENCE_THRESHOLD: float = Field(default_factory=lambda: float(get_config_value("SILENCE_THRESHOLD", 30.0)))

    # Context Module  -  compression / extraction tuning
    CONTEXT_HOT_WINDOW_SIZE: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_HOT_WINDOW_SIZE", 500)))
    CONTEXT_TOKEN_BUDGET_CAP: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_TOKEN_BUDGET_CAP", 200_000)))
    CONTEXT_ROAST_COMPRESSION_RATIO: float = Field(default_factory=lambda: float(get_config_value("CONTEXT_ROAST_COMPRESSION_RATIO", 0.05)))
    CONTEXT_ROAST_COMPRESSION_MIN_TOKENS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_ROAST_COMPRESSION_MIN_TOKENS", 1000)))
    AGENT_MAX_STEPS: int = Field(default_factory=lambda: int(get_config_value("AGENT_MAX_STEPS", 5)))
    CONTEXT_MAX_TURNS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_MAX_TURNS", 400)))
    CONTEXT_L3_COMPRESS_MAX_WORDS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_L3_COMPRESS_MAX_WORDS", 5000)))
    CONTEXT_L3_MERGE_MAX_WORDS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_L3_MERGE_MAX_WORDS", 8000)))
    CONTEXT_L4_ROAST_MAX_WORDS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_L4_ROAST_MAX_WORDS", 5000)))
    CONTEXT_L2_PROFILE_MAX_WORDS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_L2_PROFILE_MAX_WORDS", 1500)))
    
    # Welcome Greeting
    ENABLE_WELCOME_GREETING: bool = Field(default_factory=lambda: get_bool_config_value("ENABLE_WELCOME_GREETING", True))
    WELCOME_GREETING: str = Field(default_factory=lambda: get_config_value("WELCOME_GREETING", "Hello! It's Trump here. I'm the best AI assistant you'll ever talk to, believe me. What can I do for you today?"))
    
    # Advanced Agent Features
    ENABLE_PREEMPTIVE_SYNTHESIS: bool = Field(default_factory=lambda: get_bool_config_value("ENABLE_PREEMPTIVE_SYNTHESIS", True))
    ENABLE_TURN_DETECTOR: bool = Field(default_factory=lambda: get_bool_config_value("ENABLE_TURN_DETECTOR", True))
    ENABLE_FILLER_WORDS: bool = Field(default_factory=lambda: get_bool_config_value("ENABLE_FILLER_WORDS", False))
    ENABLE_POLICY_SEARCH: bool = Field(default_factory=lambda: get_bool_config_value("ENABLE_POLICY_SEARCH", False))
    FORCE_POLICY_SEARCH: bool = Field(default_factory=lambda: get_bool_config_value("FORCE_POLICY_SEARCH", False))
    
    # Policy Search Backend: "built_in" (default) or "perplexity"
    POLICY_SEARCH_BACKEND: str = Field(default_factory=lambda: get_config_value("POLICY_SEARCH_BACKEND", "built_in"))
    
    # Perplexity Search Configuration (only used when POLICY_SEARCH_BACKEND = "perplexity")
    PERPLEXITY_SEARCH_MODEL: str = Field(default_factory=lambda: get_config_value("PERPLEXITY_SEARCH_MODEL", "sonar-pro"))
    PERPLEXITY_SEARCH_BASE_URL: str = Field(default_factory=lambda: get_config_value("PERPLEXITY_SEARCH_BASE_URL", "https://api.perplexity.ai"))
    
    # Agent Mode: 1 = Default, 2 = Interrupt Mode
    AGENT_MODE: int = Field(default_factory=lambda: int(get_config_value("AGENT_MODE", 1)))
    INTERRUPT_INTERVAL_SECONDS: float = Field(default_factory=lambda: float(get_config_value("INTERRUPT_INTERVAL_SECONDS", 30.0)))
    
    # Advanced Settings (Phase 5)
    ENGAGEMENT_THRESHOLD: float = Field(default_factory=lambda: float(get_config_value("ENGAGEMENT_THRESHOLD", 0.7)))
    DEBATE_MODE_SILENCE_MULTIPLIER: float = Field(default_factory=lambda: float(get_config_value("DEBATE_MODE_SILENCE_MULTIPLIER", 1.5)))
    
    # Logging
    LOG_LEVEL: str = Field(default_factory=lambda: get_config_value("LOG_LEVEL", "INFO"))
    LOG_TO_FILE: bool = Field(default_factory=lambda: get_bool_config_value("LOG_TO_FILE", True))
    LOG_ROTATION: str = Field(default_factory=lambda: get_config_value("LOG_ROTATION", "00:00"))
    LOG_RETENTION: str = Field(default_factory=lambda: get_config_value("LOG_RETENTION", "7 days"))
    LOG_FILE_PATH: str = Field(default_factory=lambda: get_config_value("LOG_FILE_PATH", "logs/agent_{time:YYYY-MM-DD}.log"))
    
    class Config:
        case_sensitive = True
        # Environment variables can override any setting.


def get_config() -> AgentConfig:
    """
    Get agent configuration

    Configuration is loaded from environment variables, then flat .config TOML.
    """
    config_logger.info("=" * 70)
    config_logger.info(f"Loading Agent Configuration")
    config_logger.info("Config sources: environment, then .config")
    config_logger.info("=" * 70)

    config = AgentConfig()

    # Log the actual model being used and its source
    file_grok = CONFIG_FILE_DATA.get("GROK_MODEL")
    if os.getenv("GROK_MODEL"):
        config_logger.info(f"GROK_MODEL: {config.GROK_MODEL} (from environment)")
    elif file_grok:
        config_logger.info(f"GROK_MODEL: {config.GROK_MODEL} (from .config file)")
    else:
        config_logger.info(f"GROK_MODEL: {config.GROK_MODEL} (using default)")

    return config

