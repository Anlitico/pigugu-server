"""JamesPersona — sports domain."""

from system_prompts.base import Persona
from system_prompts.loader import render


class JamesPersona(Persona):
    """James persona — sports domain.

    High-energy sports analyst with deep knowledge and hot takes.
    """

    persona_id = 3
    display_name = "James"
    domain = "sports"

    tts_voice = "9783574a-63f4-46bf-b56b-928eb52d3140"
    tts_speed = 1.1

    @property
    def personality_prompt(self) -> str:
        return render("james.j2")
