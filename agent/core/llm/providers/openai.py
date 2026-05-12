# agent/core/llm/providers/openai.py
"""OpenAI Chat Completions 协议 — 覆盖 Qwen、Grok、DeepSeek、豆包等"""

from __future__ import annotations

from typing import AsyncIterator

from ..provider import LLMProvider
from ..types import Message, ChatResponse, ChatDelta, ToolCall, TokenUsage


class OpenAIChatProvider(LLMProvider):
    """
    OpenAI Chat Completions 兼容 Provider

    所有提供 `/v1/chat/completions` 端点的服务都可使用。
    各家特有参数差异在 _build_params 和 extra_body 中消化。
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str,
        temperature: float = 0.8,
        max_tokens: int | None = None,
    ):
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._base_url = base_url
        self._api_key = api_key

        import httpx
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=10.0),
                limits=httpx.Limits(max_connections=50),
            ),
        )

    # ── 属性 ──

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def provider_id(self) -> str:
        """从 base_url 推断 provider 标识（用于 _build_extra_body）"""
        u = self._base_url.lower()
        if "api.x.ai" in u:
            return "grok"
        if "dashscope-us" in u:
            return "qwen-us"
        if "dashscope" in u:
            return "qwen"
        if "deepseek" in u:
            return "deepseek"
        if "volces" in u or "ark" in u:
            return "doubao"
        return "openai"

    # ── 静态工厂 ──

    @classmethod
    def with_qwen(
        cls,
        model: str = "qwen-plus",
        region: str = "cn",
        *,
        api_key: str | None = None,
        temperature: float = 0.8,
        max_tokens: int | None = None,
    ) -> "OpenAIChatProvider":
        import os
        base_url = (
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
            if region == "cn"
            else "https://dashscope-us.aliyuncs.com/compatible-mode/v1"
        )
        return cls(
            model=model, api_key=api_key or os.getenv("DASHSCOPE_API_KEY", ""),
            base_url=base_url, temperature=temperature, max_tokens=max_tokens,
        )

    @classmethod
    def with_grok(
        cls,
        model: str = "grok-4-1-fast-reasoning",
        *,
        api_key: str | None = None,
        temperature: float = 0.8,
        max_tokens: int | None = None,
    ) -> "OpenAIChatProvider":
        import os
        return cls(
            model=model, api_key=api_key or os.getenv("XAI_API_KEY", ""),
            base_url="https://api.x.ai/v1", temperature=temperature, max_tokens=max_tokens,
        )

    @classmethod
    def with_deepseek(
        cls,
        model: str = "deepseek-chat",
        *,
        api_key: str | None = None,
        temperature: float = 0.8,
        max_tokens: int | None = None,
    ) -> "OpenAIChatProvider":
        import os
        return cls(
            model=model, api_key=api_key or os.getenv("DEEPSEEK_API_KEY", ""),
            base_url="https://api.deepseek.com/v1", temperature=temperature, max_tokens=max_tokens,
        )

    # ── 核心方法 ──

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        seed: int | None = None,
        **kwargs,
    ) -> ChatResponse:
        p = self._build_params(messages, tools, temperature, top_p, max_tokens, stop, seed, stream=False, **kwargs)
        completion = await self._client.chat.completions.create(**p)

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
        tools: list[dict] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        seed: int | None = None,
        **kwargs,
    ) -> AsyncIterator[ChatDelta]:
        p = self._build_params(messages, tools, temperature, top_p, max_tokens, stop, seed, stream=True, **kwargs)
        stream = await self._client.chat.completions.create(**p)

        buf: dict[int, dict] = {}

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # 文本
            if delta.content:
                yield ChatDelta(content=delta.content)

            # 思考过程（Qwen / DeepSeek / 豆包）
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield ChatDelta(reasoning_content=reasoning)

            # tool calls（流式分片聚合）
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

    # ── 参数构建 ──

    def _build_params(
        self,
        messages: list[Message],
        tools: list[dict] | None,
        temperature: float | None,
        top_p: float | None,
        max_tokens: int | None,
        stop: list[str] | None,
        seed: int | None,
        stream: bool,
        **kwargs,
    ) -> dict:
        body: dict = {
            "model": self._model,
            "messages": [self._serialize_message(m) for m in messages],
            "temperature": temperature if temperature is not None else self._temperature,
            "stream": stream,
        }

        if max_tokens is not None or self._max_tokens is not None:
            body["max_tokens"] = max_tokens or self._max_tokens
        if top_p is not None:
            body["top_p"] = top_p
        if stop:
            body["stop"] = stop
        if seed is not None:
            body["seed"] = seed
        if tools:
            body["tools"] = tools

        # extra_body — thinking、search、provider-specific
        extra = self._build_extra_body(**kwargs)
        if extra:
            body["extra_body"] = extra

        # stream_options
        if stream:
            body["stream_options"] = {"include_usage": True}

        return body

    def _build_extra_body(self, **kwargs) -> dict:
        extra: dict = {}

        pid = self.provider_id

        # ── Thinking ──
        if getattr(self, "_thinking", None) and self._thinking.enabled:
            if pid in ("qwen", "qwen-us"):
                extra["enable_thinking"] = True
                if self._thinking.budget_tokens:
                    extra["thinking_budget"] = self._thinking.budget_tokens
            elif pid == "deepseek":
                extra["thinking"] = {"type": "enabled"}
            elif pid == "doubao":
                extra["thinking"] = {"type": "enabled"}
            # Grok: reasoning_effort 放在顶层 body，不在 extra_body

        # ── Search ──
        if getattr(self, "_search", None) and self._search.enabled:
            if pid in ("qwen", "qwen-us"):
                extra["enable_search"] = True
                if self._search.force:
                    extra["search_options"] = {"forced_search": True}
            # Grok: web_search 通过 Response API 的 tools，不在 extra_body
            # DeepSeek: 不支持
            if pid == "deepseek":
                pass  # DeepSeek 不支持搜索

        # ── 透传 ──
        extra.update(kwargs)
        return extra

    def _serialize_message(self, m: Message) -> dict:
        """将 Message 序列化为 OpenAI 格式，处理 partial (prefix) 标记"""
        d = m.to_openai_dict()

        if m.partial and m.role == "assistant":
            pid = self.provider_id
            if pid in ("qwen", "qwen-us"):
                d["partial"] = True
            elif pid == "deepseek":
                d["prefix"] = True
            # 豆包、Grok 等无需特殊标记

        return d

    # ── Usage ──

    @staticmethod
    def _extract_usage(u) -> TokenUsage:
        details = getattr(u, "prompt_tokens_details", None)
        return TokenUsage(
            prompt_tokens=getattr(u, "prompt_tokens", 0),
            completion_tokens=getattr(u, "completion_tokens", 0),
            total_tokens=getattr(u, "total_tokens", 0),
            cached_prompt_tokens=getattr(details, "cached_tokens", 0) if details else 0,
        )
