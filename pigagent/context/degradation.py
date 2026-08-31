"""Degradation guard — detect assistant self-repetition and break the loop.

Low-cost and deterministic: normalizes the assistant's past replies and checks
whether the most recent ones form a repetition run (string similarity). When it
fires, a corrective system message is appended to the turn's context so the LLM
answers the user freshly instead of echoing itself.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Sequence

from core.llm.types import Message
from roast.constants import FREE_CHAT_EVENT_PREFIX

# Injected when the assistant repeats itself across consecutive turns. The
# prefix mirrors the director's "[Game Event]" convention — a bracketed tag
# the model recognizes as an in-conversation direct instruction.
DEGRADATION_CORRECTION = (
    "You repeated yourself in your last few replies. Break the loop: answer "
    "the user's latest message directly and freshly, in your voice. Do not "
    "repeat or rephrase your earlier replies."
)

_PUNCT = re.compile(r"[\W_]+", re.UNICODE)


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation/whitespace — for similarity comparison."""
    return _PUNCT.sub(" ", text or "").lower().strip()


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def detect_degradation(
    messages: Sequence[Message],
    *,
    threshold: float = 0.8,
    min_consecutive: int = 3,
) -> bool:
    """True if the most recent assistant replies form a repetition run.

    ``min_consecutive`` = number of consecutive assistant replies that must be
    mutually similar (>=2). Short histories never fire. Assistant replies
    marked ``partial`` (interrupted) are not counted. When the USER is repeating
    their own message, the assistant's matching answer is legitimate and the
    guard stays quiet.
    """
    texts = [
        m.content
        for m in messages
        if m.role == "assistant" and not m.partial and m.content
    ]
    if len(texts) < min_consecutive:
        return False
    # Normalize only the tail window we actually compare — keeps this O(window)
    # on the event loop even with a long history.
    recent = [_normalize(t) for t in texts[-min_consecutive:]]
    for i in range(len(recent) - 1):
        if _similar(recent[i], recent[i + 1]) < threshold:
            return False
    user_texts = [m.content for m in messages if m.role == "user" and m.content]
    user_recent = [_normalize(t) for t in user_texts[-min_consecutive:]]
    if len(user_recent) >= 2 and all(
        _similar(user_recent[i], user_recent[i + 1]) >= threshold
        for i in range(len(user_recent) - 1)
    ):
        return False
    return True


def apply_degradation_guard(messages: list[Message], **kwargs) -> bool:
    """Append the corrective system message if degradation is detected.

    Mutates ``messages`` in place. Returns True if the guard fired.
    """
    if detect_degradation(messages, **kwargs):
        messages.append(Message.system(f"{FREE_CHAT_EVENT_PREFIX}\n{DEGRADATION_CORRECTION}"))
        return True
    return False
