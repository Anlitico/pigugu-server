# agent/core/llm/types.py
"""统一类型系统 — 不绑定任何框架或 provider"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


# ═══════════════════════════════════════════════════════════════════════════════
# 消息模型
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ToolCall:
    """LLM 请求的工具调用"""
    id: str
    name: str
    arguments: str  # JSON string


@dataclass
class Message:
    """统一消息格式 — 兼容 OpenAI 的 role/content 语义"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    partial: bool = False                       # 续写标记（Qwen partial / DeepSeek prefix）
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

    def to_openai_dict(self) -> dict:
        """转为 OpenAI API 兼容的 dict"""
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


# ═══════════════════════════════════════════════════════════════════════════════
# Token 用量（计费基础）
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TokenUsage:
    """一次请求的 token 用量"""
    prompt_tokens: int = 0           # 输入 token
    completion_tokens: int = 0       # 输出 token
    total_tokens: int = 0            # prompt + completion

    # 缓存（部分 provider 支持 context caching）
    cached_prompt_tokens: int = 0    # 命中缓存的输入 token
    cache_write_tokens: int = 0      # 写入缓存的 token


# ═══════════════════════════════════════════════════════════════════════════════
# LLM 响应模型
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ChatResponse:
    """chat() 非流式响应"""
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None


@dataclass
class ChatDelta:
    """chat_stream() 流式增量"""
    content: str | None = None
    reasoning_content: str | None = None   # thinking 模式的思考过程
    tool_calls: list[ToolCall] | None = None
    usage: TokenUsage | None = None        # 仅最后一个 chunk 有值
    finish_reason: str | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# 模型元数据
# ═══════════════════════════════════════════════════════════════════════════════

class ModelCapability(str, Enum):
    TEXT = "text"
    VISION = "vision"
    TOOL_USE = "tool_use"
    STREAMING = "streaming"
    WEB_SEARCH = "web_search"


@dataclass
class ModelInfo:
    """模型描述 — 从 models.toml 加载"""
    model_id: str
    provider: str
    display_name: str
    capabilities: set[ModelCapability] = field(default_factory=set)
    context_window: int = 0
    max_output_tokens: int = 0
    thinking: bool = False          # 是否支持 thinking 模式
    search: bool = False            # 是否支持内置搜索
    temperature: float = 0.8        # 默认温度


# ═══════════════════════════════════════════════════════════════════════════════
# 工具定义
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ToolSpec:
    """工具定义 — OpenAI function calling 格式"""
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
