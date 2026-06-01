# pigagent/core/llm/providers/xai.py
"""xAI (Grok) provider — OpenAI-compatible API.

Supports: tool calling, streaming, reasoning_effort, web search, deferred.
"""

from __future__ import annotations

import os

from ..provider import LLMProvider
from ..types import Message, ChatResponse, ChatDelta, ToolCall, TokenUsage


class XaiProvider(LLMProvider):
    """xAI Grok provider via OpenAI-compatible API (https://api.x.ai/v1)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.x.ai/v1",
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or os.getenv("XAI_API_KEY", "")

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
                ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments, index=tc.index)
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
    ):
        params = self._build_params(
            messages, tools, tool_choice, parallel_tool_calls,
            temperature, top_p, max_tokens, stop, seed,
            thinking, search, response_format,
            model=model, stream=True, **kwargs,
        )

        stream = await self._client.chat.completions.create(**params)

        buf: dict[int, dict] = {}
        usage: TokenUsage | None = None
        usage_final: TokenUsage | None = None

        async for chunk in stream:
            if not chunk.choices:
                usage = self._extract_usage(chunk.usage) if chunk.usage else None
                if usage:
                    usage_final = usage
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
                    yield ChatDelta(tool_calls=[
                        ToolCall(id=buf[idx]["id"], name=buf[idx]["name"],
                                 arguments=buf[idx]["arguments"], index=idx)
                    ])

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

        self._report_usage(usage_final)

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
        *,
        model: str,
        stream: bool,
        **kwargs,
    ) -> dict:
        from ..registry import ModelRegistry
        api_model = ModelRegistry.get(model).api_model or model

        body: dict = {
            "model": api_model,
            "messages": [m.to_openai_dict() for m in messages],
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

        # ── Reasoning (xAI native: reasoning_effort) ──
        if thinking and thinking.get("enabled"):
            effort = thinking.get("budget")
            if effort is None:
                body["reasoning_effort"] = "low"
            elif isinstance(effort, int):
                # Map token budget to rough effort: >32K→high, >8K→medium, else low
                if effort >= 32000:
                    body["reasoning_effort"] = "high"
                elif effort >= 8000:
                    body["reasoning_effort"] = "medium"
                else:
                    body["reasoning_effort"] = "low"
            else:
                body["reasoning_effort"] = "low"

        # ── Web search (xAI native: web_search_options) ──
        if search and search.get("enabled"):
            body["web_search_options"] = {"search_context_size": "medium"}

        # ── Structured output ──
        if response_format:
            body["response_format"] = response_format

        if stream:
            body["stream_options"] = {"include_usage": True}

        extra = {k: v for k, v in kwargs.items() if k not in ("extra_body", "session_id")}
        if extra:
            body.update(extra)

        # ── Sticky session / routing affinity ──
        # session_id (LiveKit session) → x-grok-conv-id header for routing
        # to the same engine, enabling KV cache reuse across turns (reduces TTFT).
        # prompt_cache_key in Responses API maps to this header.
        sid = kwargs.get("session_id")
        if sid:
            body.setdefault("extra_headers", {})["x-grok-conv-id"] = sid

        return body

    @staticmethod
    def _extract_usage(u: object) -> TokenUsage:
        if isinstance(u, dict):
            return TokenUsage(
                prompt_tokens=u.get("prompt_tokens", 0) or 0,
                completion_tokens=u.get("completion_tokens", 0) or 0,
                total_tokens=u.get("total_tokens", 0) or 0,
            )
        return TokenUsage(
            prompt_tokens=getattr(u, "prompt_tokens", 0),
            completion_tokens=getattr(u, "completion_tokens", 0),
            total_tokens=getattr(u, "total_tokens", 0),
        )
