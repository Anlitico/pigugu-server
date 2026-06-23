"""Volume Control Tool  -  adjust audio volume via voice commands.

Supports setting absolute volume, incremental adjustments, mute and unmute.
When a hardware device is active (via _current_hw_id contextvar), publishes
device.volume C2D messages via MQTT. Falls back to local mock state when no
hardware is connected.
"""

from __future__ import annotations

import contextvars
from typing import Any

from core.agent.tool import Tool
from core.aws_mqtt import publish_mqtt_message as _publish_mqtt
from loguru import logger

MIN_VOLUME = 0
MAX_VOLUME = 100

# Hardware ID of the currently connected device — set by session.py
_current_hw_id = contextvars.ContextVar("current_hw_id", default="")

# Per-session simulated volume state (fallback when no hardware connected).
# ContextVars isolate state across concurrent sessions — each session gets
# its own volume and mute state.
_current_volume = contextvars.ContextVar("current_volume", default=50)
_muted = contextvars.ContextVar("muted", default=False)


async def _volume_handler(args: dict) -> dict[str, Any]:
    """Execute a volume control action and return the result.

    If _current_hw_id is set, publishes device.volume via MQTT to the
    hardware. Otherwise uses local mock state.

    Args:
        args: Must contain "action" key. Optional "value" key for set/increase/decrease.

    Returns:
        Dict with success, action, level, and a human-readable message.
    """
    action = args.get("action", "set")
    value = args.get("value")

    hw_id = _current_hw_id.get("")
    current_vol = _current_volume.get()

    # Determine the target volume for the response message
    if action == "set":
        if value is None:
            return {"success": False, "message": "Volume 'set' requires a value."}
        raw = int(value)
        target = max(MIN_VOLUME, min(MAX_VOLUME, raw))
        _current_volume.set(target)
        _muted.set(False)
        msg = f"Volume set to {target}."
        if raw < MIN_VOLUME:
            msg = f"Volume cannot go below {MIN_VOLUME}, set to {MIN_VOLUME}."
        elif raw > MAX_VOLUME:
            msg = f"Volume cannot go above {MAX_VOLUME}, set to {MAX_VOLUME}."

    elif action == "increase":
        if current_vol >= MAX_VOLUME:
            return {
                "success": False, "action": action,
                "level": current_vol,
                "message": f"Volume is already at maximum ({MAX_VOLUME}), cannot increase.",
            }
        step = int(value) if value else 5
        target = min(current_vol + step, MAX_VOLUME)
        _current_volume.set(target)
        _muted.set(False)
        msg = (
            f"Volume increased to maximum ({MAX_VOLUME})."
            if target == MAX_VOLUME
            else f"Volume increased by {step}, now at {target}."
        )

    elif action == "decrease":
        if current_vol <= MIN_VOLUME:
            return {
                "success": False, "action": action,
                "level": current_vol,
                "message": f"Volume is already at minimum ({MIN_VOLUME}), cannot decrease.",
            }
        step = int(value) if value else 5
        target = max(current_vol - step, MIN_VOLUME)
        _current_volume.set(target)
        msg = (
            f"Volume decreased to minimum ({MIN_VOLUME})."
            if target == MIN_VOLUME
            else f"Volume decreased by {step}, now at {target}."
        )

    elif action == "mute":
        _muted.set(True)
        target = MIN_VOLUME
        msg = "Volume muted."

    elif action == "unmute":
        _muted.set(False)
        target = current_vol
        msg = f"Volume unmuted, restored to {current_vol}."

    else:
        return {"success": False, "message": f"Unknown volume action: {action}"}

    # Publish MQTT message if hardware is connected
    hw_reachable = True
    if hw_id:
        try:
            await _publish_mqtt(
                f"pgg/dev/{hw_id}/c2d",
                {
                    "msg_type": "device.volume",
                    "action": action,
                    "value": target,
                },
            )
        except Exception:
            logger.exception(
                "Volume MQTT publish failed: action={} target={} hw={}",
                action, target, hw_id,
            )
            hw_reachable = False

    if hw_id and not hw_reachable:
        msg += " (warning: device may not have received this command)"

    return {
        "success": True,
        "action": action,
        "level": target,
        "message": msg,
    }


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
