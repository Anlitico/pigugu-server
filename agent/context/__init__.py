# agent/context/__init__.py
"""Context pipeline — 4-layer agent context with compression and extraction.

ContextLoader is the main entry point for agents.
For ContextManager and ContextCompressor (LLM-dependent), import directly:
    from context.manager import ContextManager
    from context.compression.compressor import ContextCompressor
"""

from .schema import WorkingContext, UserMemory, TokenBudget, RoastContext
from .storage.redis import RedisKeys
from .loader import ContextLoader, LoadResult
from .segment import detect_end

__all__ = [
    "WorkingContext", "UserMemory", "RedisKeys",
    "ContextLoader", "LoadResult", "detect_end",
]
