# pigagent/lk/session.py
"""LiveKit session wiring  -  registers event handlers and starts the session."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from loguru import logger
from livekit import rtc
from livekit.agents import AgentSession, JobContext
from livekit.agents.types import NOT_GIVEN
from livekit.agents.metrics import LLMMetrics, TTSMetrics  # type: ignore[reportUnusedImport] # used via isinstance
from livekit.agents.voice import room_io
from config import get_config
from core.agent.interrupt import get_interrupt_manager
from personas import get_persona
from bootstrap.factory import create_agent_components, get_vad
from lk.bridge import PigAgentVoiceBridge
from lk.telemetry import TurnTimer


async def run(ctx: JobContext) -> None:
    """Wire a LiveKit session: persona, components, bridge, event handlers."""
    config = get_config()
    timer = TurnTimer()

    # ── Metadata + persona ────────────────────────────────────────────
    metadata: dict[str, Any] = {}
    if ctx.job.metadata:
        try:
            metadata = json.loads(ctx.job.metadata)
            logger.info(f"Parsed job metadata: {metadata}")
        except Exception as e:
            logger.warning(f"Failed to parse job metadata: {e}")

    raw = metadata.get("persona", 1)
    persona_id = int(raw)
    persona = get_persona(persona_id)
    logger.info(
        f"Persona: {persona.persona_id} ({persona.display_name}, domain={persona.domain})"
    )

    # ── Startup banner ────────────────────────────────────────────────
    stt_provider = config.STT_PROVIDER.lower()
    stt_info = (
        f"{config.DEEPGRAM_STT_MODEL} (Deepgram, language: {config.DEEPGRAM_STT_LANGUAGE})"
        if stt_provider == "deepgram"
        else f"{config.CARTESIA_STT_MODEL} (Cartesia, language: {config.CARTESIA_STT_LANGUAGE})"
    )
    logger.info("=" * 70)
    logger.info(f"Agent starting for room: {ctx.room.name}")
    logger.info(f"Job ID: {ctx.job.id}")
    logger.info(f"STT: {stt_info}")
    logger.info(f"LLM: {config.QWEN_MODEL} ({config.LLM_PROVIDER})")
    logger.info(f"TTS: {config.CARTESIA_TTS_MODEL} (voice: {config.CARTESIA_TTS_VOICE})")
    if metadata:
        logger.info(f"Metadata: {metadata}")
    logger.info("=" * 70)

    # ── Room connection ───────────────────────────────────────────────
    # session.start() handles room connection + RoomIO internally

    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] Participant connected: {participant.identity}")

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"[ROOM] Participant disconnected: {participant.identity}")

    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"[AUDIO] Track subscribed: kind={track.kind} source={participant.identity}")

    # ── Components ────────────────────────────────────────────────────
    stt, pig_agent, tts = create_agent_components(config, persona=persona)
    stt_plugin = stt.get_plugin()
    tts_plugin = tts.get_plugin()
    vad = get_vad()

    logger.info(f"[DEBUG] STT: {type(stt_plugin).__name__}, TTS: {type(tts_plugin).__name__}")
    logger.info(f"[DEBUG] LLM: PigAgent with {pig_agent.model}")

    # Resolve user_id: app passes directly, device passes device_id  ->  lookup later
    user_id = metadata.get("user_id", "") or metadata.get("device_id", "")

    from lk.pig_llm import PigAgentLLM
    pig_llm = PigAgentLLM(pig_agent=pig_agent, persona_id=persona_id, user_id=user_id)

    bridge = PigAgentVoiceBridge(
        pig_agent=pig_agent,
        persona_id=persona_id,
        user_id=user_id,
    )

    from livekit.agents.voice.turn import TurnHandlingOptions
    session = AgentSession(
        stt=stt_plugin,
        llm=pig_llm,
        tts=tts_plugin,
        vad=vad if vad is not None else NOT_GIVEN,
        turn_handling=TurnHandlingOptions(
            preemptive_generation={"enabled": config.ENABLE_PREEMPTIVE_SYNTHESIS}
        ),
    )

    # ── Interrupt wiring ──────────────────────────────────────────────
    current_interrupt_key: str | None = None
    interrupt_mgr = get_interrupt_manager()

    # ── Event handlers ────────────────────────────────────────────────

    @session.on("agent_state_changed")
    def on_agent_state_changed(event):
        logger.info(f"[STATE] {event.old_state} -> {event.new_state}")
        nonlocal current_interrupt_key
        if event.new_state == "speaking":
            current_interrupt_key = None
        elif (
            event.old_state in ("thinking", "speaking")
            and event.new_state == "listening"
        ):
            if current_interrupt_key:
                interrupt_mgr.cleanup(current_interrupt_key)
                current_interrupt_key = None

        # Timing
        if event.new_state == "thinking" and event.old_state != "thinking":
            if timer.data["agent_start_thinking"] is None:
                timer.mark("agent_start_thinking")
        if event.old_state != "speaking" and event.new_state == "speaking":
            timer.mark("agent_start_speaking")
            timer.log_summary()

    @session.on("user_state_changed")
    def on_user_state_changed(event):
        nonlocal current_interrupt_key
        if event.old_state != "speaking" and event.new_state == "speaking":
            logger.info("[DEBUG] User started speaking")
            timer.reset()
            if current_interrupt_key:
                logger.info(f"[Interrupt] Cancelling: {current_interrupt_key}")
                asyncio.create_task(interrupt_mgr.trigger(current_interrupt_key))
        elif event.old_state == "speaking" and event.new_state != "speaking":
            logger.info(f"[DEBUG] User stopped speaking ({event.new_state})")
            timer.mark("user_stop_speaking")

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        logger.info(f"[STT] Transcript: is_final={event.is_final} text='{event.transcript}'")
        if event.is_final and event.transcript and event.transcript.strip():
            timer.mark("final_transcript")
            logger.info(f"[STT] Final transcript: '{event.transcript.strip()}'")
            try:
                payload = json.dumps({"text": event.transcript.strip()})
                asyncio.create_task(
                    ctx.room.local_participant.publish_data(
                        payload.encode("utf-8"), reliable=True, topic="user_transcript",
                    )
                )
            except Exception as e:
                logger.error(f"Error publishing transcript: {e}")

    @session.on("speech_created")
    def on_speech_created(event):
        if timer.data["speech_created"] is None:
            timer.mark("speech_created")

    @session.on("session_usage_updated")
    def on_session_usage_updated(event):
        usage = getattr(event, "usage", None)
        if usage and usage.model_usage:
            for mu in usage.model_usage:
                utype = getattr(mu, "type", "unknown")
                model = getattr(mu, "model", "") or getattr(mu, "model_id", "")
                if utype == "stt_usage":
                    dur = getattr(mu, "audio_duration", 0)
                    logger.info(f"[STT] model={model} duration={dur:.1f}s")
                elif utype == "tts_usage":
                    logger.info(f"[TTS] model={model}")
                else:
                    pt = getattr(mu, "prompt_tokens", 0)
                    ct = getattr(mu, "completion_tokens", 0)
                    logger.info(f"[LLM] model={model} tokens={pt}->{ct}")

    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        item = event.item
        text = getattr(item, "text_content", None)
        if text and hasattr(item, "role"):
            if item.role == "assistant":
                timer.mark("llm_response_logged")
                try:
                    asyncio.create_task(
                        ctx.room.local_participant.publish_data(
                            text.strip().encode("utf-8"),
                            reliable=True, topic="agent_response",
                        )
                    )
                except Exception as e:
                    logger.error(f"Error publishing response: {e}")

    @session.on("function_tools_executed")
    def on_function_tools_executed(event):
        for fc, fo in event.zipped():
            logger.info(f"[TOOL] {fc.name}  ->  {str(fo.result)[:200] if fo and fo.result else 'ok'}")

    @session.on("error")
    def on_error(event):
        logger.error(f"[SESSION] Error: {event.error}")

    @session.on("close")
    def on_close(event):
        logger.info(f"[SESSION] Closed  -  reason: {event.reason}")

    # ── Start ─────────────────────────────────────────────────────────
    logger.info(f"Starting voice agent session... ({len(ctx.room.remote_participants)} participants)")

    room_options = room_io.RoomOptions(
        audio_input=True, audio_output=True, text_output=True,
        close_on_disconnect=False,
    )

    await session.start(bridge, room=ctx.room, room_options=room_options)  # type: ignore[reportArgumentType]

    # Accept TrackSource.UNKNOWN (0) in addition to SOURCE_MICROPHONE (2).
    # LiveKit JS client's LocalAudioTrack may report source="unknown" instead
    # of "microphone", causing _on_track_available to reject the audio track.
    try:
        audio_input: Any = session.input.audio
        if audio_input:
            accepted: set[Any] = getattr(audio_input, '_accepted_sources', set())
            accepted.add(0)
    except Exception:
        pass

    if config.ENABLE_POLICY_SEARCH:
        logger.info(f"[SEARCH] Enabled, backend={config.POLICY_SEARCH_BACKEND}")

    logger.info("Agent ready  -  waiting for voice input...")
    await asyncio.Event().wait()
