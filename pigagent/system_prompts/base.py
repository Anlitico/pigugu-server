"""Persona abstract base class.

Each Persona defines: identity, system prompt template, TTS voice config,
and greeting for one Pigugu character.
"""

from abc import ABC, abstractmethod
from typing import Optional


class Persona(ABC):
    """Defines identity, voice, and behavior for one Pigugu persona.

    Subclass and set the class-level attributes, then register via PersonaRegistry.
    """

    # ── Identity (set by subclass) ────────────────────────────────────
    persona_id: int = 0
    display_name: str = ""
    domain: str = ""

    # ── TTS voice configuration ───────────────────────────────────────
    tts_voice: str = ""
    tts_speed: Optional[float] = None
    tts_emotion: Optional[list[str]] = None

    # ── Abstract ──────────────────────────────────────────────────────

    @property
    @abstractmethod
    def personality_prompt(self) -> str:
        """The core personality system prompt, rendered from template."""

    # ── Concrete ──────────────────────────────────────────────────────

    def get_full_prompt(self) -> str:
        """Full personality prompt."""
        return self.personality_prompt
