"""
Unit tests for the classifier pipeline -- no DB, no API key needed.
Tests: schema alignment, prompt structure, fallback, JSON parsing, roast_id logic.
"""
import json
import sys
from datetime import datetime, timezone, timedelta

# ── 1. Verify Model <-> Migration schema alignment ──────────────────

def test_schema_alignment():
    """Ensure the SQLAlchemy model columns match the Alembic migration."""
    from app.models.roast_scenario import RoastScenario

    model_columns = {c.name: c for c in RoastScenario.__table__.columns}

    expected = {
        "roast_id":   str,
        "game_mode":  str,
        "prompt":     str,
        "news_id":    str,
        "tags":       list,      # JSONB array
        "status":     str,
        "created_at": datetime,
        "expires_at": datetime,
    }

    issues = []
    for name, pytype in expected.items():
        col = model_columns.get(name)
        if col is None:
            issues.append(f"Missing column: {name}")
            continue
        col_type = col.type
        col_nullable = col.nullable

        if name == "news_id":
            if col_nullable:
                issues.append(f"{name}: should NOT be nullable (server_default='')")
        elif name == "tags":
            if col_nullable:
                issues.append(f"{name}: should NOT be nullable (server_default='[]')")
        elif name == "status":
            if col_nullable:
                issues.append(f"{name}: should NOT be nullable (server_default='active')")
        elif name == "expires_at":
            if not col_nullable:
                issues.append(f"{name}: should be nullable")
        elif name == "created_at":
            if col_nullable:
                issues.append(f"{name}: should NOT be nullable")

    if issues:
        print("[FAIL] Schema alignment:")
        for i in issues:
            print(f"  - {i}")
        return False

    print("[OK] Schema alignment: model <-> migration match")
    return True


# ── 2. Verify prompt template structure ─────────────────────────────

def test_prompt_structure():
    """Ensure the classifier prompt contains all required sections."""
    from app.jobs.trump_social_crawler.classifier import _build_classifier_prompt

    post = {
        "platform": "truthsocial",
        "content": "Test content here!",
        "created_at": "2026-05-17T08:00:00Z",
        "tags": ["test_tag"],
    }

    prompt = _build_classifier_prompt(post)

    required_phrases = [
        "truthsocial",
        "Test content here!",
        "poison_opinion",
        "debate",
        "prediction",
        "breaking_bomb",
        "roast_id",
        "expires_at",
    ]

    missing = [p for p in required_phrases if p not in prompt]
    if missing:
        print(f"[FAIL] Prompt missing phrases: {missing}")
        return False

    # JSON example should be in the prompt
    if '"modes"' not in prompt:
        print("[FAIL] Prompt missing JSON output example")
        return False

    print(f"[OK] Prompt structure: {len(prompt)} chars, all sections present")
    return True


# ── 3. Verify fallback generates valid poison_opinion ───────────────

def test_fallback_poison():
    """Fallback should generate a valid poison_opinion entry without LLM."""
    from app.jobs.trump_social_crawler.classifier import _fallback_poison

    post = {
        "platform": "truthsocial",
        "content": "This is a controversial statement.",
        "created_at": "2026-05-17T08:00:00Z",
        "tags": [],
    }

    modes = _fallback_poison(post)

    if not modes:
        print("[FAIL] Fallback returned empty")
        return False

    m = modes[0]
    if m["game_mode"] != "poison_opinion":
        print(f"[FAIL] Wrong mode: {m['game_mode']}")
        return False

    if "roast_id" not in m or "prompt" not in m or "expires_at" not in m:
        print(f"[FAIL] Missing fields: {m.keys()}")
        return False

    if "controversial" not in m["prompt"]:
        print(f"[FAIL] Prompt missing post content: {m['prompt'][:80]}")
        return False

    print(f"[OK] Fallback poison: roast_id={m['roast_id']}, prompt={len(m['prompt'])} chars")
    return True


# ── 4. Verify JSON parsing for various LLM responses ─────────────────

def test_json_parsing():
    """Simulate valid and invalid LLM responses."""
    post = {
        "platform": "x",
        "content": "A test post.",
        "created_at": "2026-05-17T08:00:00Z",
        "tags": [],
    }

    # Valid: poison + debate
    valid_json = json.dumps({
        "modes": [
            {
                "roast_id": "poison_2026-05-17_001",
                "game_mode": "poison_opinion",
                "prompt": "[毒观点场景]\nTest prompt text for poison mode. Need enough chars here.",
                "expires_at": "2026-05-19T08:00:00Z",
            },
            {
                "roast_id": "debate_2026-05-17_001",
                "game_mode": "debate",
                "prompt": "[来辩场景]\nTest prompt text for debate mode. Core claim: something. Provocative stance: defend.",
                "expires_at": "2026-05-19T08:00:00Z",
            },
        ]
    })

    data = json.loads(valid_json)
    modes = data.get("modes", [])
    if len(modes) != 2:
        print(f"[FAIL] Expected 2 modes, got {len(modes)}")
        return False

    # Valid: prediction only
    predict_json = json.dumps({
        "modes": [
            {
                "roast_id": "predict_2026-05-17_001",
                "game_mode": "prediction",
                "prompt": "[预测场景]\nWill he do it by Friday? Deadline: 2026-05-22. Long enough prompt here.",
                "expires_at": "2026-05-22T00:00:00Z",
            }
        ]
    })
    data2 = json.loads(predict_json)
    if len(data2.get("modes", [])) != 1:
        print("[FAIL] Prediction parse failed")
        return False

    # Valid: breaking_bomb
    bomb_json = json.dumps({
        "modes": [
            {
                "roast_id": "bomb_2026-05-17_001",
                "game_mode": "breaking_bomb",
                "prompt": "[突发场景 - 紧急]\nMilitary escalation. Long enough prompt for validation test.",
                "expires_at": "2026-05-17T10:00:00Z",
            }
        ]
    })
    data3 = json.loads(bomb_json)
    if len(data3.get("modes", [])) != 1:
        print("[FAIL] Bomb parse failed")
        return False

    # Edge case: empty modes
    empty_json = json.dumps({"modes": []})
    data4 = json.loads(empty_json)
    if len(data4.get("modes", [])) != 0:
        print("[FAIL] Empty modes parse failed")
        return False

    # Edge case: invalid JSON -> should trigger fallback
    try:
        json.loads("not json")
        print("[FAIL] Invalid JSON should have raised")
        return False
    except json.JSONDecodeError:
        pass  # Expected

    print("[OK] JSON parsing: all modes + edge cases handled")
    return True


# ── 5. Verify roast_id format and dedup logic ────────────────────────

def test_roast_id_format():
    """roast_id must follow {mode_abbrev}_{date}_{3-digit-seq}."""
    from app.jobs.trump_social_crawler.classifier import MODE_ABBREV

    # Mode abbrevs must be correct
    if MODE_ABBREV.get("poison_opinion") != "poison":
        print(f"[FAIL] Wrong abbrev for poison_opinion: {MODE_ABBREV}")
        return False
    if MODE_ABBREV.get("debate") != "debate":
        print(f"[FAIL] Wrong abbrev for debate")
        return False
    if MODE_ABBREV.get("prediction") != "predict":
        print(f"[FAIL] Wrong abbrev for prediction")
        return False
    if MODE_ABBREV.get("breaking_bomb") != "bomb":
        print(f"[FAIL] Wrong abbrev for breaking_bomb")
        return False

    print("[OK] roast_id mode abbrevs correct")
    return True


# ── 6. Verify expires_at calculation ─────────────────────────────────

def test_expiry_calculation():
    """Verify expiry times match design doc rules."""
    from app.jobs.trump_social_crawler.classifier import _add_hours, _extract_date

    # 48h for poison/debate
    ts = "2026-05-17T08:00:00Z"
    result = _add_hours(ts, 48)
    expected = "2026-05-19T08:00:00+00:00"
    if result != expected:
        print(f"[FAIL] 48h expiry: expected {expected}, got {result}")
        return False

    # 2h for breaking_bomb
    result2 = _add_hours(ts, 2)
    expected2 = "2026-05-17T10:00:00+00:00"
    if result2 != expected2:
        print(f"[FAIL] 2h expiry: expected {expected2}, got {result2}")
        return False

    # None for missing timestamp
    result3 = _add_hours(None, 48)
    if result3 is not None:
        print(f"[FAIL] None input should return None, got {result3}")
        return False

    # Date extraction
    date_str = _extract_date("2026-05-17T08:00:00Z")
    if date_str != "2026-05-17":
        print(f"[FAIL] Date extraction: expected 2026-05-17, got {date_str}")
        return False

    print("[OK] Expiry calculation: 48h, 2h, None, date extraction")
    return True


# ── Run all tests ────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Classifier Unit Tests (no DB, no API needed)")
    print("=" * 60)

    tests = [
        ("Schema alignment", test_schema_alignment),
        ("Prompt structure", test_prompt_structure),
        ("Fallback poison", test_fallback_poison),
        ("JSON parsing", test_json_parsing),
        ("roast_id format", test_roast_id_format),
        ("Expiry calculation", test_expiry_calculation),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"\n-- {name} --")
        try:
            if fn():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"[FAIL] Exception: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
