from livekit.agents import JobContext

from agent.prompts import build_system_prompt
from agent.publisher import publish_to_device
from agent.scoring import persist_score, score_conversation


async def run_pipeline(ctx: JobContext, conversation_id: str) -> None:
    """STT -> Claude API -> TTS pipeline stub.

    LiveKit handles the actual audio I/O; this module wires up
    the conversation lifecycle hooks (start, end, scoring).
    """
    device_id = ctx.room.name

    await publish_to_device(device_id, {"type": "state_change", "state": "listening"})

    # Pipeline runs inside VoiceAssistant (see worker.py).
    # This function is called at conversation end to finalise scoring.
    transcript = ""  # populated by VoiceAssistant transcript callback

    outcome, score_delta = await score_conversation(transcript, device_id)
    await persist_score(conversation_id, outcome, score_delta)

    await publish_to_device(
        device_id,
        {
            "type": "conversation_end",
            "conversation_id": conversation_id,
            "outcome": outcome,
            "score_delta": score_delta,
        },
    )
