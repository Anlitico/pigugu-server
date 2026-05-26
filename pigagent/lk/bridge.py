# pigagent/lk/bridge.py
"""PigAgentVoiceBridge — minimal Agent subclass for the LiveKit voice pipeline.

Extends livekit.agents.Agent to inherit default stt_node/llm_node/tts_node behavior.
Session's PigAgentLLM handles LLM generation via the default pipeline.
"""

from __future__ import annotations

from livekit.agents import Agent
from livekit.agents.llm import ChatContext, ChatMessage


class PigAgentVoiceBridge(Agent):
    """Minimal Agent — default pipeline nodes use session's STT/LLM/TTS."""

    def __init__(
        self,
        *,
        pig_agent,
        persona_id: int = 1,
        user_id: str = "",
    ):
        super().__init__(
            instructions="",
            chat_ctx=ChatContext.empty(),
        )
        self._pig = pig_agent
        self._persona_id = persona_id
        self._user_id = user_id

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def on_enter(self) -> None:
        pass

    async def on_exit(self) -> None:
        pass

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        pass
