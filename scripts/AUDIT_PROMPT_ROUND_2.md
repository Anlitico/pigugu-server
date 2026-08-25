# 独立代码审计 Prompt — Round 2(N1-N4 修复审查)

> 把下面整段(从 `# Role` 开始)粘贴到 Claude Code / 新 Codex session(用 OpenAI 后端)/ 任何独立 AI 工具里,让它从零开始审 N1-N4 修复。

---

# Role

You are an independent code auditor. **Read-only, do not modify any files.** Fresh review with no prior context. The previous round found 4 issues in the original audit's fixes (N1-N4 below). The author claims to have fixed them. Your job:

1. **Verify** each of N1, N2, N3, N4 is correctly fixed
2. **Find new issues** the fixes introduced
3. **Be skeptical** — assume the implementer made mistakes, find them

# Background

The 4 issues to re-verify (from the round-1 audit, see `~/.codex/sessions/2026/08/25/AUDIT_REPORT_2026-08-25.md` for full context if available, but you can re-audit from scratch):

- **N1 (Critical)**: `pigagent/metrics/turn.py` `_log` was computing `e2e = _diff(m, "vad_end", "agent_spk")` but segments use `server_received_vad_at` as start. Mismatch caused E2E to be systematically small (missing uplink RTT) and `_check_main_chain` to always WARN. **Claimed fix**: changed `_log` to use `server_received_vad_at` with fallback to `vad_end`.
- **N2 (High)**: Firmware H2 fix read `IsVoiceDetected()` at listening entry, which is a transient value. **Claimed fix**: added sticky `voice_seen_since_connecting_` flag, set in AFE VAD callback any time VAD is high; consumed at listening entry.
- **N3 (Medium)**: `tts_played` arriving after parent task flushed the turn was silently dropped. **Claimed fix**: added `_late_tts_played` one-slot buffer, flushed into next turn's meta with `device_playback_ms_late=true` flag.
- **N4 (Medium)**: `start_turn` had redundant if/elif branches both calling `_flush_turn`. **Claimed fix**: merged to single `if current is not None: cls._flush_turn()`.

# Materials to read

## Round-1 audit (context)
`/Users/lijinzhao/Developer/pigugu/pigugu-server/scripts/AUDIT_REPORT_2026-08-25.md` — the previous audit report. Read sections 1 and 2 for the full description of N1-N4.

## Current diffs (re-verify the fixes)

```bash
cd /Users/lijinzhao/Developer/pigugu/pigugu-server
git diff pigagent/metrics/turn.py pigagent/voice/connection.py
git diff pigagent/voice/connection.py
```

```bash
cd /Users/lijinzhao/Developer/pigugu/pigugu-firmware
git diff main/application.cc main/application.h
```

## Test the fixes
Run the unit tests:
```bash
cd /Users/lijinzhao/Developer/pigugu/pigugu-server
pigagent/.venv/bin/python -c "
import sys, types
asyncpg_stub = types.ModuleType('asyncpg')
asyncpg_stub.connect = lambda *a, **kw: None
sys.modules['asyncpg'] = asyncpg_stub
loguru_stub = types.ModuleType('loguru')
class _L:
    def bind(self, **kw): return self
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): print('WARN:', (a[0] if a else '')[:200])
    def error(self, *a, **kw): pass
    def exception(self, *a, **kw): pass
    def debug(self, *a, **kw): pass
loguru_stub.logger = _L()
sys.modules['loguru'] = loguru_stub
sys.path.insert(0, '.')
from pigagent.metrics.turn import _log
# Test that normal turn doesn't WARN (sum==E2E with new mark)
turn = {
    'user_id': 'u1', 'turn_id': 1, 'persona_id': 0,
    'marks': {
        'vad_start': 100.0, 'vad_end': 100.5,
        'server_received_vad_at': 100.0,
        'stt_final': 100.7, 'agent_init': 100.9,
        'agent_req': 100.95, 'ctx_done': 101.0,
        'llm_req': 101.05, 'llm_first_token': 101.2,
        'tts_first_ready': 101.3, 'agent_spk': 101.5,
    },
    'event_unix_ms': {}, 'meta': {}, '_finished': True,
}
_log(turn)
# Expected: NO warn, since sum==E2E with new mark
"
```

# Audit checklist

## A. Verify N1 fix (`pigagent/metrics/turn.py` `_log`)

- Is the e2e formula now `agent_spk - server_received_vad_at`?
- Does it fall back to `vad_end` when `server_received_vad_at` is missing?
- Is the early-return condition correct? (Should return when NEITHER mark is present)
- Is the metric written to DB consistent with what's logged? (Check `_pg_write` for any separate E2E calculation)
- Does the analyze script's E2E formula match the new server-side formula? (They should agree)

## B. Verify N2 fix (`main/application.cc` / `main/application.h`)

- Is the `voice_seen_since_connecting_` flag set correctly? (In MAIN_EVENT_VAD_CHANGE handler, regardless of state)
- Is the flag consumed at listening entry?
- **CRITICAL: Is the flag reset at end of turn (vad_silence_sent) or only at next listening entry?** If only at next entry, it could persist across turn boundaries, causing incorrect arming on the next turn.
- Does the "consumed at listening entry" logic conflict with the original `if (audio_service_.IsVoiceDetected())` fallback in the same branch? (My fix uses `else if`, so sticky path wins when sticky is true)
- Test scenarios: (a) VAD rises during connecting → listening entry, (b) VAD rises mid-listening (turn 1) then next turn starts without new VAD rise, (c) no VAD rise ever, listening entry with no VAD

## C. Verify N3 fix (`pigagent/voice/connection.py` `_on_tts_played` + `_flush_late_tts_played`)

- Does the buffer actually catch the late data?
- **CRITICAL: When the late buffer is flushed at the start of a new turn (`_on_stst_result`), does it OVERWRITE the new turn's own `device_playback_ms` if the new turn already has its own tts_played?**
  - The flush code calls `TelemetryCollector.set_meta("device_playback_ms", ms)`. If the new turn already set this meta from its own tts_played, the late data overwrites it.
  - Check: should the flush check if `device_playback_ms` is already set in the new turn's meta, and skip if so?
- Is the `device_playback_ms_late=true` flag set correctly? (Only when actually flushing late data, not for every turn)
- Is the `_late_tts_played` field properly initialized to None? (No race with first-turn initialization)
- Edge case: what if BOTH a late tts_played AND a new-turn tts_played arrive in quick succession, before the next `_flush_late_tts_played` call?

## D. Verify N4 fix (`pigagent/metrics/turn.py` `start_turn`)

- The merged `if current is not None: cls._flush_turn()` — does this match the old behavior?
- Old: if current is not None and not finished → flush; elif current is not None and finished → flush; otherwise skip
- New: if current is not None → flush; otherwise skip
- Equivalence: yes, both branches of old flushed. New does the same. ✓
- Edge case: `_finished` is True but `_flush_turn` was already called (somehow double-flush). Is the new code idempotent? (Check `_log` and `_pg_write` to see if double-call is safe)

## E. New issues introduced

For each fix, think about:
- Did the fix break any old test cases that should still work?
- Are there race conditions I didn't think of?
- Is the new code consistent with surrounding patterns?
- Does the fix interact badly with other parts of the system?

# Output format

Produce a single Markdown report. Structure:

```
# Audit Report — Round 2

## Summary
- N1: ✅ / ⚠️ / ❌ + brief reason
- N2: ✅ / ⚠️ / ❌ + brief reason
- N3: ✅ / ⚠️ / ❌ + brief reason
- N4: ✅ / ⚠️ / ❌ + brief reason
- New issues: count by severity
- Should we commit and push? Yes / No / Yes-with-fixes

## Per-finding verification
### N1
- Status: ✅ / ⚠️ / ❌
- Location: file:line
- What the fix does
- Whether it actually solves the problem
- Edge cases

### N2
...

## New issues found
For each:
  - [Severity] — Title
  - Location
  - Problem
  - Suggested fix
  - Confidence

## Action list
1. [must-fix] ...
2. [should-fix] ...
3. [nice-to-have] ...
```

