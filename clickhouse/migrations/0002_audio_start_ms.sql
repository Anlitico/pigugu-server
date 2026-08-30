-- 0002_audio_start_ms.sql
-- Real start of the recorded input.wav window (set at the previous
-- turn's TTS start). utc_start_ms is the END of the window (STT final).
ALTER TABLE voice.turns ADD COLUMN IF NOT EXISTS audio_start_ms Int64 DEFAULT 0;
