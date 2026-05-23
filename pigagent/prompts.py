# pigagent/prompts.py
"""System prompt registry — stateless, id-indexed.

Usage:
    from prompts import get_system_prompt, register_prompt

    register_prompt("trump", "You are Donald Trump...")
    prompt = get_system_prompt("trump")
"""

from __future__ import annotations

from personas import PersonaRegistry, get_persona

# ── Registry ──────────────────────────────────────────────────────────────

_registry: dict[str, str] = {}


def register(prompt_id: str, content: str) -> None:
    """Register a system prompt by ID."""
    _registry[prompt_id] = content


def get(prompt_id: str) -> str:
    """Get a system prompt by ID. Falls back to persona system if found."""
    if prompt_id in _registry:
        return _registry[prompt_id]

    # Fallback: try persona registry
    try:
        persona = get_persona(prompt_id)
        return persona.get_full_prompt("qwen")
    except Exception:
        pass

    return ""


def list_ids() -> list[str]:
    """List all registered prompt IDs."""
    return list(_registry.keys())
