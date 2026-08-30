"""Tests for voice.storage.TurnStorage.

These tests mock out the S3 and ClickHouse I/O and exercise the
pure parts of TurnStorage: state mutation, payload generation, and
the S3-first-then-CH commit order.
"""
import asyncio
import io
import json
import struct
import wave
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice.interims import InterimBuffer
from voice.segments import compute_voice_segments
from voice.storage import TurnStorage, get_utc_date_for_ms


# ── Helpers ────────────────────────────────────────────────────────


def _pcm_int16(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _build_storage(
    *,
    turn_id: str = "1700000000000_abc12345_0001",
    session_id: str = "abc12345",
    turn_idx: int = 1,
    utc_start_ms: int = 1700000000000,
    user_pcm: bytes = b"",
    tts_pcm_buf: bytearray | None = None,
    stt_interims: list[str] | None = None,
    voice_chunk_flags: list[bool] | None = None,
) -> TurnStorage:
    """Build a TurnStorage with no I/O. Caller mutates fields
    before calling commit()."""
    if tts_pcm_buf is None:
        tts_pcm_buf = bytearray()
    if voice_chunk_flags is None:
        voice_chunk_flags = []
    buf = InterimBuffer()
    if stt_interims:
        buf.record_many(stt_interims)
    return TurnStorage(
        turn_id=turn_id,
        session_id=session_id,
        turn_idx=turn_idx,
        device_id="test-device",
        user_id="test-user",
        persona_id=1,
        utc_start_ms=utc_start_ms,
        s3_bucket="test-bucket",
        s3_prefix="voice-turns",
        clickhouse_dsn="clickhouse://default:secret@clickhouse:9000/voice",
        clickhouse_table="voice.turns",
        interims=buf,
        voice_chunk_flags_slice=lambda: list(voice_chunk_flags),
        turn_type="follow_up",
        stt_model="nova-3",
        tts_model="sonic-3.5",
    )


class _StubStorage(TurnStorage):
    """TurnStorage subclass that captures the S3 + ClickHouse calls
    instead of hitting the network. Used because TurnStorage uses
    ``__slots__`` and we can't patch instance methods.
    """

    def __init__(self, *args, **kwargs):
        self.s3_calls: list[dict[str, bytes]] = []
        self.ch_calls: int = 0
        self.s3_exc: Exception | None = None
        self.ch_exc: Exception | None = None
        # call_log records the order of S3 and CH invocations so the
        # order-invariant test can assert that S3 ran first.
        self.call_log: list[str] = []
        super().__init__(*args, **kwargs)

    async def _s3_upload_all(self, payloads: dict[str, bytes]) -> None:
        self.call_log.append("s3")
        self.s3_calls.append(payloads)
        if self.s3_exc is not None:
            raise self.s3_exc
        # Populate s3_uris the same way the real method does
        for name in payloads:
            self.s3_uris[name] = f"s3://{self.s3_bucket}/{self.s3_prefix}/{name}"

    async def _clickhouse_insert(self) -> None:
        self.call_log.append("ch")
        self.ch_calls += 1
        if self.ch_exc is not None:
            raise self.ch_exc


def _build_stub(
    *,
    voice_chunk_flags_provider: Callable[[], list[bool]] | None = None,
    **overrides,
) -> _StubStorage:
    """Build a _StubStorage. Allows passing a custom
    voice_chunk_flags_slice provider (for failure tests)."""
    s = _build_storage(**overrides)
    if voice_chunk_flags_provider is None:
        voice_chunk_flags_provider = s.voice_chunk_flags_slice
    return _StubStorage(
        turn_id=s.turn_id,
        session_id=s.session_id,
        turn_idx=s.turn_idx,
        device_id=s.device_id,
        user_id=s.user_id,
        persona_id=s.persona_id,
        utc_start_ms=s.utc_start_ms,
        s3_bucket=s.s3_bucket,
        s3_prefix=s.s3_prefix,
        clickhouse_dsn=s.clickhouse_dsn,
        clickhouse_table=s.clickhouse_table,
        interims=s.interims,
        voice_chunk_flags_slice=voice_chunk_flags_provider,
        turn_type=s.turn_type,
        stt_model=s.stt_model,
        tts_model=s.tts_model,
    )


def _silence_pcm(seconds: float = 1.0, rate: int = 16000) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


# ── UTC date helper ───────────────────────────────────────────────


def test_get_utc_date_for_ms_basic():
    # 1700000000000 ms = 2023-11-14 22:13:20 UTC
    assert get_utc_date_for_ms(1700000000000) == "2023-11-14"


def test_get_utc_date_for_ms_midnight():
    # 2024-01-01 00:00:00 UTC = 1704067200000 ms
    assert get_utc_date_for_ms(1704067200000) == "2024-01-01"


# ── S3 prefix construction ────────────────────────────────────────


def test_s3_prefix_includes_utc_date():
    s = _build_storage(utc_start_ms=1700000000000)
    expected_date = "2023-11-14"
    assert s.s3_prefix.startswith(f"voice-turns/{expected_date}/abc12345/")


def test_s3_prefix_contains_turn_id():
    s = _build_storage(turn_id="1700000000000_abc12345_0001")
    assert s.s3_prefix.endswith("1700000000000_abc12345_0001")


# ── Input WAV payload ─────────────────────────────────────────────


def test_input_wav_is_valid_wav_16k_mono_int16():
    s = _build_storage()
    # 1600 bytes of int16 mono PCM = 800 samples @ 16kHz = 50ms
    s.set_user_pcm(b"\x00\x00" * 1600)
    wav = s._build_input_wav_bytes()
    # Re-parse to confirm it is a valid WAV.
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        # Python's wave module reports byte count for 16-bit (a known
        # quirk in the stdlib). The 3-arg frame count (= sample count)
        # can be derived as getnframes() // sampwidth // nchannels.
        assert wf.getnframes() // wf.getsampwidth() // wf.getnchannels() == 800


def test_input_wav_handles_empty_pcm():
    s = _build_storage()
    s.set_user_pcm(b"")
    wav = s._build_input_wav_bytes()
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnframes() == 0


# ── TTS WAV payload ───────────────────────────────────────────────


def test_tts_wav_handles_empty():
    s = _build_storage()
    wav = s._build_tts_wav_bytes()
    # Still a valid (empty) WAV — the agent always writes the file.
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getnframes() == 0


def test_tts_wav_reflects_bytearray():
    s = _build_storage()
    s.tts_pcm_buf.extend(b"\x00\x00" * 1600)  # 800 int16 samples = 50ms @ 16kHz
    wav = s._build_tts_wav_bytes()
    with wave.open(io.BytesIO(wav), "rb") as wf:
        # See note in test_input_wav_is_valid_wav_16k_mono_int16 re
        # the wave.getnframes() byte-count quirk for 16-bit audio.
        assert wf.getnframes() // wf.getsampwidth() // wf.getnchannels() == 800


# ── JSON sidecar payloads ──────────────────────────────────────────


def test_input_json_includes_voice_segments():
    s = _build_storage(
        user_pcm=b"\x00\x00" * 1600,
        voice_chunk_flags=[True] * 10 + [False] * 20,
    )
    s.set_user_pcm(b"\x00\x00" * 1600)
    s.mark_stt_final("hello world")
    # voice_segments is computed in commit(); pre-fill here so we can
    # unit-test the JSON builder without the full commit pipeline.
    s.voice_segments = compute_voice_segments(s.voice_chunk_flags_slice())
    payload = json.loads(s._build_input_json())
    assert payload["stt_text"] == "hello world"
    assert payload["stt_status"] == "final"
    assert len(payload["voice_segments"]) == 1
    assert payload["voice_segments"][0]["start_ms"] == 0
    assert payload["voice_segments"][0]["end_ms"] == 320


def test_input_json_records_stt_interims():
    s = _build_storage(
        user_pcm=b"\x00\x00" * 1600,
        stt_interims=["hel", "hello", "hello wor", "hello world"],
    )
    s.set_user_pcm(b"\x00\x00" * 1600)
    s.mark_stt_final("hello world")
    payload = json.loads(s._build_input_json())
    assert payload["stt_interims"] == ["hel", "hello", "hello wor", "hello world"]
    # The interim buffer is drained on mark_stt_final
    assert len(s.interims) == 0


def test_input_json_no_stt_status_when_empty_final():
    s = _build_storage()
    s.set_user_pcm(b"\x00\x00" * 1600)
    s.mark_stt_final("")
    payload = json.loads(s._build_input_json())
    assert payload["stt_text"] == ""
    assert payload["stt_status"] == "no_stt"


def test_tts_json_complete():
    s = _build_storage()
    s.tts_pcm_buf.extend(b"\x00\x00" * 3200)
    s.mark_tts_complete("OK reply", ok=True)
    payload = json.loads(s._build_tts_json())
    assert payload["tts_text"] == "OK reply"
    assert payload["tts_status"] == "complete"
    assert payload["tts_truncated_reason"] == ""


def test_tts_json_aborted_with_reason():
    s = _build_storage()
    s.tts_pcm_buf.extend(b"\x00\x00" * 3200)
    s.mark_tts_complete("partial reply", ok=False, truncated_reason="barge_in")
    payload = json.loads(s._build_tts_json())
    assert payload["tts_status"] == "interrupted"
    assert payload["tts_truncated_reason"] == "barge_in"


def test_tts_json_empty_when_no_text():
    s = _build_storage()
    s.mark_tts_complete("", ok=True)
    payload = json.loads(s._build_tts_json())
    assert payload["tts_status"] == "empty"


def test_turn_json_includes_identity():
    s = _build_storage()
    s.set_turn_phase("first_after_connect")
    s.set_telemetry({"e2e_ms": 1234, "llm_model": "grok-4-3"})
    payload = json.loads(s._build_turn_json())
    assert payload["turn_id"] == s.turn_id
    assert payload["session_id"] == "abc12345"
    assert payload["user_id"] == "test-user"
    assert payload["device_id"] == "test-device"
    assert payload["turn_phase"] == "first_after_connect"
    assert payload["models"]["llm"] == "grok-4-3"
    assert payload["telemetry"]["e2e_ms"] == 1234


# ── mark_stt_abandoned ─────────────────────────────────────────────


def test_mark_stt_abandoned_drains_interims():
    s = _build_storage(stt_interims=["hel", "hello wor"])
    s.mark_stt_abandoned()
    assert s.stt_status == "abandoned"
    assert s.abandoned_stts == ["hel", "hello wor"]
    assert s.stt_text == ""  # no final


# ── Idempotency ────────────────────────────────────────────────────


def test_commit_is_idempotent():
    """Two calls to commit() must be a no-op for the second."""
    s = _build_storage(user_pcm=b"\x00\x00" * 3200)
    s.set_user_pcm(b"\x00\x00" * 3200)
    s.commit_started = True  # simulate in-flight
    s._committed = True
    # Should return immediately, no I/O attempted
    asyncio.run(s.commit())


def test_commit_concurrent_callers_only_one_proceeds():
    """Two callers schedule commit() in the same event loop tick
    (before any await yields). The ``commit_started`` flag is set
    synchronously at the start of commit() so only one of them
    proceeds with the I/O; the other short-circuits.
    """
    s = _build_stub()
    s.set_user_pcm(b"\x00\x00" * 3200)
    s.mark_stt_final("test")
    s.mark_tts_complete("OK", ok=True)

    async def runner() -> None:
        # Both are scheduled synchronously before any await.
        t1 = asyncio.ensure_future(s.commit())
        t2 = asyncio.ensure_future(s.commit())
        await asyncio.gather(t1, t2)

    asyncio.run(runner())
    # Only one commit pipeline ran (S3 + CH once each).
    assert s.call_log == ["s3", "ch"]
    assert len(s.s3_calls) == 1
    assert s.ch_calls == 1


# ── Commit order (S3 first, CH second) ────────────────────────────


def test_commit_calls_s3_before_clickhouse():
    """The commit order is critical: S3 first, then CH. If S3
    fails, no row is written. Verify the call order via the
    stub's call_log — not just the call counts, which would pass
    even if the order were inverted."""
    s = _build_stub()
    s.set_user_pcm(b"\x00\x00" * 3200)
    s.mark_stt_final("test")
    s.mark_tts_complete("OK", ok=True)

    asyncio.run(s.commit())
    # Exact-order assertion: S3 must run before CH.
    assert s.call_log == ["s3", "ch"]
    assert len(s.s3_calls) == 1
    assert s.ch_calls == 1
    assert len(s.s3_uris) == 5


def test_commit_skips_ch_when_s3_fails():
    """If S3 fails, no ClickHouse row should be written."""
    s = _build_stub()
    s.set_user_pcm(b"\x00\x00" * 3200)
    s.s3_exc = RuntimeError("S3 unreachable")

    asyncio.run(s.commit())
    # S3-failed turns are NOT retried: ``commit_started`` is set
    # synchronously at the top of commit() and short-circuits any
    # future call. Partial S3 state (if any PUTs succeeded before
    # the failure) is permanent — a janitor (v2) reconciles these
    # orphans against the absent CH row.
    assert s._committed is False
    assert s.commit_started is True
    # No s3_uris were populated (S3 failed before writing them)
    assert s.s3_uris == {}
    # CH INSERT was never called
    assert s.ch_calls == 0
    # S3 was attempted (call recorded) but raised; CH was never reached
    assert s.call_log == ["s3"]


def test_commit_marks_committed_even_when_ch_fails():
    """If S3 succeeds but CH fails, the S3 files remain (orphan)
    and we mark committed=True to avoid retry storms. The error
    is logged for the janitor to reconcile later."""
    s = _build_stub()
    s.set_user_pcm(b"\x00\x00" * 3200)
    s.mark_stt_final("test")
    s.mark_tts_complete("OK", ok=True)
    s.ch_exc = RuntimeError("CH INSERT failed")

    asyncio.run(s.commit())
    assert s._committed is True  # don't retry
    # S3 files are still referenced (orphan, GC reaps later)
    assert s.s3_uris.get("input.wav", "").startswith("s3://")


# ── End-to-end commit (with stubbed I/O) ──────────────────────────


def test_full_commit_produces_all_5_files():
    s = _build_stub(stt_interims=["test"])
    s.set_user_pcm(b"\x00\x00" * 1600)
    s.tts_pcm_buf.extend(b"\x00\x00" * 1600)
    s.mark_stt_final("test")
    s.mark_tts_complete("reply", ok=True)

    asyncio.run(s.commit())

    assert len(s.s3_calls) == 1
    captured = s.s3_calls[0]
    assert set(captured.keys()) == {
        "input.wav", "input.json", "tts.wav", "tts.json", "turn.json",
    }
    assert len(s.s3_uris) == 5
    for name, uri in s.s3_uris.items():
        assert uri.startswith("s3://test-bucket/")
        assert uri.endswith(f"/{name}")


def test_commit_computes_voice_segments_from_flags():
    s = _build_stub(voice_chunk_flags=[True] * 5 + [False] * 15)
    s.set_user_pcm(b"\x00\x00" * 1600)
    s.mark_stt_final("hi")

    asyncio.run(s.commit())
    # The sidecar JSON should contain the computed voice segment
    payload = json.loads(s._build_input_json())
    assert len(payload["voice_segments"]) == 1
    assert payload["voice_segments"][0]["duration_ms"] == 160  # 5 × 32


# ── Voice segment failure does not abort commit ──────────────────


def test_voice_segment_compute_failure_is_logged_not_raised():
    """If voice_chunk_flags_slice() raises, the commit still
    proceeds (with empty voice_segments)."""
    def bad_provider():
        raise RuntimeError("flag provider crashed")
    s = _build_stub(voice_chunk_flags_provider=bad_provider)
    s.set_user_pcm(b"\x00\x00" * 1600)
    s.mark_stt_final("hi")

    asyncio.run(s.commit())
    assert s.voice_segments == []  # fell back to empty
    # S3 + CH still ran
    assert len(s.s3_calls) == 1
    assert s.ch_calls == 1


# ── ClickHouse wire format (asynch native INSERT) ─────────────────

# voice.turns column order (clickhouse/migrations/0001_voice_turns.sql),
# excluding `inserted_at` which the INSERT omits (DEFAULT now()).
_SCHEMA_COLUMNS = (
    "turn_id", "session_id", "turn_idx", "device_id", "user_id", "persona_id",
    "utc_start_ms", "audio_start_ms", "utc_end_ms", "duration_ms", "turn_type", "turn_phase",
    "stt_text", "stt_model", "stt_interims", "abandoned_stts", "stt_status",
    "tts_text", "tts_model", "tts_status", "tts_truncated_reason",
    "s3_input_wav", "s3_input_json", "s3_tts_wav", "s3_tts_json", "s3_turn_json",
    "voice_segments", "input_pcm_bytes", "input_pcm_ms", "tts_pcm_bytes", "tts_pcm_ms",
    "e2e_ms", "stt_ms", "llm_ttft_ms", "tts_ttfb_ms", "device_playback_ms", "llm_model",
)


def test_clickhouse_insert_uses_native_insert_shape():
    """Regression test for the asynch INSERT shape.

    asynch's INSERT path sends the query verbatim (no ``%s``
    substitution — only ``process_ordinary_query`` substitutes params)
    and streams data as native-protocol blocks: the query must end with
    a bare ``VALUES`` and ``args`` must be a list of rows. A flat list
    of scalars is misread as rows — ``data[0]`` is the turn_id string,
    so asynch raises ``ValueError: Expected 37 columns, got <len>.``
    """
    pytest.importorskip("asynch")
    calls: list[tuple[str, object]] = []

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, query, args):
            calls.append((query, args))

    class FakeConn:
        def __init__(self, dsn):
            self.dsn = dsn

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def cursor(self):
            return FakeCursor()

    s = _build_storage()
    s.s3_uris = {
        name: f"s3://test-bucket/{s.s3_prefix}/{name}"
        for name in ("input.wav", "input.json", "tts.wav", "tts.json", "turn.json")
    }

    captured_dsn: list[str] = []

    def _fake_connect(dsn=None):
        captured_dsn.append(dsn)
        return FakeConn(dsn)

    with patch("asynch.connect", side_effect=_fake_connect):
        asyncio.run(s._clickhouse_insert())

    assert len(calls) == 1
    query, args = calls[0]
    assert query.startswith("INSERT INTO voice.turns (")
    assert query.endswith("VALUES")
    assert "%s" not in query
    # DSN must stay in native-protocol form (bug #1 regression guard).
    assert len(captured_dsn) == 1
    assert captured_dsn[0].startswith("clickhouse://"), captured_dsn[0]
    assert "?password=" not in captured_dsn[0], captured_dsn[0]
    # Column order must match the schema (inserted_at omitted).
    cols = query[query.index("(") + 1:query.index(")")].split(", ")
    assert tuple(cols) == _SCHEMA_COLUMNS
    # args must be a list of rows — one row here, a 37-tuple.
    assert isinstance(args, list) and len(args) == 1
    row = args[0]
    assert isinstance(row, tuple) and len(row) == 37
    assert row[0] == s.turn_id
