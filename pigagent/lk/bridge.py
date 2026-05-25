# pigagent/lk/bridge.py
"""PigAgentVoiceBridge — pure LiveKit adaptation layer, zero business logic.

Converts LiveKit ChatContext → user text, delegates to PigAgent.generate_reply(),
yields text chunks to LiveKit's TTS pipeline. No filler, no search, no roast
routing — PigAgent owns all of that.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from loguru import logger
from livekit.agents.types import NOT_GIVEN
from livekit.agents.voice.agent import ModelSettings
from livekit.agents.llm import ChatContext, ChatMessage


class PigAgentVoiceBridge:
    """Pure adapter: satisfies AgentSession's duck-type interface."""

    def __init__(
        self,
        *,
        pig_agent,
        persona_id: str = "",
        user_id: str = "",
        stt=None,
        tts=None,
        vad=None,
        allow_interruptions: bool = True,
    ):
        self._pig = pig_agent
        self._persona_id = persona_id
        self._user_id = user_id

        # ── LiveKit pipeline plugins ──────────────────────────────────
        self.stt = stt
        self.tts = tts
        self.vad = vad
        self.llm = NOT_GIVEN  # llm_node() handles LLM

        # ── AgentSession-required identity ────────────────────────────
        self.id = "pigugu_agent"
        self.label = "Pigugu Voice Agent"  # type: ignore[reportCallIssue]

        # ── LiveKit Agent abstractions (unused, PigAgent owns all) ────
        self.instructions: str = ""
        self.tools: list[Any] = []
        self.chat_ctx: ChatContext = ChatContext.empty()

        # ── Internal state (mutated by AgentActivity) ─────────────────
        self._activity: Any = None
        self._turn_handling: dict = {}
        self._allow_interruptions = allow_interruptions

    # ── Properties AgentActivity accesses ─────────────────────────────

    label = "Pigugu Voice Agent"  # type: ignore[reportCallIssue]

    @property
    def allow_interruptions(self):
        return self._allow_interruptions

    @property
    def turn_detection(self):
        return NOT_GIVEN

    @property
    def mcp_servers(self):
        return NOT_GIVEN

    @property
    def min_consecutive_speech_delay(self):
        return NOT_GIVEN

    @property
    def use_tts_aligned_transcript(self):
        return NOT_GIVEN

    @property
    def stt_node(self):
        return None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def on_exit(self) -> None:
        pass

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        pass

    # ── LLM node ──────────────────────────────────────────────────────

    async def llm_node(
        self,
        chat_ctx: ChatContext,
        tools: list[Any],
        model_settings: ModelSettings,
    ) -> AsyncIterator[str]:
        """Extract user text, delegate to PigAgent, yield for TTS."""
        user_text = self._extract_user_text(chat_ctx)
        async for text in self._pig.generate_reply(
            user_text, user_id=self._user_id, persona_id=self._persona_id,
        ):
            yield text

    def _extract_user_text(self, chat_ctx: ChatContext) -> str:
        """Get the last user message from the chat context."""
        for item in reversed(chat_ctx.items):
            role = getattr(item, "role", "")
            if role == "user":
                text = getattr(item, "text_content", None)
                if text:
                    return text
        return ""
