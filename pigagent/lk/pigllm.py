# pigagent/lk/pigllm.py
"""Placeholder llm.LLM — satisfies isinstance() check so AgentSession enters
the pipeline path and calls bridge.llm_node(). Never actually used for generation.
"""

from __future__ import annotations

from livekit.agents import llm
from livekit.agents.llm import ChatContext, ChatChunk, ChoiceDelta
from livekit.agents.types import APIConnectOptions

_CONN = APIConnectOptions()


class PigAgentLLM(llm.LLM):
    """Placeholder — bridge.llm_node() handles all LLM generation."""

    def chat(self, *, chat_ctx: ChatContext, **_kw: object) -> _Stream:
        return _Stream(llm=self, chat_ctx=chat_ctx)


class _Stream(llm.LLMStream):
    def __init__(self, *, llm: llm.LLM, chat_ctx: ChatContext) -> None:
        super().__init__(llm=llm, chat_ctx=chat_ctx, tools=[], conn_options=_CONN)

    async def _run(self) -> None:
        self._event_ch.send_nowait(ChatChunk(id="done", delta=ChoiceDelta(content="")))

    @property
    def function_calls(self) -> list[llm.FunctionCall]:
        return []

    @property
    def is_function_call(self) -> bool:
        return False
