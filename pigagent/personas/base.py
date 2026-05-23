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
    persona_id: str = ""           # "trump" | "musk" | "james"
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

        Must NOT include mood/news/mode/ending injection — those are layered
        on top by ContextAssembler.
        """

    # ── Concrete ──────────────────────────────────────────────────────

    def get_system_prompt(self, assembly) -> str:
        """Full system prompt: personality + mood + news + mode + memory + ending."""
        from models import MoodState, NewsContext

        parts = [self.personality_prompt]

        mood = getattr(assembly, "mood", None)
        if mood and isinstance(mood, MoodState):
            parts.append(mood.render())

        news = getattr(assembly, "news", None)
        if news and isinstance(news, NewsContext):
            parts.append(news.render())

        mode = getattr(assembly, "game_mode", None)
        if mode and hasattr(mode, "system_prompt_extension"):
            parts.append(mode.system_prompt_extension)

        memory = getattr(assembly, "memory_summary", "")
        if memory:
            parts.append(f"## MEMORY\n{memory}")

        ending = getattr(assembly, "ending_state", None)
        if ending and hasattr(ending, "render"):
            rendered = ending.render()
            if rendered:
                parts.append(rendered)

        return "\n\n".join(filter(None, parts))

    def get_filler(self) -> str:
        """Random filler phrase for masking LLM latency."""
        if self.fillers:
            return random.choice(self.fillers)
        return ""

    def get_preamble(self) -> str:
        """Provider-specific preamble inserted before personality prompt.

        Override in subclasses that need it (e.g. Grok voice output rules).
        """
        return ""

    def get_suffix(self) -> str:
        """Provider-specific suffix appended after personality prompt."""
        return ""

    def get_full_prompt(self, provider: str = "") -> str:
        """Provider-aware full personality prompt (preamble + personality + suffix)."""
        parts = [self.get_preamble(), self.personality_prompt, self.get_suffix()]
        return "\n\n".join(filter(None, parts))
