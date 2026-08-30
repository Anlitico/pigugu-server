-- 0003_listen_wav.sql
-- S3 URI of the post-turn upstream mic wav (AEC probe): the audio between
-- this turn's input.wav end and the next turn's input start (or session end).
ALTER TABLE voice.turns ADD COLUMN IF NOT EXISTS s3_listen_wav String DEFAULT '';
