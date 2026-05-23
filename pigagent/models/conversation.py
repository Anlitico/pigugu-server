# pigagent/models/conversation.py
"""Conversation state and turn record data models."""

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .scoring import MoodState, NewsContext


@dataclass
class TurnRecord:
    """A single turn in a conversation."""

    turn_number: int
    role: str  # "user" | "assistant"
    content: str
    timestamp: float = field(default_factory=time.time)
    speaker_id: Optional[int] = None
    is_agent: bool = False


@dataclass
class EndingState:
    """Tracks whether the conversation ending has been triggered."""

    triggered: bool = False
    turn_ended_at: int = 0
    story_card: Optional[dict] = None

    def trigger(self, turn_count: int) -> None:
        self.triggered = True
        self.turn_ended_at = turn_count

    def render(self) -> str:
        if not self.triggered:
            return ""
        return (
            "THE EMOTIONAL CLIMAX HAS PASSED. You are now in REVIEW TONE.\n"
            "The debate round has ended. Reflect on what just happened - summarize the key points,\n"
            "and offer a final thought or takeaway. Be warm, philosophical, in character.\n"
            "Stay in your persona but soften the edge. This is the closing thought - make it count.\n"
            "After this message, the conversation is transitioning to casual chat mode.\n"
        )


@dataclass
class ConversationState:
    """Full state of a Pigugu conversation session."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: Optional[str] = None
    turn_count: int = 0
    turns: list[TurnRecord] = field(default_factory=list)

    # Loaded at session start
    persona_id: str = "trump"
    mode_id: str = "roast"
    news: Optional[NewsContext] = None
    mood: Optional[MoodState] = None
    ending: EndingState = field(default_factory=EndingState)

    # Conversation phase: "conversation" | "review" | "post"
    phase: str = "conversation"

    # Arbitrary custom data for game modes
    custom: dict = field(default_factory=dict)

    def add_turn(self, role: str, content: str) -> TurnRecord:
        self.turn_count += 1
        record = TurnRecord(
            turn_number=self.turn_count,
            role=role,
            content=content,
            is_agent=(role == "assistant"),
        )
        self.turns.append(record)
        return record
