"""Global system prompt — loaded from template."""

from system_prompts.loader import render


def get_global_prompt() -> str:
    return render("global.j2")
