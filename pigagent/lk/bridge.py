# pigagent/lk/bridge.py
"""PigAgentVoiceBridge — minimal duck-type bridge to LiveKit AgentSession.

Does NOT inherit from livekit.agents.Agent. Just satisfies the attribute
interface that AgentActivity accesses at runtime. All LLM/content logic
lives in PigAgent; LiveKit only handles STT → VAD → TTS audio pipeline.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.voice.agent import ModelSettings
from livekit.agents.llm import ChatContext, ChatMessage

from core.llm.types import Message
from core.agent.runner import AgentRunner, RunnerConfig
from core.agent.stop import no_tool_calls
from tools.search.utils import build_search_messages


class PigAgentVoiceBridge:
    """Bridges PigAgent into LiveKit's AgentSession pipeline.

    Satisfies the duck-type interface that AgentActivity accesses:
    - id, label, instructions, tools, chat_ctx
    - llm_node() — THE core method, delegates to PigAgent
    - on_user_turn_completed() — filler words, search flag
    - stt, tts, vad — passed through to LiveKit pipeline
    """

    def __init__(
        self,
        *,
        pig_agent,
        persona_id: str = "",
        stt=None,
        tts=None,
        vad=None,
        enable_filler_words: bool = False,
        fillers: list[str] | None = None,
        enable_policy_search: bool = False,
        allow_interruptions: bool = True,
    ):
        self._pig = pig_agent
        self._persona_id = persona_id

        # ── LiveKit pipeline plugins ──────────────────────────────────
        self.stt = stt
        self.tts = tts
        self.vad = vad
        self.llm = NOT_GIVEN  # llm_node() handles LLM, not a plugin

        # ── AgentSession-required identity ────────────────────────────
        self.id = "pigugu_agent"
        self.label = "Pigugu Voice Agent"  # type: ignore[reportCallIssue]

        # ── LiveKit Agent abstractions (all empty — PigAgent owns these) ──
        self.instructions: str = ""  # LiveKit's Instructions type not importable
        self.tools: list[Any] = []
        self.chat_ctx: ChatContext = ChatContext.empty()

        # ── Internal state (mutated by AgentActivity) ─────────────────
        self._activity: Any = None
        self._turn_handling: dict = {}

        # ── Turn detection / interruption config ─────────────────────
        self._allow_interruptions = allow_interruptions

        # ── PigAgent feature flags ───────────────────────────────────
        self._enable_filler_words = enable_filler_words
        self._fillers = fillers or []
        self._enable_policy_search = enable_policy_search

        # ── Per-turn state ───────────────────────────────────────────
        self._pending_filler: str | None = None
        self._filler_yielded_at: float | None = None
        self._use_search: bool = False

    # ── Properties AgentActivity accesses ───────────────────────────────

    @property
    def label(self) -> str:
        return "Pigugu Voice Agent"

    @label.setter
    def label(self, value: str) -> None:
        pass

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

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def on_exit(self) -> None:
        pass

    # ── User turn hook ──────────────────────────────────────────────────

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        """Called by AgentActivity when STT detects end of user turn."""
        user_text = (new_message.text_content or "").strip()

        # Filler word injection
        if self._enable_filler_words and self._fillers and len(user_text.split()) > 5:
            filler = random.choice(self._fillers)
            self._pending_filler = filler
            turn_ctx.add_message(
                role="system",
                content=f'You already began your reply with: "{filler}". '
                        f"Continue from there. Do NOT repeat it.",
            )
            logger.info(f'[Bridge] Filler queued: "{filler}"')

        # Policy search flag
        self._use_search = self._enable_policy_search

    # ── LLM node (THE core method) ──────────────────────────────────────

    async def llm_node(
        self,
        chat_ctx: ChatContext,
        tools: list[Any],
        model_settings: ModelSettings,
    ) -> AsyncIterator[str]:
        """Generate reply via PigAgent. LiveKit passes output to TTS."""
        filler = self._pending_filler
        self._pending_filler = None
        use_search = self._use_search
        self._use_search = False

        # Convert LiveKit ChatContext → PigAgent Message list
        dict_msgs = build_search_messages(chat_ctx.items)
        messages = [Message(role=m["role"], content=m["content"]) for m in dict_msgs]  # type: ignore[reportArgumentType]

        search_param = {"enabled": True} if use_search else None

        async def _stream():
            async for text in self._pig.stream(
                messages, persona_id=self._persona_id, search=search_param,
            ):
                yield text

        if filler:
            self._filler_yielded_at = time.perf_counter()
            yield filler + " "
            queue: asyncio.Queue[str | None] = asyncio.Queue()

            async def _buffer():
                try:
                    async for chunk in _stream():
                        await queue.put(chunk)
                except Exception as e:
                    logger.error(f"[Bridge] LLM stream failed: {e}")
                finally:
                    await queue.put(None)

            asyncio.create_task(_buffer())

            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        else:
            async for text in _stream():
                yield text
