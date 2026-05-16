# agent/context/__init__.py
"""Context pipeline — system prompt assembly + conversation context management.

ContextLoader is the main entry point for agents.
For ContextManager and ContextCompressor (LLM-dependent), import directly:
    from context.manager import ContextManager
    from context.compression import ContextCompressor
"""

from .assembler import ContextAssembler
from .mood_provider import MoodProvider
from .news_provider import NewsProvider
from .schemas import WorkingContext, UserMemory, ContextSegment, RedisKeys
from .loader import ContextLoader, LoadResult
from .segment import detect_end

__all__ = [
    "ContextAssembler", "MoodProvider", "NewsProvider",
    "WorkingContext", "UserMemory", "RedisKeys",
    "ContextLoader", "LoadResult", "detect_end",
]
