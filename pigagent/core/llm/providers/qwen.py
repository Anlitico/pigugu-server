# pigagent/core/llm/providers/qwen.py
"""Qwen provider — DashScope OpenAI-compatible API"""

from __future__ import annotations

import os
from typing import AsyncIterator

from ..provider import LLMProvider
from ..registry import ModelRegistry
from ..types import Message, ChatResponse, ChatDelta, ToolCall, TokenUsage, ModelCapability


class QwenProvider(LLMProvider):
    """Qwen chat provider via DashScope compatible-mode (/v1/chat/completions)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or os.getenv("DASHSCOPE_US_API_KEY", "")

        import httpx
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            http_client=httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=10.0),
                limits=httpx.Limits(max_connections=50),
            ),
        )

    # ── Properties ──

    @property
    def base_url(self) -> str:
        return self._base_url

    # ── Core API ──

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
        self._validate(tools, thinking, search, model=model)
        params = self._build_params(
            messages, tools, tool_choice, parallel_tool_calls,
            temperature, top_p, max_tokens, stop, seed,
            thinking, search, response_format,
            model=model, stream=False, **kwargs,
        )
        completion = await self._client.chat.completions.create(**params)

        choice = completion.choices[0]
        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [
                ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                for tc in choice.message.tool_calls
            ]

        return ChatResponse(
            content=choice.message.content or "",
            tool_calls=tool_calls,
            usage=self._extract_usage(completion.usage) if completion.usage else None,
            finish_reason=choice.finish_reason,
        )

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
        self._validate(tools, thinking, search, model=model)
        params = self._build_params(
            messages, tools, tool_choice, parallel_tool_calls,
            temperature, top_p, max_tokens, stop, seed,
            thinking, search, response_format,
            model=model, stream=True, **kwargs,
        )
        stream = await self._client.chat.completions.create(**params)

        buf: dict[int, dict] = {}

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.content:
                yield ChatDelta(content=delta.content)

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield ChatDelta(reasoning_content=reasoning)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in buf:
                        buf[idx] = {"id": tc.id or "", "name": "", "arguments": ""}
                    if tc.id:
                        buf[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            buf[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            buf[idx]["arguments"] += tc.function.arguments

            usage = self._extract_usage(chunk.usage) if chunk.usage else None
            finish = chunk.choices[0].finish_reason

            if finish and buf:
                tool_calls = [
                    ToolCall(id=b["id"], name=b["name"], arguments=b["arguments"])
                    for b in buf.values()
                ]
                yield ChatDelta(tool_calls=tool_calls, usage=usage, finish_reason=finish)
                buf.clear()
            elif finish:
                yield ChatDelta(usage=usage, finish_reason=finish)

    # ── Validation ──

    def _validate(
        self,
        tools: list[dict] | None,
        thinking: dict | None,
        search: dict | None,
        *,
        model: str,
    ) -> None:
        """Raise before API call if the model does not support a requested feature."""
        info = ModelRegistry.get(model)

        if tools and ModelCapability.TOOL_USE not in info.capabilities:
            available = [m.model_id for m in ModelRegistry.list(capability=ModelCapability.TOOL_USE)]
            raise ValueError(
                f"Model '{model}' does not support tool_use. "
                f"Available: {available}"
            )

        if thinking and thinking.get("enabled") and not info.thinking:
            available = [m.model_id for m in ModelRegistry.list() if m.thinking]
            raise ValueError(
                f"Model '{model}' does not support thinking. "
                f"Available: {available}"
            )

        if search and search.get("enabled") and not info.search:
            available = [m.model_id for m in ModelRegistry.list() if m.search]
            raise ValueError(
                f"Model '{model}' does not support web search. "
                f"Available: {available}"
            )

    # ── Internal ──

    def _build_params(
        self,
        messages: list[Message],
        tools: list[dict] | None,
        tool_choice: str | None,
        parallel_tool_calls: bool,
        temperature: float | None,
        top_p: float | None,
        max_tokens: int | None,
        stop: list[str] | None,
        seed: int | None,
        thinking: dict | None,
        search: dict | None,
        response_format: dict | None,
        stream: bool,
        *,
        model: str,
        **kwargs,
    ) -> dict:
        body: dict = {
            "model": model,
            "messages": [self._serialize_message(m) for m in messages],
            "temperature": temperature if temperature is not None else 0.6,
            "stream": stream,
        }

        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if top_p is not None:
            body["top_p"] = top_p
        if stop:
            body["stop"] = stop
        if seed is not None:
            body["seed"] = seed
        if tools:
            body["tools"] = tools
            body["parallel_tool_calls"] = parallel_tool_calls
            if tool_choice and tool_choice not in ("auto",):
                body["tool_choice"] = tool_choice

        # ── Thinking ──
        if thinking and thinking.get("enabled"):
            body.setdefault("extra_body", {})["enable_thinking"] = True
            budget = thinking.get("budget")
            if budget:
                body["extra_body"]["thinking_budget"] = budget

        # ── Search (Ref: https://www.alibabacloud.com/help/en/model-studio/web-search) ──
        # search_strategy only supports "agent" (multi-turn tool calls, slow).
        # For real-time voice, omit strategy so model does single-pass search.
        if search and search.get("enabled"):
            body.setdefault("extra_body", {})["enable_search"] = True
            if search.get("force"):
                body["extra_body"]["search_options"] = {"search_strategy": "agent"}

        # ── Structured output ──
        if response_format:
            body["response_format"] = response_format

        # ── Stream options ──
        if stream:
            body["stream_options"] = {"include_usage": True}

        # ── Extra kwargs ──
        remaining = {k: v for k, v in kwargs.items() if k != "extra_body"}
        if remaining:
            body.setdefault("extra_body", {}).update(remaining)

        return body

    # ── Token Counting ──

    async def _tokenize(self, text: str) -> int:
        """DashScope tokenizer API."""
        if not text:
            return 0
        try:
            resp = await self._client.post(
                f"{self._base_url}/tokenizer",
                json={"model": "qwen-plus", "input": text},
            )
            data = resp.json()
            return data.get("total_tokens", await super()._tokenize(text))
        except Exception:
            return await super()._tokenize(text)

    def _serialize_message(self, m: Message) -> dict:
        d = m.to_openai_dict()
        if m.partial and m.role == "assistant":
            d["partial"] = True
        return d

    @staticmethod
    def _extract_usage(u) -> TokenUsage:
        details = getattr(u, "prompt_tokens_details", None)
        return TokenUsage(
            prompt_tokens=getattr(u, "prompt_tokens", 0),
            completion_tokens=getattr(u, "completion_tokens", 0),
            total_tokens=getattr(u, "total_tokens", 0),
            cached_prompt_tokens=getattr(details, "cached_tokens", 0) if details else 0,
        )
