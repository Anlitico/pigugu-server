"""M4 test: per-turn TurnStorage is built, filled, and committed end-to-end.

Runs the full loop (like the M3 conversation test) with S3/ClickHouse env vars
set and TurnStorage's I/O stubbed so commit() completes in memory. Asserts:
- exactly one turn committed, with the merged STT text + reply TTS text,
- user PCM captured over the turn window, tts PCM collected from the fake TTS,
- tts_status == complete,
- device_playback_ms from the tts_played ack lands in telemetry,
- s3_uris populated (the 5-file layout).
"""

import asyncio
import json

import numpy as np
import opuslib
import pytest
import websockets
from websockets.asyncio.server import serve

from voice.pipecat.pigugu_serializer import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE
from voice.pipecat.session import PiguguSession
from voice.storage import TurnStorage

SPLIT_FINALS = ["alexa good", "evening", "how are you"]
EXPECTED_MERGED = "alexa good evening how are you"
REPLY_CHUNKS = ["hi there, ", "this is pigugu!"]
FRAMES_PER_BATCH = 6


class FakeAgent:
    interface_type = "stream"

    async def generate_reply(
        self, user_text, *, persona_id=1, interrupt_event=None, session_id=None
    ):
        for chunk in REPLY_CHUNKS:
            if interrupt_event and interrupt_event.is_set():
                return
            yield chunk


class FakeTTS:
    interface_type = "stream"

    async def stream_audio(self, text_source, interrupt_event, collect_pcm=None):
        async for text in text_source:
            if interrupt_event and interrupt_event.is_set():
                break
            frames = [_make_opus_tone() for _ in range(FRAMES_PER_BATCH)]
            if collect_pcm is not None:
                collect_pcm.extend(b"\x00" * (FRAME_SAMPLES * 2 * FRAMES_PER_BATCH))
            yield frames

    async def synthesize(self, text, collect_pcm=None):
        frames = [_make_opus_tone() for _ in range(FRAMES_PER_BATCH)]
        if collect_pcm is not None:
            collect_pcm.extend(b"\x00" * (FRAME_SAMPLES * 2 * FRAMES_PER_BATCH))
        return frames


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


def _make_opus_tone() -> bytes:
    enc = opuslib.Encoder(SAMPLE_RATE, CHANNELS, "voip")
    samples = (
        np.sin(np.arange(FRAME_SAMPLES) / FRAME_SAMPLES * 2 * np.pi * 440) * 8000
    ).astype(np.int16)
    return enc.encode(samples.tobytes(), FRAME_SAMPLES)


async def _run(port: int, *, send_tts_played: bool) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
        )
        await asyncio.wait_for(ws.recv(), 5)  # hello reply
        # Wake word first — the following turn must be classified wake_word.
        await ws.send(json.dumps({"type": "listen", "state": "detect", "text": "pigugu"}))
        await ws.send(json.dumps({"type": "listen", "state": "start"}))
        for _ in range(3):
            await ws.send(_make_opus_tone())
        await asyncio.sleep(0.2)
        await ws.send(
            json.dumps({"type": "listen", "state": "vad_silence", "user_stop_age_ms": 0})
        )
        # Wait for tts/start, ack playback, then read until tts/stop.
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(msg, str):
                parsed = json.loads(msg)
                if parsed.get("type") == "tts" and parsed.get("state") == "start":
                    if send_tts_played:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "listen",
                                    "state": "tts_played",
                                    "device_playback_ms": 120,
                                    "sentence_id": parsed.get("sentence_id"),
                                }
                            )
                        )
                elif parsed.get("type") == "tts" and parsed.get("state") == "stop":
                    break
        await ws.close()


@pytest.mark.asyncio
async def test_turn_storage_commits_full_row(monkeypatch):
    monkeypatch.setenv("AUDIO_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("AUDIO_S3_PREFIX", "voice-turns")
    monkeypatch.setenv("CLICKHOUSE_HOST", "ch")
    monkeypatch.setenv("CLICKHOUSE_PORT", "9000")
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "voice")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")

    committed: list[TurnStorage] = []

    async def fake_s3(self, payloads):
        for name in payloads:
            self.s3_uris[name] = f"s3://{self.s3_bucket}/{self.s3_prefix}/{name}"

    async def fake_ch_insert(self):
        committed.append(self)

    monkeypatch.setattr(TurnStorage, "_s3_upload_all", fake_s3)
    monkeypatch.setattr(TurnStorage, "_clickhouse_insert", fake_ch_insert)

    def make_session(ws):
        return PiguguSession(
            ws,
            client_id="m4",
            session_id="m4s",
            vad=None,
            stt=FakeSTT(),
            pig=FakeAgent(),
            tts=FakeTTS(),
        )

    async def on_connect(ws):
        session = make_session(ws)
        await session.run()

    async with serve(on_connect, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(_run(port, send_tts_played=True), timeout=10)

    # commit() is fire-and-forget — poll for it on the running loop.
    for _ in range(50):
        if committed:
            break
        await asyncio.sleep(0.05)
    assert len(committed) == 1
    storage = committed[0]

    assert storage.turn_idx == 1
    assert storage.session_id == "m4s"
    assert storage.device_id == "m4"
    # The wake-word turn is classified as such (listen/detect).
    assert storage.turn_type == "wake_word"
    # STT side: merged utterance, not the three finals.
    assert storage.stt_text == EXPECTED_MERGED
    assert storage.stt_status == "final"
    # TTS side: full reply text + collected PCM.
    assert storage.tts_text == "hi there, this is pigugu!"
    assert storage.tts_status == "complete"
    assert storage.tts_truncated_reason == ""
    # Audio windows.
    assert len(storage.user_pcm_bytes) > 0
    assert len(storage.tts_pcm_buf) > 0
    # The 5-file S3 layout.
    assert set(storage.s3_uris) == {
        "input.wav", "input.json", "tts.wav", "tts.json", "turn.json",
    }
    # device_playback_ms came through the tts_played ack.
    assert storage.telemetry.get("device_playback_ms") == 120
    # E2E segments present and REAL (marks recorded by the bridges land in a
    # shared turn dict — a regression here silently zeros every telemetry col).
    assert storage.telemetry.get("e2e_ms", 0) > 0
    assert storage.telemetry.get("llm_ttft_ms") is not None
    # voice_segments[] computed from the (empty, in this harness) VAD slice.
    assert isinstance(storage.voice_segments, list)
