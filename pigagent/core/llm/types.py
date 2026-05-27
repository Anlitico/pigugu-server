# pigagent/core/llm/types.py
"""Unified type system — no framework or provider binding"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


# -------------------------------------------------------------------------------
# Message model
# -------------------------------------------------------------------------------

@dataclass
class ToolCall:
    """Tool call in an LLM request"""
    id: str
    name: str
    arguments: str  # JSON string


@dataclass
class Message:
    """Unified message format — OpenAI-compatible role/content semantics"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    partial: bool = False                       # Continuation marker (Qwen partial / DeepSeek prefix)
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "", *, partial: bool = False,
                  tool_calls: list[ToolCall] | None = None) -> "Message":
        return cls(role="assistant", content=content, partial=partial, tool_calls=tool_calls)

    @classmethod
    def tool(cls, call_id: str, name: str, content: str) -> "Message":
        return cls(role="tool", content=content, tool_call_id=call_id, name=name)

    def to_dict(self) -> dict:
        """Full serialization for Redis/PG storage (includes all fields)."""
        d: dict = {"role": self.role, "content": self.content}
        if self.partial:
            d["partial"] = True
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        """Deserialize from Redis/PG JSON dict."""
        tool_calls = None
        if d.get("tool_calls"):
            tool_calls = [ToolCall(**tc) for tc in d["tool_calls"]]
        return cls(
            role=d["role"],
            content=d["content"],
            partial=d.get("partial", False),
            tool_calls=tool_calls,
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
        )

    def to_openai_dict(self) -> dict:
        """Convert to OpenAI API compatible dict"""
        d: dict = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


# -------------------------------------------------------------------------------
# Token usage (billing basis)
# -------------------------------------------------------------------------------

@dataclass
class TokenUsage:
    """Token usage for one request."""
    prompt_tokens: int = 0           # input tokens
    completion_tokens: int = 0       # output tokens
    total_tokens: int = 0            # prompt + completion

    # Cache (some providers support context caching)
    cached_prompt_tokens: int = 0    # cached input tokens (cache hit)
    cache_write_tokens: int = 0      # tokens written to cache


# -------------------------------------------------------------------------------
# LLM response model
# -------------------------------------------------------------------------------

@dataclass
class ChatResponse:
    """Non-streaming chat response"""
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None


@dataclass
class ChatDelta:
    """Streaming chat delta."""
    content: str | None = None
    reasoning_content: str | None = None   # reasoning content in thinking mode
    tool_calls: list[ToolCall] | None = None
    usage: TokenUsage | None = None        # only present on the last chunk
    finish_reason: str | None = None


# -------------------------------------------------------------------------------
# Model metadata
# -------------------------------------------------------------------------------

class ModelCapability(str, Enum):
    TEXT = "text"
    VISION = "vision"
    TOOL_USE = "tool_use"
    STREAMING = "streaming"
    WEB_SEARCH = "web_search"


@dataclass
class ModelInfo:
    """Model descriptor — loaded from models.toml"""
    model_id: str
    provider: str
    display_name: str
    capabilities: set[ModelCapability] = field(default_factory=set)
    context_window: int = 0
    max_output_tokens: int = 0
    thinking: bool = False          # whether thinking mode is supported
    search: bool = False            # whether built-in search is supported
    api_model: str = ""             # actual model name sent to API (defaults to model_id)


# -------------------------------------------------------------------------------
# Tool definition
# -------------------------------------------------------------------------------

@dataclass
class ToolSpec:
    """Tool definition — OpenAI function calling format"""
    name: str
    description: str
    parameters: dict  # JSON Schema

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
