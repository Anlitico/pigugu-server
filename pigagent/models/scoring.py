# pigagent/models/scoring.py
"""Scoring and context data models."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MoodState:
    """Pigugu's current emotional state."""

    excitement: float = 0.5  # 0.0 - 1.0
    sarcasm: float = 0.5
    anger: float = 0.2
    label: str = "default"  # "麻木" | "失控" | "躺平" | "燃烧"

    def render(self) -> str:
        labels = {
            "default": "😒 Default — dry sarcasm, low-energy roasting",
            "chaos": "🤯 Chaos — wild, exaggerated takes, everything is crazy",
            "lethargy": "😴 Lethargy — existential nihilism, absurdism",
            "burning": "🔥 Burning — maximum snark, no filter, going for the jugular",
        }
        label_desc = labels.get(self.label, labels["default"])
        return (
            f"## CURRENT MOOD\n"
            f"Pigugu is feeling: {label_desc}\n"
            f"Mood stats: excitement={self.excitement:.1f}, sarcasm={self.sarcasm:.1f}, anger={self.anger:.1f}\n"
        )


@dataclass
class NewsContext:
    """News item context for the current conversation."""

    news_id: str = ""
    title: str = ""
    summary: str = ""
    source: str = ""
    domain: str = ""
    mode: str = "roast"  # Game mode assigned to this news
    persona: str = "trump"  # Persona assigned to this news
    raw_data: dict = field(default_factory=dict)

    def render(self) -> str:
        if not self.title:
            return ""
        return (
            f"## NEWS CONTEXT\n"
            f"Topic: {self.title}\n"
            f"Summary: {self.summary}\n"
            f"Source: {self.source}\n"
        )


@dataclass
class ScoreResult:
    """Post-conversation scoring result."""

    credibility: float = 0.0
    roast_points: int = 0
    mood_delta: dict = field(default_factory=dict)
    mode_scores: dict = field(default_factory=dict)
    achievement_ids: list[str] = field(default_factory=list)
