"""Tests for voice.segments.compute_voice_segments.

These cover the unit-level edge cases the new voice.turns sidecar
relies on: empty input, all-silence, all-voice, multi-segment with
gaps, gap-merge below threshold, and gap-split above threshold.
"""
from voice.segments import compute_voice_segments


def test_empty():
    assert compute_voice_segments([]) == []


def test_all_silence():
    assert compute_voice_segments([False] * 100) == []


def test_all_voice():
    segs = compute_voice_segments([True] * 100)
    assert len(segs) == 1
    assert segs[0]["start_ms"] == 0
    assert segs[0]["end_ms"] == 3200  # 100 chunks × 32ms
    assert segs[0]["duration_ms"] == 3200


def test_single_short_segment():
    """A 3-chunk voice run surrounded by silence → one short segment."""
    flags = [False] * 5 + [True, True, True] + [False] * 20
    segs = compute_voice_segments(flags)
    assert len(segs) == 1
    assert segs[0]["start_ms"] == 160  # 5 × 32
    assert segs[0]["end_ms"] == 256   # 8 × 32
    assert segs[0]["duration_ms"] == 96


def test_two_segments_with_long_gap():
    """Two voice runs separated by > 320ms (10 chunks) of silence →
    two separate segments."""
    flags = [True] * 5 + [False] * 15 + [True] * 5 + [False] * 15
    segs = compute_voice_segments(flags)
    assert len(segs) == 2
    assert segs[0]["start_ms"] == 0
    assert segs[0]["end_ms"] == 160
    assert segs[1]["start_ms"] == 640
    assert segs[1]["end_ms"] == 800


def test_short_gap_merges_into_one_segment():
    """5-chunk silence gap is below the 10-chunk threshold → both
    voice runs merge into one continuous segment."""
    flags = [True] * 5 + [False] * 5 + [True] * 5 + [False] * 20
    segs = compute_voice_segments(flags)
    assert len(segs) == 1
    # End at last voice chunk of the second run (idx 14 → 480ms)
    assert segs[0]["start_ms"] == 0
    assert segs[0]["end_ms"] == 480


def test_trailing_voice_without_closing_silence():
    """A voice run that runs to the end of the buffer without a
    closing silence should still be captured."""
    flags = [False] * 10 + [True] * 5
    segs = compute_voice_segments(flags)
    assert len(segs) == 1
    assert segs[0]["start_ms"] == 320
    assert segs[0]["end_ms"] == 480


def test_exact_threshold_gap():
    """A 10-chunk silence gap exactly equals the threshold → split."""
    flags = [True] * 3 + [False] * 10 + [True] * 3 + [False] * 20
    segs = compute_voice_segments(flags)
    assert len(segs) == 2
    # First segment ends at last voice chunk of first run (idx 2 → 96ms)
    assert segs[0]["end_ms"] == 96
    # Second segment starts at idx 13 → 416ms
    assert segs[1]["start_ms"] == 416


def test_nine_chunk_gap_still_merges():
    """9 chunks < 10 threshold → merge into one segment."""
    flags = [True] * 3 + [False] * 9 + [True] * 3 + [False] * 20
    segs = compute_voice_segments(flags)
    assert len(segs) == 1


def test_custom_chunk_size():
    """The 32ms default comes from Silero; a 16ms chunk would
    halve the segment timestamps."""
    flags = [True] * 4 + [False] * 30
    segs = compute_voice_segments(flags, ms_per_chunk=16.0, min_silence_chunks=10)
    assert len(segs) == 1
    assert segs[0]["start_ms"] == 0
    assert segs[0]["end_ms"] == 64  # 4 × 16


def test_three_segments_chronological():
    """Three voice runs in order are returned in order."""
    flags = (
        [True] * 4 + [False] * 12
        + [True] * 4 + [False] * 12
        + [True] * 4 + [False] * 12
    )
    segs = compute_voice_segments(flags)
    assert len(segs) == 3
    assert segs[0]["start_ms"] < segs[1]["start_ms"] < segs[2]["start_ms"]


def test_segments_chronological_invariant():
    """Stress: many small runs interleaved with various gaps."""
    flags: list[bool] = []
    for _ in range(5):
        flags += [True] * 3 + [False] * 12  # long gap → new segment
    segs = compute_voice_segments(flags)
    assert len(segs) == 5
    for prev, nxt in zip(segs, segs[1:]):
        assert prev["end_ms"] <= nxt["start_ms"]
