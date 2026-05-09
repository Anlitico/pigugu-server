# agent/context/__init__.py
"""Context assembly pipeline — builds system prompt per turn."""

from .assembler import ContextAssembler
from .mood_provider import MoodProvider
from .news_provider import NewsProvider

__all__ = ["ContextAssembler", "MoodProvider", "NewsProvider"]
