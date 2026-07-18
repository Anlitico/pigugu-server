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
from agent_config import get_config
from system_prompts import get_persona
from bootstrap.factory import create_agent_components, create_pig_agent, get_pg_pool, get_redis, get_vad
from lk.bridge import PigAgentVoiceBridge
from metrics.session import ColdStartMetrics
from metrics.turn import TelemetryCollector


async def run(ctx: JobContext) -> None:
    """Wire a LiveKit session: persona, components, bridge, event handlers."""
    config = get_config()

    ColdStartMetrics.start(session_id=ctx.job.id, room_name=ctx.room.name)

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
    logger.info(f"LLM: {config.LLM_MODEL}")
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

    # ── Handle injected start_roast from App via LiveKit data channel ────
    # Defined BEFORE session.start() so data_received can call it immediately.
    # Uses bridge._pig (set after user_id resolved) with a None guard.
    async def _handle_inject_start_roast(msg: dict) -> None:
        """Called when the API server sends a start_roast command through the
        LiveKit room data channel (topic="roast_inject").

        Runs the full pig_agent.start_roast() pipeline: activate roast,
        persist context, generate opening lines, then speak via TTS.
        """
        try:
            pig = bridge._pig
            if pig is None:
                logger.warning("[Inject] PigAgent not yet wired — retry later")
                return
            logger.info(
                f"[Inject] start_roast: roast_id={msg.get('roast_id')} "
                f"mode={msg.get('mode_id')}"
            )
            full_text: str = ""
            async for text in pig.start_roast(
                persona_id=msg.get("persona_id", persona_id),
                roast_id=msg["roast_id"],
                mode_id=msg["mode_id"],
                prompt=msg["prompt"],
                headline=msg.get("headline", ""),
                source=msg.get("source", ""),
            ):
                if isinstance(text, str):
                    full_text += text

            if full_text.strip():
                await session.say(
                    full_text.strip(),
                    add_to_chat_ctx=False,
                )
                # session.say with add_to_chat_ctx=False doesn't fire
                # conversation_item_added — write opening manually to both
                # agent_conversations (via ctx.add_turn) and roast_conversations.
                asyncio.create_task(
                    _safe_add_turn(user_id, "assistant", full_text.strip())
                )
                logger.info(
                    f"[Inject] start_roast spoken: {len(full_text)} chars"
                )
        except Exception as exc:
            logger.error(f"[Inject] start_roast failed: {exc}")

    # ── Injected commands via LiveKit data channel (API server → agent) ──
    @ctx.room.on("data_received")
    def on_data_received(packet: rtc.DataPacket) -> None:
        topic = getattr(packet, "topic", "")
        if topic != "roast_inject":
            return
        try:
            data = packet.data if isinstance(packet.data, bytes) else str(packet.data).encode()
            msg = json.loads(data)
            if msg.get("type") == "start_roast":
                asyncio.create_task(
                    _handle_inject_start_roast(msg)
                )
        except Exception as exc:
            logger.error(f"[Inject] Failed to parse data_received: {exc}")

    # ── STT + TTS (created early, no user_id needed) ──────────────────
    stt, tts = await create_agent_components(config, persona=persona)
    stt_plugin = stt.get_plugin()
    tts_plugin = tts.get_plugin()

    vad = get_vad()
    ColdStartMetrics.mark("vad")

    logger.info(f"[DEBUG] STT: {type(stt_plugin).__name__}, TTS: {type(tts_plugin).__name__}")

    # Resolve user_id: metadata (app) > device_id > participant identity
    user_id = metadata.get("user_id", "") or metadata.get("device_id", "")
    hw_id = metadata.get("hw_id", "")

    from lk.pigllm import PigAgentLLM
    pigllm = PigAgentLLM()

    # ── Bridge (placeholder — PigAgent wired after user_id resolved) ──
    bridge = PigAgentVoiceBridge(
        pig_agent=None,
        persona_id=persona_id,
        session_id=ctx.job.id,
    )

    from livekit.agents.voice.turn import TurnHandlingOptions
    session = AgentSession(
        stt=stt_plugin,
        llm=pigllm,
        tts=tts_plugin,
        vad=vad if vad is not None else NOT_GIVEN,
        turn_detection="vad",
        min_endpointing_delay=config.ENDPOINTING_DELAY,
        turn_handling=TurnHandlingOptions(
            preemptive_generation={"enabled": config.ENABLE_PREEMPTIVE_SYNTHESIS, "preemptive_tts": True},
            interruption={"mode": "adaptive", "min_duration": 0.1},
        ),
    )

    # ── Interrupt wiring ──────────────────────────────────────────────
    # Single source of truth: bridge.current_interrupt_event.
    # bridge.llm_node() creates the event, session triggers it, runner checks it.

    # ── Event handlers ────────────────────────────────────────────────

    @session.on("overlapping_speech")
    def on_overlapping_speech(event):
        """Fires when VAD detects user speech during agent output — earliest
        possible interrupt signal, before any state transitions."""
        if getattr(event, "is_interruption", False) and bridge.current_interrupt_event:
            logger.info("[Interrupt] Overlapping speech — cancelling LLM")
            bridge.current_interrupt_event.set()

    @session.on("agent_state_changed")
    def on_agent_state_changed(event):
        logger.info(f"[STATE] {event.old_state} -> {event.new_state}")

        # Timing
        if event.new_state == "thinking" and event.old_state != "thinking":
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
            if bridge._pig:
                TelemetryCollector.set_meta("llm_model", bridge._pig.model)
            TelemetryCollector.mark("vad_start")
            if bridge.current_interrupt_event:
                logger.info("[Interrupt] Triggering")
                bridge.current_interrupt_event.set()
        elif event.old_state == "speaking" and event.new_state != "speaking":
            # Only record the first silence transition per turn. Agent speech
            # pauses also trigger speaking→non-speaking, which would overwrite
            # the real user endpoint and corrupt stt/vad metrics.
            if not TelemetryCollector.has_mark("vad_end"):
                logger.info(f"[DEBUG] User stopped speaking ({event.new_state})")
                TelemetryCollector.mark("vad_end")

    async def _safe_add_turn(uid: str, role: str, content: str) -> None:
        """Persist a turn to context + roast_conversations (for App display).

        All roles → context (agent_conversations)
        user/assistant during active roast → roast_conversations
        """
        try:
            pig = bridge._pig
            if pig and pig.ctx:
                await pig.ctx.add_turn(
                    role=role, content=content,
                )
            # Dual-write to roast_conversations for App display
            if role in ("user", "assistant"):
                asyncio.create_task(
                    _write_roast_conversation(uid, role, content)
                )
        except Exception as e:
            logger.error(f"[Session] Failed to persist {role} turn: {e}")

    async def _write_roast_conversation(uid: str, role: str, content: str) -> None:
        """Write user/assistant message to roast_conversations table + push to App WS."""
        try:
            pig = bridge._pig
            if not pig:
                return
            state = await pig.get_active_roast()
            if not state:
                return  # Not in a roast — skip
            # Only write during active roast. During CLOSING, only allow
            # assistant (the closing statement itself), not user interjections.
            from roast.types import Phase
            if state.phase == Phase.CLOSING and role != "assistant":
                return
            if state.phase not in (Phase.ACTIVE, Phase.CLOSING):
                return

            # 1. Persist to DB
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO roast_conversations
                       (user_id, roast_id, roast_instance_id, role, content)
                       VALUES ($1, $2, $3, $4, $5)""",
                    uid, str(state.roast_id), state.roast_instance_id, role, content,
                )

            # 2. Push to App WS via Redis Pub/Sub → cross-pod fan-out
            redis = get_redis()
            if redis:
                msg = json.dumps({
                    "type": "roast_message",
                    "roast_id": str(state.roast_id),
                    "role": role,
                    "content": content,
                })
                await redis.publish(f"ws:user:{uid}", msg)
        except Exception as e:
            logger.error(f"[Session] Failed to write roast_conversation: {e}")

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        logger.info(f"[STT] Transcript: is_final={event.is_final} text='{event.transcript}'")
        if not event.is_final:
            TelemetryCollector.mark("stt_first")
        else:
            TelemetryCollector.mark("stt_final")
            logger.info(f"[STT] Final transcript: '{event.transcript.strip()}'")
            # Persist user message to context (memory + Redis + PG)
            if bridge._pig and bridge._pig.ctx:
                asyncio.create_task(
                    _safe_add_turn(user_id, "user", event.transcript.strip())
                )
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

    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        item = event.item
        text = getattr(item, "text_content", None)
        if text and hasattr(item, "role"):
            if item.role == "assistant":
                # Persist assistant message to context (memory + Redis + PG)
                if bridge._pig and bridge._pig.ctx:
                    asyncio.create_task(
                        _safe_add_turn(user_id, "assistant", text.strip())
                    )
                try:
                    payload = json.dumps({"text": text.strip()})
                    asyncio.create_task(
                        ctx.room.local_participant.publish_data(
                            payload.encode("utf-8"),
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

    ColdStartMetrics.mark("session_start")
    await session.start(bridge, room=ctx.room, room_options=room_options)  # type: ignore[reportArgumentType]
    ColdStartMetrics.mark("session_started")

    # Resolve user_id from participant identity if not in metadata
    if not user_id:
        # session.start() connects the room — now we can see remote participants
        for identity in ctx.room.remote_participants:
            user_id = identity
            break
    if not user_id:
        # Explicit dispatch — agent arrives before user. Wait for first participant.
        logger.info("No user_id yet, waiting for participant to join...")
        _user_joined = asyncio.Event()

        @ctx.room.on("participant_connected")
        def _resolve_user(participant: rtc.RemoteParticipant):
            nonlocal user_id
            user_id = participant.identity
            _user_joined.set()

        try:
            await asyncio.wait_for(_user_joined.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.error("No participant joined within 30s — refusing to run session")
            await session.aclose()
            return
        logger.info(f"User ID resolved from joined participant: {user_id}")
    logger.info(f"User ID resolved: {user_id}")
    ColdStartMetrics.mark("user_id")
    ColdStartMetrics.set_meta("user_id", user_id)
    ColdStartMetrics.set_meta("persona_id", persona_id)

    # ── Create PigAgent now that user_id is known ─────────────────────
    pig_agent = await create_pig_agent(user_id, config, hw_id=hw_id)
    bridge._pig = pig_agent
    logger.info("[DEBUG] LLM: PigAgent with %s wired for user=%s hw_id=%s", pig_agent.model, user_id, hw_id)
    ColdStartMetrics.mark("agent_created")
    ColdStartMetrics.set_meta("stt_provider", config.STT_PROVIDER)
    ColdStartMetrics.set_meta("llm_model", pig_agent.model)
    ColdStartMetrics.set_meta("tts_model", config.CARTESIA_TTS_MODEL)

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

    ColdStartMetrics.mark("ready")
    ColdStartMetrics.flush()

    logger.info("Agent ready  -  waiting for voice input...")

    try:
        await asyncio.Event().wait()
    finally:
        logger.info(f"[Session] Cleaned up: user_id={user_id}")
