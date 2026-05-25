# pigagent/main.py
"""
AI Voice Agent - Modular STT/LLM/TTS Architecture

This agent is separated into modular components:
- STT (Speech-to-Text): stt.py
- LLM (Large Language Model): llm.py  
- TTS (Text-to-Speech): tts.py
- Config: config.py

Supports multiple providers for each component.
"""

import asyncio
import json
import os
import random
import sys
import time
from dotenv import load_dotenv
from loguru import logger

from livekit.agents import (
    AgentSession,
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    cli,
)
from livekit.agents.voice import room_io
from livekit import rtc
from livekit.plugins import silero

from config import get_config
from core.llm.types import ModelCapability
from core.llm.registry import ModelRegistry
from personas import PersonaRegistry, get_persona
from roast import GameModeRegistry, get_game_mode
from bootstrap.factory import create_agent_components, validate_configuration
from roast.types import Mode
from core.agent.interrupt import get_interrupt_manager
from lk import PigAgentVoiceBridge

load_dotenv()


# Initialize configuration
config = get_config()

# Configure loguru
logger.remove()  # Remove default handler

# Add console handler with colors
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level=config.LOG_LEVEL.upper(),
    colorize=True,
)

# Add file logging if enabled
if config.LOG_TO_FILE:
    import os
    from pathlib import Path
    
    # Create log directory from LOG_FILE_PATH
    log_file = Path(config.LOG_FILE_PATH)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    logger.add(
        config.LOG_FILE_PATH,
        rotation=config.LOG_ROTATION,
        retention=config.LOG_RETENTION,
        level=config.LOG_LEVEL.upper(),
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        enqueue=True,  # Thread-safe logging
    )
    logger.info(f"File logging enabled: {config.LOG_FILE_PATH}")


async def entrypoint(ctx: JobContext):
    """Main agent entry point"""
    
    # Interrupt timer state for Mode 2
    last_user_interaction_time = asyncio.get_event_loop().time()
    interrupt_task = None
    
    # Track conversation history for interrupt generation
    conversation_history = []
    
    # === LATENCY TIMING INSTRUMENTATION (v3 - Highly Granular) ===
    # Track timestamps for each conversation turn to measure pipeline latency
    turn_timing = {
        "turn_id": 0,                    # Incrementing turn counter
        "user_stop_speaking": None,      # T0: When user finishes speaking
        "final_transcript": None,        # T1: When final STT transcript received
        "agent_start_thinking": None,    # T2: When agent enters "thinking" state (LLM request)
        "llm_first_token": None,         # T2.5: When LLM streams first token (NEW)
        "speech_created": None,          # T3: When TTS synthesis starts (speech_created event)
        "llm_response_logged": None,     # T4: When LLM full response is logged
        "agent_start_speaking": None,    # T5: When agent voice starts playing
        "filler_yielded": None,          # TF: When filler text was yielded to TTS pipeline
    }
    
    def reset_turn_timing():
        """Reset timing for a new turn"""
        turn_timing["turn_id"] += 1
        turn_timing["user_stop_speaking"] = None
        turn_timing["final_transcript"] = None
        turn_timing["agent_start_thinking"] = None
        turn_timing["llm_first_token"] = None
        turn_timing["speech_created"] = None
        turn_timing["llm_response_logged"] = None
        turn_timing["agent_start_speaking"] = None
        turn_timing["filler_yielded"] = None
    
    def log_turn_timing_summary():
        """Log a detailed summary of timing for the completed turn"""
        t0 = turn_timing["user_stop_speaking"]
        t1 = turn_timing["final_transcript"]
        t2 = turn_timing["agent_start_thinking"]
        t2_5 = turn_timing["llm_first_token"]
        t3 = turn_timing["speech_created"]
        t4 = turn_timing["llm_response_logged"]
        t5 = turn_timing["agent_start_speaking"]
        tf = turn_timing["filler_yielded"]
        
        if not t5:
            return  # Not enough data - agent hasn't spoken yet
        
        # Use the best available start time
        start_time = t0 or t1 or t2
        if not start_time:
            return
        
        total = t5 - start_time
        
        # Build detailed breakdown
        logger.info(f"⏱️ ══════════════════════════════════════════════════════════")
        logger.info(f"⏱️ [TIMING] Turn #{turn_timing['turn_id']} DETAILED BREAKDOWN")
        logger.info(f"⏱️ ══════════════════════════════════════════════════════════")
        
        # Log each timestamp
        if t0:
            logger.info(f"⏱️   T0   User stopped speaking:    {t0:.3f}")
        if t1:
            logger.info(f"⏱️   T1   Final STT transcript:     {t1:.3f}")
        if t2:
            logger.info(f"⏱️   T2   Agent started thinking:   {t2:.3f}")
        if t2_5:
            logger.info(f"⏱️   T2.5 LLM first token:          {t2_5:.3f}")
        if t3:
            logger.info(f"⏱️   T3   TTS speech created:       {t3:.3f}")
        if t4:
            logger.info(f"⏱️   T4   LLM response complete:    {t4:.3f}")
        if tf:
            logger.info(f"⏱️   TF   Filler yielded to TTS:    {tf:.3f}")
        if t5:
            logger.info(f"⏱️   T5   Agent voice playing:      {t5:.3f}")
        
        logger.info(f"⏱️ ──────────────────────────────────────────────────────────")
        
        # === PHASE 1: STT PROCESSING ===
        logger.info(f"⏱️ 📊 PHASE 1: STT PROCESSING")
        if t0 and t1:
            delta = t1 - t0
            verdict = "✅" if delta < 0.5 else "⚠️" if delta < 1.0 else "❌"
            logger.info(f"⏱️   T0→T1 (STT finalization):          {delta:+.3f}s  {verdict}")
        
        # === PHASE 2: PIPELINE OVERHEAD ===
        logger.info(f"⏱️ 📊 PHASE 2: PIPELINE OVERHEAD")
        if t1 and t2:
            delta = t2 - t1
            verdict = "✅" if delta < 0.3 else "⚠️" if delta < 1.0 else "❌"
            logger.info(f"⏱️   T1→T2 (Pipeline → LLM request):    {delta:+.3f}s  {verdict}")
        elif t0 and t2:
            delta = t2 - t0
            verdict = "✅" if delta < 0.5 else "⚠️" if delta < 1.0 else "❌"
            logger.info(f"⏱️   T0→T2 (User stop → LLM request):   {delta:+.3f}s  {verdict}")
        
        # === PHASE 3: LLM PROCESSING ===
        logger.info(f"⏱️ 📊 PHASE 3: LLM PROCESSING")
        if t2 and t2_5:
            delta = t2_5 - t2
            verdict = "✅" if delta < 0.5 else "⚠️" if delta < 1.5 else "❌"
            logger.info(f"⏱️   T2→T2.5 (LLM Time-To-First-Token):  {delta:+.3f}s  {verdict} [TTFT]")
        
        if t2_5 and t4:
            delta = t4 - t2_5
            logger.info(f"⏱️   T2.5→T4 (LLM streaming):            {delta:+.3f}s  [Full generation]")
        elif t2 and t4:
            delta = t4 - t2
            logger.info(f"⏱️   T2→T4 (LLM total time):            {delta:+.3f}s  [Request → Complete]")
        
        # If we don't have T2.5 but have T2 and T5, note it
        if not t2_5 and t2 and t5:
            logger.info(f"⏱️   (T2.5 not captured - LLM metrics may arrive late)")
        
        # === PHASE 4: TTS & AUDIO PIPELINE ===
        logger.info(f"⏱️ 📊 PHASE 4: TTS & AUDIO PIPELINE")
        if t3 and t5:
            delta = t5 - t3
            verdict = "✅" if delta < 1.0 else "⚠️" if delta < 3.0 else "❌"
            logger.info(f"⏱️   T3→T5 (TTS synthesis → Playback):   {delta:+.3f}s  {verdict} [TTS+Buffer]")
        
        if t2 and t3:
            delta = t3 - t2
            verdict = "✅" if delta < 0.5 else "⚠️" if delta < 2.0 else "❌"
            logger.info(f"⏱️   T2→T3 (LLM request → TTS start):    {delta:+.3f}s  {verdict}")
        
        # === AGGREGATE METRICS ===
        logger.info(f"⏱️ ──────────────────────────────────────────────────────────")
        logger.info(f"⏱️ 📊 AGGREGATE METRICS")
        
        if t2 and t5:
            delta = t5 - t2
            verdict = "✅" if delta < 2.0 else "⚠️" if delta < 5.0 else "❌"
            logger.info(f"⏱️   T2→T5 (Total LLM+TTS time):        {delta:+.3f}s  {verdict}")
        
        if t4 and t5:
            delta = t5 - t4
            if delta < 0:
                logger.info(f"⏱️   T4→T5 (LLM done → Audio start):    {delta:+.3f}s  ✅ [Streaming worked!]")
            else:
                verdict = "⚠️" if delta < 2.0 else "❌"
                logger.info(f"⏱️   T4→T5 (LLM done → Audio start):    {delta:+.3f}s  {verdict} [Post-generation delay]")
        
        # === FILLER METRICS ===
        if tf:
            logger.info(f"⏱️ 📊 FILLER WORD METRICS")
            if t0 and tf:
                delta = tf - t0
                verdict = "✅" if delta < 0.5 else "⚠️" if delta < 1.0 else "❌"
                logger.info(f"⏱️   T0→TF (User stop → Filler yield):  {delta:+.3f}s  {verdict}")
            if tf and t5:
                delta = t5 - tf
                verdict = "✅" if delta < 0.5 else "⚠️" if delta < 1.5 else "❌"
                logger.info(f"⏱️   TF→T5 (Filler yield → Playback):   {delta:+.3f}s  {verdict} [TTS buffer]")
        
        logger.info(f"⏱️ ──────────────────────────────────────────────────────────")
        logger.info(f"⏱️   TOTAL PERCEIVED LAG: {total:.3f}s")
        
        # === BOTTLENECK DIAGNOSIS ===
        bottleneck = "Unknown"
        bottleneck_time = 0.0
        
        # Find the slowest phase
        if t1 and t2:
            pipeline_time = t2 - t1
            if pipeline_time > 0.5 and pipeline_time > bottleneck_time:
                bottleneck = "Pipeline overhead (T1→T2)"
                bottleneck_time = pipeline_time
        
        if t2 and t2_5:
            ttft_time = t2_5 - t2
            if ttft_time > 1.0 and ttft_time > bottleneck_time:
                bottleneck = "LLM TTFT (T2→T2.5)"
                bottleneck_time = ttft_time
        
        if t2_5 and t4:
            streaming_time = t4 - t2_5
            if streaming_time > 3.0 and streaming_time > bottleneck_time:
                bottleneck = "LLM streaming (T2.5→T4)"
                bottleneck_time = streaming_time
        
        if t3 and t5:
            tts_audio_time = t5 - t3
            if tts_audio_time > 2.0 and tts_audio_time > bottleneck_time:
                bottleneck = "TTS/Audio pipeline (T3→T5)"
                bottleneck_time = tts_audio_time
        
        # If T2.5 is missing, note that we can't fully diagnose
        if not t2_5 and t2 and t5 and (t5 - t2) > 3.0:
            bottleneck = "LLM+TTS pipeline (T2→T5, TTFT not captured)"
        
        if total < 2.0:
            logger.info(f"⏱️   VERDICT: ✅ EXCELLENT (under 2s)")
        elif total < 4.0:
            logger.info(f"⏱️   VERDICT: ⚠️ ACCEPTABLE (2-4s) - Bottleneck: {bottleneck}")
        elif total < 6.0:
            logger.info(f"⏱️   VERDICT: ❌ SLOW (4-6s) - Bottleneck: {bottleneck}")
        else:
            logger.info(f"⏱️   VERDICT: ❌❌ VERY SLOW (>6s) - Bottleneck: {bottleneck}")
        logger.info(f"⏱️ ══════════════════════════════════════════════════════════")
    # === END LATENCY TIMING INSTRUMENTATION ===
    
    # Parse metadata from job context
    metadata = {}
    if ctx.job.metadata:
        try:
            import json
            metadata = json.loads(ctx.job.metadata)
            logger.info(f"Parsed job metadata: {metadata}")
        except Exception as e:
            logger.warning(f"Failed to parse job metadata: {e}")
    
    # ── Init persona + game mode ──────────────────────────────────────
    PersonaRegistry.register_defaults()
    GameModeRegistry.register_defaults()

    persona_id = metadata.get("persona", "trump") if metadata else "trump"
    mode_id = metadata.get("mode", "roast_together") if metadata else "roast_together"

    persona = get_persona(persona_id)
    game_mode = get_game_mode(mode_id)

    logger.info(f"🎭 Persona: {persona.persona_id} ({persona.display_name}, domain={persona.domain})")
    logger.info(f"🎮 Game Mode: {game_mode.mode}")

    # ── Build roast body from news metadata ─────────────────────────────
    roast_parts: list[str] = []
    if metadata:
        title = metadata.get("news_title", "")
        summary = metadata.get("news_summary", "")
        source = metadata.get("news_source", "")
        if title:
            roast_parts.append(
                f"## NEWS CONTEXT\n"
                f"Topic: {title}\n"
                f"Summary: {summary}\n"
                f"Source: {source}\n"
            )
    roast_parts.append(game_mode.system_prompt_extension)
    roast_body = "\n\n".join(p for p in roast_parts if p.strip())
    logger.info(f"Game mode '{game_mode.mode}' loaded as roast body ({len(roast_body)} chars)")

    # Determine STT model info for logging
    stt_provider = config.STT_PROVIDER.lower()
    if stt_provider == "deepgram":
        stt_info = f"{config.DEEPGRAM_STT_MODEL} (Deepgram, language: {config.DEEPGRAM_STT_LANGUAGE})"
    else:
        stt_info = f"{config.CARTESIA_STT_MODEL} (Cartesia, language: {config.CARTESIA_STT_LANGUAGE})"

    # Determine LLM model info for logging
    llm_provider = config.LLM_PROVIDER.lower()
    if llm_provider == "grok" or llm_provider == "xai":
        llm_model_info = f"{config.GROK_MODEL} (Provider: {config.LLM_PROVIDER})"
    else:
        llm_model_info = f"{config.QWEN_MODEL} (Provider: {config.LLM_PROVIDER})"

    logger.info("=" * 70)
    logger.info(f"Agent starting for room: {ctx.room.name}")
    logger.info(f"Job ID: {ctx.job.id}")
    mode_desc = {1: "Default", 2: f"Interrupt (every {config.INTERRUPT_INTERVAL_SECONDS}s)"}
    logger.info(f"Agent Mode: {config.AGENT_MODE} ({mode_desc.get(config.AGENT_MODE, 'Unknown')})")
    logger.info(f"STT: {stt_info}")
    logger.info(f"LLM: {llm_model_info}")
    logger.info(f"TTS: {config.CARTESIA_TTS_MODEL} (voice: {config.CARTESIA_TTS_VOICE}, language: {config.CARTESIA_TTS_LANGUAGE})")
    if metadata:
        logger.info(f"Metadata: {metadata}")
    logger.info("=" * 70)
    
    # Connect to room
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    
    # Initialize unified logger with room connection
    
    # Add room event handlers for connection tracking
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"🔗 [ROOM] Participant connected: {participant.identity}")
    
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"🔌 [ROOM] Participant disconnected: {participant.identity}")
    
    # Create agent components using factory
    stt, pig_agent, tts = create_agent_components(config, persona=persona)

    # Get STT/TTS plugin instances for LiveKit
    stt_plugin = stt.get_plugin() if hasattr(stt, 'get_plugin') else stt
    tts_plugin = tts.get_plugin() if hasattr(tts, 'get_plugin') else tts

    logger.info(f"[DEBUG] STT plugin: {type(stt_plugin).__name__}")
    logger.info(f"[DEBUG] TTS plugin: {type(tts_plugin).__name__}")
    logger.info(f"[DEBUG] LLM: PigAgent with {pig_agent.model}")

    # Build VAD (Voice Activity Detection)
    vad = silero.VAD.load()

    # ── PigAgentVoiceBridge — no Agent inheritance, no dummy LLM ────────
    # All LLM/content logic lives in PigAgent; the bridge only satisfies
    # AgentSession's duck-type interface for the voice pipeline.

    min_ep = 0.5
    max_ep = 2.0

    # Warn if model lacks native web_search; tool-based search will be used instead
    model_info = ModelRegistry.get(pig_agent.model)
    if ModelCapability.WEB_SEARCH not in model_info.capabilities:
        logger.info("Provider doesn't support web_search, will use tool-based search")

    user_id = ctx.room.name  # TODO: resolve from session auth
    bridge = PigAgentVoiceBridge(
        pig_agent=pig_agent,
        persona_id=persona_id,
        user_id=user_id,
        stt=stt_plugin,
        tts=tts_plugin,
        vad=vad,
        allow_interruptions=config.ENABLE_INTERRUPTIONS,
    )

    # Create AgentSession (manages the STT → bridge.llm_node → TTS pipeline)
    session = AgentSession(
        preemptive_generation=config.ENABLE_PREEMPTIVE_SYNTHESIS,
    )

    logger.info("Agent and session created (VAD + PigAgentVoiceBridge + LiveKit pipeline)")

    # ── Interrupt wiring ────────────────────────────────────────────
    # When user starts speaking while agent is responding, trigger
    # InterruptManager → AgentRunner cancels the current loop.
    room_name = getattr(ctx.room, 'name', 'unknown')
    current_interrupt_key: str | None = None
    interrupt_mgr = get_interrupt_manager()

    @session.on("agent_state_changed")
    def on_agent_state_changed(event):
        nonlocal current_interrupt_key
        if event.new_state == "speaking":
            # Agent started speaking — clear any previous interrupt
            current_interrupt_key = None
        elif event.old_state in ("thinking", "speaking") and event.new_state == "listening":
            # Agent finished — clean up interrupt event
            if current_interrupt_key:
                interrupt_mgr.cleanup(current_interrupt_key)
                current_interrupt_key = None

    # Add comprehensive logging to debug the conversation pipeline
    @session.on("user_state_changed")
    def on_user_state_changed(event):
        nonlocal current_interrupt_key
        if event.old_state != "speaking" and event.new_state == "speaking":
            logger.info("🎤 [DEBUG] User started speaking")
            reset_turn_timing()

            # Trigger interrupt if agent is currently running
            if current_interrupt_key:
                logger.info(f"🛑 [Interrupt] User started speaking, cancelling agent: {current_interrupt_key}")
                asyncio.create_task(interrupt_mgr.trigger(current_interrupt_key))
        elif event.old_state == "speaking" and event.new_state != "speaking":
            logger.info(f"🎤 [DEBUG] User stopped speaking (now {event.new_state})")
            turn_timing["user_stop_speaking"] = time.perf_counter()
            logger.info(f"⏱️ [TIMING] T0: User stopped speaking at {turn_timing['user_stop_speaking']:.3f}")
        else:
            logger.info(f"👤 [DEBUG] User state: {event.old_state} → {event.new_state}")
        
        @session.on("user_input_transcribed")
        def on_user_input_transcribed(event):
            nonlocal last_user_interaction_time
            
            logger.info(f"👤 [STT] User transcribed: {event.transcript}")
            logger.info(f"🔍 [DEBUG] is_final={event.is_final}, type={event.type}")
            logger.info(f"🔍 [DEBUG] Session agent_state: {session.agent_state}")
            logger.info(f"🔍 [DEBUG] Session user_state: {session.user_state}")
            
            if event.is_final and event.transcript and event.transcript.strip():
                # === TIMING: Record T1 - final transcript received ===
                turn_timing["final_transcript"] = time.perf_counter()
                logger.info(f"⏱️ [TIMING] T1: Final STT transcript at {turn_timing['final_transcript']:.3f}")

            # Reset interaction timer when user speaks to agent
            if event.is_final and event.transcript and event.transcript.strip():
                last_user_interaction_time = asyncio.get_event_loop().time()
                logger.debug(f"⏰ [MODE] Interaction timer reset")

            # Publish user transcript to frontend in real-time (when final)
            if event.is_final and event.transcript and event.transcript.strip():
                try:
                    import json
                    trimmed_transcript = event.transcript.strip()
                    payload = {"text": trimmed_transcript}
                    asyncio.create_task(
                        ctx.room.local_participant.publish_data(
                            json.dumps(payload).encode('utf-8'),
                            reliable=True,
                            topic="user_transcript"
                        )
                    )
                    logger.debug("📤 Published user transcript (real-time)")
                except Exception as e:
                    logger.error(f"❌ Error publishing user transcript: {e}")
        
        @session.on("agent_state_changed")
        def on_agent_state_changed(event):
            # === TIMING: Capture "thinking" state (LLM processing) ===
            if event.new_state == "thinking" and event.old_state != "thinking":
                logger.info(f"🤖 [DEBUG] Agent started THINKING (LLM processing)")
                # Record T2 - agent enters thinking state (LLM request begins)
                if turn_timing["agent_start_thinking"] is None:
                    turn_timing["agent_start_thinking"] = time.perf_counter()
                    logger.info(f"⏱️ [TIMING] T2: Agent started thinking at {turn_timing['agent_start_thinking']:.3f}")
            
            if event.old_state != "speaking" and event.new_state == "speaking":
                logger.info("🤖 [DEBUG] Agent started speaking")
                # === TIMING: Record T5 - agent started speaking and log summary ===
                turn_timing["agent_start_speaking"] = time.perf_counter()
                logger.info(f"⏱️ [TIMING] T5: Agent started speaking at {turn_timing['agent_start_speaking']:.3f}")
                log_turn_timing_summary()
                # Send signal to frontend for timing
                try:
                    asyncio.create_task(
                        ctx.room.local_participant.publish_data(
                            b"started",
                            reliable=True,
                            topic="agent_voice_started"
                        )
                    )
                    logger.debug("📤 Published agent_voice_started signal")
                except Exception as e:
                    logger.error(f"❌ Error publishing voice started signal: {e}")
            elif event.old_state == "speaking" and event.new_state != "speaking":
                logger.info(f"🤖 [DEBUG] Agent stopped speaking (now {event.new_state})")
                # Reset interrupt timer when agent finishes speaking (Mode 2)
                nonlocal last_user_interaction_time
                last_user_interaction_time = asyncio.get_event_loop().time()
                logger.debug(f"⏰ [MODE 2] Timer reset - agent finished speaking")
            elif event.new_state != "thinking":  # Don't double-log thinking state
                logger.info(f"🤖 [DEBUG] Agent state: {event.old_state} → {event.new_state}")
        
        @session.on("speech_created")
        def on_speech_created(event):
            logger.info(f"🤖 [SPEECH] Speech created - source: {event.source}, user_initiated: {event.user_initiated}")
            # === TIMING: Record T3 - TTS synthesis started ===
            # Only record first speech_created per turn (avoid overwrites from retries)
            if turn_timing["speech_created"] is None:
                turn_timing["speech_created"] = time.perf_counter()
                logger.info(f"⏱️ [TIMING] T3: TTS speech created at {turn_timing['speech_created']:.3f}")
        
        @session.on("metrics_collected")
        def on_metrics_collected(event):
            """Handle metrics from LiveKit - captures actual LLM TTFT and TTS TTFB"""
            from livekit.agents.metrics import LLMMetrics, TTSMetrics
            
            metrics = event.metrics
            
            # === LLM METRICS: Capture Time-To-First-Token ===
            if isinstance(metrics, LLMMetrics):
                # Record T2.5 - LLM first token (only once per turn)
                if turn_timing["llm_first_token"] is None and metrics.ttft > 0:
                    # Calculate when first token actually arrived
                    first_token_time = time.perf_counter() - metrics.duration + metrics.ttft
                    turn_timing["llm_first_token"] = first_token_time
                    logger.info(f"⏱️ [TIMING] T2.5: LLM first token at {first_token_time:.3f} (TTFT: {metrics.ttft:.3f}s)")
                
                logger.info(f"📊 [LLM METRICS] TTFT: {metrics.ttft:.3f}s, Duration: {metrics.duration:.3f}s, "
                          f"Tokens: {metrics.prompt_tokens}→{metrics.completion_tokens}, "
                          f"Speed: {metrics.tokens_per_second:.1f} tok/s")
            
            # === TTS METRICS: Capture Time-To-First-Byte ===
            elif isinstance(metrics, TTSMetrics):
                logger.info(f"📊 [TTS METRICS] TTFB: {metrics.ttfb:.3f}s, Duration: {metrics.duration:.3f}s, "
                          f"Audio: {metrics.audio_duration:.2f}s, Characters: {metrics.characters_count}")
        
        @session.on("conversation_item_added")
        def on_conversation_item_added(event):
            nonlocal conversation_history
            
            # Log conversation items (ChatMessage objects)
            item = event.item
            
            # Try to get text from the item using different methods
            text = None
            
            # Method 1: Check for text_content property (most common)
            if hasattr(item, 'text_content'):
                text = item.text_content
            # Method 2: Check for content list
            elif hasattr(item, 'content') and item.content:
                text_parts = []
                for content_block in item.content:
                    if hasattr(content_block, 'text') and content_block.text:
                        text_parts.append(content_block.text)
                text = ''.join(text_parts) if text_parts else None
            
            # Log and publish based on role
            if text and hasattr(item, 'role'):
                # Store in conversation history for interrupt generation
                conversation_history.append({"role": item.role, "content": text})
                # Keep last 10 messages only
                if len(conversation_history) > 10:
                    conversation_history = conversation_history[-10:]
                
                if item.role == "assistant":
                    logger.info(f"🤖 [LLM] Response: {text}")
                    # === TIMING: Record T4 - LLM full response logged ===
                    turn_timing["llm_response_logged"] = time.perf_counter()
                    logger.info(f"⏱️ [TIMING] T4: LLM response complete at {turn_timing['llm_response_logged']:.3f}")
                    
                    # Publish agent response to room for frontend display
                    try:
                        trimmed_text = text.strip()
                        asyncio.create_task(
                            ctx.room.local_participant.publish_data(
                                trimmed_text.encode('utf-8'),
                                reliable=True,
                                topic="agent_response"
                            )
                        )
                        logger.debug(f"📤 Published agent response to room")
                    except Exception as e:
                        logger.error(f"❌ Error publishing agent response: {e}")
                elif item.role == "user":
                    logger.info(f"👤 [USER] Message added to context: {text}")
                    # Note: User transcripts are already published in user_input_transcribed event
        
        @session.on("function_tools_executed")
        def on_function_tools_executed(event):
            for func_call, func_output in event.zipped():
                logger.info(f"🔧 [TOOL] Executed: {func_call.name}")
                if func_output and func_output.result:
                    logger.info(f"🔧 [TOOL] Result: {func_output.result}")
        
        # Log any errors
        @session.on("error")
        def on_error(event):
            logger.error(f"❌ [ERROR] Session error: {event.error}")
        
        # Log session close events
        @session.on("close")
        def on_close(event):
            logger.info(f"🔌 [SESSION] Session closed - reason: {event.reason}")
            if event.error:
                logger.error(f"❌ [SESSION] Close error: {event.error}")
        
        logger.info("✅ Starting voice agent session...")
        logger.info(f"🔍 [DEBUG] Room participants: {len(ctx.room.remote_participants)}")
        
        
        
        # Configure room options to keep session alive on disconnect/reconnect
        room_options = room_io.RoomOptions(
            audio_input=True,
            audio_output=True,
            text_output=True,
            close_on_disconnect=False,  # Keep session alive when user disconnects
        )
        
        # Start the session with the agent - this runs the session in background tasks
        await session.start(bridge, room=ctx.room, room_options=room_options)
        
        logger.info("✅ Session started successfully!")
        logger.info(f"🔍 [DEBUG] Session state - agent: {session.agent_state}, user: {session.user_state}")
        
        # Log search configuration
        search_status = "ENABLED" if config.ENABLE_POLICY_SEARCH else "DISABLED"
        if config.ENABLE_POLICY_SEARCH:
            backend = config.POLICY_SEARCH_BACKEND
            model_info = ""
            if backend == "perplexity":
                model_info = f" (model={config.PERPLEXITY_SEARCH_MODEL})"
            logger.info(f"🔍 [SEARCH] Policy web search: {search_status}, backend={backend}{model_info}")
        else:
            logger.info(f"🔍 [SEARCH] Policy web search: {search_status}")
        
        logger.info("🎤 Agent is ready and will respond when you speak...")
        
        
        # Mode 2: Auto-Interrupt Mode - start background task to check for interrupts
        async def interrupt_checker():
            """Background task to check if agent should interrupt"""
            nonlocal last_user_interaction_time
            
            
            # Send debug message to frontend
            try:
                await ctx.room.local_participant.publish_data(
                    "Interrupt checker started".encode('utf-8'),
                    reliable=True,
                    topic="interrupt_debug"
                )
            except Exception as e:
                logger.error(f"❌ Error publishing interrupt debug: {e}")
            
            while True:
                try:
                    await asyncio.sleep(5)  # Check every 5 seconds
                    
                    current_time = asyncio.get_event_loop().time()
                    time_since_last_interaction = current_time - last_user_interaction_time
                    
                    # Send periodic status to frontend
                    status_msg = f"Interrupt check: {time_since_last_interaction:.1f}s / {config.INTERRUPT_INTERVAL_SECONDS}s | Agent: {session.agent_state}"
                    try:
                        await ctx.room.local_participant.publish_data(
                            status_msg.encode('utf-8'),
                            reliable=True,
                            topic="interrupt_debug"
                        )
                    except Exception as e:
                        logger.error(f"❌ Error publishing status: {e}")
                    
                    # If enough time has passed and agent isn't already speaking
                    if time_since_last_interaction >= config.INTERRUPT_INTERVAL_SECONDS:
                        if session.agent_state != "speaking":
                            logger.info(f"⏰ [MODE 2] Auto-interrupt triggered after {time_since_last_interaction:.1f}s")
                            
                            # Send interrupt trigger notification to frontend
                            try:
                                await ctx.room.local_participant.publish_data(
                                    f"INTERRUPT TRIGGERED after {time_since_last_interaction:.1f}s".encode('utf-8'),
                                    reliable=True,
                                    topic="interrupt_debug"
                                )
                            except Exception as e:
                                logger.error(f"❌ Error publishing interrupt trigger: {e}")
                            
                            # Generate LLM interrupt using LiveKit's ChatChunk structure
                            try:
                                # Build conversation history summary
                                history_summary = ""
                                if conversation_history and len(conversation_history) > 0:
                                    recent_messages = conversation_history[-6:]
                                    history_summary = "Recent conversation:\n"
                                    for msg in recent_messages:
                                        role_label = "User" if msg["role"] == "user" else "You"
                                        history_summary += f"{role_label}: {msg['content']}\n"
                                
                                # Create interrupt generation prompt
                                interrupt_prompt = f"""You are Donald Trump. Analyze the conversation below and decide how to jump back in.

{history_summary if history_summary else "No conversation history yet - this is the start of the conversation."}

ANALYZE THE CONVERSATION:
- Was it "bland"? (just greetings like "hello", "hi", "how are you", small talk, nothing substantive)
- Or was it substantive? (discussing real topics, policies, opinions, etc.)

YOUR RESPONSE STRATEGY:

IF BLAND/GREETINGS ONLY:
Pick a random hot topic (economy, trade, immigration, China, energy, taxes, jobs, military, media, elections, etc.) and:
1. Share YOUR bold opinion on it in Trump's style
2. Then ask the user what THEY think about it
Example: "You know what's been on my mind? [topic]. Here's my take: [opinion]. What do you think about that?"

IF SUBSTANTIVE:
Continue the existing topic naturally - add a follow-up thought, ask a deeper question, or share a related opinion.

RULES:
- 1-3 sentences maximum
- Pure Trump speaking style - confident, bold, entertaining
- Don't mention you're "interrupting" or apologize
- Just jump in naturally

Your response:"""

                                logger.info(f"🔍 [MODE 2] Calling LLM for interrupt generation")

                                # Call LLM via PigAgent stream
                                from core.llm.types import Message as PigMessage
                                messages = [PigMessage.user(interrupt_prompt)]
                                interrupt_msg = ""
                                async for text in pig_agent.stream(messages):
                                    interrupt_msg += text
                                
                                interrupt_msg = interrupt_msg.strip()
                                logger.info(f"🔍 [MODE 2] LLM generated interrupt: '{interrupt_msg[:100]}{'...' if len(interrupt_msg) > 100 else ''}'")
                                
                                if interrupt_msg:
                                    
                                    await ctx.room.local_participant.publish_data(
                                        f"LLM Interrupt: {interrupt_msg}".encode('utf-8'),
                                        reliable=True,
                                        topic="interrupt_debug"
                                    )
                                    
                                    # session.say() returns SpeechHandle directly (not a coroutine)
                                    session.say(interrupt_msg, allow_interruptions=True)
                                    logger.info(f"🤖 [MODE 2] Agent interrupting with LLM: {interrupt_msg[:100]}")
                                else:
                                    logger.warning(f"⚠️ [MODE 2] LLM returned empty")
                                
                            except Exception as e:
                                logger.error(f"❌ [MODE 2] Error generating interrupt: {e}")
                            
                            # Reset timer after interrupt
                            last_user_interaction_time = current_time
                        else:
                            logger.debug(f"⏰ [MODE 2] Skip interrupt - agent already speaking")
                            try:
                                await ctx.room.local_participant.publish_data(
                                    "Skip interrupt - agent already speaking".encode('utf-8'),
                                    reliable=True,
                                    topic="interrupt_debug"
                                )
                            except Exception as e:
                                logger.error(f"❌ Error publishing skip notice: {e}")
                
                except asyncio.CancelledError:
                    logger.info("⏰ [MODE 2] Interrupt checker stopped")
                    break
                except Exception as e:
                    logger.error(f"❌ [MODE 2] Error in interrupt checker: {e}")
        
        # Start interrupt checker if in Mode 2
        if config.AGENT_MODE == 2:
            interrupt_task = asyncio.create_task(interrupt_checker())
            logger.info(f"⏰ [MODE 2] Auto-interrupt enabled (every {config.INTERRUPT_INTERVAL_SECONDS}s)")

        # Keep the job alive - the session runs in background tasks
        try:
            await asyncio.Event().wait()
        finally:
            if interrupt_task:
                interrupt_task.cancel()
                try:
                    await interrupt_task
                except asyncio.CancelledError:
                    pass
        
    # Keep agent running
    logger.info("Agent session started. Press Ctrl+C to stop.")


if __name__ == "__main__":
    # Print banner
    stt_provider = config.STT_PROVIDER.lower()
    if stt_provider == "deepgram":
        stt_display = f"Deepgram {config.DEEPGRAM_STT_MODEL}"
    else:
        stt_display = f"Cartesia {config.CARTESIA_STT_MODEL}"
    
    # Determine LLM model for banner
    llm_provider = config.LLM_PROVIDER.lower()
    if llm_provider == "grok" or llm_provider == "xai":
        llm_model_display = f"{config.GROK_MODEL} (Provider: {config.LLM_PROVIDER})"
    else:
        llm_model_display = f"{config.QWEN_MODEL} (Provider: {config.LLM_PROVIDER})"
    
    logger.info("=" * 70)
    logger.info("🤖 AI Voice Agent - Modular Architecture")
    logger.info("=" * 70)
    logger.info(f"LiveKit URL: {config.LIVEKIT_URL}")
    logger.info(f"STT: {stt_display}")
    logger.info(f"LLM: {llm_model_display}")
    logger.info(f"TTS: Cartesia {config.CARTESIA_TTS_MODEL}")
    logger.info(f"Workers: {config.AGENT_WORKERS}")
    logger.info("=" * 70)
    
    # Validate configuration
    if not validate_configuration():
        logger.error("Please fix the configuration errors and try again.")
        exit(1)
    
    logger.info("✅ Configuration validated successfully")
    logger.info("🚀 Starting agent workers...")
    
    # Run the agent
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
            ws_url=config.LIVEKIT_URL,
        )
    )