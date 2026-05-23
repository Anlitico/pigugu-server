# pigagent/core/__init__.py
"""Core AI pipeline — LLM, agent, audio, search."""

from .audio import create_stt, STTProvider, create_tts, TTSProvider

from .llm import (
    Message, ChatResponse, ChatDelta, ToolCall,
    ModelCapability, ModelInfo, ToolSpec,
    LLMProvider, QwenProvider, VolcengineProvider,
    ModelRegistry, load_models, list_providers,
)
from .search import (
    build_search_messages, create_search_adapter, perplexity_web_search,
)
__all__ = [
    "create_stt", "STTProvider",
    "create_tts", "TTSProvider",
    "Message", "ChatResponse", "ChatDelta", "ToolCall",
    "ModelCapability", "ModelInfo", "ToolSpec",
    "LLMProvider", "QwenProvider", "VolcengineProvider",
    "ModelRegistry", "load_models", "list_providers",
    "build_search_messages", "create_search_adapter", "perplexity_web_search",
]
