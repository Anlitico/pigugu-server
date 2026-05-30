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
        config_logger.trace(f"Config {key}: from environment")
        return env_value

    config_value = CONFIG_FILE_DATA.get(key)
    if config_value is not None and config_value != "":
        config_logger.trace(f"Config {key}: from .config file")
        return config_value

    config_logger.trace(f"Config {key}: using default={default}")
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
    CARTESIA_TTS_MODEL: str = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_MODEL", "sonic-3.5"))
    CARTESIA_TTS_VOICE: str = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_VOICE", "9783574a-63f4-46bf-b56b-928eb52d3140"))
    CARTESIA_TTS_LANGUAGE: Optional[str] = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_LANGUAGE", "en"))
    CARTESIA_TTS_ENCODING: str = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_ENCODING", "pcm_s16le"))
    CARTESIA_TTS_SAMPLE_RATE: int = Field(default_factory=lambda: int(get_config_value("CARTESIA_TTS_SAMPLE_RATE", 24000)))
    CARTESIA_TTS_SPEED: Optional[float] = Field(default_factory=lambda: float(get_config_value("CARTESIA_TTS_SPEED")) if get_config_value("CARTESIA_TTS_SPEED") else None)
    CARTESIA_TTS_EMOTION: Optional[str] = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_EMOTION"))
    CARTESIA_TTS_VOLUME: float = Field(default_factory=lambda: float(get_config_value("CARTESIA_TTS_VOLUME", 1.0)))
    CARTESIA_TTS_WORD_TIMESTAMPS: bool = Field(default_factory=lambda: get_bool_config_value("CARTESIA_TTS_WORD_TIMESTAMPS", True))
    CARTESIA_TTS_BASE_URL: str = Field(default_factory=lambda: get_config_value("CARTESIA_TTS_BASE_URL", "https://api.cartesia.ai"))
    
    # LLM Configuration

    QWEN_MODEL: str = Field(default_factory=lambda: get_config_value("QWEN_MODEL", "qwen-flash-us"))

    # LLM Settings
    LLM_TEMPERATURE: float = Field(default_factory=lambda: float(get_config_value("LLM_TEMPERATURE", 0.6)))
    LLM_MAX_TOKENS: Optional[int] = Field(default_factory=lambda: int(get_config_value("LLM_MAX_TOKENS")) if get_config_value("LLM_MAX_TOKENS") else None)

    def resolve_model(self) -> str:
        """Resolve the effective model name."""
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

    # Agent Settings
    AGENT_WORKERS: int = Field(default_factory=lambda: int(get_config_value("AGENT_WORKERS", 2)))
    ENABLE_INTERRUPTIONS: bool = Field(default_factory=lambda: get_bool_config_value("ENABLE_INTERRUPTIONS", True))
    AGENT_MAX_STEPS: int = Field(default_factory=lambda: int(get_config_value("AGENT_MAX_STEPS", 5)))
    ENDPOINTING_DELAY: float = Field(default_factory=lambda: float(get_config_value("ENDPOINTING_DELAY", 0.5)))

    # Context Module  -  compression / extraction tuning
    CONTEXT_TOKEN_BUDGET_CAP: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_TOKEN_BUDGET_CAP", 200_000)))
    CONTEXT_ROAST_COMPRESSION_RATIO: float = Field(default_factory=lambda: float(get_config_value("CONTEXT_ROAST_COMPRESSION_RATIO", 0.05)))
    CONTEXT_ROAST_COMPRESSION_MIN_TOKENS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_ROAST_COMPRESSION_MIN_TOKENS", 1000)))
    CONTEXT_MAX_TURNS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_MAX_TURNS", 100)))
    CONTEXT_L3_COMPRESS_MAX_WORDS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_L3_COMPRESS_MAX_WORDS", 5000)))
    CONTEXT_L3_MERGE_MAX_WORDS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_L3_MERGE_MAX_WORDS", 8000)))
    CONTEXT_L4_ROAST_MAX_WORDS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_L4_ROAST_MAX_WORDS", 5000)))
    CONTEXT_L2_PROFILE_MAX_WORDS: int = Field(default_factory=lambda: int(get_config_value("CONTEXT_L2_PROFILE_MAX_WORDS", 1500)))

    @property
    def CONTEXT_HOT_WINDOW_SIZE(self) -> int:
        """Redis turn storage = max turns + 50 buffer. Not a separate config knob."""
        return self.CONTEXT_MAX_TURNS + 50

    # Advanced Agent Features
    ENABLE_PREEMPTIVE_SYNTHESIS: bool = Field(default_factory=lambda: get_bool_config_value("ENABLE_PREEMPTIVE_SYNTHESIS", True))
    ENABLE_POLICY_SEARCH: bool = Field(default_factory=lambda: get_bool_config_value("ENABLE_POLICY_SEARCH", False))

    # Policy Search Backend: "built_in" (default) or "perplexity"
    POLICY_SEARCH_BACKEND: str = Field(default_factory=lambda: get_config_value("POLICY_SEARCH_BACKEND", "built_in"))
    
    # Logging
    LOG_LEVEL: str = Field(default_factory=lambda: get_config_value("LOG_LEVEL", "INFO"))
    LOG_TO_FILE: bool = Field(default_factory=lambda: get_bool_config_value("LOG_TO_FILE", True))
    LOG_ROTATION: str = Field(default_factory=lambda: get_config_value("LOG_ROTATION", "00:00"))
    LOG_RETENTION: str = Field(default_factory=lambda: get_config_value("LOG_RETENTION", "7 days"))
    LOG_FILE_PATH: str = Field(default_factory=lambda: get_config_value("LOG_FILE_PATH", "logs/agent_{time:YYYY-MM-DD}.log"))
    
    class Config:
        case_sensitive = True
        # Environment variables can override any setting.


_config_cache: AgentConfig | None = None


def get_config() -> AgentConfig:
    """Get agent configuration (singleton, cached after first call).

    Configuration is loaded from environment variables, then flat .config TOML.
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config_logger.info("=" * 70)
    config_logger.info("Loading Agent Configuration")
    config_logger.info("Config sources: environment, then .config")
    config_logger.info("=" * 70)

    _config_cache = AgentConfig()
    _log_config_summary(_config_cache)
    return _config_cache


def _log_config_summary(cfg: AgentConfig) -> None:
    """Print all config values at INFO level once after loading."""
    fields = [
        ("LIVEKIT_URL", cfg.LIVEKIT_URL),
        ("STT_PROVIDER", cfg.STT_PROVIDER),
        ("DEEPGRAM", f"model={cfg.DEEPGRAM_STT_MODEL} lang={cfg.DEEPGRAM_STT_LANGUAGE} rate={cfg.DEEPGRAM_STT_SAMPLE_RATE} diarization={cfg.DEEPGRAM_ENABLE_DIARIZATION}"),
        ("CARTESIA_STT", f"model={cfg.CARTESIA_STT_MODEL} lang={cfg.CARTESIA_STT_LANGUAGE} encoding={cfg.CARTESIA_STT_ENCODING} rate={cfg.CARTESIA_STT_SAMPLE_RATE}"),
        ("CARTESIA_TTS", f"model={cfg.CARTESIA_TTS_MODEL} voice={cfg.CARTESIA_TTS_VOICE} lang={cfg.CARTESIA_TTS_LANGUAGE} speed={cfg.CARTESIA_TTS_SPEED} emotion={cfg.CARTESIA_TTS_EMOTION} volume={cfg.CARTESIA_TTS_VOLUME}"),
        ("QWEN_MODEL", cfg.QWEN_MODEL),
        ("LLM_TEMPERATURE", cfg.LLM_TEMPERATURE),
        ("LLM_MAX_TOKENS", cfg.LLM_MAX_TOKENS),
        ("AGENT_WORKERS", cfg.AGENT_WORKERS),
        ("AGENT_MAX_STEPS", cfg.AGENT_MAX_STEPS),
        ("ENABLE_INTERRUPTIONS", cfg.ENABLE_INTERRUPTIONS),
        ("ENABLE_PREEMPTIVE_SYNTHESIS", cfg.ENABLE_PREEMPTIVE_SYNTHESIS),
        ("ENABLE_POLICY_SEARCH", cfg.ENABLE_POLICY_SEARCH),
        ("POLICY_SEARCH_BACKEND", cfg.POLICY_SEARCH_BACKEND),
        ("CONTEXT_HOT_WINDOW_SIZE", cfg.CONTEXT_HOT_WINDOW_SIZE),
        ("CONTEXT_TOKEN_BUDGET_CAP", cfg.CONTEXT_TOKEN_BUDGET_CAP),
        ("CONTEXT_MAX_TURNS", cfg.CONTEXT_MAX_TURNS),
        ("LOG_LEVEL", cfg.LOG_LEVEL),
        ("LOG_TO_FILE", cfg.LOG_TO_FILE),
    ]
    for name, value in fields:
        config_logger.info(f"  {name}: {value}")
    config_logger.info("=" * 70)

