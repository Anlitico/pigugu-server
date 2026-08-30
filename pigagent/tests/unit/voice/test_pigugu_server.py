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

import numpy as np
import opuslib
import pytest
import websockets
from websockets.asyncio.server import serve

import voice.server as server
from voice.pipecat.pigugu_serializer import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE
from voice.pipecat.tts_bridge import PiguguTtsBridge

SPLIT_FINALS = ["alexa good", "evening", "how are you"]


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

    async def on_connect(ws):
        await server._on_connect(ws)

    async with serve(on_connect, "127.0.0.1", 0) as wss:
        port = wss.sockets[0].getsockname()[1]
        hello, seen = await asyncio.wait_for(_run(port), timeout=10)

    assert hello["type"] == "hello" and hello["audio_params"]["format"] == "opus"
    # Registry was cleaned up after the connection closed.
    assert "smoke-dev" not in server._connections
    states = [p["state"] for kind, p in seen if kind == "msg" and p["type"] == "tts"]
    # Turn start/stop, then inject start/stop routed through the registry.
    assert states == ["start", "stop", "start", "stop"]
    assert any(kind == "audio" for kind, _ in seen)
