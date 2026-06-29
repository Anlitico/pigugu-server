"""Domain weights + scoring formula for topic ranking.

Tunable per event season — bump sports during World Cup, politics during elections.
No code changes needed: just edit this file or override via env DOMAIN_WEIGHTS_JSON.
"""

from __future__ import annotations

import json
import os

# ── Domain base weights ────────────────────────────────────────────────────
# Higher = more likely to be selected, all else equal.
# Multiply with raw score to get final_score.

DOMAIN_WEIGHTS: dict[str, float] = {
    "Politics": 1.0,
    "Economy": 1.0,
    "Tech": 0.9,
    "Business": 0.8,
    "Social": 0.8,
    "Health": 0.8,
    "Climate": 0.8,
    "International": 0.7,
    "Sports": 0.5,
    "Entertainment": 0.4,
    "Science": 0.6,
    "Immigration": 0.9,
    "Housing": 0.9,
}

# ── Dimension weights for raw score ────────────────────────────────────────
# Raw score = Σ(dimension_score × weight) — domain-agnostic.

DIMENSION_WEIGHTS: dict[str, float] = {
    "us_relevance": 0.30,       # Direct impact on American daily life
    "roast_potential": 0.25,    # Absurdity / irony / hypocrisy level
    "controversy": 0.15,        # Position split / debatability
    "timeliness": 0.15,         # Recency & heat
    "social_buzz": 0.10,        # Social media discussion volume
    "trump_related": 0.05,      # Trump connection — intentionally LOW
}


def load_weights() -> tuple[dict[str, float], dict[str, float]]:
    """Load weights, optionally overriding from env DOMAIN_WEIGHTS_JSON.

    Example env override:
        DOMAIN_WEIGHTS_JSON='{"Sports":0.8,"Politics":0.9}'
    """
    domain = dict(DOMAIN_WEIGHTS)
    dim = dict(DIMENSION_WEIGHTS)

    override = os.environ.get("DOMAIN_WEIGHTS_JSON", "")
    if override:
        try:
            domain.update(json.loads(override))
        except json.JSONDecodeError:
            pass

    return domain, dim


def get_domain_weight(domain: str) -> float:
    """Get base weight for a domain, defaulting to 0.5 for unknown."""
    return DOMAIN_WEIGHTS.get(domain, 0.5)


def compute_raw_score(scores: dict[str, float]) -> float:
    """Compute weighted raw score from dimension scores.

    Args:
        scores: dict with keys matching DIMENSION_WEIGHTS (1-5 scale each).

    Returns:
        Weighted raw score (0.0 - 5.0).
    """
    _, dim_w = load_weights()
    total = 0.0
    for key, weight in dim_w.items():
        total += scores.get(key, 2.5) * weight
    return round(total, 2)


def compute_final_score(domain: str, raw_score: float) -> float:
    """Apply domain weight to raw score."""
    domain_w = get_domain_weight(domain)
    return round(raw_score * domain_w, 2)
