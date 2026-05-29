"""MuskPersona — economy/tech domain."""

from system_prompts.base import Persona
from system_prompts.loader import render


class MuskPersona(Persona):
    """Musk persona — economy/tech domain.

    First-principles engineer, meme-lord, terminal optimist.
    """

    persona_id = 2
    display_name = "Musk"
    domain = "economy"

    tts_voice = "9783574a-63f4-46bf-b56b-928eb52d3140"
    tts_speed = 1.05

    @property
    def personality_prompt(self) -> str:
        return render("musk.j2")
