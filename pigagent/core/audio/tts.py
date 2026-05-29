# pigagent/tts.py
"""
Text-to-Speech (TTS) Module

Base class and implementations for various TTS providers.
Reference: https://docs.livekit.io/agents/models/tts/plugins/cartesia/
"""

import os
from abc import ABC, abstractmethod
from typing import Optional, Union
from loguru import logger

import aiohttp
from livekit.agents import tts
from livekit.plugins import cartesia


class TTSProvider(ABC):
    """
    Abstract base class for Text-to-Speech providers
    
    This base class defines the interface that all TTS implementations must follow.
    Subclasses should implement the initialization and plugin retrieval methods.
    """
    
    @abstractmethod
    def __init__(self, **kwargs):
        """Initialize the TTS provider with configuration parameters"""
        pass
    
    @abstractmethod
    def get_plugin(self) -> tts.TTS:
        """
        Get the LiveKit plugin instance for use with LiveKit Agent
        
        Returns:
            The underlying TTS plugin instance compatible with LiveKit Agents
            (livekit.agents.tts.TTS)
        """
        pass
    
    @property
    @abstractmethod
    def model(self) -> str:
        """Return the model name being used"""
        pass
    
    @property
    @abstractmethod
    def voice(self) -> Union[str, list[float]]:
        """Return the voice ID or embedding being used"""
        pass


class CartesiaTTS(TTSProvider):
    """Cartesia TTS implementation using LiveKit plugin"""
    
    def __init__(
        self,
        model: str = "sonic-3.5",
        language: Optional[str] = "en",
        encoding: str = "pcm_s16le",
        voice: Union[str, list[float]] = "9783574a-63f4-46bf-b56b-928eb52d3140",  # Default Cartesia voice
        speed: Optional[float] = None,
        emotion: Optional[list[str]] = None,
        volume: Optional[float] = None,
        sample_rate: int = 24000,
        word_timestamps: bool = True,
        pronunciation_dict_id: Optional[str] = None,
        api_key: Optional[str] = None,
        http_session: Optional[aiohttp.ClientSession] = None,
        base_url: str = "https://api.cartesia.ai"
    ):
        """
        Initialize Cartesia TTS using LiveKit plugin
        
        Args:
            model: Model name (default: "sonic-2")
            language: Language code (default: "en")
            encoding: Audio encoding format (default: "pcm_s16le")
            voice: Voice ID or embedding array (default: Cartesia default voice)
            speed: Speed of speech, valid between 0.6 and 2.0 for sonic-3 (optional)
            emotion: List of emotion strings (optional)
            volume: Volume of speech, valid between 0.5 and 2.0 for sonic-3 (optional)
            sample_rate: Sample rate of audio in Hz (default: 24000)
            word_timestamps: Whether to add word timestamps (default: True)
            pronunciation_dict_id: Custom pronunciation dictionary ID (optional)
            api_key: Cartesia API key (reads from CARTESIA_API_KEY env var if not provided)
            http_session: Optional aiohttp ClientSession for HTTP requests (default: None)
            base_url: Base URL for Cartesia API (default: "https://api.cartesia.ai")
        
        Reference:
            https://docs.livekit.io/agents/models/tts/plugins/cartesia/
        """
        self._model = model
        self._voice = voice
        self._language = language
        self._encoding = encoding
        self._speed = speed
        self._emotion = emotion
        self._volume = volume
        self._sample_rate = sample_rate
        self._word_timestamps = word_timestamps
        self._pronunciation_dict_id = pronunciation_dict_id
        self._base_url = base_url
        self.api_key = api_key or os.getenv("CARTESIA_API_KEY")
        
        if not self.api_key:
            logger.error("CARTESIA_API_KEY not set!")
            raise ValueError("CARTESIA_API_KEY is required for Cartesia TTS plugin")
        
        try:
            # Create Cartesia TTS plugin instance with all parameters
            self._tts = cartesia.TTS(
                model=model,
                language=language,
                encoding=encoding,  # type: ignore[reportArgumentType]
                voice=voice,
                speed=speed,
                emotion=emotion,
                volume=volume,
                sample_rate=sample_rate,
                word_timestamps=word_timestamps,
                pronunciation_dict_id=pronunciation_dict_id,
                api_key=self.api_key,
                http_session=http_session,
                base_url=base_url
            )
            
            logger.info(f"Initialized Cartesia TTS plugin")
            logger.info(f"   Model: {model}")
            logger.info(f"   Voice: {voice if isinstance(voice, str) else 'custom embedding'}")
            logger.info(f"   Language: {language}")
            logger.info(f"   Encoding: {encoding}")
            logger.info(f"   Sample Rate: {sample_rate} Hz")
            if speed:
                logger.info(f"   Speed: {speed}")
            if emotion:
                logger.info(f"   Emotion: {emotion}")
            if volume:
                logger.info(f"   Volume: {volume}")
            logger.info(f"   Base URL: {base_url}")
            
        except ImportError:
            logger.error("livekit-plugins-cartesia not installed!")
            logger.error("Install with: uv add 'livekit-plugins-cartesia~=1.3'")
            raise
    
    def get_plugin(self) -> tts.TTS:
        """Get the LiveKit Cartesia TTS plugin instance"""
        return self._tts
    
    @property
    def model(self) -> str:
        """Return the model name being used"""
        return self._model
    
    @property
    def voice(self) -> Union[str, list[float]]:
        """Return the voice ID or embedding being used"""
        return self._voice


def create_tts(
    model: str = "sonic-3.5",
    language: Optional[str] = "en",
    encoding: str = "pcm_s16le",
    voice: Union[str, list[float]] = "9783574a-63f4-46bf-b56b-928eb52d3140",
    speed: Optional[float] = None,
    emotion: Optional[list[str]] = None,
    volume: Optional[float] = None,
    sample_rate: int = 24000,
    word_timestamps: bool = True,
    pronunciation_dict_id: Optional[str] = None,
    api_key: Optional[str] = None,
    http_session: Optional[aiohttp.ClientSession] = None,
    base_url: str = "https://api.cartesia.ai"
) -> TTSProvider:
    """
    Factory function to create TTS provider
    
    Currently supports Cartesia TTS. Can be extended to support
    multiple providers in the future.
    
    Args:
        model: Model name (default: "sonic-2")
        language: Language code (default: "en")
        encoding: Audio encoding format (default: "pcm_s16le")
        voice: Voice ID or embedding array
        speed: Speed of speech (optional, valid 0.6-2.0 for sonic-3)
        emotion: List of emotion strings (optional)
        volume: Volume of speech (optional, valid 0.5-2.0 for sonic-3)
        sample_rate: Sample rate of audio in Hz (default: 24000)
        word_timestamps: Whether to add word timestamps (default: True)
        pronunciation_dict_id: Custom pronunciation dictionary ID (optional)
        api_key: Cartesia API key (optional, reads from env if not provided)
        http_session: Optional aiohttp ClientSession for HTTP requests
        base_url: Base URL for Cartesia API (default: "https://api.cartesia.ai")
    
    Returns:
        TTSProvider instance
    
    Reference:
        https://docs.livekit.io/agents/models/tts/plugins/cartesia/
    
    Example - Adding a new provider:
        To add a new TTS provider, create a class that inherits from TTSProvider:
        
        ```python
        from livekit.agents import tts
        
        class NewTTS(TTSProvider):
            def __init__(self, model: str = "model-name", voice: str = "voice-id"):
                self._model = model
                self._voice = voice
                from livekit.plugins import newprovider
                self._tts = newprovider.TTS(model=model, voice=voice)
            
            def get_plugin(self) -> tts.TTS:
                return self._tts
            
            @property
            def model(self) -> str:
                return self._model
            
            @property
            def voice(self) -> str:
                return self._voice
        ```
    """
    logger.info(
        f"Creating Cartesia TTS plugin: model={model}, voice={voice if isinstance(voice, str) else 'custom embedding'}, "
        f"language={language}, sample_rate={sample_rate}Hz"
    )
    return CartesiaTTS(
        model=model,
        language=language,
        encoding=encoding,
        voice=voice,
        speed=speed,
        emotion=emotion,
        volume=volume,
        sample_rate=sample_rate,
        word_timestamps=word_timestamps,
        pronunciation_dict_id=pronunciation_dict_id,
        api_key=api_key,
        http_session=http_session,
        base_url=base_url
    )

