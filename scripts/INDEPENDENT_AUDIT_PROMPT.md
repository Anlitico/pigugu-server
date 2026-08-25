# 独立代码审计 Prompt

> 把下面整段(从 `# Role` 开始到 `PROMPT_EOF` 之前)粘贴到 Claude Code / 新 Codex session / 任何独立 AI 工具里,让它从零开始审。

---

# Role

You are an independent code auditor. **Read-only, do not modify any files.** This is a fresh review with no prior context from the agent that wrote the code.

# Background

A previous audit of the pigugu firmware + server identified 13 issues (Critical / High / Medium / Low). Code changes were made to address all 13. Your job:

1. **Verify** each of the 13 issues was correctly fixed (not just "code was changed near it" — actually fixed)
2. **Look for new bugs** introduced by the fixes themselves
3. **Look for issues the original audit missed** but that should have been caught

Be skeptical. Assume the implementer made mistakes. Find them.

# Materials to read

## 1. Original audit report (the "ground truth" — what should have been fixed)

**File**: `~/.codex/sessions/2026/08/25/rollout-2026-08-25T01-21-21-01a034ca-8143-7442-a847-2ccbb331bb3a.jsonl`

This is a JSONL session log. The audit report is the **last long assistant message** (~9000 chars). To extract it, run:

```bash
python3 -c "
import json
with open('$HOME/.codex/sessions/2026/08/25/rollout-2026-08-25T01-21-21-01a034ca-8143-7442-a847-2ccbb331bb3a.jsonl') as f:
    for line in f:
        d = json.loads(line)
        if d.get('type') == 'response_item':
            p = d.get('payload', {})
            if p.get('role') == 'assistant':
                for c in p.get('content', []):
                    if c.get('type') == 'output_text' and len(c.get('text', '')) > 500:
                        print(c['text'])
                        break
"
```

## 2. Current state of the changes (uncommitted work)

Two repos at `/Users/lijinzhao/Developer/pigugu/`:

### pigugu-server
```bash
cd /Users/lijinzhao/Developer/pigugu/pigugu-server
git status --short
git diff
```

Modified: `.claude/skills/cicd.md`, `.claude/skills/ops.md`, `.github/workflows/deploy.yml`, `pigagent/agent.py`, `pigagent/metrics/turn.py`, `pigagent/voice/connection.py`
New files: `.cicd/Dockerfile.tools`, `.claude/skills/ops/metrics.md`, `scripts/analyze_latency.py`, `scripts/migrate_metrics_format.py`
Also present: `backups/metrics-20260825-pre-cleanup.csv` (a 402-row backup of historical data, unrelated to the audit)

### pigugu-firmware
```bash
cd /Users/lijinzhao/Developer/pigugu/pigugu-firmware
git status --short
git diff
```

Modified: `main/application.cc`, `main/application.h`, `main/audio/audio_service.cc`, `main/audio/audio_service.h`, `main/protocols/protocol.cc`, `main/protocols/protocol.h`

# Audit checklist

## A. Verify the 13 original findings

For each of C1, H1, H2, H3, H4, H5, M1, M2, M3, M4, M5, L1, L2:

- Was the change actually applied at the right location?
- Does the change actually fix the issue, or just cosmetically address it?
- Are there edge cases the original audit mentioned that the fix misses?
- Did the fix introduce a new bug? (e.g. wrong lock ordering, race condition, dead code, broken invariant)

## B. Look for new issues

Especially focus on:

- **`pigagent/metrics/turn.py`** — `_resolve_active_turn` filtering, `_finished` flag lifecycle, E2E formula change to `server_received_vad_at → agent_spk`, `event_unix_ms` sync writes, `_log` ISO timestamp output, `_check_main_chain` non-negative validation, role classification in `_pg_write`
- **`pigagent/voice/connection.py`** — `_restore_turn_context` / `_capture_turn_context` pair, call sites at all cross-thread entry points, `_current_tts_sentence_id` lifecycle, vad_end fallback order, `_tts_producer_consumer` no longer calls `finish_turn` (now parent task's job), `_on_tts_played` validates sentence_id
- **`pigagent/agent.py`** — `llm_first_token` only on first string chunk (not FlushSentinel / tool filler)
- **Firmware `audio_service.cc/.h`** — `voice_detected_` as `std::atomic<bool>` (memory order), `std::atomic<int64_t>` reverted to plain `int64_t + playback_timing_mutex_`, `is_tts_playback_active_` controls TTS first-packet timing
- **Firmware `application.cc/.h`** — `HandleStateChangedEvent` seeds `vad_voice_seen_in_listening_` on listening entry, `SendWakeWordDetected` no longer sends `user_stop_ms`, tts/start parses sentence_id, tts/stop/abort calls `EndTtsPlaybackTiming`
- **Firmware `protocol.cc/.h`** — `SendTtsPlayed` includes sentence_id
- **Firmware `audio_service.cc`** — M4 lock ordering: `audio_queue_mutex_` first, `playback_timing_mutex_` second, to avoid deadlock
- **`scripts/analyze_latency.py` + `migrate_metrics_format.py`** — PG* env var fallback, dual format compatibility, percentile calculation correctness
- **`Dockerfile.tools` + `ops/metrics.md` + `deploy.yml`** — Image correctly defined, skill correctly uses the image, workflow step correctly placed

## C. Things the original audit might have missed

Common blind spots in voice-agent latency code:

- **Time-base mixing**: `time.perf_counter` vs `time.time` vs `time.monotonic` — is anything using non-monotonic for deltas?
- **Concurrent writes to shared state**: especially in firmware RTOS tasks
- **State machine transitions**: first turn / wake word / follow-up / barge-in — does each path produce the right marks?
- **Negative or zero deltas**: are they silently dropped, warned, or could they pollute percentiles?
- **Resource cleanup**: do all paths release file handles / DB connections / audio buffers?
- **Backwards compatibility**: can new code read old data and vice versa?
- **Concurrency primitives**: lock order, recursive locks, lock scope

# Output format

Produce a single Markdown report. Structure:

```
# Audit Report

## Summary
- Original 13 findings: N addressed correctly, M partially, K not addressed
- New issues introduced: X Critical, Y High, Z Medium, W Low
- Should we commit and push? Yes / No / Yes-with-fixes — give reason

## Original findings — verification
For each of C1, H1, H2, H3, H4, H5, M1, M2, M3, M4, M5, L1, L2:
  ### [ID] — [Original title]
  - Status: ✅ correctly fixed / ⚠️ partially / ❌ not fixed / ➕ new issue
  - Location: file:line
  - What was changed
  - Whether the change actually solves the original problem
  - Edge cases the fix may miss

## New issues found
For each new issue:
  ### [Critical/High/Medium/Low] — [Title]
  - Location: file:line
  - What's there now
  - Why it's a problem
  - Suggested fix
  - Confidence: high / medium / low

## Action list (sorted by priority)
1. [must-fix] ...
2. [should-fix] ...
3. [nice-to-have] ...
```

Be specific with file:line references. Use `git diff` line numbers where possible. Don't repeat back the original audit — assume the reader has it.

