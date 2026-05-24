"""Utilities for converting LiveKit chat context items to dict messages.

Used by main.py's llm_node to bridge LiveKit's native chat items
into the standard dict format before wrapping in core Message objects.
"""

from __future__ import annotations


def _normalize_role(role: str) -> str:
    normalized = (role or "").lower()
    if normalized == "developer":
        return "system"
    if normalized in {"system", "user", "assistant", "tool"}:
        return normalized
    return "user"


def build_search_messages(items) -> list[dict[str, str]]:
    """Convert LiveKit ChatContext items to a list of {role, content} dicts.

    Deduplicates identical system messages and ensures the first system
    message is at index 0.
    """
    messages: list[dict[str, str]] = []

    for item in items:
        content = getattr(item, "text_content", None)
        if not content:
            continue
        role = _normalize_role(str(getattr(item, "role", "user")))
        messages.append({"role": role, "content": content})

    # Keep one copy of each exact system message.
    seen_system = set()
    deduped: list[dict[str, str]] = []
    for msg in messages:
        if msg["role"] == "system":
            key = msg["content"].strip()
            if key in seen_system:
                continue
            seen_system.add(key)
        deduped.append(msg)

    # Ensure the first system message is at index 0 when present.
    first_system_idx = next(
        (i for i, msg in enumerate(deduped) if msg["role"] == "system"), None
    )
    if first_system_idx not in (None, 0):
        first_system = deduped.pop(first_system_idx)
        deduped.insert(0, first_system)

    return deduped
