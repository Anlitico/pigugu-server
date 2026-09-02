"""Proper-noun vocabulary loader tests.

``vocabulary.conf`` (one term per line) is the config file, separate from code,
that feeds STT keyterm prompting. These tests pin the loader contract: blank
lines and ``#`` comments (full-line or inline) never become terms, whitespace is
trimmed, and the file is read once into an immutable shared cache.
"""

import pytest

from vocabulary import _cached_terms, _read_terms, stt_keyterms


def test_committed_vocabulary_yields_expected_terms():
    terms = stt_keyterms()
    # Product name is always present; the rest is whatever the conf ships.
    assert "Pigugu" in terms
    assert terms
    assert all(isinstance(t, str) and t.strip() for t in terms)


def test_keyterms_return_fresh_list_does_not_corrupt_cache():
    terms = stt_keyterms()
    terms.append("nope")  # mutating the returned copy must not leak in
    assert stt_keyterms() == terms[:-1]


def test_cache_is_single_read_and_immutable():
    cached = _cached_terms()
    assert cached is _cached_terms()
    with pytest.raises(AttributeError):
        cached.append("nope")  # tuple — no append, the cache can't be corrupted


def test_read_terms_skips_comment_only_and_blank_lines(tmp_path):
    f = tmp_path / "vocab.conf"
    f.write_text("# comment\n\n   \n# another\n")
    assert _read_terms(f) == ()


def test_read_terms_trims_and_strips_inline_comments(tmp_path):
    f = tmp_path / "vocab.conf"
    f.write_text("  Pigugu  # the assistant\nTrump\n\nroast # trigger word\n")
    assert _read_terms(f) == ("Pigugu", "Trump", "roast")
