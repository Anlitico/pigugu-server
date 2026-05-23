# pigagent/core/llm/provider.py
"""LLM Provider 抽象基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from .types import Message, ChatResponse, ChatDelta


class LLMProvider(ABC):
    """Stateless LLM provider — every parameter is per-call.

    === Tool Calling (5/5 providers) ==========================================

    tools: list[dict] | None
        OpenAI-compatible function definitions:
        [{"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}]

    tool_choice: str | None
        Controls whether and how the model calls tools.
        ``None`` / ``"auto"`` — model decides (default)
        ``"required"``       — must call at least one tool
        ``"none"``           — never call tools
        ``"<function_name>"`` — force a specific function

        Provider translation:
        Qwen / DeepSeek / Grok / Doubao — passes through as-is, specific function
        wrapped as ``{"type":"function","function":{"name":"<fn>"}}``.
        Gemini — ``AUTO`` / ``ANY`` / ``NONE``; specific function uses
        ``allowed_function_names``.

    parallel_tool_calls: bool = True
        Allow multiple tool calls in a single response (5/5). Set False for
        dependent tool chains where order matters.

    === Generation Parameters (5/5) ===========================================

    temperature: float | None    [0, 2]  randomness control
    top_p: float | None          [0, 1]  nucleus sampling alternative
    max_tokens: int | None               output token limit
    stop: list[str] | None               stop sequences
    seed: int | None                     reproducibility (Qwen/DeepSeek/Doubao;
                                          Grok/Gemini silently ignore)

    === Thinking / Reasoning (5/5) ===========================================

    thinking: dict | None
        Enable extended reasoning (chain-of-thought / thinking tokens).
        Schema: ``{"enabled": bool, "budget": int | None}``
        - ``enabled`` — toggle thinking mode
        - ``budget``  — max thinking tokens (None = provider default, 0 = unlimited)

        Reasoning content is streamed via ``ChatDelta.reasoning_content``.

        Provider translation:
        Qwen     — extra_body: {enable_thinking: True, thinking_budget: N}
        DeepSeek — extra_body: {thinking: {type: "enabled"}}; passes reasoning_effort
        Grok     — reasoning_effort top-level parameter
        Gemini   — thinking_config: {thinking_budget: N}
        Doubao   — thinking: {type: "enabled"}

    === Web Search (4/5, DeepSeek unsupported) ===============================

    search: dict | None
        Enable built-in web search (provider-native, not tool-based).
        Schema: ``{"enabled": bool, "force": bool}``
        - ``enabled`` — toggle search
        - ``force``   — force search regardless of query (default: model decides)

        Provider translation:
        Qwen     — extra_body: {enable_search: True, search_options: {search_strategy: "agent"}}
        Grok     — Chat: tools=[{type: "web_search"}] + tool_choice: "required"
                    Responses API: native support
        Gemini   — tools: [{googleSearch: {}}]; force = dynamic threshold 1.0
        Doubao   — Web Search plugin

    === Structured Output (5/5) ==============================================

    response_format: dict | None
        Constrain output to JSON.
        Schema: ``{"type": "json_object" | "json_schema", "schema": {...}}``
        - ``type``   — "json_object" (free-form JSON) or "json_schema" (with schema)
        - ``schema`` — JSON Schema dict (required when type="json_schema")

        Provider translation:
        Qwen / DeepSeek / Grok / Doubao — {type, json_schema: {name, schema}}
        Gemini — response_mime_type: "application/json" + response_json_schema

    === Prefix / Continuation =================================================

    Not a chat() parameter. Use ``Message.partial = True`` on the last
    assistant message to signal the model should continue from that prefix.

        msg = Message.assistant("The three reasons are", partial=True)

    Provider translation:
        Qwen     — {"role": "assistant", "content": "...", "partial": True}
        DeepSeek — {"role": "assistant", "content": "...", "prefix": True}
        Doubao   — continuation mode
        Grok / Gemini — plain assistant message appended (no native parameter)

    === Escape Hatch ==========================================================

    **kwargs
        Passed through to the provider as-is. Use for provider-specific
        parameters not covered above (safety_settings, top_k, reasoning_effort, etc.).
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        parallel_tool_calls: bool = True,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        seed: int | None = None,
        thinking: dict | None = None,
        search: dict | None = None,
        response_format: dict | None = None,
        **kwargs,
    ) -> ChatResponse:
        """非流式调用"""

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        parallel_tool_calls: bool = True,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        seed: int | None = None,
        thinking: dict | None = None,
        search: dict | None = None,
        response_format: dict | None = None,
        **kwargs,
    ) -> AsyncIterator[ChatDelta]:
        """流式调用"""

    # ── Token Counting ───────────────────────────────────────────────
    #
    # count_tokens = WHAT to count  (common logic, provider-agnostic)
    # _tokenize    = HOW to count   (per-provider, override in subclass)

    _PER_MESSAGE_OVERHEAD = 4  # role marker: <|start|>role\n

    async def count_tokens(
        self, message: Message | list[Message] | str, model: str = "qwen3.6-plus",
    ) -> int:
        """Token count. Accepts Message | list[Message] | str.

        Counts: per-message overhead + content + tool_calls + tool_call_id + name.
        Default uses offline tiktoken. Override for provider-specific tokenizer API.
        """
        from .types import Message as M
        if not message:
            return 0

        if isinstance(message, list):
            total = 0
            for m in message:
                total += await self.count_tokens(m, model=model)
            return total

        if isinstance(message, M):
            total = self._PER_MESSAGE_OVERHEAD
            total += await self._tokenize(message.content)

            if message.tool_calls:
                import json
                for tc in message.tool_calls:
                    total += await self._tokenize(json.dumps(
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    ))
            if message.tool_call_id:
                total += await self._tokenize(message.tool_call_id)
            if message.name:
                total += await self._tokenize(message.name)

            return total

        return await self._tokenize(str(message))

    async def _tokenize(self, text: str) -> int:
        """Tokenize plain text. Override in subclass for provider-specific tokenizer."""
        if not text:
            return 0
        try:
            import tiktoken
            return len(tiktoken.get_encoding("cl100k_base").encode(text))
        except Exception:
            cjk = sum(1 for c in text if '一' <= c <= '鿿' or '　' <= c <= '〿')
            other = len(text) - cjk
            return int(cjk * 1.5 + other / 3.5)

    @property
    @abstractmethod
    def base_url(self) -> str:
        """Provider API base URL."""
