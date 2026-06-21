# pigagent/lk/bridge.py
"""PigAgentVoiceBridge — Agent subclass that directly delegates LLM to PigAgent.

Overrides llm_node() to bypass LiveKit's LLM wrapper entirely.
STT and TTS are handled by the default pipeline nodes via AgentSession.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger
from livekit.agents import Agent
from livekit.agents.types import FlushSentinel
from livekit.agents.voice.agent import ModelSettings
from livekit.agents.llm import ChatContext
from metrics.turn import TelemetryCollector


class PigAgentVoiceBridge(Agent):
    """Agent subclass — default stt_node/tts_node, custom llm_node."""

    def __init__(
        self,
        *,
        pig_agent: Any,
        persona_id: int = 1,
        session_id: str | None = None,
    ) -> None:
        super().__init__(
            instructions="",
            chat_ctx=ChatContext.empty(),
        )
        self._pig = pig_agent
        self._persona_id = persona_id
        self._session_id = session_id
        self.current_interrupt_event: asyncio.Event | None = None

    # ── LLM node ──────────────────────────────────────────────────────

    async def llm_node(
        self,
        chat_ctx: ChatContext,
        tools: list[Any],
        model_settings: ModelSettings,
    ) -> AsyncIterator[str | FlushSentinel]:
        if self._pig is None:
            logger.warning("[BRIDGE] PigAgent not yet wired — skipping LLM call")
            return
        user_text = self._extract_user_text(chat_ctx)
        logger.info(f"[BRIDGE] llm_node: user_text='{user_text[:120]}' persona_id={self._persona_id}")
        if not user_text.strip():
            logger.warning("[BRIDGE] Empty user text — nothing to send to LLM")
            return
        # Create a fresh event right before the LLM call — session triggers it on VAD
        self.current_interrupt_event = asyncio.Event()
        first = True
        async for text in self._pig.generate_reply(
            user_text,
            persona_id=self._persona_id,
            interrupt_event=self.current_interrupt_event,
            session_id=self._session_id,
        ):
            if first:
                TelemetryCollector.mark("llm_ttft")
                first = False
            yield text

    @staticmethod
    def _extract_user_text(chat_ctx: ChatContext) -> str:
        for item in reversed(chat_ctx.items):
            role = getattr(item, "role", "")
            if role == "user":
                text: str | None = getattr(item, "text_content", None)
                if text:
                    return text
        return ""
