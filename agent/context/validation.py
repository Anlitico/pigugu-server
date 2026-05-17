# agent/context/validation.py
"""Tool call completeness validation and token estimation fallback."""


def _len_fallback(text: str) -> int:
    """Character-length fallback when no provider token counter is available."""
    return len(text) if text else 0


def validate_tool_calls(messages: list) -> list:
    """Filter out incomplete tool calls before sending to LLM.

    LangGraph-style: every assistant tool_call must have a matching tool
    response with the same tool_call_id. Dangling calls (no response yet)
    are removed to avoid LLM API errors.
    """
    if not messages:
        return messages

    from core.llm.types import Message

    responded_ids: set[str] = set()
    for m in messages:
        if m.role == "tool" and m.tool_call_id:
            responded_ids.add(m.tool_call_id)

    cleaned: list[Message] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            valid_calls = [
                tc for tc in m.tool_calls
                if tc.id in responded_ids
            ]
            if valid_calls:
                cleaned.append(Message(
                    role="assistant",
                    content=m.content,
                    tool_calls=valid_calls,
                    partial=m.partial,
                ))
            elif m.content:
                cleaned.append(Message(
                    role="assistant",
                    content=m.content,
                    partial=m.partial,
                ))
        else:
            cleaned.append(m)

    return cleaned
