"""
LLM provider pool for non-LiveKit contexts (crawler classifier, etc.).

Uses DeepSeek's OpenAI-compatible endpoint. Configured via:
  DEEPSEEK_API_KEY   — API key
  DEEPSEEK_BASE_URL  — (optional) override base URL (default: https://api.deepseek.com/v1)

Uses the synchronous OpenAI client wrapped in asyncio.to_thread for
reliable cross-platform async execution.
"""

import asyncio
import os
from dataclasses import dataclass, field
from typing import Optional

from openai import OpenAI


@dataclass
class Message:
    role: str
    content: str

    @staticmethod
    def user(content: str) -> "Message":
        return Message(role="user", content=content)

    @staticmethod
    def assistant(content: str) -> "Message":
        return Message(role="assistant", content=content)

    @staticmethod
    def system(content: str) -> "Message":
        return Message(role="system", content=content)


@dataclass
class ChatResponse:
    content: str
    model: str
    usage: dict = field(default_factory=dict)


class LLM:
    """Thin wrapper around OpenAI client for the project's provider pool."""

    def __init__(self, client: OpenAI):
        self._client = client

    async def chat(
        self,
        messages: list[Message],
        model: str,
        *,
        response_format: Optional[dict] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> ChatResponse:
        kwargs = dict(
            model=model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=temperature,
        )
        if response_format is not None:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        resp = await asyncio.to_thread(
            self._client.chat.completions.create, **kwargs
        )
        choice = resp.choices[0]
        return ChatResponse(
            content=choice.message.content or "",
            model=resp.model,
            usage={
                "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
            },
        )


_providers: dict[str, LLM] = {}


def get_llm(name: str) -> LLM:
    """Get or create an LLM instance from the provider pool.

    Currently supports DeepSeek models (deepseek-chat, deepseek-reasoner).
    """
    if name not in _providers:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        base_url = os.environ.get(
            "DEEPSEEK_BASE_URL",
            "https://api.deepseek.com/v1",
        )
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=60.0,
            max_retries=2,
        )
        _providers[name] = LLM(client)
    return _providers[name]
