# pigagent/personas/__init__.py
"""Persona registry for Pigugu AI personas."""

from loguru import logger

from .base import Persona
from .trump import TrumpPersona
from .musk import MuskPersona
from .james import JamesPersona


class PersonaRegistry:
    """Registry of all available personas, keyed by persona_id."""

    _personas: dict[int, Persona] = {}
    _initialized: bool = False

    @classmethod
    def register(cls, persona: Persona) -> None:
        """Register a persona."""
        cls._personas[persona.persona_id] = persona
        logger.info(f"Registered persona: {persona.persona_id} ({persona.display_name})")

    @classmethod
    def get(cls, persona_id: int) -> Persona:
        """Get a persona by ID. Falls back to ID 1 if not found."""
        if not cls._initialized:
            cls.register_defaults()
        persona = cls._personas.get(persona_id)
        if persona is None:
            logger.warning(
                f"Persona '{persona_id}' not found, falling back to '1'"
            )
            return cls._personas[1]
        return persona

    @classmethod
    def get_by_domain(cls, domain: str) -> Persona:
        """Return the persona mapped to a news topic domain."""
        if not cls._initialized:
            cls.register_defaults()
        for persona in cls._personas.values():
            if persona.domain == domain:
                return persona
        return cls._personas.get(1, list(cls._personas.values())[0])

    @classmethod
    def list_ids(cls) -> list[int]:
        """Return all registered persona IDs."""
        if not cls._initialized:
            cls.register_defaults()
        return list(cls._personas.keys())

    @classmethod
    def register_defaults(cls) -> None:
        """Register all built-in personas."""
        if cls._initialized:
            return
        cls.register(TrumpPersona())
        cls.register(MuskPersona())
        cls.register(JamesPersona())
        cls._initialized = True

    @classmethod
    def build_prompt_cache(cls) -> dict[int, str]:
        """Pre-build {persona_id: system_prompt} for all registered personas.

        Each entry is: global system prompt + persona-specific prompt.
        """
        if not cls._initialized:
            cls.register_defaults()
        from system_prompts.global_prompt import get_global_prompt
        global_prompt = get_global_prompt()
        return {
            pid: f"{global_prompt}\n\n{p.get_full_prompt()}"
            for pid, p in cls._personas.items()
        }


def get_persona(persona_id: int = 1) -> Persona:
    """Convenience function: get a persona by ID."""
    return PersonaRegistry.get(persona_id)


__all__ = [
    "Persona",
    "PersonaRegistry",
    "TrumpPersona",
    "MuskPersona",
    "JamesPersona",
    "get_persona",
]
