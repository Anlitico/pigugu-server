"""Unit tests for the pigugu wire-protocol serializer."""

import asyncio
import json

import numpy as np
import pytest
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame

from voice.pipecat.pigugu_serializer import (
    CHANNELS,
    FRAME_DURATION_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    PiguguFrameSerializer,
    PiguguMessageFrame,
    PiguguOutputMessageFrame,
)


def _make_pcm(ms: int = 60, freq: int = 440) -> bytes:
    n = int(SAMPLE_RATE * ms / 1000)
    t = np.arange(n) / SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * 10000).astype(np.int16).tobytes()


def test_json_message_deserialize():
    ser = PiguguFrameSerializer()
    raw = json.dumps({"type": "listen", "state": "start", "mode": "realtime", "session_id": "abc"})
    frame = asyncio.run(ser.deserialize(raw))
    assert isinstance(frame, PiguguMessageFrame)
    assert frame.message["type"] == "listen"
    assert frame.message["state"] == "start"


def test_bad_json_returns_none():
    ser = PiguguFrameSerializer()
    assert asyncio.run(ser.deserialize("{not json")) is None


def test_output_message_serialize():
    ser = PiguguFrameSerializer()
    msg = {"type": "tts", "state": "start", "session_id": "abc", "sentence_id": 3}
    out = asyncio.run(ser.serialize(PiguguOutputMessageFrame(message=msg)))
    assert json.loads(out) == msg


def test_opus_roundtrip_60ms():
    ser = PiguguFrameSerializer()
    pcm = _make_pcm()
    opus = asyncio.run(
        ser.serialize(
            OutputAudioRawFrame(audio=pcm, sample_rate=SAMPLE_RATE, num_channels=CHANNELS)
        )
    )
    assert opus and isinstance(opus, bytes)
    frame = asyncio.run(ser.deserialize(opus))
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.sample_rate == SAMPLE_RATE
    assert frame.num_channels == CHANNELS
    assert len(frame.audio) == FRAME_SAMPLES * CHANNELS * 2  # 60ms in, 60ms out
    # Opus is lossy — decoded audio should have similar energy to the source.
    src = np.frombuffer(pcm, dtype=np.int16)
    dec = np.frombuffer(frame.audio, dtype=np.int16)
    assert abs(float(np.sqrt(np.mean(src.astype(np.float32) ** 2))) - float(
        np.sqrt(np.mean(dec.astype(np.float32) ** 2))
    )) < 3000


def test_incremental_60ms_framing():
    ser = PiguguFrameSerializer()
    frames = []
    for _ in range(6):  # 6 * 20ms = 120ms of audio
        out = asyncio.run(
            ser.serialize(
                OutputAudioRawFrame(
                    audio=_make_pcm(ms=20), sample_rate=SAMPLE_RATE, num_channels=CHANNELS
                )
            )
        )
        if out:
            frames.append(out)
    # 120ms of PCM yields exactly two 60ms Opus frames.
    assert len(frames) == 2
    for opus in frames:
        decoded = asyncio.run(ser.deserialize(opus))
        assert len(decoded.audio) == FRAME_SAMPLES * CHANNELS * 2


def test_flush_pads_partial_frame():
    ser = PiguguFrameSerializer()
    assert asyncio.run(
        ser.serialize(
            OutputAudioRawFrame(
                audio=_make_pcm(ms=40), sample_rate=SAMPLE_RATE, num_channels=CHANNELS
            )
        )
    ) is None  # 40ms buffered, not a full frame yet
    flushed = ser.flush()
    assert flushed and isinstance(flushed, bytes)
    decoded = asyncio.run(ser.deserialize(flushed))
    assert len(decoded.audio) == FRAME_SAMPLES * CHANNELS * 2


def test_raw_pcm_passthrough():
    ser = PiguguFrameSerializer()
    ser.raw_pcm = True
    pcm = _make_pcm()
    out = asyncio.run(
        ser.serialize(
            OutputAudioRawFrame(audio=pcm, sample_rate=SAMPLE_RATE, num_channels=CHANNELS)
        )
    )
    assert out == pcm
    frame = asyncio.run(ser.deserialize(pcm))
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.audio == pcm


def test_empty_audio_serialize_none():
    ser = PiguguFrameSerializer()
    assert (
        asyncio.run(
            ser.serialize(
                OutputAudioRawFrame(audio=b"", sample_rate=SAMPLE_RATE, num_channels=CHANNELS)
            )
        )
        is None
    )


def test_hello_roundtrip_message_shapes():
    ser = PiguguFrameSerializer()
    # Device hello → PiguguMessageFrame with the exact v1 fields.
    hello = json.dumps(
        {
            "type": "hello",
            "version": 1,
            "features": {"mcp": True},
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
    )
    frame = asyncio.run(ser.deserialize(hello))
    assert frame.message["audio_params"]["format"] == "opus"
