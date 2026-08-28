"""Per-turn audio + metadata storage: S3 + ClickHouse.

Why this exists
---------------
Today the voice agent saves WAVs to ``/tmp/pigugu_*.wav`` — per-session
and overwritten on every TTS turn. We need a per-turn record (input +
TTS, full PCM, sidecar JSON) that is queryable by user / device /
session / turn, persistent beyond pod lifetime, and includes enough
metadata to selectively replay only the user-voice portions of a
recording.

Layout
------
Every committed turn produces 5 files in S3, grouped under one
``turn_id`` directory::

    s3://pigugu-clickhouse-audio/
      {utc_date}/{session_id}/{turn_id}/
        input.wav         # raw user PCM, 16k mono int16
        input.json        # voice_segments[], stt_interims[], abandoned_stts[], stt_status
        tts.wav           # TTS PCM, possibly 0 bytes if LLM empty
        tts.json          # tts_status, tts_truncated_reason
        turn.json         # turn-level metadata

Plus one row in ClickHouse ``voice.turns`` for indexing.

Commit order (best-effort)
--------------------------
1. Compute voice_segments from Silero chunk flags.
2. **S3 upload first** (5 PUTs, sequential). If any PUT fails → log
   ERROR with ``error_phase="s3"``, abort. No CH INSERT.
3. **ClickHouse INSERT** via asynch. If INSERT fails → log ERROR with
   ``error_phase="clickhouse"``, abort. S3 files remain (orphan, GC
   reaps later).

This ordering ensures we never write a CH row claiming audio that
doesn't exist in S3. Orphan S3 files (no CH row) are the failure mode
we accept; a janitor CronJob (deferred to v2) reconciles them.

Threading
---------
``TurnStorage`` itself is single-threaded (it lives on the asyncio
loop). The interim recording path goes through ``InterimBuffer`` which
is lock-protected. ``commit()`` runs as a fire-and-forget asyncio
task — its failure is logged but never propagates to the WebSocket
loop, matching the existing ``_save_input_wav`` / ``_save_tts_wav``
swallow-and-log behavior.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
import wave
from datetime import datetime, timezone
from typing import Any, Callable

from loguru import logger

from voice.interims import InterimBuffer
from voice.segments import VoiceSegment, compute_voice_segments

# Sample rate for input.wav and tts.wav. Hard-coded because both the
# audio path (Silero at 16kHz) and the TTS path (Cartesia default 16kHz)
# produce 16kHz mono int16 PCM.
_SAMPLE_RATE = 16000
_SAMPLE_WIDTH = 2  # int16
_CHANNELS = 1


# ── Public env helpers ───────────────────────────────────────────────


def is_turn_storage_enabled() -> bool:
    """Read once at startup; gate the whole subsystem. Default ON unless
    explicitly disabled — the new code path is opt-out so a misconfigured
    ClickHouse does not block turn audio in dev."""
    return os.getenv("ENABLE_TURN_STORAGE", "true").lower() in (
        "1", "true", "yes", "on",
    )


def get_utc_date_for_ms(utc_ms: int) -> str:
    """``YYYY-MM-DD`` UTC date for an epoch millisecond timestamp.

    Used for the S3 prefix so a diagnostic tool can compute the right
    prefix without reading the CH row. The bucket layout uses this
    date, NOT the S3 PUT time — if you turn off the agent for a week
    and restart, the files still land under their turn's date.
    """
    return datetime.fromtimestamp(utc_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ── ClickHouse row shape (column order must match INSERT) ───────────

# Tuple of (column_name, value_extractor) used to build the INSERT
# row from a TurnStorage instance. The order MUST match the column
# order in the INSERT statement below. Keep both in sync with
# migrations/0001_voice_turns.sql.
_CH_ROW_EXTRACTORS: tuple[tuple[str, Callable[["TurnStorage"], Any]], ...] = (
    ("turn_id",             lambda s: s.turn_id),
    ("session_id",          lambda s: s.session_id),
    ("turn_idx",            lambda s: s.turn_idx),
    ("device_id",           lambda s: s.device_id),
    ("user_id",             lambda s: s.user_id),
    ("persona_id",          lambda s: s.persona_id),
    ("utc_start_ms",        lambda s: s.utc_start_ms),
    ("utc_end_ms",          lambda s: s.utc_end_ms),
    ("duration_ms",         lambda s: max(0, s.utc_end_ms - s.utc_start_ms)),
    ("turn_type",           lambda s: s.turn_type),
    ("turn_phase",          lambda s: s.turn_phase),
    ("stt_text",            lambda s: s.stt_text),
    ("stt_model",           lambda s: s.stt_model),
    ("stt_interims",        lambda s: s.stt_interims),
    ("abandoned_stts",      lambda s: s.abandoned_stts),
    ("stt_status",          lambda s: s.stt_status),
    ("tts_text",            lambda s: s.tts_text),
    ("tts_model",           lambda s: s.tts_model),
    ("tts_status",          lambda s: s.tts_status),
    ("tts_truncated_reason",lambda s: s.tts_truncated_reason),
    ("s3_input_wav",        lambda s: s.s3_uris["input.wav"]),
    ("s3_input_json",       lambda s: s.s3_uris["input.json"]),
    ("s3_tts_wav",          lambda s: s.s3_uris["tts.wav"]),
    ("s3_tts_json",         lambda s: s.s3_uris["tts.json"]),
    ("s3_turn_json",        lambda s: s.s3_uris["turn.json"]),
    ("voice_segments",      lambda s: [(seg["start_ms"], seg["end_ms"], seg["duration_ms"])
                                      for seg in s.voice_segments]),
    ("input_pcm_bytes",     lambda s: len(s.user_pcm_bytes)),
    ("input_pcm_ms",        lambda s: len(s.user_pcm_bytes) * 1000 // (_SAMPLE_RATE * _SAMPLE_WIDTH * _CHANNELS)),
    ("tts_pcm_bytes",       lambda s: len(s.tts_pcm_buf)),
    ("tts_pcm_ms",          lambda s: len(s.tts_pcm_buf) * 1000 // (_SAMPLE_RATE * _SAMPLE_WIDTH * _CHANNELS)),
    ("e2e_ms",              lambda s: s.telemetry.get("e2e_ms", 0) or 0),
    ("stt_ms",              lambda s: s.telemetry.get("stt_ms", 0) or 0),
    ("llm_ttft_ms",         lambda s: s.telemetry.get("llm_ttft_ms", 0) or 0),
    ("tts_ttfb_ms",         lambda s: s.telemetry.get("tts_ttfb_ms", 0) or 0),
    ("device_playback_ms",  lambda s: s.telemetry.get("device_playback_ms", 0) or 0),
    ("llm_model",           lambda s: s.telemetry.get("llm_model", "")),
)


# ── TurnStorage ──────────────────────────────────────────────────────


class TurnStorage:
    """Per-turn accumulator. Owns the PCM buffers and the sidecar state;
    one instance per turn, scoped to ``ConnectionHandler``.

    Lifecycle:
        storage = TurnStorage(turn_id=..., session_id=..., ...)
        storage.set_user_pcm(asr_audio)         # call once, after STT final
        storage.mark_stt_final(text)            # moves interims into stt_interims[]
        storage.mark_tts_complete(text, ok=True, truncated_reason="")
        asyncio.create_task(storage.commit())   # fire-and-forget
    """

    __slots__ = (
        "turn_id", "session_id", "turn_idx", "device_id", "user_id", "persona_id",
        "utc_start_ms", "utc_end_ms", "turn_type", "turn_phase",
        "stt_text", "stt_model", "stt_interims", "abandoned_stts", "stt_status",
        "tts_text", "tts_model", "tts_status", "tts_truncated_reason",
        "user_pcm_bytes", "tts_pcm_buf", "voice_segments", "telemetry",
        "s3_bucket", "s3_prefix", "s3_uris",
        "clickhouse_dsn", "clickhouse_table",
        "interims", "voice_chunk_flags_slice",
        "commit_started", "_committed",
    )

    def __init__(
        self,
        *,
        turn_id: str,
        session_id: str,
        turn_idx: int,
        device_id: str,
        user_id: str,
        persona_id: int,
        utc_start_ms: int,
        s3_bucket: str,
        s3_prefix: str,
        clickhouse_dsn: str,
        clickhouse_table: str,
        interims: InterimBuffer,
        voice_chunk_flags_slice: Callable[[], list[bool]],
        turn_type: str = "follow_up",
        turn_phase: str = "",
        stt_model: str = "",
        tts_model: str = "",
    ) -> None:
        self.turn_id = turn_id
        self.session_id = session_id
        self.turn_idx = turn_idx
        self.device_id = device_id
        self.user_id = user_id
        self.persona_id = persona_id
        self.utc_start_ms = utc_start_ms
        self.utc_end_ms = utc_start_ms  # updated on commit
        self.turn_type = turn_type
        self.turn_phase = turn_phase
        self.stt_text = ""
        self.stt_model = stt_model
        self.stt_interims: list[str] = []
        self.abandoned_stts: list[str] = []
        self.stt_status = ""
        self.tts_text = ""
        self.tts_model = tts_model
        self.tts_status = ""
        self.tts_truncated_reason = ""
        # PCM buffers — owned by TurnStorage so commit() reads from a
        # frozen snapshot even if the ConnectionHandler resets its own
        # ``asr_audio`` mid-flight.
        self.user_pcm_bytes: bytes = b""
        self.tts_pcm_buf = bytearray()
        # Computed at commit from Silero chunk flags.
        self.voice_segments: list[VoiceSegment] = []
        # Telemetry snapshot (e2e_ms, llm_model, stt_model, etc.)
        self.telemetry: dict[str, Any] = {}
        # S3 layout
        self.s3_bucket = s3_bucket
        utc_date = get_utc_date_for_ms(utc_start_ms)
        self.s3_prefix = (
            f"{s3_prefix.rstrip('/')}/{utc_date}/{session_id}/{turn_id}"
        )
        # Populated at commit() with the full s3:// URIs.
        self.s3_uris: dict[str, str] = {}
        # ClickHouse
        self.clickhouse_dsn = clickhouse_dsn
        self.clickhouse_table = clickhouse_table
        # Cross-thread interims + Silero chunk flags (read at commit).
        # The slice closure is captured at TurnStorage construction time
        # and returns only the per-turn range of chunk flags, so
        # compute_voice_segments() never sees another turn's voice.
        self.interims = interims
        self.voice_chunk_flags_slice = voice_chunk_flags_slice
        # Idempotency
        self.commit_started = False
        self._committed = False

    # ── Mutation API (called from the asyncio loop) ─────────────────

    def set_user_pcm(self, pcm: bytes) -> None:
        """Freeze the user PCM at STT-final time. Subsequent resets of
        ``ConnectionHandler.asr_audio`` don't affect this."""
        self.user_pcm_bytes = bytes(pcm)

    def mark_stt_final(self, text: str) -> None:
        """Set the STT final text. Drains the interim buffer into
        ``stt_interims[]`` so the sidecar JSON records the conversation
        the LLM was driven by."""
        text = (text or "").strip()
        self.stt_text = text
        self.stt_status = "final" if text else "no_stt"
        if text:
            self.stt_interims = self.interims.drain()
        else:
            # If STT final was empty, anything buffered is abandoned.
            self.abandoned_stts = self.interims.drain()

    def mark_stt_abandoned(self) -> None:
        """Set by barge-in: whatever interims were in the buffer were
        never finalized, so they move to ``abandoned_stts[]``."""
        self.stt_status = "abandoned"
        self.abandoned_stts = self.interims.drain()

    def mark_tts_complete(
        self,
        text: str,
        *,
        ok: bool,
        truncated_reason: str = "",
    ) -> None:
        """Set by ``_tts_producer_consumer`` finally block.

        ``ok=True`` means TTS reached ``tts/stop``; ``ok=False`` means
        it was cancelled (barge-in / abort) and the sidecar should
        record why.
        """
        self.tts_text = (text or "").strip()
        if not self.tts_text:
            self.tts_status = "empty"
        elif ok and not truncated_reason:
            self.tts_status = "complete"
        else:
            self.tts_status = "aborted" if ok else "interrupted"
        self.tts_truncated_reason = truncated_reason or self.tts_truncated_reason

    def set_turn_type(self, turn_type: str) -> None:
        self.turn_type = turn_type

    def set_turn_phase(self, turn_phase: str) -> None:
        self.turn_phase = turn_phase

    def set_telemetry(self, snapshot: dict[str, Any]) -> None:
        """Update the latency / model snapshot. Called once at commit
        time so it reflects the final ``TelemetryCollector`` state."""
        if snapshot:
            self.telemetry.update(snapshot)

    # ── File payload builders (pure) ────────────────────────────────

    def _build_input_wav_bytes(self) -> bytes:
        """Serialize the raw user PCM to a 16k mono int16 WAV."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(_CHANNELS)
            wf.setsampwidth(_SAMPLE_WIDTH)
            wf.setframerate(_SAMPLE_RATE)
            wf.writeframes(self.user_pcm_bytes)
        return buf.getvalue()

    def _build_tts_wav_bytes(self) -> bytes:
        """Serialize the TTS PCM to a 16k mono int16 WAV. May be empty
        (0 bytes) if LLM was empty and TTS never produced a frame."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(_CHANNELS)
            wf.setsampwidth(_SAMPLE_WIDTH)
            wf.setframerate(_SAMPLE_RATE)
            wf.writeframes(bytes(self.tts_pcm_buf))
        return buf.getvalue()

    def _build_input_json(self) -> bytes:
        payload = {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "turn_idx": self.turn_idx,
            "utc_start_ms": self.utc_start_ms,
            "sample_rate": _SAMPLE_RATE,
            "channels": _CHANNELS,
            "sample_width": _SAMPLE_WIDTH,
            "pcm_bytes": len(self.user_pcm_bytes),
            "pcm_ms": len(self.user_pcm_bytes) * 1000 // (_SAMPLE_RATE * _SAMPLE_WIDTH * _CHANNELS),
            "stt_status": self.stt_status,
            "stt_text": self.stt_text,
            "stt_model": self.stt_model,
            "stt_interims": self.stt_interims,
            "abandoned_stts": self.abandoned_stts,
            "voice_segments": self.voice_segments,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def _build_tts_json(self) -> bytes:
        payload = {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "turn_idx": self.turn_idx,
            "utc_start_ms": self.utc_start_ms,
            "sample_rate": _SAMPLE_RATE,
            "channels": _CHANNELS,
            "sample_width": _SAMPLE_WIDTH,
            "pcm_bytes": len(self.tts_pcm_buf),
            "pcm_ms": len(self.tts_pcm_buf) * 1000 // (_SAMPLE_RATE * _SAMPLE_WIDTH * _CHANNELS),
            "tts_status": self.tts_status,
            "tts_text": self.tts_text,
            "tts_model": self.tts_model,
            "tts_truncated_reason": self.tts_truncated_reason,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def _build_turn_json(self) -> bytes:
        payload = {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "turn_idx": self.turn_idx,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "persona_id": self.persona_id,
            "turn_type": self.turn_type,
            "turn_phase": self.turn_phase,
            "utc_start_ms": self.utc_start_ms,
            "utc_end_ms": self.utc_end_ms,
            "duration_ms": max(0, self.utc_end_ms - self.utc_start_ms),
            "models": {
                "stt": self.stt_model,
                "llm": self.telemetry.get("llm_model", ""),
                "tts": self.tts_model,
            },
            "telemetry": self.telemetry,
            "s3_uris": self.s3_uris,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    # ── Commit (fire-and-forget) ────────────────────────────────────

    async def commit(self) -> None:
        """Best-effort: build 5 files, S3-upload them, then INSERT into
        ClickHouse. Failures are logged with ``error_phase`` but never
        propagate to the WebSocket loop.

        Idempotent: calling ``commit()`` twice is a no-op (the second
        call short-circuits at ``self._committed``). This protects
        against the existing ``_save_input_wav`` / ``_save_tts_wav``
        call sites being invoked twice during cleanup.
        """
        if self._committed or self.commit_started:
            return
        self.commit_started = True
        self.utc_end_ms = int(time.time() * 1000)
        t0 = time.perf_counter()

        # 1. Compute voice segments from the per-turn slice of Silero
        # chunk flags. The slice closure is owned by the ConnectionHandler
        # and returns only the chunks for THIS turn.
        try:
            per_turn_flags = self.voice_chunk_flags_slice()
            self.voice_segments = compute_voice_segments(per_turn_flags)
        except Exception:
            logger.exception("[Storage] voice_segments compute failed")
            self.voice_segments = []

        # 2. Build payloads.
        try:
            payloads: dict[str, bytes] = {
                "input.wav": self._build_input_wav_bytes(),
                "input.json": self._build_input_json(),
                "tts.wav": self._build_tts_wav_bytes(),
                "tts.json": self._build_tts_json(),
                "turn.json": self._build_turn_json(),
            }
        except Exception:
            logger.exception(
                f"[Storage] payload build failed turn_id={self.turn_id} "
                f"error_phase=build"
            )
            return

        # 3. S3 upload (sequential). If any PUT fails, abort.
        try:
            await self._s3_upload_all(payloads)
        except Exception as e:
            logger.error(
                f"[Storage] S3 upload failed turn_id={self.turn_id} "
                f"error_phase=s3 prefix={self.s3_prefix} err={e!r}"
            )
            return

        s3_ms = int((time.perf_counter() - t0) * 1000)

        # 4. ClickHouse INSERT. Failure logs; S3 files remain (orphan).
        try:
            await self._clickhouse_insert()
        except Exception as e:
            logger.error(
                f"[Storage] ClickHouse INSERT failed turn_id={self.turn_id} "
                f"error_phase=clickhouse err={e!r} "
                f"orphan_s3_prefix={self.s3_bucket}/{self.s3_prefix}"
            )
            self._committed = True
            return

        ch_ms = int((time.perf_counter() - t0) * 1000)
        total_ms = ch_ms  # since t0 includes s3
        logger.info(
            f"[Storage] committed turn_id={self.turn_id} "
            f"s3={s3_ms}ms ch={ch_ms - s3_ms}ms total={total_ms}ms "
            f"input_bytes={len(self.user_pcm_bytes)} "
            f"tts_bytes={len(self.tts_pcm_buf)} "
            f"voice_segments={len(self.voice_segments)} "
            f"stt_status={self.stt_status} tts_status={self.tts_status}"
        )
        self._committed = True

    # ── S3 + ClickHouse I/O (overridable in tests) ──────────────────

    async def _s3_upload_all(self, payloads: dict[str, bytes]) -> None:
        """Sequential S3 PUT. Uses ``aioboto3`` if available, else falls
        back to ``boto3`` in a thread executor.

        We import lazily so unit tests that don't touch S3 don't have to
        install boto3 (it is already a project dep, but a unit test
        running outside a venv might not have it).
        """
        try:
            import aioboto3  # type: ignore[import-not-found]
            session = aioboto3.Session()
            async with session.client("s3", region_name=os.getenv("AWS_REGION")) as s3:
                for name, data in payloads.items():
                    key = f"{self.s3_prefix}/{name}"
                    self.s3_uris[name] = f"s3://{self.s3_bucket}/{key}"
                    await s3.put_object(
                        Bucket=self.s3_bucket,
                        Key=key,
                        Body=data,
                        ContentType=(
                            "audio/wav" if name.endswith(".wav")
                            else "application/json"
                        ),
                    )
            return
        except ImportError:
            pass

        # Fallback: boto3 sync, run in default executor so we don't
        # block the asyncio loop on the 5 PUTs.
        import boto3  # type: ignore[import-untyped]
        loop = asyncio.get_running_loop()
        client = boto3.client("s3", region_name=os.getenv("AWS_REGION"))
        for name, data in payloads.items():
            key = f"{self.s3_prefix}/{name}"
            self.s3_uris[name] = f"s3://{self.s3_bucket}/{key}"
            await loop.run_in_executor(
                None,
                lambda k=key, d=data, n=name: client.put_object(
                    Bucket=self.s3_bucket,
                    Key=k,
                    Body=d,
                    ContentType=(
                        "audio/wav" if n.endswith(".wav")
                        else "application/json"
                    ),
                ),
            )

    async def _clickhouse_insert(self) -> None:
        """One INSERT into ``voice.turns``. Uses ``asynch`` (async
        ClickHouse Python driver) on the running event loop.

        We import lazily so unit tests can mock without installing
        asynch.
        """
        from asynch import connect as ch_connect  # type: ignore[import-not-found]

        # asynch's INSERT path does NOT substitute %s placeholders — the
        # query goes to the server verbatim and the data is streamed as
        # native-protocol blocks. So the query ends with a bare VALUES and
        # the data must be a list of rows (one row here), never a flat list
        # of scalars (a flat list is misread as "rows" and len(row) is then
        # the length of the first scalar, e.g. the turn_id string).
        values: list[tuple[Any, ...]] = [
            tuple(extractor(self) for _, extractor in _CH_ROW_EXTRACTORS)
        ]
        columns = ", ".join(name for name, _ in _CH_ROW_EXTRACTORS)

        # asynch's connect() is synchronous — it returns a Connection,
        # not a coroutine. Awaiting it raises TypeError; `async with`
        # opens/closes the connection.
        conn = ch_connect(self.clickhouse_dsn)
        async with conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"INSERT INTO {self.clickhouse_table} ({columns}) VALUES",
                    values,
                )
