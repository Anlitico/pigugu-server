"""Unit tests for the echo gate in the STT bridge.

The device's speaker bleeds into its own mic. The on-device AEC leaves a
residual that never clears the server VAD's energy gate, but Deepgram
normalizes, so it transcribes that residual anyway -- and that phantom
transcript used to start a user turn, which barge-ins on the reply being
played. These tests pin the gate: drop transcripts that arrive while the
assistant is speaking with no VAD-confirmed user voice, and leave every other
path exactly as it was.
"""

import asyncio
import time

import pytest
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
)

from voice.interims import InterimBuffer
from voice.pipecat.state import PiguguTurnState
from voice.pipecat.stt_bridge import PiguguSttBridge
from voice.pipecat.vad_bridge import PiguguVadBridge


class _FakeStt:
    """Only the attribute the bridge reads outside the audio path."""

    supports_context = False


class _FakeVad:
    """Only the surface ``PiguguVadBridge._on_audio`` touches."""

    def __init__(self, verdict: bool):
        self.verdict = verdict

    def is_vad(self, conn, pcm):
        # The real providers stash the per-chunk decision here.
        conn.last_is_voice = self.verdict
        return self.verdict


class _SilentVad:
    """A provider that returns without publishing a verdict.

    That is what ``onnx.is_vad`` does when it swallows an ONNX Runtime error
    on the chunk.
    """

    def is_vad(self, conn, pcm):
        return False


def _make_bridge(state):
    bridge = PiguguSttBridge(_FakeStt(), state=state)
    bridge._loop = asyncio.get_running_loop()
    pushed = []

    async def _spy(frame, direction=None):
        pushed.append(frame)

    bridge.push_frame = _spy
    return bridge, pushed


def _speaking_state(*, vad_voice_active: bool, started_ms_ago: float,
                    vad_wired: bool = True):
    """A session whose assistant has been speaking for ``started_ms_ago`` ms."""
    state = PiguguTurnState()
    state.client_is_speaking = True
    state.speaking_started_pc = time.perf_counter() - started_ms_ago / 1000.0
    state.vad_voice_active = vad_voice_active
    state.vad_wired = vad_wired
    state.interims = InterimBuffer()
    return state


# ── the gate ──────────────────────────────────────────────────────────

# Long past the AEC convergence window, so only the VAD decides here.


@pytest.mark.asyncio
async def test_echo_final_is_dropped():
    state = _speaking_state(vad_voice_active=False, started_ms_ago=2000)
    bridge, pushed = _make_bridge(state)
    await bridge._on_stt_final("I hear you.")
    assert pushed == []


@pytest.mark.asyncio
async def test_echo_interim_is_dropped_and_not_buffered():
    state = _speaking_state(vad_voice_active=False, started_ms_ago=2000)
    bridge, pushed = _make_bridge(state)
    await bridge._on_stt_interim("I hear you")
    assert pushed == []
    assert state.interims.snapshot() == []


@pytest.mark.asyncio
async def test_genuine_barge_in_passes():
    state = _speaking_state(vad_voice_active=True, started_ms_ago=2000)
    bridge, pushed = _make_bridge(state)
    await bridge._on_stt_final("stop, tell me about the weather")
    assert len(pushed) == 1
    assert isinstance(pushed[0], TranscriptionFrame)


@pytest.mark.asyncio
async def test_interim_passes_and_is_buffered_on_a_genuine_barge_in():
    state = _speaking_state(vad_voice_active=True, started_ms_ago=2000)
    bridge, pushed = _make_bridge(state)
    await bridge._on_stt_interim("stop, tell me")
    assert len(pushed) == 1
    assert isinstance(pushed[0], InterimTranscriptionFrame)
    assert state.interims.snapshot() == ["stop, tell me"]


# ── the two carve-outs ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transcript_passes_when_the_assistant_is_silent():
    # The wake-word turn and every turn taken while the bot is quiet: the gate
    # must not exist for them, even with no VAD voice.
    state = PiguguTurnState()
    state.vad_wired = True
    state.interims = InterimBuffer()
    bridge, pushed = _make_bridge(state)
    await bridge._on_stt_final("can you hear me?")
    await bridge._on_stt_interim("can you")
    assert len(pushed) == 2
    assert state.interims.snapshot() == ["can you"]


@pytest.mark.asyncio
async def test_gate_is_inert_without_a_vad():
    # No VAD wired means no verdict can ever arrive, so a gate keyed on it
    # would silently disable barge-in for the whole session.
    state = _speaking_state(vad_voice_active=False, started_ms_ago=2000,
                            vad_wired=False)
    bridge, pushed = _make_bridge(state)
    await bridge._on_stt_final("stop, tell me about the weather")
    assert len(pushed) == 1


@pytest.mark.asyncio
async def test_grace_window_is_blanked_even_with_vad_voice():
    # 100ms into the reply: the AEC has not converged, so nothing may start a
    # turn there -- not even something the VAD calls voice.
    state = _speaking_state(vad_voice_active=True, started_ms_ago=100)
    bridge, pushed = _make_bridge(state)
    await bridge._on_stt_final("hello there")
    await bridge._on_stt_interim("hello")
    assert pushed == []
    assert state.interims.snapshot() == []


@pytest.mark.asyncio
async def test_echo_utterance_end_is_dropped():
    # An echoed utterance-end would close a user turn that is still open
    # (the roast/inject path sets client_is_speaking mid-utterance).
    state = _speaking_state(vad_voice_active=False, started_ms_ago=2000)
    bridge, pushed = _make_bridge(state)
    await bridge._on_utterance_end()
    assert pushed == []


@pytest.mark.asyncio
async def test_genuine_utterance_end_passes():
    state = _speaking_state(vad_voice_active=True, started_ms_ago=2000)
    bridge, pushed = _make_bridge(state)
    await bridge._on_utterance_end()
    assert len(pushed) == 1


@pytest.mark.asyncio
async def test_utterance_end_passes_when_the_assistant_is_silent():
    state = PiguguTurnState()
    state.vad_wired = True
    bridge, pushed = _make_bridge(state)
    await bridge._on_utterance_end()
    assert len(pushed) == 1


@pytest.mark.asyncio
async def test_kill_switch_disables_the_gate(monkeypatch):
    monkeypatch.setenv("VOICE_ECHO_GATE", "0")
    state = _speaking_state(vad_voice_active=False, started_ms_ago=2000)
    bridge, pushed = _make_bridge(state)
    await bridge._on_stt_final("I hear you.")
    assert len(pushed) == 1


# ── what the VAD bridge publishes ─────────────────────────────────────


@pytest.mark.asyncio
async def test_vad_bridge_publishes_voice_active():
    state = PiguguTurnState()
    bridge = PiguguVadBridge(_FakeVad(True), state=state)
    await bridge._on_audio(b"\x00\x00" * 512)
    assert state.vad_voice_active is True


@pytest.mark.asyncio
async def test_voice_active_survives_a_dip_then_decays():
    # A real utterance is voiced for only part of its chunks, so a single
    # unvoiced chunk must not close the gate...
    state = PiguguTurnState()
    vad = _FakeVad(True)
    bridge = PiguguVadBridge(vad, state=state)
    await bridge._on_audio(b"\x00\x00" * 512)
    assert state.vad_voice_active is True

    vad.verdict = False
    await bridge._on_audio(b"\x00\x00" * 512)
    assert state.vad_voice_active is True

    # ...but the hold is a hold, not a latch.
    bridge._last_voice_pc -= 1.0
    await bridge._on_audio(b"\x00\x00" * 512)
    assert state.vad_voice_active is False


@pytest.mark.asyncio
async def test_voice_active_stays_closed_without_a_provider_verdict():
    # ``last_is_voice`` is onnx-provider specific; a provider that never sets
    # it must leave the gate closed rather than crash.
    state = PiguguTurnState()
    bridge = PiguguVadBridge(_SilentVad(), state=state)
    await bridge._on_audio(b"\x00\x00" * 512)
    assert state.vad_voice_active is False


@pytest.mark.asyncio
async def test_a_vad_that_never_publishes_a_verdict_disarms_the_gate():
    # A verdict that never arrives would keep the gate closed for the whole
    # session and silently kill barge-in, so it must disarm instead -- loudly.
    state = PiguguTurnState()
    bridge = PiguguVadBridge(_SilentVad(), state=state)
    assert state.vad_wired is True  # armed at construction
    await bridge._on_audio(b"\x00\x00" * 512)
    assert state.vad_wired is False

    # ...and having disarmed, a barge-in is no longer gated.
    state.client_is_speaking = True
    state.speaking_started_pc = time.perf_counter() - 2.0
    state.interims = InterimBuffer()
    bridge_, pushed = _make_bridge(state)
    await bridge_._on_stt_final("stop, tell me about the weather")
    assert len(pushed) == 1
