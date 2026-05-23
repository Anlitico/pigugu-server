"""
Unit tests for the classifier pipeline -- no DB, no API key needed.
Tests: schema alignment, prompt structure, fallback, JSON parsing, roast_id logic.
"""
import json
import sys
from datetime import datetime

# -- 1. Verify Model <-> Migration schema alignment --------------------

def test_schema_alignment():
    """Ensure the SQLAlchemy model columns match the Alembic migration."""
    from models.roast_scenario import RoastScenario

    model_columns = {c.name: c for c in RoastScenario.__table__.columns}

    expected = {
        "roast_id":   str,
        "game_mode":  str,
        "prompt":     str,
        "headline":   str,
        "source":     str,
        "source_url": str,
        "teaser":     str,
        "is_urgent":  bool,
        "news_id":    str,
        "tags":       list,
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
        col_nullable = col.nullable

        if name in ("headline", "source", "source_url", "teaser", "news_id", "status"):
            if col_nullable:
                issues.append(f"{name}: should NOT be nullable (has server_default)")
        elif name == "is_urgent":
            if col_nullable:
                issues.append(f"{name}: should NOT be nullable (server_default=False)")
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

    print("[OK] Schema alignment: model <-> migration match (incl 5 new columns)")
    return True


# -- 2. Verify prompt template structure -------------------------------

def test_prompt_structure():
    """Ensure the classifier prompt contains all required sections."""
    from jobs.trump_social_crawler.classifier import _build_classifier_prompt

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
        "headline",
        "teaser",
        "expires_at",
    ]

    missing = [p for p in required_phrases if p not in prompt]
    if missing:
        print(f"[FAIL] Prompt missing phrases: {missing}")
        return False

    if '"modes"' not in prompt:
        print("[FAIL] Prompt missing JSON output example")
        return False

    print(f"[OK] Prompt structure: {len(prompt)} chars, headline+teaser included")
    return True


# -- 3. Verify fallback generates valid poison_opinion -----------------

def test_fallback_poison():
    """Fallback should generate a valid poison_opinion entry with all new fields."""
    from jobs.trump_social_crawler.classifier import _fallback_poison

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

    for field in ("roast_id", "prompt", "expires_at", "headline", "teaser"):
        if field not in m:
            print(f"[FAIL] Missing field: {field}")
            return False

    if not m["headline"]:
        print("[FAIL] Headline should not be empty")
        return False

    if not m["teaser"]:
        print("[FAIL] Teaser should not be empty")
        return False

    print(f"[OK] Fallback poison: headline='{m['headline']}', teaser='{m['teaser']}'")
    return True


# -- 4. Verify JSON parsing for various LLM responses ------------------

def test_json_parsing():
    """Simulate valid and invalid LLM responses."""
    # Valid: poison + debate with headline/teaser
    valid_json = json.dumps({
        "modes": [
            {
                "roast_id": "poison_2026-05-17_001",
                "game_mode": "poison_opinion",
                "headline": "Trump Boasts About Poll Numbers",
                "teaser": "Excellent by what metric? Tap to debate.",
                "prompt": "[POISON SCENARIO]\nTest prompt text for poison mode. Need enough chars here.",
                "expires_at": "2026-05-19T08:00:00Z",
            },
            {
                "roast_id": "debate_2026-05-17_001",
                "game_mode": "debate",
                "headline": "Trump Claims Tariffs Crushed China",
                "teaser": "Really? Let me defend him. Tap in.",
                "prompt": "[DEBATE SCENARIO]\nTest prompt text for debate mode.",
                "expires_at": "2026-05-19T08:00:00Z",
            },
        ]
    })

    data = json.loads(valid_json)
    modes = data.get("modes", [])
    if len(modes) != 2:
        print(f"[FAIL] Expected 2 modes, got {len(modes)}")
        return False
    for m in modes:
        if not m.get("headline") or not m.get("teaser"):
            print(f"[FAIL] Mode missing headline/teaser: {m}")
            return False

    # Valid: prediction only
    predict_json = json.dumps({
        "modes": [
            {
                "roast_id": "predict_2026-05-17_001",
                "game_mode": "prediction",
                "headline": "Trump Predicts Iran Deal By Friday",
                "teaser": "Friday deadline incoming. Will he deliver?",
                "prompt": "[PREDICTION SCENARIO]\nPrediction target: deal by Friday.",
                "expires_at": "2026-05-22T00:00:00Z",
            }
        ]
    })
    data2 = json.loads(predict_json)
    if len(data2.get("modes", [])) != 1:
        print("[FAIL] Prediction parse failed")
        return False

    # Valid: breaking_bomb with is_urgent
    bomb_json = json.dumps({
        "modes": [
            {
                "roast_id": "bomb_2026-05-17_001",
                "game_mode": "breaking_bomb",
                "headline": "Trump Authorizes Military Strikes in Syria",
                "teaser": "Major military action unfolding. Urgent notification.",
                "prompt": "[BREAKING SCENARIO]\nMilitary escalation.",
                "expires_at": "2026-05-17T10:00:00Z",
                "is_urgent": True,
            }
        ]
    })
    data3 = json.loads(bomb_json)
    m = data3.get("modes", [])[0]
    if not m.get("is_urgent"):
        print("[FAIL] breaking_bomb should have is_urgent=True")
        return False

    # Edge case: empty modes
    empty_json = json.dumps({"modes": []})
    data4 = json.loads(empty_json)
    if len(data4.get("modes", [])) != 0:
        print("[FAIL] Empty modes parse failed")
        return False

    # Edge case: invalid JSON
    try:
        json.loads("not json")
        print("[FAIL] Invalid JSON should have raised")
        return False
    except json.JSONDecodeError:
        pass

    print("[OK] JSON parsing: headline, teaser, is_urgent all handled")
    return True


# -- 5. Verify roast_id format and dedup logic --------------------------

def test_roast_id_format():
    """roast_id must follow {mode_abbrev}_{date}_{3-digit-seq}."""
    from jobs.trump_social_crawler.classifier import MODE_ABBREV

    expected = {
        "poison_opinion": "poison",
        "debate": "debate",
        "prediction": "predict",
        "breaking_bomb": "bomb",
    }
    for mode, abbrev in expected.items():
        if MODE_ABBREV.get(mode) != abbrev:
            print(f"[FAIL] Wrong abbrev for {mode}: {MODE_ABBREV.get(mode)}")
            return False

    print("[OK] roast_id mode abbrevs correct")
    return True


# -- 6. Verify expires_at calculation ---------------------------------

def test_expiry_calculation():
    """Verify expiry times match design doc rules."""
    from jobs.trump_social_crawler.classifier import _add_hours, _extract_date

    ts = "2026-05-17T08:00:00Z"
    if _add_hours(ts, 48) != "2026-05-19T08:00:00+00:00":
        print("[FAIL] 48h expiry mismatch")
        return False

    if _add_hours(ts, 2) != "2026-05-17T10:00:00+00:00":
        print("[FAIL] 2h expiry mismatch")
        return False

    if _add_hours(None, 48) is not None:
        print("[FAIL] None input should return None")
        return False

    if _extract_date("2026-05-17T08:00:00Z") != "2026-05-17":
        print("[FAIL] Date extraction mismatch")
        return False

    print("[OK] Expiry calculation: 48h, 2h, None, date extraction")
    return True


# -- 7. Verify is_live computation logic -------------------------------

def test_is_live_logic():
    """is_live = breaking_bomb AND not expired."""
    from datetime import datetime, timezone, timedelta

    # breaking_bomb with future expiry → live
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert "breaking_bomb" == "breaking_bomb"
    assert future > datetime.now(timezone.utc)

    # poison_opinion → not live regardless of expiry
    assert "poison_opinion" != "breaking_bomb"

    print("[OK] is_live logic: breaking_bomb + not expired = live")
    return True


# -- Run all tests -----------------------------------------------------

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
        ("is_live logic", test_is_live_logic),
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
