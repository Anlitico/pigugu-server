from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.voice_assistant import VoiceAssistant
from livekit.plugins import anthropic as lk_anthropic
from livekit.plugins import silero

from agent.prompts import build_system_prompt
from agent.publisher import publish_to_device
from agent.scoring import persist_score, score_conversation
from app.core.config import settings


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    device_id = ctx.room.name
    system_prompt = await build_system_prompt(device_id)

    await publish_to_device(device_id, {"type": "state_change", "state": "thinking"})

    assistant = VoiceAssistant(
        vad=silero.VAD.load(),
        stt=lk_anthropic.STT(),
        llm=lk_anthropic.LLM(
            model="claude-sonnet-4-6",
            api_key=settings.anthropic_api_key,
        ),
        tts=lk_anthropic.TTS(),
        chat_ctx=None,
    )

    assistant.start(ctx.room)

    await publish_to_device(device_id, {"type": "state_change", "state": "idle"})


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
