"""Volume control tool — adjusts the device's speaker volume by voice.

The device is the authority on its own volume. This tool asks it what the
current level is (over the session's MCP channel), computes the target for
relative actions from that real value, then sets it and reports the level the
device echoes back. It never reports success the device did not confirm — the
earlier implementation published an MQTT command and returned success
regardless, so the assistant would announce a change that never happened.

The one deliberate exception is a write that lands on zero. This device has no
screen and no LED, so the assistant's spoken confirmation is the only feedback
the user gets — and going silent takes that down with it. Those writes are
handed to the bridge to dispatch once the reply has played (see
``PiguguMcpBridge.defer``); the tool reports the target it has committed to.
"""

from __future__ import annotations

import contextvars
from typing import Any

from loguru import logger

from core.agent.tool import Tool

MIN_VOLUME = 0
MAX_VOLUME = 100
DEFAULT_STEP = 5
# Level unmute restores to. The device has no notion of "muted" and the server
# keeps no memory of the level we muted from — a contextvar cannot outlive the
# turn it was set in, and "mute now, unmute later" crosses turns. So unmute
# brings the audio back at the device's own power-on default, and says which
# level it used.
UNMUTE_LEVEL = 70

NO_DEVICE = "No device is connected to this session, so the volume cannot be changed."
DEVICE_SILENT = "The device did not respond, so the volume was not changed."

# The session's MCP channel to the device, set per turn by the agent. None
# outside a voice session (e.g. the text-only roast path), where there is no
# device to control. ContextVars isolate concurrent sessions in one process.
_current_mcp = contextvars.ContextVar("current_mcp", default=None)


def _clamp(value: int) -> int:
    return max(MIN_VOLUME, min(MAX_VOLUME, value))


def _step(value: Any) -> int:
    """A relative step is always a positive amount.

    The schema describes 5/10/15, but the model can send anything: a negative
    step would turn "increase" into a decrease and push the target out of
    0-100, which the device's own tool property rejects — so the user just
    hears a refusal for a command they never gave.
    """
    if not value:
        return DEFAULT_STEP
    return max(1, min(int(value), MAX_VOLUME))


def _failed(action: Any, message: str, level: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"success": False, "action": action, "message": message}
    if level is not None:
        result["level"] = level
    return result


async def _read_device_volume(bridge) -> int:
    """The level the device actually holds right now."""
    status = await bridge.call_tool("self.get_device_status", {})
    return int(status["audio_speaker"]["volume"])


async def _apply(bridge, target: int) -> int:
    """Set the level and return what the device says it now holds."""
    result = await bridge.call_tool("self.audio_speaker.set_volume", {"volume": target})
    level = result.get("volume") if isinstance(result, dict) else None
    if isinstance(level, int) and not isinstance(level, bool):
        return level
    # Firmware from before the level-echo change answers with a bare success and
    # no level. The write still landed, so read it back rather than reporting a
    # failure the device never had — otherwise a fleet mid-rollout hears "the
    # volume was not changed" while the volume audibly changes.
    logger.warning("[volume] set_volume returned no level ({!r}), reading it back", result)
    # Imported here rather than at module scope: the voice stack is heavy, and
    # this path is only reached on a device that predates the level echo.
    from voice.pipecat.mcp_bridge import McpToolError, McpToolTimeout

    try:
        return await _read_device_volume(bridge)
    except (McpToolError, McpToolTimeout, KeyError, TypeError, ValueError) as exc:
        # The write landed but the level is unreadable. Report the level that was
        # asked for; claiming the change failed would be the same false negative
        # this fallback exists to remove.
        logger.warning("[volume] could not read the level back after a bare success: {!r}", exc)
        return target


async def _volume_handler(args: dict) -> dict[str, Any]:
    """Execute a volume control action against the connected device."""
    action = args.get("action", "set")
    value = args.get("value")

    # Imported lazily: the voice stack is heavy and this module is also
    # imported by paths that never touch a device (the text-only roast path).
    from voice.pipecat.mcp_bridge import McpToolError, McpToolTimeout

    bridge = _current_mcp.get(None)
    if bridge is None:
        return _failed(action, NO_DEVICE)

    try:
        if action == "set":
            if value is None:
                return _failed(action, "Volume 'set' requires a value.")
            raw = int(value)
            target = _clamp(raw)
            msg = (
                f"Volume set to {target}."
                if raw == target
                else f"Volume must stay between {MIN_VOLUME} and {MAX_VOLUME}, set to {target}."
            )

        elif action == "increase":
            current = await _read_device_volume(bridge)
            if current >= MAX_VOLUME:
                return _failed(
                    action,
                    f"Volume is already at maximum ({MAX_VOLUME}).",
                    level=current,
                )
            step = _step(value)
            target = min(current + step, MAX_VOLUME)
            msg = (
                f"Volume increased to maximum ({MAX_VOLUME})."
                if target == MAX_VOLUME
                else f"Volume increased by {step}."
            )

        elif action == "decrease":
            current = await _read_device_volume(bridge)
            if current <= MIN_VOLUME:
                return _failed(
                    action,
                    f"Volume is already at minimum ({MIN_VOLUME}).",
                    level=current,
                )
            step = _step(value)
            target = max(current - step, MIN_VOLUME)
            msg = (
                f"Volume decreased to minimum ({MIN_VOLUME})."
                if target == MIN_VOLUME
                else f"Volume decreased by {step}."
            )

        elif action == "mute":
            target = MIN_VOLUME
            msg = "Volume muted."

        elif action == "unmute":
            current = await _read_device_volume(bridge)
            if current > MIN_VOLUME:
                # Already audible — do not change it, just say so. Still a
                # statement of intent, so it supersedes any mute still waiting
                # to be dispatched.
                bridge.note_write()
                return {
                    "success": True,
                    "action": action,
                    "level": current,
                    "message": f"Volume is already at {current}.",
                }
            target = UNMUTE_LEVEL
            msg = f"Volume unmuted, now at {UNMUTE_LEVEL}."

        else:
            return _failed(action, f"Unknown volume action: {action}")

        if target == MIN_VOLUME:
            # Not just 'mute': "音量调到 0" (set) and "小点声" bottoming out
            # (decrease) land on the same write, and it has the same problem —
            # this device has no screen and no LED, so the spoken confirmation
            # is the only feedback the user gets, and going silent now would
            # take that down with it. The bridge dispatches it once the reply
            # has finished playing.
            #
            # The write generation is claimed here rather than on entry: only a
            # command that actually commits device state may supersede a
            # deferred one. A command that failed or changed nothing leaves the
            # user's earlier "静音" standing.
            bridge.defer(lambda: _apply(bridge, target), seq=bridge.note_write())
            return {
                "success": True,
                "action": action,
                "level": target,
                "message": msg,
            }

        # This one reaches the device, so it supersedes any write still waiting.
        bridge.note_write()
        applied = await _apply(bridge, target)

    except McpToolTimeout:
        logger.warning("[volume] device did not answer action={}", action)
        return _failed(action, DEVICE_SILENT)
    except McpToolError as exc:
        logger.warning("[volume] device refused action={}: {}", action, exc)
        return _failed(action, f"The device refused the change: {exc}")
    except (KeyError, TypeError, ValueError) as exc:
        # The device answered in a shape we cannot read. Report a failure
        # rather than a guessed level.
        logger.warning("[volume] unreadable device answer for action={}: {}", action, exc)
        return _failed(action, DEVICE_SILENT)

    return {
        "success": True,
        "action": action,
        "level": applied,
        "message": msg,
    }


volume_tool = Tool(
    name="volume_control",
    description=(
        "Adjust the audio volume. "
        "Use 'set' to go to a specific level (0-100). "
        "Use 'increase' or 'decrease' to adjust by a step (5, 10, or 15). "
        "Use 'mute' to silence the audio, and 'unmute' to make it audible again."
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
