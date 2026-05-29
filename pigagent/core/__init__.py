# pigagent/core/__init__.py
"""Core AI pipeline  -  LLM, agent, audio."""

from .audio import create_stt, STTProvider, create_tts

from .llm import (
    Message, ChatResponse, ChatDelta, ToolCall,
    ModelCapability, ModelInfo, ToolSpec,
    LLMProvider,
    ModelRegistry, load_models, list_providers,
)

__all__ = [
    "create_stt", "STTProvider",
    "create_tts",
    "Message", "ChatResponse", "ChatDelta", "ToolCall",
    "ModelCapability", "ModelInfo", "ToolSpec",
    "LLMProvider",
    "ModelRegistry", "load_models", "list_providers",
]
