"""Safe JSON parsing utilities for LLM responses."""

from __future__ import annotations

import json
import re
from typing import Any

from json_repair import repair_json
from loguru import logger


def safe_parse_llm_json(raw: str) -> dict[str, Any]:
    """Parse JSON from an LLM response with multiple fallback strategies.

    Args:
        raw: Raw string response from the LLM.

    Returns:
        Parsed dict.

    Raises:
        ValueError: If all parsing strategies fail.
    """
    # Strategy 1: Direct parsing
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: Extract from markdown code fences ```json ... ``` or ``` ... ```
    try:
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if match:
            return json.loads(match.group(1).strip())
    except (json.JSONDecodeError, AttributeError):
        pass

    # Strategy 3: Repair malformed JSON
    try:
        repaired = repair_json(raw)
        return json.loads(repaired)
    except Exception as e:
        logger.error(f"[json] All parse strategies failed: {e}")
        logger.debug(f"[json] Raw input (first 500): {raw[:500]}")
        raise ValueError(
            f"Failed to parse LLM JSON response: {e}. "
            f"Input preview: {raw[:200]}"
        )
