# agent/context/_utils.py
"""Shared helpers for the context module."""


def serialize_tool_calls(tool_calls: list | None) -> str | None:
    """Serialize ToolCall list to JSONB string for PG insert. Returns None if empty."""
    if not tool_calls:
        return None
    import json
    return json.dumps([
        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
        for tc in tool_calls
    ])
