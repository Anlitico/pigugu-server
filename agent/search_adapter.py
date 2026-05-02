from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from loguru import logger
from openai import AsyncOpenAI


def _normalize_role(role: str) -> str:
    normalized = (role or "").lower()
    if normalized == "developer":
        return "system"
    if normalized in {"system", "user", "assistant", "tool"}:
        return normalized
    return "user"


def build_search_messages(items) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []

    for item in items:
        content = getattr(item, "text_content", None)
        if not content:
            continue
        role = _normalize_role(str(getattr(item, "role", "user")))
        messages.append({"role": role, "content": content})

    # Keep one copy of each exact system message.
    seen_system = set()
    deduped: list[dict[str, str]] = []
    for msg in messages:
        if msg["role"] == "system":
            key = msg["content"].strip()
            if key in seen_system:
                continue
            seen_system.add(key)
        deduped.append(msg)

    # Ensure the first system message is at index 0 when present.
    first_system_idx = next((i for i, msg in enumerate(deduped) if msg["role"] == "system"), None)
    if first_system_idx not in (None, 0):
        first_system = deduped.pop(first_system_idx)
        deduped.insert(0, first_system)

    return deduped


class SearchAdapter(ABC):
    @abstractmethod
    async def stream_with_search(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        api_key: str,
        base_url: str,
        temperature: float,
        force_search: bool,
    ) -> AsyncIterator[str]:
        raise NotImplementedError


class QwenSearchAdapter(SearchAdapter):
    async def stream_with_search(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        api_key: str,
        base_url: str,
        temperature: float,
        force_search: bool,
    ) -> AsyncIterator[str]:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        extra_body = {"enable_search": True}
        if force_search:
            extra_body["search_options"] = {"forced_search": True}

        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            temperature=temperature,
            extra_body=extra_body,
        )

        chunk_count = 0
        yielded_count = 0
        async for chunk in stream:
            chunk_count += 1
            if chunk.choices and chunk.choices[0].delta.content:
                yielded_count += 1
                yield chunk.choices[0].delta.content
        logger.info(
            f"🔍 [SEARCH] Qwen stream finished: {chunk_count} total chunks, {yielded_count} yielded"
        )


class GrokSearchAdapter(SearchAdapter):
    async def stream_with_search(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        api_key: str,
        base_url: str,
        temperature: float,
        force_search: bool,
    ) -> AsyncIterator[str]:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        request = {
            "model": model,
            "input": messages,
            "tools": [{"type": "web_search"}],
            "stream": True,
        }
        if force_search:
            request["tool_choice"] = "required"

        stream = await client.responses.create(**request)

        event_count = 0
        yielded_count = 0
        tool_call_count = 0
        async for event in stream:
            event_count += 1
            event_type = getattr(event, "type", "")
            if "web_search" in str(event_type):
                tool_call_count += 1
            if event_type == "response.output_text.delta" and getattr(event, "delta", None):
                yielded_count += 1
                yield event.delta

        logger.info(
            f"🔍 [SEARCH] Grok stream finished: {event_count} events, "
            f"{yielded_count} text deltas, {tool_call_count} web_search calls"
        )


def create_search_adapter(provider: str) -> SearchAdapter:
    if (provider or "").lower() in {"grok", "xai"}:
        return GrokSearchAdapter()
    return QwenSearchAdapter()
