"""Proper-noun vocabulary the STT must hear — product/assistant name + role names.

Read from ``vocabulary.conf`` (this directory, one term per line) into an
in-process cache at first use. Kept as a plain config file, separate from code,
so adding a term is a one-line file edit. The STT providers turn these into
keyterm prompting (AssemblyAI ``keyterms_prompt``, Deepgram ``keyterm``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_CONFIG = Path(__file__).parent / "vocabulary.conf"


def _read_terms(path: Path) -> tuple[str, ...]:
    """Parse a term-per-line vocabulary file (test seam; no cache).

    Blank lines are skipped and a ``#`` starts a comment anywhere in a line, so
    a term never carries one. Whitespace is trimmed.
    """
    terms: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = raw.split("#", 1)[0].strip()
        if term:
            terms.append(term)
    return tuple(terms)


@lru_cache(maxsize=1)
def _cached_terms() -> tuple[str, ...]:
    """Terms read once from the committed file (startup config — a new line is
    picked up after a restart). A tuple, so the cache is immutable and no caller
    can corrupt the process-wide list.
    """
    return _read_terms(_CONFIG)


def stt_keyterms() -> list[str]:
    """Proper nouns the STT decoder must hear, as keyterm strings.

    Returns a fresh copy of the cached terms — callers may mutate the result
    without affecting the shared cache.
    """
    return list(_cached_terms())
