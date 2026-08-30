"""Tests for SileroVAD provider — double-threshold + sliding window logic.

These tests run WITHOUT the real ONNX model (headless / CI).
They verify the algorithmic behaviour using a mock that returns
configurable speech probabilities.
"""

from collections import deque

import numpy as np
import pytest


class FakeConn:
    """Minimal ConnectionHandler stub for VAD testing."""

    def __init__(self):
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_voice_window: deque[bool] = deque(maxlen=5)
        self.client_listen_mode: str = "auto"
        self.last_is_voice: bool = False
        self.vad_last_voice_time: float = 0.0
        self.session_id: str = "test"
        # VAD internal state
        self._vad_pcm_buffer: bytearray = bytearray()
        self._voice_window: deque[bool] = deque(maxlen=5)


class _TestableSileroVAD:
    """SileroVAD with a pluggable probability source for testing."""

    def __init__(
        self,
        threshold=0.5,
        threshold_low=0.2,
        min_silence_duration_ms=700,
        frame_window_size=5,
        frame_window_threshold=3,
    ):
        self.vad_threshold = threshold
        self.vad_threshold_low = threshold_low
        self.silence_threshold_ms = min_silence_duration_ms
        self.frame_window_size = frame_window_size
        self.frame_window_threshold = frame_window_threshold
        self._fake_probs: list[float] = []
        self._prob_index = 0

    def _next_prob(self) -> float:
        if self._prob_index < len(self._fake_probs):
            p = self._fake_probs[self._prob_index]
            self._prob_index += 1
            return p
        return 0.0  # silence fallback

    def set_probs(self, probs: list[float]) -> None:
        self._fake_probs = probs
        self._prob_index = 0

    def is_vad(self, conn: FakeConn, pcm_frame: bytes) -> bool:
        if conn.client_listen_mode == "manual":
            return True
        try:
            if not hasattr(conn, "_vad_pcm_buffer"):
                conn._vad_pcm_buffer = bytearray()
            if not hasattr(conn, "_voice_window"):
                conn._voice_window = deque(maxlen=self.frame_window_size)
            if not hasattr(conn, "last_is_voice"):
                conn.last_is_voice = False

            conn._vad_pcm_buffer.extend(pcm_frame)
            client_have_voice = getattr(conn, "client_have_voice", False)
            import time

            while len(conn._vad_pcm_buffer) >= 512 * 2:
                conn._vad_pcm_buffer = conn._vad_pcm_buffer[512 * 2 :]
                prob = self._next_prob()

                if prob >= self.vad_threshold:
                    is_voice = True
                elif prob <= self.vad_threshold_low:
                    is_voice = False
                else:
                    is_voice = conn.last_is_voice

                conn.last_is_voice = is_voice
                conn._voice_window.append(is_voice)
                prev_have = conn.client_have_voice
                client_have_voice = (
                    conn._voice_window.count(True) >= self.frame_window_threshold
                )

                if prev_have and not client_have_voice:
                    now_ms = time.time() * 1000
                    last = getattr(conn, "vad_last_voice_time", 0.0)
                    if now_ms - last >= self.silence_threshold_ms:
                        conn.client_voice_stop = True
                if client_have_voice:
                    conn.vad_last_voice_time = time.time() * 1000

                conn.client_have_voice = client_have_voice

            return client_have_voice
        except Exception:
            return True


# ── Helpers ───────────────────────────────────────────────────────────

def _pcm_chunk_512() -> bytes:
    """Return 512 * 2 bytes of pseudo-PCM (silence)."""
    return b"\x00" * 1024


# ── Tests ─────────────────────────────────────────────────────────────


class TestManualMode:
    def test_always_returns_true(self):
        vad = _TestableSileroVAD()
        conn = FakeConn()
        conn.client_listen_mode = "manual"
        assert vad.is_vad(conn, _pcm_chunk_512()) is True
        assert vad.is_vad(conn, b"") is True


class TestDoubleThreshold:
    """Speech starts > 0.5, stays until < 0.2."""

    def test_high_prob_triggers_voice(self):
        vad = _TestableSileroVAD(frame_window_threshold=1)
        conn = FakeConn()
        # 3 consecutive high probs to fill window
        vad.set_probs([0.9, 0.9, 0.9])
        result = vad.is_vad(conn, _pcm_chunk_512() * 3)
        assert result is True
        assert conn.client_have_voice is True

    def test_low_prob_below_threshold_ends_voice(self):
        vad = _TestableSileroVAD(frame_window_threshold=3)
        conn = FakeConn()
        # 3 high (voice on) then 4 low (voice off after window fills)
        vad.set_probs([0.9, 0.9, 0.9, 0.1, 0.1, 0.1, 0.1])
        result = vad.is_vad(conn, _pcm_chunk_512() * 7)
        assert result is False

    def test_mid_range_keeps_previous(self):
        """Prob between 0.2-0.5 keeps the previous state."""
        vad = _TestableSileroVAD(frame_window_threshold=1)
        conn = FakeConn()
        conn.last_is_voice = True
        vad.set_probs([0.35])  # between thresholds
        result = vad.is_vad(conn, _pcm_chunk_512())
        assert result is True  # kept previous (True)

    def test_mid_range_keeps_previous_false(self):
        vad = _TestableSileroVAD(frame_window_threshold=1)
        conn = FakeConn()
        conn.last_is_voice = False
        vad.set_probs([0.35])  # between thresholds
        result = vad.is_vad(conn, _pcm_chunk_512())
        assert result is False  # kept previous (False)


class TestSlidingWindow:
    """Need >= frame_window_threshold of window to confirm speech."""

    def test_insufficient_frames_not_voice(self):
        vad = _TestableSileroVAD(frame_window_threshold=3)
        conn = FakeConn()
        # Only 2 high, 3 low → window count True = 2 < 3
        vad.set_probs([0.9, 0.9, 0.1, 0.1, 0.1])
        result = vad.is_vad(conn, _pcm_chunk_512() * 5)
        assert result is False

    def test_sufficient_frames_is_voice(self):
        vad = _TestableSileroVAD(frame_window_threshold=3)
        conn = FakeConn()
        # 3 high, 2 low → window count True = 3 >= 3
        vad.set_probs([0.9, 0.9, 0.9, 0.1, 0.1])
        result = vad.is_vad(conn, _pcm_chunk_512() * 5)
        assert result is True


class TestSilenceTracking:
    """Voice stop is triggered when silence exceeds min_silence_duration_ms."""

    def test_voice_stop_on_sustained_silence(self):
        vad = _TestableSileroVAD(
            frame_window_threshold=3, min_silence_duration_ms=0
        )
        conn = FakeConn()
        # 3 high (voice on) then 4 low (fills window with False → voice off)
        vad.set_probs([0.9, 0.9, 0.9, 0.1, 0.1, 0.1, 0.1])
        result = vad.is_vad(conn, _pcm_chunk_512() * 7)
        assert result is False
        assert conn.client_voice_stop is True


class TestErrorFallback:
    def test_returns_true_on_unknown_error(self):
        """Any unhandled exception → return True (no false-timeout)."""
        vad = _TestableSileroVAD()
        conn = FakeConn()
        # Corrupt VAD state to trigger error
        conn._vad_pcm_buffer = None  # type: ignore
        result = vad.is_vad(conn, _pcm_chunk_512())
        assert result is True


class TestRealSileroAudioTime:
    """Phase 1 — the real onnx.py VAD with a mocked session: confirmation and
    stop use AUDIO time, plus an energy floor.

    The firmware flushes the wake-word + first utterance as a pre-buffered
    burst right after the ~6s connect, so a long voice stretch can process in
    <500ms of wall time. Wall-time confirmation never fires → the first
    utterance never ends and later speech is swallowed into the same turn
    (prod: turn 1 input_pcm_ms=33900 across two utterances).
    """

    @pytest.fixture()
    def vad(self):
        from providers.vad.onnx import SileroVAD

        vad = SileroVAD(
            min_speech_duration_ms=500, min_silence_duration_ms=700, min_volume=0.10
        )
        state = np.zeros((2, 1, 128), dtype=np.float32)
        holder = {"prob": 0.99}

        def fake_run(_none, ort_inputs):
            return (np.array([[holder["prob"]]], dtype=np.float32), state)

        vad.session.run = fake_run
        vad._prob_holder = holder
        return vad

    @staticmethod
    def _loud() -> bytes:
        # 0x3000 samples → ~0.37 float → ×10 gain clips at 1.0; RMS ≫ 0.10.
        return b"\x00\x30" * 512

    @staticmethod
    def _quiet() -> bytes:
        return b"\x00" * 1024

    def test_burst_voice_confirms_by_audio_time(self, vad):
        conn = FakeConn()
        # 16 × 32ms = 512ms of voice, all processed instantly (the burst).
        for _ in range(16):
            vad.is_vad(conn, self._loud())
        assert conn.client_have_voice is True

    def test_low_energy_gated_despite_high_prob(self, vad):
        conn = FakeConn()
        # High model confidence but no energy (silence) → must not confirm.
        for _ in range(40):
            vad.is_vad(conn, self._quiet())
        assert conn.client_have_voice is False

    def test_burst_voice_then_silence_fires_stop(self, vad):
        conn = FakeConn()
        for _ in range(16):
            vad.is_vad(conn, self._loud())
        assert conn.client_have_voice is True
        vad._prob_holder["prob"] = 0.01
        # 25 × 32ms = 800ms of silence ≥ the 700ms stop window, processed
        # instantly too — the stop must be audio-time as well.
        for _ in range(25):
            vad.is_vad(conn, self._quiet())
        assert conn.client_voice_stop is True
