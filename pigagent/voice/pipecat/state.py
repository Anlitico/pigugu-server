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
        # perf_counter at which client_is_speaking last went True (set by the
        # TTS bridge). The STT bridge uses it to blank the head of a reply,
        # where the AEC has not converged yet and the residual echo is worst.
        self.speaking_started_pc: float = 0.0
        # Server-side Silero VAD verdict, held briefly by the VAD bridge so a
        # dip between voiced chunks cannot flap it. The STT bridge reads it to
        # tell the assistant's own echoed reply from a real user: residual echo
        # never clears the VAD's energy gate, so this stays False for the whole
        # reply, while real user speech clears it within ~0.2s.
        self.vad_voice_active: bool = False
        # Whether a VAD is wired at all. Without one the verdict above can
        # never light up, so the echo gate must stay inert -- otherwise a
        # session built without a VAD would silently lose barge-in entirely.
        self.vad_wired: bool = False
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
        # to PREPEND it to the wake turn's text. The firmware does not stream
        # the wake-word audio (CONFIG_SEND_WAKE_WORD_DATA is off), so the
        # transcript never contains it — see PiguguAgentGateway.
        self.wake_word: str = ""
        # User text held back from a turn whose reply never voiced, until a turn
        # that does speak picks it up. A replyless turn is silent for the user
        # (the reply was killed before any audio), so its words belong with the
        # next turn's input rather than standing alone: the LLM then answers the
        # whole utterance at once, and the history holds one user message
        # instead of a run of fragments with no reply between them.
        self.pending_user_text: str = ""
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
