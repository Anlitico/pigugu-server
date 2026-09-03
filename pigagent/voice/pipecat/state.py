"""Shared per-session turn state, read/written by the pipeline processors.

The STT bridge needs to know whether the assistant is speaking (barge-in gate),
the TTS bridge owns the interrupt event, the TurnStorage observer hands the
per-turn record to the TTS bridge for finalization, and telemetry marks need a
stable sentence id for tts_played validation — without one shared object each
half of the pipeline would carry a private copy and they would drift apart.
"""

from __future__ import annotations

import asyncio
from typing import Any


class PiguguTurnState:
    def __init__(self):
        self.interrupt_event = asyncio.Event()
        self.client_is_speaking = False
        # Next sentence id (incremented per turn); the TTS bridge sets
        # ``current_sentence_id`` to the one actually playing so a late
        # device tts_played ack can be validated against the right turn.
        self.sentence_id = 0
        self.current_sentence_id = 0
        # TurnStorage under construction: the observer builds + fills it at
        # turn end (user PCM + window), the TTS bridge finalizes + commits it.
        self.turn_storage: Any | None = None
        # Frozen user PCM for the current turn (observer → storage).
        self.user_pcm: bytes = b""
        # Wall-clock ms the current user-audio window began (observer).
        self.audio_start_ms: int = 0
        # Device-reported first-packet→first-DAC latency (from tts_played).
        self.device_playback_ms: int = 0
        # Thread-safe interim transcript buffer, recorded by the STT bridge
        # and drained into TurnStorage by mark_stt_final.
        self.interims: Any | None = None
        # Reconstructed vad_end + server-received-vad perf_counter values from
        # the device vad_silence ack (VadBridge stores the raw values; the
        # observer applies them to the CORRECT turn dict at turn end, since
        # cross-processor contextvars are isolated and async ordering between
        # the vad bridge and the observer cannot be assumed).
        self.vad_end_mark: float | None = None
        self.server_received_vad_at: float | None = None
        # Parsed from the hello message (input transport); used for lazy
        # PigAgent creation and persona routing.
        self.hw_id: str = ""
        self.persona_id: int = 1
        # Turn classification for the current/next turn (the vad bridge sets
        # it to "wake_word" on listen/detect; reset after the turn).
        self.turn_type: str = "follow_up"
        # The wake word the device detected (from listen/detect "text"), used
        # to strip it from the first turn's transcript before it reaches the
        # LLM (the firmware streams the wake-word audio to the STT, so the
        # transcript starts with e.g. "Alexa? ...").
        self.wake_word: str = ""
        # The live TelemetryCollector turn dict for the current turn. Pipecat
        # runs each FrameProcessor in its own asyncio task with an isolated
        # contextvars copy, so marks set in one processor are invisible to the
        # others. We share the dict explicitly here and re-bind it per
        # processor (see telemetry.ensure_turn_context).
        self.active_turn: Any | None = None
        # Connection pre-roll timestamps (perf_counter, 0 = unset) for the
        # per-session connect_pre_roll metric (metrics.session): when the
        # server accepted / parsed hello / saw the first audio frame.
        self.accept_pc: float = 0.0
        self.hello_pc: float = 0.0
        self.first_audio_pc: float = 0.0
