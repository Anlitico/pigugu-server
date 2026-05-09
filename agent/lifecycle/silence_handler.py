# agent/lifecycle/silence_handler.py
"""
Silence handling per PRD spec: 3s wait → 6s gentle prompt → 10s snark → 15s exit.

Stateless per session — reset() is called when user speaks again.
"""

from enum import Enum
from typing import Optional


class SilenceAction(Enum):
    WAIT = "wait"
    GENTLE_PROMPT = "gentle_prompt"
    SNARKY_COMMENT = "snarky_comment"
    EXIT = "exit"


# Stage config: (elapsed_seconds, action)
SILENCE_STAGES = [
    (3.0, SilenceAction.WAIT),
    (6.0, SilenceAction.GENTLE_PROMPT),
    (10.0, SilenceAction.SNARKY_COMMENT),
    (15.0, SilenceAction.EXIT),
]

# Persona-specific messages for each action stage
DEFAULT_MESSAGES: dict[SilenceAction, str] = {
    SilenceAction.GENTLE_PROMPT: "I'm here. No rush.",
    SilenceAction.SNARKY_COMMENT: "Silence is also a stance. I just don't know how to score it.",
    SilenceAction.EXIT: "Alright, not feeling like talking today. That's fine. See you tomorrow.",
}


class SilenceHandler:
    """Implements PRD-specified silence stages.

    Usage:
        handler = SilenceHandler()
        # On every silence check:
        msg = handler.check(elapsed_seconds, persona)
        if msg:
            agent.say(msg)
        # When user speaks:
        handler.reset()
    """

    def __init__(self):
        self._stage_index: int = 0
        self._last_action: Optional[SilenceAction] = None

    def check(
        self,
        elapsed: float,
        persona=None,
    ) -> Optional[str]:
        """Check whether a silence action should fire.

        Returns a message the agent should speak, or None if no action needed.
        Each stage fires only once per silence period.
        """
        new_stage_index = 0
        for i, (threshold, _action) in enumerate(SILENCE_STAGES):
            if elapsed >= threshold:
                new_stage_index = i

        if new_stage_index <= self._stage_index:
            return None

        # Advance to the highest triggered stage
        self._stage_index = new_stage_index
        _, action = SILENCE_STAGES[new_stage_index]

        if action == SilenceAction.WAIT:
            return None

        return self._get_message(action, persona)

    def _get_message(self, action: SilenceAction, persona=None) -> str:
        """Get persona-appropriate message for a silence action."""
        # Try persona-specific messages first
        if persona is not None:
            persona_msgs = getattr(persona, "silence_messages", None)
            if persona_msgs and action in persona_msgs:
                return persona_msgs[action]

        return DEFAULT_MESSAGES.get(action, "")

    def reset(self) -> None:
        """Reset state when user speaks."""
        self._stage_index = 0
        self._last_action = None

    @property
    def should_exit(self) -> bool:
        """Whether the EXIT stage has been reached."""
        return self._stage_index >= len(SILENCE_STAGES) - 1
