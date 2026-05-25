# pigagent/personas/__init__.py
"""Persona registry for Pigugu AI personas."""

from loguru import logger

from .base import Persona
from .trump import TrumpPersona
from .musk import MuskPersona
from .james import JamesPersona


class PersonaRegistry:
    """Registry of all available personas, keyed by persona_id."""

    _personas: dict[str, Persona] = {}
    _initialized: bool = False

    @classmethod
    def register(cls, persona: Persona) -> None:
        """Register a persona."""
        cls._personas[persona.persona_id] = persona
        logger.info(f"✅ Registered persona: {persona.persona_id} ({persona.display_name})")

    @classmethod
    def get(cls, persona_id: str) -> Persona:
        """Get a persona by ID. Falls back to 'trump' if not found."""
        if not cls._initialized:
            cls.register_defaults()
        persona = cls._personas.get(persona_id)
        if persona is None:
            logger.warning(
                f"Persona '{persona_id}' not found, falling back to 'trump'"
            )
            return cls._personas["trump"]
        return persona

    @classmethod
    def get_by_domain(cls, domain: str) -> Persona:
        """Return the persona mapped to a news topic domain."""
        if not cls._initialized:
            cls.register_defaults()
        for persona in cls._personas.values():
            if persona.domain == domain:
                return persona
        return cls._personas.get("trump", list(cls._personas.values())[0])

    @classmethod
    def list_ids(cls) -> list[str]:
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
    def build_prompt_cache(cls, provider_id: str) -> dict[str, str]:
        """Pre-build {persona_id: system_prompt} for all registered personas."""
        if not cls._initialized:
            cls.register_defaults()
        return {pid: p.get_full_prompt(provider_id) for pid, p in cls._personas.items()}


def get_persona(persona_id: str = "trump") -> Persona:
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
