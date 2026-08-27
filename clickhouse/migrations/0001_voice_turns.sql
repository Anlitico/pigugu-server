-- 0001_voice_turns.sql
-- Per-turn voice agent audio + metadata index.
--
-- Why this exists:
--   The voice agent saves WAVs to /tmp/pigugu_*.wav (per-session,
--   overwritten on every TTS turn). We need a per-turn record
--   (input + TTS, full PCM, sidecar JSON) that is queryable by user /
--   device / session / turn, persistent beyond pod lifetime, and
--   includes enough metadata to selectively replay only the
--   user-voice portions of a recording.
--
-- Storage layout (paired with the agent's TurnStorage):
--   s3://pigugu-clickhouse-audio/
--     {utc_date}/{session_id}/{turn_id}/
--       input.wav, input.json, tts.wav, tts.json, turn.json
--
--   turn_id format: {utc_start_ms}_{session_id}_{turn_idx:04d}
--                   e.g. 1787734749647_fef37f1f_0001
--
-- Commit order (best-effort, see pigagent/voice/storage.py):
--   1. S3 upload 5 files
--   2. INSERT this row
--   If S3 fails, no row is written. If CH INSERT fails, the S3 files
--   remain (orphan, GC reaps later). A janitor k8s CronJob
--   reconciles them; deferred to v2.
--
-- Retention: 365 days via TTL.

CREATE DATABASE IF NOT EXISTS voice;

CREATE TABLE IF NOT EXISTS voice.turns
(
    -- Identity
    turn_id            String,                          -- {utc_start_ms}_{session}_{turn_idx:04d}
    session_id         String,
    turn_idx           UInt32,                          -- per-session ordinal (1-based)
    device_id          LowCardinality(String),          -- client_id from hello
    user_id            LowCardinality(String),
    persona_id         UInt16,

    -- Timing (UTC ms)
    utc_start_ms       Int64,
    utc_end_ms         Int64,
    duration_ms        Int32,

    -- Turn classification
    turn_type          LowCardinality(String),          -- wake_word | follow_up | inject | interrupted
    turn_phase         LowCardinality(String) DEFAULT '',

    -- User speech
    stt_text           String DEFAULT '',
    stt_model          LowCardinality(String) DEFAULT '',
    stt_interims       Array(String) DEFAULT [],        -- every Deepgram interim during this turn
    abandoned_stts     Array(String) DEFAULT [],        -- interims orphaned by barge-in
    stt_status         LowCardinality(String) DEFAULT '',  -- final | abandoned | no_stt | interrupted

    -- TTS
    tts_text           String DEFAULT '',
    tts_model          LowCardinality(String) DEFAULT '',
    tts_status         LowCardinality(String) DEFAULT '',  -- complete | aborted | interrupted | empty
    tts_truncated_reason LowCardinality(String) DEFAULT '',

    -- S3 paths (one URI per artifact)
    s3_input_wav       String DEFAULT '',
    s3_input_json      String DEFAULT '',
    s3_tts_wav         String DEFAULT '',
    s3_tts_json        String DEFAULT '',
    s3_turn_json       String DEFAULT '',

    -- Voice activity (debug aid for selective replay)
    -- Each tuple = (start_ms, end_ms, duration_ms) of a contiguous
    -- voice run, derived from Silero per-chunk is_voice flags.
    -- 32ms chunks; gaps > 320ms (10 chunks) close a segment.
    voice_segments     Array(Tuple(start_ms Int32, end_ms Int32, duration_ms Int32)) DEFAULT [],
    input_pcm_bytes    UInt32 DEFAULT 0,
    input_pcm_ms       UInt32 DEFAULT 0,
    tts_pcm_bytes      UInt32 DEFAULT 0,
    tts_pcm_ms         UInt32 DEFAULT 0,

    -- Latency (denormalized from TelemetryCollector for query convenience)
    e2e_ms             Int32 DEFAULT 0,
    stt_ms             Int32 DEFAULT 0,
    llm_ttft_ms        Int32 DEFAULT 0,
    tts_ttfb_ms        Int32 DEFAULT 0,
    device_playback_ms Int32 DEFAULT 0,

    -- Models
    llm_model          LowCardinality(String) DEFAULT '',

    inserted_at        DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(fromUnixTimestamp64Milli(utc_start_ms))
ORDER BY (device_id, fromUnixTimestamp64Milli(utc_start_ms), turn_id)
TTL toDateTime(fromUnixTimestamp64Milli(utc_start_ms)) + INTERVAL 365 DAY;

-- Verification queries for ops:
--   SELECT turn_id, stt_text, tts_status, voice_segments
--     FROM voice.turns
--    WHERE device_id = 'fef37f1f'
--      AND fromUnixTimestamp64Milli(utc_start_ms) >= now() - INTERVAL 1 DAY
--    ORDER BY utc_start_ms DESC
--    LIMIT 10;
