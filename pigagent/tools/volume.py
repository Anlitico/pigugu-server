"""Volume Control Tool  -  adjust audio volume via voice commands.

Supports setting absolute volume, incremental adjustments, mute and unmute.
Implementation is a mock  -  always returns success for now.
"""

from __future__ import annotations

from typing import Any

from core.agent.tool import Tool


MIN_VOLUME = 0
MAX_VOLUME = 100

# Simulated current volume (will be replaced with real state later)
_current_volume = 50
_muted = False


async def _volume_handler(args: dict) -> dict[str, Any]:
    """Execute a volume control action and return the result.

    Args:
        args: Must contain "action" key. Optional "value" key for set/increase/decrease.

    Returns:
        Dict with success, action, level, and a human-readable message.
    """
    global _current_volume, _muted
    action = args.get("action", "set")
    value = args.get("value")

    if action == "set":
        if value is None:
            return {"success": False, "message": "Volume 'set' requires a value."}
        raw = int(value)
        if raw < MIN_VOLUME:
            _current_volume = MIN_VOLUME
            _muted = False
            return {
                "success": True, "action": action,
                "level": MIN_VOLUME,
                "message": f"Volume cannot go below {MIN_VOLUME}, set to {MIN_VOLUME}.",
            }
        if raw > MAX_VOLUME:
            _current_volume = MAX_VOLUME
            _muted = False
            return {
                "success": True, "action": action,
                "level": MAX_VOLUME,
                "message": f"Volume cannot go above {MAX_VOLUME}, set to {MAX_VOLUME}.",
            }
        _current_volume = raw
        _muted = False
        return {
            "success": True, "action": action,
            "level": _current_volume,
            "message": f"Volume set to {_current_volume}.",
        }

    elif action == "increase":
        if _current_volume >= MAX_VOLUME:
            return {
                "success": False, "action": action,
                "level": _current_volume,
                "message": f"Volume is already at maximum ({MAX_VOLUME}), cannot increase.",
            }
        step = int(value) if value else 5
        target = _current_volume + step
        if target > MAX_VOLUME:
            _current_volume = MAX_VOLUME
            return {
                "success": True, "action": action,
                "level": _current_volume,
                "message": f"Volume increased to maximum ({MAX_VOLUME}).",
            }
        _current_volume = target
        _muted = False
        return {
            "success": True, "action": action,
            "level": _current_volume,
            "message": f"Volume increased by {step}, now at {_current_volume}.",
        }

    elif action == "decrease":
        if _current_volume <= MIN_VOLUME:
            return {
                "success": False, "action": action,
                "level": _current_volume,
                "message": f"Volume is already at minimum ({MIN_VOLUME}), cannot decrease.",
            }
        step = int(value) if value else 5
        target = _current_volume - step
        if target < MIN_VOLUME:
            _current_volume = MIN_VOLUME
            return {
                "success": True, "action": action,
                "level": _current_volume,
                "message": f"Volume decreased to minimum ({MIN_VOLUME}).",
            }
        _current_volume = target
        return {
            "success": True, "action": action,
            "level": _current_volume,
            "message": f"Volume decreased by {step}, now at {_current_volume}.",
        }

    elif action == "mute":
        _muted = True
        return {
            "success": True, "action": action,
            "level": MIN_VOLUME,
            "message": "Volume muted.",
        }

    elif action == "unmute":
        _muted = False
        return {
            "success": True, "action": action,
            "level": _current_volume,
            "message": f"Volume unmuted, restored to {_current_volume}.",
        }

    else:
        return {"success": False, "message": f"Unknown volume action: {action}"}


volume_tool = Tool(
    name="volume_control",
    description=(
        "Adjust the audio volume. "
        "Use 'set' to go to a specific level (0-100). "
        "Use 'increase' or 'decrease' to adjust by a step (5, 10, or 15). "
        "Use 'mute' to silence the audio, and 'unmute' to restore the previous level."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "increase", "decrease", "mute", "unmute"],
                "description": (
                    "The volume action to perform. "
                    "'set'  -  set to a specific level. "
                    "'increase'  -  raise volume by a step. "
                    "'decrease'  -  lower volume by a step. "
                    "'mute'  -  silence audio. "
                    "'unmute'  -  restore audio from mute."
                ),
            },
            "value": {
                "type": "integer",
                "description": (
                    "For 'set': target volume level (0-100). "
                    "For 'increase'/'decrease': amount to change (5, 10, or 15, default 5). "
                    "For 'mute'/'unmute': not required."
                ),
            },
        },
        "required": ["action"],
    },
    execute=_volume_handler,
)
