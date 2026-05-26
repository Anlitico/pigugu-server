"""PigAgent LLM wrapper — implements LiveKit's LLM.chat() interface."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from livekit.agents import llm
from livekit.agents.llm import ChatContext, ChatChunk, ChoiceDelta
from livekit.agents.types import NOT_GIVEN, APIConnectOptions, NotGivenOr

DEFAULT_API_CONNECT_OPTIONS = APIConnectOptions()


class PigAgentLLM(llm.LLM):
    """LLM wrapper that delegates chat() to PigAgent.generate_reply()."""

    def __init__(self, pig_agent, persona_id: int = 1, user_id: str = ""):
        super().__init__()
        self._pig = pig_agent
        self._persona_id = persona_id
        self._user_id = user_id

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Any] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[Any] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> LLMStream:
        return LLMStream(
            llm=self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            pig=self._pig,
            persona_id=self._persona_id,
            user_id=self._user_id,
        )


class LLMStream(llm.LLMStream):
    """Stream wrapper around PigAgent.generate_reply()."""

    def __init__(
        self,
        *,
        llm: llm.LLM,
        chat_ctx: ChatContext,
        tools: list[Any],
        conn_options: APIConnectOptions,
        pig,
        persona_id: int,
        user_id: str,
    ):
        super().__init__(
            llm=llm,
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
        )
        self._pig = pig
        self._persona_id = persona_id
        self._user_id = user_id
        self._chat_ctx = chat_ctx

    async def _run(self) -> None:
        # Find the last message with text content, regardless of role.
        # Handles user messages (normal turns) and system/instructions
        # (from generate_reply with instructions=).
        last_text = ""
        for item in reversed(self._chat_ctx.items):
            text = getattr(item, "text_content", None)
            if text:
                last_text = text
                break

        if not last_text:
            self._event_ch.send_nowait(
                ChatChunk(id="done", delta=ChoiceDelta(content=""))
            )
            return

        async for text in self._pig.generate_reply(
            self._user_id, last_text,
            persona_id=self._persona_id,
        ):
            chunk = ChatChunk(
                id="chunk",
                delta=ChoiceDelta(content=text),
            )
            self._event_ch.send_nowait(chunk)

    @property
    def function_calls(self) -> list[llm.FunctionCall]:
        return []

    @property
    def is_function_call(self) -> bool:
        return False

    async def aclose(self) -> None:
        pass
