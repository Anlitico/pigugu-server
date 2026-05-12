# agent/core/__init__.py
"""Core AI pipeline components — STT, LLM, TTS."""

from .stt import create_stt, STTProvider
from .tts import create_tts, TTSProvider

from .llm import (
    Message, ChatResponse, ChatDelta, ToolCall,
    ModelCapability, ModelInfo, ToolSpec,
    LLMProvider, OpenAIChatProvider, QwenProvider, VolcengineProvider,
    ModelRegistry, load_models, list_providers,
)
from .search import (
    build_search_messages, create_search_adapter, perplexity_web_search,
)
from .pigagent import PigAgent, AgentConfig, AgentHook

__all__ = [
    "create_stt", "STTProvider",
    "create_tts", "TTSProvider",
    "Message", "ChatResponse", "ChatDelta", "ToolCall",
    "ModelCapability", "ModelInfo", "ToolSpec",
    "LLMProvider", "OpenAIChatProvider", "QwenProvider", "VolcengineProvider",
    "ModelRegistry", "load_models", "list_providers",
    "PigAgent", "AgentConfig", "AgentHook",
    "build_search_messages", "create_search_adapter", "perplexity_web_search",
]
