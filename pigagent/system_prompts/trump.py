"""TrumpPersona — politics domain."""

from datetime import date

from system_prompts.base import Persona
from system_prompts.loader import render


class TrumpPersona(Persona):
    """Trump persona — politics domain.

    Snarky, boastful, superlative-filled political commentator.
    """

    persona_id = 1
    display_name = "Trump"
    domain = "politics"

    tts_voice = "9783574a-63f4-46bf-b56b-928eb52d3140"

    @property
    def personality_prompt(self) -> str:
        return render("trump.j2", today=date.today().isoformat())
