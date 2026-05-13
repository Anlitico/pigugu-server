# agent/main.py
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
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    StopResponse,
    cli,
    function_tool,
)
from livekit.agents.voice import room_io
from livekit import rtc
from livekit.plugins import silero

# Import our modular components
from config import get_config
from core.llm.types import Message, ModelCapability
from core.llm.registry import ModelRegistry
from core.search.adapter import build_search_messages
from utils import SpeakerTracker, ResponseStrategy
from personas import PersonaRegistry, get_persona, GROUP_DISCUSSION_PROMPT
from roasts import GameModeRegistry, get_game_mode
from components.factory import create_agent_components, validate_configuration
from lifecycle import ConversationManager, PersistenceProvider
from models import ConversationState, NewsContext
from memory import ShortTermMemory
from context import ContextAssembler, MoodProvider

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


# Filler phrases are now provided by Persona. During migration, the
# TrumpAgent still references TRUMP_FILLERS imported from personas.trump
# for backward compat. New code should use persona.get_filler().
from personas.trump import TRUMP_FILLERS


# Import perplexity search for tool-based web search (only used when POLICY_SEARCH_BACKEND = "perplexity")
from core.search.perplexity import web_search as perplexity_web_search


class TrumpAgent(Agent):
    """
    Custom Agent that adds speaker attribution and response gating for Mode 3.
    
    In Mode 3, user messages are prefixed with [Speaker X]: and the agent
    decides whether to respond based on talk-show guest heuristics.
    """
    
    def __init__(self, pig_agent, *, speaker_tracker: SpeakerTracker, agent_mode: int,
                 agent_config=None, game_mode=None, persona=None, conv_manager=None,
                 search_adapter=None, **kwargs):
        super().__init__(**kwargs)
        self._pig_agent = pig_agent         # 我们自己的 Agent 引擎
        self._speaker_tracker = speaker_tracker
        self._agent_mode = agent_mode
        self._config = agent_config
        self._pending_filler = None
        self._filler_yielded_at = None
        self._use_search = False
        self._use_perplexity_tool = False
        # 供 search adapter 使用
        self._search_adapter = search_adapter
        self._llm_provider = self._pig_agent.provider.provider_name if hasattr(self._pig_agent, 'provider') else ""
        self._llm_model = self._pig_agent.config.model if hasattr(self._pig_agent, 'config') else ""
        # Persona + Game Mode + Lifecycle
        self._game_mode = game_mode
        self._persona = persona
        self._conv_manager = conv_manager
    
    @function_tool(description="Search the web for current information using Perplexity")
    async def web_search(self, query: str) -> str:
        """
        Tool-based web search using Perplexity API.
        Only available when POLICY_SEARCH_BACKEND is set to "perplexity".
        
        Args:
            query: The search query to execute
            
        Returns:
            JSON string with search results including content and citations
        """
        if not self._config or self._config.POLICY_SEARCH_BACKEND != "perplexity":
            logger.warning("🔍 [PERPLEXITY] web_search tool called but backend is not perplexity")
            return '{"error": "Perplexity search not enabled"}'
        
        try:
            result = await perplexity_web_search(
                query=query,
                model=self._config.PERPLEXITY_SEARCH_MODEL if self._config else "sonar-pro",
                base_url=self._config.PERPLEXITY_SEARCH_BASE_URL if self._config else None,
            )
            return json.dumps(result)
        except Exception as e:
            logger.error(f"🔍 [PERPLEXITY] web_search tool failed: {e}")
            return json.dumps({"error": str(e)})
    
    async def on_user_turn_completed(self, turn_ctx, new_message):
        """
        Called when the user's turn has ended, before the agent's reply.
        
        Mode 3: Adds [Speaker X]: prefix AND gates whether the agent should
        respond at all (raise StopResponse to suppress). Behaves like a
        talk-show side guest -- only speaks when appropriate.
        
        All modes: If filler words are enabled, picks a Trump-style filler
        and injects context so the LLM continues from it.
        """
        # --- Mode 3: speaker attribution + response gating ---
        if self._agent_mode == 3:
            last_speaker = self._speaker_tracker.get_last_speaker()
            original_text = new_message.text_content or ""
            if last_speaker is not None and original_text:
                new_message.content = [f"[Speaker {last_speaker}]: {original_text}"]
                logger.debug(f"📊 [MODE 3] Speaker attribution: [Speaker {last_speaker}]")
            
            text_lower = original_text.lower()
            
            cooldown = self._config.GROUP_RESPONSE_COOLDOWN_SECONDS if self._config else 15.0
            min_turns = self._config.GROUP_MIN_TURNS_BEFORE_RESPONSE if self._config else 4
            rapid_threshold = self._config.GROUP_RAPID_EXCHANGE_THRESHOLD if self._config else 3.0
            
            kw_str = self._config.DIRECT_ADDRESS_KEYWORDS if self._config else "Trump,president,Donald,you,what do you think"
            direct_keywords = [k.strip().lower() for k in kw_str.split(",")]
            
            is_direct = any(kw in text_lower for kw in direct_keywords)
            if is_direct:
                logger.info(f"🎯 [MODE 3] Direct address detected -- responding")
            else:
                recent_turns = self._speaker_tracker.get_recent_turns(count=40)
                now = time.time()
                
                for turn in reversed(recent_turns):
                    if turn.is_agent:
                        if (now - turn.timestamp) < cooldown:
                            logger.info(f"🔇 [MODE 3] Suppressed -- cooldown ({now - turn.timestamp:.1f}s < {cooldown}s)")
                            raise StopResponse()
                        break
                
                segments = []
                for turn in recent_turns:
                    key = "agent" if turn.is_agent else turn.speaker_id
                    if segments and segments[-1][0] == key:
                        segments[-1] = (key, turn.timestamp)
                    else:
                        segments.append((key, turn.timestamp))
                
                user_segments = [(k, ts) for k, ts in segments if k != "agent"]
                if len(user_segments) >= 2:
                    prev_speaker, prev_ts = user_segments[-2]
                    curr_speaker, curr_ts = user_segments[-1]
                    gap = curr_ts - prev_ts
                    if prev_speaker != curr_speaker and gap < rapid_threshold:
                        logger.info(f"🔇 [MODE 3] Suppressed -- rapid exchange between speakers (gap {gap:.1f}s < {rapid_threshold}s)")
                        raise StopResponse()
                
                segments_since_agent = 0
                for key, _ in reversed(segments):
                    if key == "agent":
                        break
                    segments_since_agent += 1
                
                if segments_since_agent < min_turns:
                    logger.info(f"🔇 [MODE 3] Suppressed -- only {segments_since_agent}/{min_turns} speaker segments since last response")
                    raise StopResponse()
                
                logger.info(f"🎯 [MODE 3] Allowing response ({segments_since_agent} speaker segments since last, gap OK)")
        
        # --- Filler words (all modes, skip short messages like greetings) ---
        user_text = (new_message.text_content or "").strip()
        if self._config and self._config.ENABLE_FILLER_WORDS and len(user_text.split()) > 5:
            filler = random.choice(TRUMP_FILLERS)
            self._pending_filler = filler
            # Modify turn_ctx to cancel preemptive generation so llm_node
            # runs fresh with the filler set.
            turn_ctx.add_message(
                role="system",
                content=f'You already began your reply with: "{filler}". Continue from there. Do NOT repeat it.',
            )
            logger.info(f"💬 [FILLER] Queued filler: \"{filler}\"")
        
        # --- Policy search: let the model decide when to search ---
        self._use_search = bool(self._config and self._config.ENABLE_POLICY_SEARCH)
        self._use_perplexity_tool = (
            self._use_search and
            self._config and
            self._config.POLICY_SEARCH_BACKEND == "perplexity"
        )
        if self._use_search:
            backend = "perplexity" if self._use_perplexity_tool else "built_in"
            logger.info(f"🔍 [SEARCH] Policy search enabled, backend={backend}")

        # --- Lifecycle: delegate to ConversationManager ---
        user_text = (new_message.text_content or "").strip()
        if self._conv_manager:
            lifecycle_result = await self._conv_manager.on_user_turn_completed(user_text)
            if lifecycle_result:
                # Inject ending review tone if triggered
                if lifecycle_result.get("ending_triggered"):
                    review_tone = lifecycle_result.get("review_tone", "")
                    if review_tone:
                        turn_ctx.add_message(role="system", content=review_tone)
                        logger.info("📖 [LIFECYCLE] Review tone injected into context")
                    ending_line = lifecycle_result.get("ending_line", "")
                    if ending_line:
                        logger.info(f"🏁 [LIFECYCLE] Ending line: {ending_line[:80]}...")

                # Inject mode-specific context if any
                if lifecycle_result.get("mode_context"):
                    turn_ctx.add_message(
                        role="system", content=lifecycle_result["mode_context"]
                    )
    
    async def _search_llm_stream(self, chat_ctx):
        """Stream LLM response with web search enabled (bypasses LiveKit plugin).

        For Qwen: uses enable_search=True via extra_body on the Chat Completions API.
        For Grok: uses the Responses API with web_search tool.
        """
        messages = build_search_messages(chat_ctx.items)
        has_system = any(msg["role"] == "system" for msg in messages)
        force_search = bool(self._config and self._config.FORCE_POLICY_SEARCH)
        adapter_name = type(self._search_adapter).__name__ if self._search_adapter else "None"

        provider = self._pig_agent.provider

        search_model = self._config.resolve_model() if self._config else "unknown"
        logger.info(
            f"🔍 [SEARCH] Sending {len(messages)} messages to {search_model} "
            f"(adapter={adapter_name}, has_system={has_system}, force_search={force_search})"
        )
        for i, m in enumerate(messages):
            preview = m["content"][:80] if m["content"] else "(empty)"
            logger.info(f"🔍 [SEARCH]   msg[{i}] role={m['role']} content={preview!r}")

        try:
            if not self._search_adapter:
                raise RuntimeError(f"No search adapter configured for provider={search_model}")

            async for chunk in self._search_adapter.stream_with_search(
                messages=messages,
                model=search_model,
                api_key=provider._api_key,
                base_url=provider.base_url,
                temperature=self._config.LLM_TEMPERATURE if self._config else 0.6,
                force_search=force_search,
            ):
                yield chunk
        except Exception as e:
            logger.error(f"🔍 [SEARCH] Search API error: {e}")
            raise
    
    async def llm_node(self, chat_ctx, tools, model_settings):
        """Override: 用自己的 PigAgent 引擎，不再走 LiveKit 的 LLM 管道。

        Search 路径保持不变：
        - built_in: _search_llm_stream (provider-native search)
        - perplexity: 暂留 LiveKit tool loop（后续迁移到 PigAgent tool system）
        """
        # ── Dynamic context assembly ──────────────────────────────────
        if self._conv_manager:
            await self._conv_manager.assemble_context(
                chat_ctx, provider=self._llm_provider
            )

        filler = self._pending_filler
        self._pending_filler = None
        use_search = self._use_search
        use_perplexity_tool = self._use_perplexity_tool
        self._use_search = False
        self._use_perplexity_tool = False

        if use_search:
            if use_perplexity_tool:
                logger.info("🔍 [SEARCH] Using Perplexity tool-based search")
            else:
                logger.info(f"🔍 [SEARCH] Using built-in search ({self._llm_provider})")

        def _get_llm_gen():
            if use_search and not use_perplexity_tool:
                return self._search_llm_stream(chat_ctx)

            if use_perplexity_tool:
                # Perplexity tool search — 暂留 LiveKit tool loop
                return Agent.default.llm_node(self, chat_ctx, tools, model_settings)

            # Normal path: 委托给 PigAgent
            messages = build_search_messages(chat_ctx.items)
            wrapped = [Message(role=m["role"], content=m["content"]) for m in messages]

            async def _pig_agent_stream():
                try:
                    async for text in self._pig_agent.run(wrapped):
                        yield text
                except Exception as e:
                    logger.error(f"[PigAgent] run failed: {e}")
                    raise

            return _pig_agent_stream()

        if filler:
            self._filler_yielded_at = time.perf_counter()
            logger.info(f"⏱️ [TIMING] Filler yielded to TTS at {self._filler_yielded_at:.3f}")
            yield filler + " "

            chat_ctx.add_message(role="assistant", content=filler)

            queue = asyncio.Queue()

            async def _buffer_llm():
                try:
                    async for chunk in _get_llm_gen():
                        await queue.put(chunk)
                except Exception as e:
                    logger.error(f"[LLM] Stream failed: {e}")
                finally:
                    await queue.put(None)

            llm_task = asyncio.create_task(_buffer_llm())

            await asyncio.sleep(3.0)

            llm_task = asyncio.create_task(_buffer_llm())

            await asyncio.sleep(3.0)

            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk

            await llm_task
        else:
            async for chunk in _get_llm_gen():
                yield chunk


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
    
    # Initialize speaker tracker
    speaker_tracker = SpeakerTracker(active_window_seconds=60.0)
    
    # Initialize response strategy
    direct_keywords = [k.strip() for k in config.DIRECT_ADDRESS_KEYWORDS.split(",")]
    response_strategy = ResponseStrategy(
        enabled=config.ENABLE_SMART_RESPONSE,
        group_silence_threshold=config.GROUP_RESPONSE_SILENCE_THRESHOLD,
        direct_address_keywords=direct_keywords
    )
    
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
    mode_id = metadata.get("mode", "roast") if metadata else "roast"

    persona = get_persona(persona_id)
    game_mode = get_game_mode(mode_id)

    logger.info(f"🎭 Persona: {persona.persona_id} ({persona.display_name}, domain={persona.domain})")
    logger.info(f"🎮 Game Mode: {game_mode.mode_id} ({game_mode.display_name})")

    # ── Init conversation lifecycle ────────────────────────────────────
    news_context = None
    if metadata:
        news_context = NewsContext(
            news_id=metadata.get("news_id", ""),
            title=metadata.get("news_title", ""),
            summary=metadata.get("news_summary", ""),
            source=metadata.get("news_source", ""),
            domain=metadata.get("news_domain", persona.domain),
            mode=game_mode.mode_id,
            persona=persona.persona_id,
        )
    # ── Memory + Context + Persistence ─────────────────────────────────
    memory_store = ShortTermMemory()
    context_assembler = ContextAssembler()
    mood_provider = MoodProvider()

    # Persistence: PG + Redis when available, console fallback otherwise
    device_id = metadata.get("device_id", "") if metadata else ""
    persistence = PersistenceProvider(pg_pool=None, redis_client=None)

    conv_state = ConversationState(
        persona_id=persona.persona_id,
        mode_id=game_mode.mode_id,
        news=news_context,
        user_id=metadata.get("user_id", "") if metadata else "",
    )
    conv_manager = ConversationManager(
        state=conv_state,
        persona=persona,
        game_mode=game_mode,
        memory_store=memory_store,
        context_assembler=context_assembler,
        persistence=persistence,
        device_id=device_id,
    )
    logger.info(f"📋 ConversationManager initialized — session={conv_state.session_id[:8]}...")

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
    mode_desc = {1: "Default", 2: f"Interrupt (every {config.INTERRUPT_INTERVAL_SECONDS}s)", 3: "Group Discussion"}
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

    # LLM plugin not needed — TrumpAgent overrides llm_node() with PigAgent
    # We pass a dummy to satisfy LiveKit AgentSession type check (never called)
    from livekit.plugins import openai as _lk_openai
    _dummy_llm = _lk_openai.LLM(model="gpt-4.1", api_key="unused")

    logger.info(f"[DEBUG] STT plugin: {type(stt_plugin).__name__}")
    logger.info(f"[DEBUG] TTS plugin: {type(tts_plugin).__name__}")
    logger.info(f"[DEBUG] LLM: PigAgent with {pig_agent.config.model}")

    # Build VAD (Voice Activity Detection)
    vad = silero.VAD.load()

    # Build instructions from persona
    llm_provider_id = config.LLM_PROVIDER.lower()
    instructions = persona.get_full_prompt(llm_provider_id)
    logger.info(f"Using persona '{persona.persona_id}' prompt (provider={llm_provider_id})")

    # Add metadata context
    if metadata:
        instructions += "\n\nContext:\n"
        for key, value in metadata.items():
            instructions += f"- {key}: {value}\n"

    # Add Game Mode system prompt extension
    instructions += "\n\n" + game_mode.system_prompt_extension
    logger.info(f"Added game mode prompt: {game_mode.mode_id}")

    # Add Group Discussion prompt for Mode 3
    if config.AGENT_MODE == 3:
        gd_prompt = getattr(persona, 'group_discussion_prompt', GROUP_DISCUSSION_PROMPT)
        instructions += gd_prompt
        logger.info("Added Group Discussion prompt for Mode 3")

    # Mode 3 endpointing delays
    if config.AGENT_MODE == 3:
        min_ep = config.GROUP_MIN_ENDPOINTING_DELAY
        max_ep = config.GROUP_MAX_ENDPOINTING_DELAY
    else:
        min_ep = 0.5
        max_ep = 2.0

    agent_kwargs = {
        "instructions": instructions,
        "stt": stt_plugin,
        "llm": _dummy_llm,          # 不会被用 — llm_node() override 了
        "tts": tts_plugin,
        "vad": vad,
        "allow_interruptions": config.ENABLE_INTERRUPTIONS,
        "min_endpointing_delay": min_ep,
        "max_endpointing_delay": max_ep,
    }

    # Build search adapter
    search_adapter = None
    model_info = ModelRegistry.get(pig_agent.config.model)
    if ModelCapability.WEB_SEARCH in model_info.capabilities:
        from core.search.adapter import create_search_adapter
        search_adapter = create_search_adapter(llm_provider_id)
    else:
        # 没有 web search 能力的 provider 走 Perplexity 工具搜索
        logger.info("Provider doesn't support web_search, will use perplexity tool search")

    # Create TrumpAgent — our LiveKit adapter wrapping PigAgent
    agent = TrumpAgent(
        pig_agent=pig_agent,
        speaker_tracker=speaker_tracker,
        agent_mode=config.AGENT_MODE,
        agent_config=config,
        search_adapter=search_adapter,
        game_mode=game_mode,
        persona=persona,
        conv_manager=conv_manager,
        **agent_kwargs
    )

    # Create AgentSession (manages the runtime)
    session = AgentSession(
        preemptive_generation=config.ENABLE_PREEMPTIVE_SYNTHESIS,
    )

    logger.info("Agent and session created (VAD + PigAgent + LiveKit pipeline)")
        
        # Add comprehensive logging to debug the conversation pipeline
        @session.on("user_state_changed")
        def on_user_state_changed(event):
            if event.old_state != "speaking" and event.new_state == "speaking":
                logger.info("🎤 [DEBUG] User started speaking")
                # Reset timing for new turn when user starts speaking
                reset_turn_timing()
            elif event.old_state == "speaking" and event.new_state != "speaking":
                logger.info(f"🎤 [DEBUG] User stopped speaking (now {event.new_state})")
                # === TIMING: Record T0 - user stopped speaking ===
                turn_timing["user_stop_speaking"] = time.perf_counter()
                logger.info(f"⏱️ [TIMING] T0: User stopped speaking at {turn_timing['user_stop_speaking']:.3f}")
            else:
                logger.info(f"👤 [DEBUG] User state: {event.old_state} → {event.new_state}")
        
        @session.on("user_input_transcribed")
        def on_user_input_transcribed(event):
            nonlocal last_user_interaction_time
            
            # Extract speaker information if available (when diarization is enabled)
            speaker_info = ""
            speaker_id = None
            if hasattr(event, 'speaker_id') and event.speaker_id is not None:
                speaker_id = event.speaker_id
                speaker_info = f" (speaker: {speaker_id})"
            
            logger.info(f"👤 [STT] User transcribed: {event.transcript}{speaker_info}")
            logger.info(f"🔍 [DEBUG] is_final={event.is_final}, type={event.type}")
            logger.info(f"🔍 [DEBUG] Session agent_state: {session.agent_state}")
            logger.info(f"🔍 [DEBUG] Session user_state: {session.user_state}")
            
            # Track speaker if we have speaker_id (diarization enabled)
            if event.is_final and event.transcript and event.transcript.strip() and speaker_id is not None:
                speaker_tracker.track_utterance(
                    speaker_id=speaker_id,
                    text=event.transcript.strip()
                )
                # Log conversation mode periodically
                logger.info(speaker_tracker.get_conversation_mode_summary())
            
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
                    
                    # Send JSON payload with speaker_id for Mode 3 web client display
                    payload = {
                        "text": trimmed_transcript,
                        "speaker_id": speaker_id  # Will be None in Mode 1/2
                    }
                    
                    asyncio.create_task(
                        ctx.room.local_participant.publish_data(
                            json.dumps(payload).encode('utf-8'),
                            reliable=True,
                            topic="user_transcript"
                        )
                    )
                    logger.debug(f"📤 Published user transcript (real-time, speaker: {speaker_id})")
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
                # Capture filler yield timestamp from agent if available
                if hasattr(agent, '_filler_yielded_at') and agent._filler_yielded_at is not None:
                    turn_timing["filler_yielded"] = agent._filler_yielded_at
                    agent._filler_yielded_at = None
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
                    
                    # Track agent response in speaker tracker
                    speaker_tracker.track_agent_response(text)

                    # Lifecycle: notify ConversationManager of agent message
                    conv_manager.on_agent_message(text)

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
        logger.info(f"🔍 [DEBUG] Agent instructions length: {len(instructions)} chars")
        logger.info(f"🔍 [DEBUG] Room participants: {len(ctx.room.remote_participants)}")
        
        
        
        # Configure room options to keep session alive on disconnect/reconnect
        room_options = room_io.RoomOptions(
            audio_input=True,
            audio_output=True,
            text_output=True,
            close_on_disconnect=False,  # Keep session alive when user disconnects
        )
        
        # Start the session with the agent - this runs the session in background tasks
        await session.start(agent, room=ctx.room, room_options=room_options)
        
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

                                # Call LLM using PigAgent's provider
                                provider = pig_agent.provider
                                messages = [Message.user(interrupt_prompt)]
                                interrupt_msg = ""
                                async for delta in provider.chat_stream(messages, model=pig_agent.config.model):
                                    if delta.content:
                                        interrupt_msg += delta.content
                                
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
        
        # Mode 3: Group Discussion Mode - LLM decides whether to intervene
        async def should_intervene_group(last_utterance: str) -> tuple[bool, str]:
            """
            LLM decides if agent should speak in group discussion.
            Returns (should_speak, response_if_yes)
            """
            # Build context with speaker info
            active_speakers = speaker_tracker.get_active_speakers()
            is_group = speaker_tracker.is_group_conversation()
            recent_turns = speaker_tracker.get_recent_turns(count=8)
            
            # Build conversation history with speaker attribution
            history_lines = []
            for turn in recent_turns:
                if turn.is_agent:
                    history_lines.append(f"Trump (you): {turn.text}")
                else:
                    history_lines.append(f"Speaker {turn.speaker_id}: {turn.text}")
            history_text = "\n".join(history_lines) if history_lines else "No conversation yet."
            
            decision_prompt = f"""You are Donald Trump in a GROUP DISCUSSION with multiple people.

CURRENT SITUATION:
- Active speakers: {len(active_speakers)} people
- Conversation type: {"Group discussion" if is_group else "1-on-1"}
- Last utterance: "{last_utterance}"

RECENT CONVERSATION:
{history_text}

YOUR DECISION:
Analyze whether you should speak now or stay quiet. Consider:
1. Was something directed at you? (your name, "Trump", "president", "you", asking your opinion)
2. Is there a natural pause where you could add value?
3. Would jumping in disrupt an ongoing exchange between others?
4. Do you have something meaningful to contribute?

RESPOND IN THIS EXACT FORMAT:
DECISION: [SPEAK or QUIET]
REASON: [one short sentence why]
RESPONSE: [if SPEAK, your 1-3 sentence response in Trump style. If QUIET, write "none"]

Example if you should speak:
DECISION: SPEAK
REASON: They asked what I think about the economy.
RESPONSE: Let me tell you about the economy - we had the best numbers ever, believe me!

Example if you should stay quiet:
DECISION: QUIET
REASON: They're having a back-and-forth, I'd be interrupting.
RESPONSE: none"""

            try:
                # Use PigAgent's provider directly
                provider = pig_agent.provider
                messages = [Message.user(decision_prompt)]
                decision_text = ""
                async for delta in provider.chat_stream(messages, model=pig_agent.config.model):
                    if delta.content:
                        decision_text += delta.content
                
                decision_text = decision_text.strip()
                logger.info(f"🎯 [MODE 3] LLM decision: {decision_text[:150]}...")
                
                # Parse decision
                should_speak = "DECISION: SPEAK" in decision_text.upper()
                response = ""
                
                if should_speak and "RESPONSE:" in decision_text:
                    response_start = decision_text.find("RESPONSE:") + 9
                    response = decision_text[response_start:].strip()
                    if response.lower() == "none":
                        response = ""
                
                return (should_speak, response)
                
            except Exception as e:
                logger.error(f"❌ [MODE 3] Error in should_intervene_group: {e}")
                return (False, "")
        
        async def group_discussion_checker():
            """Background task for Mode 3 - periodic check if agent should intervene"""
            nonlocal last_user_interaction_time
            
            logger.info(f"🎯 [MODE 3] Group discussion mode active (check every {config.GROUP_MODE_SILENCE_CHECK_SECONDS}s)")
            
            while True:
                try:
                    await asyncio.sleep(config.GROUP_MODE_SILENCE_CHECK_SECONDS)
                    
                    current_time = asyncio.get_event_loop().time()
                    time_since_last = current_time - last_user_interaction_time
                    
                    # Only check if there's been some silence and agent isn't speaking
                    if time_since_last >= config.GROUP_MODE_SILENCE_CHECK_SECONDS:
                        if session.agent_state != "speaking":
                            logger.info(f"🎯 [MODE 3] Silence detected ({time_since_last:.1f}s), checking if should intervene...")
                            
                            # Get last utterance from history
                            last_utterance = ""
                            if conversation_history:
                                last_msg = conversation_history[-1]
                                if last_msg["role"] == "user":
                                    last_utterance = last_msg["content"]
                            
                            should_speak, response = await should_intervene_group(last_utterance)
                            
                            if should_speak and response:
                                session.say(response, allow_interruptions=True)
                                logger.info(f"🎯 [MODE 3] Agent intervening: {response[:100]}")
                                last_user_interaction_time = current_time
                            else:
                                logger.debug(f"🎯 [MODE 3] LLM decided to stay quiet")
                
                except asyncio.CancelledError:
                    logger.info("🎯 [MODE 3] Group discussion checker stopped")
                    break
                except Exception as e:
                    logger.error(f"❌ [MODE 3] Error in group discussion checker: {e}")
        
        # Start interrupt checker if in Mode 2
        group_mode_task = None
        if config.AGENT_MODE == 2:
            interrupt_task = asyncio.create_task(interrupt_checker())
            logger.info(f"⏰ [MODE 2] Auto-interrupt enabled (every {config.INTERRUPT_INTERVAL_SECONDS}s)")
        elif config.AGENT_MODE == 3:
            group_mode_task = asyncio.create_task(group_discussion_checker())
            logger.info(f"🎯 [MODE 3] Group discussion mode enabled (diarization: {config.DEEPGRAM_ENABLE_DIARIZATION})")
        
        # Keep the job alive - the session runs in background tasks
        # Wait forever (until cancelled by job shutdown)
        try:
            await asyncio.Event().wait()
        finally:
            # Clean up background tasks
            if interrupt_task:
                interrupt_task.cancel()
                try:
                    await interrupt_task
                except asyncio.CancelledError:
                    pass
            if group_mode_task:
                group_mode_task.cancel()
                try:
                    await group_mode_task
                except asyncio.CancelledError:
                    pass
        
    # Keep agent running
    logger.info("Agent session started. Press Ctrl+C to stop.")


async def handle_text_message(pig_agent, ctx: JobContext, message: str):
    """Handle text messages via PigAgent"""
    try:
        messages = [Message.user(message)]
        response = await pig_agent.run_once(messages)
        
        # Send response back
        await ctx.room.local_participant.publish_data(
            response.encode('utf-8'),
            reliable=True
        )
        logger.info(f"🤖 Agent: {response}")
        
    except Exception as e:
        logger.error(f"Error handling text message: {e}")


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