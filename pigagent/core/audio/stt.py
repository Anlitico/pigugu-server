# pigagent/stt.py
"""
Speech-to-Text (STT) Module

Base class and implementations for various STT providers.
Reference: https://docs.livekit.io/agents/models/stt/plugins/cartesia/
"""

import os
from abc import ABC, abstractmethod
from typing import Optional
from loguru import logger

import aiohttp
from livekit.agents import stt
from livekit.plugins import cartesia, deepgram


class STTProvider(ABC):
    """
    Abstract base class for Speech-to-Text providers
    
    This base class defines the interface that all STT implementations must follow.
    Subclasses should implement the initialization and plugin retrieval methods.
    """
    
    @abstractmethod
    def __init__(self, **kwargs):
        """Initialize the STT provider with configuration parameters"""
        pass
    
    @abstractmethod
    def get_plugin(self) -> stt.STT:
        """
        Get the LiveKit plugin instance for use with LiveKit Agent
        
        Returns:
            The underlying STT plugin instance compatible with LiveKit Agents
            (livekit.agents.stt.STT)
        """
        pass
    
    @property
    @abstractmethod
    def model(self) -> str:
        """Return the model name being used"""
        pass
    
    @property
    @abstractmethod
    def language(self) -> str:
        """Return the language code being used"""
        pass


class CartesiaSTT(STTProvider):
    """Cartesia ink-whisper STT implementation using LiveKit plugin"""
    
    def __init__(
        self,
        model: str = "ink-whisper",
        language: str = "en",
        encoding: str = "pcm_s16le",
        sample_rate: int = 16000,
        api_key: Optional[str] = None,
        http_session: Optional[aiohttp.ClientSession] = None,
        base_url: str = "https://api.cartesia.ai"
    ):
        """
        Initialize Cartesia STT using LiveKit plugin
        
        Args:
            model: Model name (default: "ink-whisper")
            language: Language code in ISO-639-1 format (default: "en")
            encoding: Audio encoding format (default: "pcm_s16le")
            sample_rate: Sample rate of audio in Hz (default: 16000)
            api_key: Cartesia API key (reads from CARTESIA_API_KEY env var if not provided)
            http_session: Optional aiohttp ClientSession for HTTP requests (default: None)
            base_url: Base URL for Cartesia API (default: "https://api.cartesia.ai")
        
        Reference:
            https://docs.livekit.io/agents/models/stt/plugins/cartesia/
        """
        self._model = model
        self._language = language
        self._encoding = encoding
        self._sample_rate = sample_rate
        self._base_url = base_url
        self.api_key = api_key or os.getenv("CARTESIA_API_KEY")
        
        if not self.api_key:
            logger.error("CARTESIA_API_KEY not set!")
            raise ValueError("CARTESIA_API_KEY is required for Cartesia STT plugin")
        
        try:
            # Create Cartesia STT plugin instance with all parameters
            self._stt = cartesia.STT(
                model=model,
                language=language,
                encoding=encoding,  # type: ignore[reportArgumentType]
                sample_rate=sample_rate,
                api_key=self.api_key,
                http_session=http_session,
                base_url=base_url
            )
            
            logger.info(f"Initialized Cartesia STT plugin")
            logger.info(f"   Model: {model}")
            logger.info(f"   Language: {language}")
            logger.info(f"   Encoding: {encoding}")
            logger.info(f"   Sample Rate: {sample_rate} Hz")
            logger.info(f"   Base URL: {base_url}")
            
        except ImportError:
            logger.error("livekit-plugins-cartesia not installed!")
            logger.error("Install with: uv add 'livekit-agents[cartesia]~=1.3'")
            raise
    
    def get_plugin(self) -> stt.STT:
        """Get the LiveKit Cartesia STT plugin instance"""
        return self._stt
    
    @property
    def model(self) -> str:
        """Return the model name being used"""
        return self._model
    
    @property
    def language(self) -> str:
        """Return the language code being used"""
        return self._language


class DeepgramSTT(STTProvider):
    """Deepgram Nova STT implementation using LiveKit plugin"""
    
    def __init__(
        self,
        model: str = "nova-3",
        language: str = "en",
        sample_rate: int = 16000,
        enable_diarization: bool = False,
        endpointing_ms: int = 500,
        api_key: Optional[str] = None,
        http_session: Optional[aiohttp.ClientSession] = None,
    ):
        """
        Initialize Deepgram STT using LiveKit plugin
        
        Args:
            model: Model name (default: "nova-3")
            language: Language code (default: "en")
            sample_rate: Sample rate of audio in Hz (default: 16000)
            enable_diarization: Enable speaker diarization (default: False)
            api_key: Deepgram API key (reads from DEEPGRAM_API_KEY env var if not provided)
            http_session: Optional aiohttp ClientSession for HTTP requests (default: None)
        
        Reference:
            https://docs.livekit.io/agents/models/stt/plugins/deepgram/
        """
        self._model = model
        self._language = language
        self._sample_rate = sample_rate
        self._enable_diarization = enable_diarization
        self.api_key = api_key or os.getenv("DEEPGRAM_API_KEY")
        
        if not self.api_key:
            logger.error("DEEPGRAM_API_KEY not set!")
            raise ValueError("DEEPGRAM_API_KEY is required for Deepgram STT plugin")
        
        try:
            # Create Deepgram STT plugin instance with all parameters
            self._stt = deepgram.STT(
                model=model,
                language=language,
                sample_rate=sample_rate,
                endpointing_ms=endpointing_ms,
                enable_diarization=enable_diarization,
                api_key=self.api_key,
                http_session=http_session,
            )
            
            logger.info(f"Initialized Deepgram STT plugin")
            logger.info(f"   Model: {model}")
            logger.info(f"   Language: {language}")
            logger.info(f"   Sample Rate: {sample_rate} Hz")
            logger.info(f"   Diarization: {enable_diarization}")
            
        except ImportError:
            logger.error("livekit-plugins-deepgram not installed!")
            logger.error("Install with: uv add livekit-plugins-deepgram")
            raise
    
    def get_plugin(self) -> stt.STT:
        """Get the LiveKit Deepgram STT plugin instance"""
        return self._stt
    
    @property
    def model(self) -> str:
        """Return the model name being used"""
        return self._model
    
    @property
    def language(self) -> str:
        """Return the language code being used"""
        return self._language


def create_stt(
    provider: str = "deepgram",
    model: Optional[str] = None,
    language: str = "en",
    encoding: str = "pcm_s16le",
    sample_rate: int = 16000,
    enable_diarization: bool = False,
    endpointing_ms: int = 500,
    api_key: Optional[str] = None,
    http_session: Optional[aiohttp.ClientSession] = None,
    base_url: str = "https://api.cartesia.ai"
) -> STTProvider:
    """
    Factory function to create STT provider
    
    Supports multiple STT providers: Deepgram (recommended) and Cartesia.
    
    Args:
        provider: STT provider to use - "deepgram" or "cartesia" (default: "deepgram")
        model: Model name (default: "nova-3" for Deepgram, "ink-whisper" for Cartesia)
        language: Language code (default: "en")
        encoding: Audio encoding format for Cartesia (default: "pcm_s16le")
        sample_rate: Sample rate of audio in Hz (default: 16000)
        enable_diarization: Enable speaker diarization for Deepgram (default: False)
        api_key: API key (optional, reads from DEEPGRAM_API_KEY or CARTESIA_API_KEY env)
        http_session: Optional aiohttp ClientSession for HTTP requests
        base_url: Base URL for Cartesia API (default: "https://api.cartesia.ai")
    
    Returns:
        STTProvider instance
    
    References:
        - Deepgram: https://docs.livekit.io/agents/models/stt/plugins/deepgram/
        - Cartesia: https://docs.livekit.io/agents/models/stt/plugins/cartesia/
    """
    provider = provider.lower()
    
    if provider == "deepgram":
        # Default model for Deepgram
        if model is None:
            model = "nova-3"
        
        logger.info(
            f"Creating Deepgram STT plugin: model={model}, language={language}, "
            f"sample_rate={sample_rate}Hz, diarization={enable_diarization}"
        )
        return DeepgramSTT(
            model=model,
            language=language,
            sample_rate=sample_rate,
            enable_diarization=enable_diarization,
            endpointing_ms=endpointing_ms,
            api_key=api_key,
            http_session=http_session,
        )
    
    elif provider == "cartesia":
        # Default model for Cartesia
        if model is None:
            model = "ink-whisper"
        
        logger.info(
            f"Creating Cartesia STT plugin: model={model}, language={language}, "
            f"encoding={encoding}, sample_rate={sample_rate}Hz"
        )
        return CartesiaSTT(
            model=model,
            language=language,
            encoding=encoding,
            sample_rate=sample_rate,
            api_key=api_key,
            http_session=http_session,
            base_url=base_url
        )
    
    else:
        raise ValueError(f"Unknown STT provider: {provider}. Supported: 'deepgram', 'cartesia'")

