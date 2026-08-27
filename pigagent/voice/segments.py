"""Voice segment detection — pure function over a list of per-chunk VAD flags.

The Silero VAD already runs on every audio frame and produces a per-32ms-chunk
``is_voice`` boolean. We piggyback on that signal instead of running a new
RMS-based detector, so this module has zero dependencies and is trivially
testable.

Wire-up (in ``silero.py``): after computing ``is_speech`` for the current
chunk, append to ``conn._voice_chunk_flags`` (created on first use, bounded
to ~10 minutes of chunks to keep memory flat across long sessions).

Wire-up (in ``TurnStorage``): at ``start_turn()`` snapshot
``len(conn._voice_chunk_flags)`` as the chunk offset for this turn; at
``commit()`` slice the flags from that offset and feed them to
``compute_voice_segments`` to produce the ``voice_segments[]`` sidecar list.

Output format (matches ClickHouse ``voice.turns.voice_segments``):

    [
      {"start_ms": 123, "end_ms": 456, "duration_ms": 333},
      ...
    ]
"""

from __future__ import annotations

from typing import TypedDict


class VoiceSegment(TypedDict):
    start_ms: int
    end_ms: int
    duration_ms: int


def compute_voice_segments(
    chunk_flags: list[bool],
    *,
    ms_per_chunk: float = 32.0,
    min_silence_chunks: int = 10,
) -> list[VoiceSegment]:
    """Convert a per-chunk VAD boolean sequence into a list of voice segments.

    A "voice segment" is a contiguous run of ``True`` chunks bounded by at
    least ``min_silence_chunks`` consecutive ``False`` chunks (default 10
    chunks = 320ms at 32ms/chunk). The 320ms gap mirrors the existing
    EOU-bounce delay in the voice pipeline, so segments align with how the
    STT/TTS pipeline actually perceives pauses.

    Args:
        chunk_flags: per-chunk ``is_voice`` booleans from Silero, in
            chronological order. Empty list is valid and returns ``[]``.
        ms_per_chunk: how many milliseconds each chunk represents. The
            Silero chunk size in ``silero.py`` is 32ms (16kHz, 512 samples).
        min_silence_chunks: how many consecutive non-voice chunks close a
            segment. Default 10 = 320ms.

    Returns:
        List of ``{start_ms, end_ms, duration_ms}`` dicts, sorted by
        ``start_ms``. ``end_ms`` is exclusive (the end of the last
        voice chunk, not the start of the trailing silence). ``start_ms``
        and ``end_ms`` are rounded to the nearest millisecond.
    """
    if not chunk_flags:
        return []

    segments: list[VoiceSegment] = []
    seg_start: int | None = None
    silence_run = 0

    for idx, is_voice in enumerate(chunk_flags):
        if is_voice:
            if seg_start is None:
                seg_start = idx
            silence_run = 0
        else:
            silence_run += 1
            if seg_start is not None and silence_run >= min_silence_chunks:
                # Close the segment at the chunk BEFORE the silence gap
                # began, so end_ms lands on the last voice chunk boundary.
                end_idx = idx - silence_run + 1
                start_ms = round(seg_start * ms_per_chunk)
                end_ms = round(end_idx * ms_per_chunk)
                segments.append(
                    VoiceSegment(
                        start_ms=start_ms,
                        end_ms=end_ms,
                        duration_ms=max(0, end_ms - start_ms),
                    )
                )
                seg_start = None
                silence_run = 0

    # Close a trailing voice segment that runs to the end of the buffer
    # without a closing silence gap.
    if seg_start is not None:
        end_idx = len(chunk_flags)
        start_ms = round(seg_start * ms_per_chunk)
        end_ms = round(end_idx * ms_per_chunk)
        segments.append(
            VoiceSegment(
                start_ms=start_ms,
                end_ms=end_ms,
                duration_ms=max(0, end_ms - start_ms),
            )
        )

    return segments
