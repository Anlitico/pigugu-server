# agent/core/pig_agent.py
"""
PigAgent — 纯逻辑 Agent 引擎

独立于 LiveKit，可脱离语音管道独立测试和使用。

分层位置：
  LLMProvider (API 调���) → PigAgent (会话逻辑) → TrumpAgent (LiveKit 适配)
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from loguru import logger

from .llm.types import (
    Message,
    ChatDelta,
    ToolCall,
    ToolSpec,
)
from .llm.provider import LLMProvider


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 配置
# ═══════════════════════════════════════════════════════════════════════════════

ToolHandler = Callable[[ToolCall], Any]
"""工具处理函数：接收 ToolCall，返回 str/dict/list（同步或异步均可）"""


@dataclass
class AgentConfig:
    """PigAgent 配置"""
    provider: LLMProvider
    model: str
    instructions: str = ""
    tools: list[ToolSpec] = field(default_factory=list)
    tool_handlers: dict[str, ToolHandler] = field(default_factory=dict)
    temperature: float = 0.6
    max_tokens: int | None = None
    max_tool_iterations: int = 5


# ═══════════════════════════════════════════════════════════════════════════════
# 生命周期 Hook
# ═══════════════════════════════════════════════════════════════════════════════

class AgentHook(ABC):
    """PigAgent 生命周期钩子 — 用于注入上下文组装、记忆、日志等横切逻辑"""

    async def on_before_llm(
        self, messages: list[Message], agent: "PigAgent"
    ) -> list[Message]:
        """LLM 调用前 — 可修改 messages（如注入系统 prompt、动态上下文）"""
        return messages

    async def on_after_llm(
        self, delta: ChatDelta, agent: "PigAgent"
    ) -> None:
        """每个 delta 产生后 — 用于日志、metrics 收集"""

    async def on_before_tool(
        self, tool_call: ToolCall, agent: "PigAgent"
    ) -> ToolCall | None:
        """工具执行前 — 可拦截/修改 tool_call，返回 None 则跳过执行"""
        return tool_call

    async def on_after_tool(
        self, tool_call: ToolCall, result: str, agent: "PigAgent"
    ) -> str:
        """工具执行后 — 可修改结果"""
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# PigAgent
# ═══════════════════════════════════════════════════════════════════════════════

class PigAgent:
    """
    PigAgent — 纯逻辑 Agent 引擎

    用法：
        agent = PigAgent(AgentConfig(
            provider=get_llm("qwen-plus"),
            instructions="You are Trump...",
            tools=[ToolSpec(name="search", description="...", parameters={...})],
            tool_handlers={"search": do_search},
        ))
        async for text in agent.run([Message.user("What's up?")]):
            print(text, end="")
    """

    def __init__(self, config: AgentConfig, *, hooks: list[AgentHook] | None = None):
        self.config = config
        self._hooks = hooks or []
        self._tool_schemas = [t.to_openai_schema() for t in config.tools] if config.tools else None

    @property
    def provider(self) -> LLMProvider:
        return self.config.provider

    # ── 核心入口 ──

    async def run(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式运行 Agent

        流程：
        1. 注入系统指令（如果 messages 中没有 system 消息）
        2. 经过 on_before_llm hooks
        3. Tool Loop：调 LLM → 检测 tool_call → 执行 → 循环
        4. yield 文本流
        """
        messages = list(messages)

        # 注入 instructions 为 system 消息（如果还没有）
        if self.config.instructions and not any(m.role == "system" for m in messages):
            messages.insert(0, Message.system(self.config.instructions))

        temp = temperature if temperature is not None else self.config.temperature
        max_tok = max_tokens or self.config.max_tokens

        iterations = 0
        while iterations <= self.config.max_tool_iterations:
            iterations += 1

            # ── on_before_llm hooks ──
            for hook in self._hooks:
                messages = await hook.on_before_llm(messages, self)

            # ── 调用 LLM 流式 ──
            content_parts: list[str] = []
            pending_tool_calls: list[ToolCall] | None = None

            async for delta in self.provider.chat_stream(
                messages,
                model=self.config.model,
                tools=self._tool_schemas,
                temperature=temp,
                max_tokens=max_tok,
            ):
                # on_after_llm hook
                for hook in self._hooks:
                    await hook.on_after_llm(delta, self)

                if delta.content:
                    content_parts.append(delta.content)

                if delta.tool_calls:
                    pending_tool_calls = delta.tool_calls

            # ── 有 tool_call → 执行工具，继续循环 ──
            if pending_tool_calls:
                logger.info(
                    f"[PigAgent] Tool calls: {[tc.name for tc in pending_tool_calls]}"
                )
                messages.append(Message.assistant(
                    content="".join(content_parts),
                    tool_calls=pending_tool_calls,
                ))

                for tc in pending_tool_calls:
                    result = await self._execute_tool(tc)
                    messages.append(Message.tool(tc.id, tc.name, result))

                # 清空内容，下一轮继续
                content_parts.clear()
                pending_tool_calls = None
                continue

            # ── 无 tool_call → yield 文本并结束 ──
            for part in content_parts:
                yield part
            break

        if iterations > self.config.max_tool_iterations:
            logger.error(f"[PigAgent] Max tool iterations ({self.config.max_tool_iterations}) reached")

    async def run_once(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """非流式运行，收集全部文本后返回"""
        parts: list[str] = []
        async for text in self.run(messages, temperature=temperature, max_tokens=max_tokens):
            parts.append(text)
        return "".join(parts)

    # ── 工具执行 ──

    async def _execute_tool(self, tool_call: ToolCall) -> str:
        # on_before_tool hooks
        for hook in self._hooks:
            result = await hook.on_before_tool(tool_call, self)
            if result is None:
                return json.dumps({"status": "skipped"})
            if isinstance(result, ToolCall):
                tool_call = result

        handler = self.config.tool_handlers.get(tool_call.name)
        if handler is None:
            error = f"Unknown tool: {tool_call.name}"
            logger.error(f"[PigAgent] {error}")
            return json.dumps({"error": error})

        try:
            result = handler(tool_call)
            import inspect
            if inspect.iscoroutine(result):
                result = await result

            if isinstance(result, (dict, list)):
                result_str = json.dumps(result, ensure_ascii=False)
            else:
                result_str = str(result)
        except Exception as e:
            result_str = json.dumps({"error": str(e)})
            logger.error(f"[PigAgent] Tool '{tool_call.name}' failed: {e}")

        # on_after_tool hooks
        for hook in self._hooks:
            result_str = await hook.on_after_tool(tool_call, result_str, self)

        return result_str
