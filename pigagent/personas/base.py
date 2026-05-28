# pigagent/personas/base.py
"""
Persona abstract base class.

Each Persona defines: identity, system prompt, TTS voice config, filler phrases,
and greeting for one Pigugu character.
"""

import random
from abc import ABC, abstractmethod
from typing import Optional


class Persona(ABC):
    """Defines identity, voice, and behavior for one Pigugu persona.

    Subclass and set the class-level attributes, then register via PersonaRegistry.
    """

    # ── Identity (set by subclass) ────────────────────────────────────
    persona_id: int = 0            # 1-16, maps to firmware key
    display_name: str = ""
    domain: str = ""               # Mapped to news topic domain

    # ── TTS voice configuration ───────────────────────────────────────
    tts_voice: str = ""            # Cartesia voice ID
    tts_speed: Optional[float] = None
    tts_emotion: Optional[list[str]] = None

    # ── Behavior ──────────────────────────────────────────────────────
    fillers: list[str] = []        # Persona-specific latency fillers
    greeting: str = ""             # Welcome message

    # ── Abstract ──────────────────────────────────────────────────────

    @property
    @abstractmethod
    def personality_prompt(self) -> str:
        """The core personality system prompt.

        Must NOT include mood/news/mode/ending injection  -  those are layered
        on top by ContextAssembler.
        """

    # ── Concrete ──────────────────────────────────────────────────────

    def get_filler(self) -> str:
        """Random filler phrase for masking LLM latency."""
        if self.fillers:
            return random.choice(self.fillers)
        return ""

    def get_preamble(self) -> str:
        """Provider-specific preamble inserted before personality prompt."""
        return ""

    def get_suffix(self) -> str:
        """Provider-specific suffix appended after personality prompt."""
        return ""

    def get_full_prompt(self) -> str:
        """Full personality prompt (preamble + personality + suffix)."""
        parts = [self.get_preamble(), self.personality_prompt, self.get_suffix()]
        return "\n\n".join(filter(None, parts))
