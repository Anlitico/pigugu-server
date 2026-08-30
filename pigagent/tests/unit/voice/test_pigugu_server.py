"""M5 test: server.py wiring — a device WS connection builds a full session.

Starts the real ``voice.server._on_connect`` handler (as run_server does) with
the shared providers stubbed to fakes and the TTS bridge's lazy PigAgent
creation stubbed to a fake. Verifies:
- client-id is extracted from the request headers and the session is registered,
- hello handshake replies,
- a full turn runs end-to-end (STT finals → merged turn → reply → tts/start/stop),
- inject routes through the registry.
"""

import asyncio
import json
import logging

import numpy as np
import opuslib
import pytest
import websockets
from websockets.asyncio.server import serve

import voice.server as server
from voice.pipecat.pigugu_serializer import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE
from voice.pipecat.tts_bridge import PiguguTtsBridge

SPLIT_FINALS = ["alexa good", "evening", "how are you"]


def test_websockets_logger_silenced_for_health_check_probes():
    """The NLB TCP health check connects to :8080 and closes immediately;
    websockets logs INFO "connection closed" for each probe (~3.7/s), flooding
    the log. voice.server must raise the websockets.server logger to WARNING so
    probes stay quiet while real failures (handler errors) remain visible."""
    ws_logger = logging.getLogger("websockets.server")
    assert ws_logger.level == logging.WARNING


def test_vad_bridge_provides_silero_conn_contract():
    """PiguguVadBridge is the ``conn`` object SileroVAD.is_vad writes to (the
    migration from connection.py must keep the contract). Missing
    client_audio_buffer crashed every audio frame (onnx.py:74) and killed
    server-side VAD turn detection — a continuous-stream device then never
    triggers a turn, so no TTS. Regression guard."""
    from voice.pipecat.state import PiguguTurnState
    from voice.pipecat.vad_bridge import PiguguVadBridge

    bridge = PiguguVadBridge(None, state=PiguguTurnState())
    assert isinstance(bridge.client_audio_buffer, bytearray)
    assert bridge.client_have_voice is False
    assert bridge.client_voice_stop is False
    assert bridge.client_listen_mode == "auto"


class _FakeSileroVad:
    """Emulates Silero's conn contract: voice frames confirm client_have_voice,
    the first silent frame after confirmed voice sets client_voice_stop (and
    clears client_have_voice). The bridge must treat this as shared state, not
    mutate it during wake-word suppression."""

    def is_vad(self, conn, pcm):
        if b"\xff" in pcm:
            conn.client_have_voice = True
        elif conn.client_have_voice:
            conn.client_voice_stop = True
            conn.client_have_voice = False
        return conn.client_have_voice


@pytest.mark.asyncio
async def test_wake_suppression_suppresses_start_but_not_stop():
    """Wake-word suppression must gate only the server-VAD START frame. Silero's
    stop detection reads client_have_voice on the NEXT frame to arm the silence
    timer; the old code zeroed it during suppression, so the wake-word utterance
    never ended and later speech got swallowed into the same turn (prod: turn 1
    input_pcm_ms=19920 across two utterances)."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    from voice.pipecat.pigugu_serializer import PiguguMessageFrame
    from voice.pipecat.state import PiguguTurnState
    from voice.pipecat.vad_bridge import PiguguVadBridge

    bridge = PiguguVadBridge(_FakeSileroVad(), state=PiguguTurnState())
    frames: list = []

    async def capture(frame, direction=None):
        frames.append(frame)

    bridge.push_frame = capture

    # Wake word fires → server-VAD start suppression armed for 2s.
    await bridge._on_control(PiguguMessageFrame({"type": "listen", "state": "detect"}))
    # User keeps talking: VAD confirms voice DURING the suppression window.
    await bridge._on_audio(b"\xff" * 640)
    # ...then stops: the voice→silence transition must still yield a stop.
    await bridge._on_audio(b"\x00" * 640)

    started = [f for f in frames if isinstance(f, VADUserStartedSpeakingFrame)]
    stopped = [f for f in frames if isinstance(f, VADUserStoppedSpeakingFrame)]
    assert started == [], "server-VAD start must stay suppressed inside the wake window"
    assert len(stopped) == 1, "voice stop must fire even inside the wake window"
    assert stopped[0].stop_secs == 0.7

    # After the window lapses, a fresh voice→silence pair emits both frames.
    bridge._suppress_until = 0.0
    await bridge._on_audio(b"\xff" * 640)
    await bridge._on_audio(b"\x00" * 640)
    started = [f for f in frames if isinstance(f, VADUserStartedSpeakingFrame)]
    stopped = [f for f in frames if isinstance(f, VADUserStoppedSpeakingFrame)]
    assert len(started) == 1
    assert len(stopped) == 2


class FakeVAD:
    def is_vad(self, conn, pcm):  # noqa: ARG002
        return True


class FakeSTT:
    interface_type = "stream"

    def __init__(self):
        self._emitted = False

    async def open_audio_channels(self, conn):
        pass

    async def receive_audio(self, conn, pcm, have_voice):
        if not self._emitted and pcm:
            self._emitted = True
            for text in SPLIT_FINALS:
                await conn._on_stt_final(text)


class FakeTTS:
    interface_type = "stream"

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None):
        async for text in text_source:
            if interrupt_event and interrupt_event.is_set():
                break
            yield [_make_opus_tone() for _ in range(6)]

    async def synthesize(self, text, collect_pcm=None):
        return [_make_opus_tone() for _ in range(6)]


class FakeAgent:
    async def generate_reply(
        self, user_text, *, persona_id=1, interrupt_event=None, session_id=None
    ):
        yield "hello from pigugu!"


def _make_opus_tone() -> bytes:
    enc = opuslib.Encoder(SAMPLE_RATE, CHANNELS, "voip")
    samples = (
        np.sin(np.arange(FRAME_SAMPLES) / FRAME_SAMPLES * 2 * np.pi * 440) * 8000
    ).astype(np.int16)
    return enc.encode(samples.tobytes(), FRAME_SAMPLES)


async def _read_until_stop(ws: websockets.ClientConnection, seen: list[tuple[str, object]]) -> None:
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        if isinstance(msg, str):
            parsed = json.loads(msg)
            seen.append(("msg", parsed))
            if parsed.get("type") == "tts" and parsed.get("state") == "stop":
                return
        else:
            seen.append(("audio", msg))


async def _run(port: int) -> tuple[dict, list[tuple[str, object]]]:
    seen: list[tuple[str, object]] = []
    async with websockets.connect(
        f"ws://127.0.0.1:{port}", additional_headers={"client-id": "smoke-dev"}
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "persona_id": 3,
                    "hw_id": "hw-42",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
        )
        hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        # Session is registered under the client-id from the headers.
        assert "smoke-dev" in server._connections
        await ws.send(json.dumps({"type": "listen", "state": "start"}))
        for _ in range(3):
            await ws.send(_make_opus_tone())
        await asyncio.sleep(0.2)
        await ws.send(
            json.dumps({"type": "listen", "state": "vad_silence", "user_stop_age_ms": 0})
        )
        # Full turn: start → audio → stop.
        await _read_until_stop(ws, seen)
        # Inject through the REST registry while still connected.
        await server.send_inject("smoke-dev", {"text": "go pigugu"})
        await _read_until_stop(ws, seen)
        await ws.close()
        return hello, seen


@pytest.mark.asyncio
async def test_server_wiring_full_turn(monkeypatch):
    monkeypatch.setattr(server, "_get_shared_vad", lambda: FakeVAD())
    monkeypatch.setattr(server, "_get_shared_stt", lambda: FakeSTT())
    monkeypatch.setattr(server, "_get_shared_tts", lambda: FakeTTS())
    # The server passes pig=None; the TTS bridge lazily creates it. Stub that.
    async def _fake_ensure_pig(self):  # noqa: ARG001
        return FakeAgent()

    monkeypatch.setattr(PiguguTtsBridge, "_ensure_pig", _fake_ensure_pig)

    records: list[str] = []
    sink_id = server.logger.add(records.append, level="INFO")
    try:
        async def on_connect(ws):
            await server._on_connect(ws)

        async with serve(on_connect, "127.0.0.1", 0) as wss:
            port = wss.sockets[0].getsockname()[1]
            hello, seen = await asyncio.wait_for(_run(port), timeout=10)
    finally:
        server.logger.remove(sink_id)

    assert hello["type"] == "hello" and hello["audio_params"]["format"] == "opus"
    # Registry was cleaned up after the connection closed.
    assert "smoke-dev" not in server._connections
    # Real disconnects stay visible now that websockets' own INFO
    # "connection closed" is silenced (health-check probe noise).
    assert any("Session ended client_id=smoke-dev" in r for r in records)
    states = [p["state"] for kind, p in seen if kind == "msg" and p["type"] == "tts"]
    # Turn start/stop, then inject start/stop routed through the registry.
    assert states == ["start", "stop", "start", "stop"]
    assert any(kind == "audio" for kind, _ in seen)
