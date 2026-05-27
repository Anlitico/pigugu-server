# pigagent/lk/session.py
"""LiveKit session wiring  -  registers event handlers and starts the session."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger
from livekit import rtc
from livekit.agents import AgentSession, JobContext
from livekit.agents.types import NOT_GIVEN
from livekit.agents.voice import room_io
from config import get_config
from personas import get_persona
from bootstrap.factory import create_agent_components, get_vad
from lk.bridge import PigAgentVoiceBridge
from utils.telemetry import TelemetryCollector


async def run(ctx: JobContext) -> None:
    """Wire a LiveKit session: persona, components, bridge, event handlers."""
    config = get_config()

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
    logger.info(f"LLM: {config.QWEN_MODEL}")
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

    # Resolve user_id: metadata (app) > device_id > participant identity
    user_id = metadata.get("user_id", "") or metadata.get("device_id", "")

    from lk.pigllm import PigAgentLLM
    pigllm = PigAgentLLM()

    bridge = PigAgentVoiceBridge(
        pig_agent=pig_agent,
        persona_id=persona_id,
    )

    from livekit.agents.voice.turn import TurnHandlingOptions
    session = AgentSession(
        stt=stt_plugin,
        llm=pigllm,
        tts=tts_plugin,
        vad=vad if vad is not None else NOT_GIVEN,
        turn_handling=TurnHandlingOptions(
            preemptive_generation={"enabled": config.ENABLE_PREEMPTIVE_SYNTHESIS}
        ),
    )

    # ── Interrupt wiring ──────────────────────────────────────────────
    # Single source of truth: bridge.current_interrupt_event.
    # No nonlocal variable — avoids divergence between session and bridge refs.

    # ── Event handlers ────────────────────────────────────────────────

    @session.on("agent_state_changed")
    def on_agent_state_changed(event):
        logger.info(f"[STATE] {event.old_state} -> {event.new_state}")

        # Timing
        if event.new_state == "thinking" and event.old_state != "thinking":
            bridge.current_interrupt_event = asyncio.Event()
            logger.info("[Interrupt] Event armed")
            TelemetryCollector.mark("llm_start")
        if event.old_state != "speaking" and event.new_state == "speaking":
            TelemetryCollector.mark("agent_spk")
        elif event.old_state == "speaking" and event.new_state != "speaking":
            TelemetryCollector.mark("tts_end")

    @session.on("user_state_changed")
    def on_user_state_changed(event):
        if event.old_state != "speaking" and event.new_state == "speaking":
            logger.info("[DEBUG] User started speaking")
            TelemetryCollector.start_turn(user_id=user_id, persona_id=persona_id)
            TelemetryCollector.set_meta("llm_model", pig_agent.model)
            TelemetryCollector.mark("vad_start")
            if bridge.current_interrupt_event:
                logger.info("[Interrupt] Triggering")
                bridge.current_interrupt_event.set()
        elif event.old_state == "speaking" and event.new_state != "speaking":
            logger.info(f"[DEBUG] User stopped speaking ({event.new_state})")
            TelemetryCollector.mark("vad_end")

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        logger.info(f"[STT] Transcript: is_final={event.is_final} text='{event.transcript}'")
        if not event.is_final:
            TelemetryCollector.mark("stt_first")
        else:
            TelemetryCollector.mark("stt_final")
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
        TelemetryCollector.mark("tts_start")

    @session.on("session_usage_updated")
    def on_session_usage_updated(event):
        usage = getattr(event, "usage", None)
        if usage and usage.model_usage:
            for mu in usage.model_usage:
                utype = getattr(mu, "type", "unknown")
                model = getattr(mu, "model", "") or getattr(mu, "model_id", "")
                if utype == "stt_usage":
                    TelemetryCollector.set_meta("stt_model", str(model))
                elif utype == "tts_usage":
                    TelemetryCollector.set_meta("tts_model", str(model))
                else:
                    TelemetryCollector.set_meta("llm_model", str(model))

    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        item = event.item
        text = getattr(item, "text_content", None)
        if text and hasattr(item, "role"):
            if item.role == "assistant":
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
        TelemetryCollector.finish_turn()  # flush the last turn
        logger.info(f"[SESSION] Closed  -  reason: {event.reason}")

    # ── Start ─────────────────────────────────────────────────────────
    logger.info(f"Starting voice agent session... ({len(ctx.room.remote_participants)} participants)")

    room_options = room_io.RoomOptions(
        audio_input=True, audio_output=True, text_output=True,
        close_on_disconnect=False,
    )

    await session.start(bridge, room=ctx.room, room_options=room_options)  # type: ignore[reportArgumentType]

    # Resolve user_id from participant identity if not in metadata
    if not user_id:
        # session.start() connects the room — now we can see remote participants
        for identity in ctx.room.remote_participants:
            user_id = identity
            break
    if not user_id:
        logger.error("No user_id available — refusing to run session without identity")
        await session.aclose()
        return
    bridge._user_id = user_id
    logger.info(f"User ID resolved: {user_id}")

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
