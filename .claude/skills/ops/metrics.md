---
name: metrics
description: Run latency analysis against the production metrics table on AWS RDS
---

# Latency Metrics Analysis

Runs `scripts/analyze_latency.py` against ClickHouse `metrics.turn_latency`
(the observability refactor migrated all metrics off the pgsql `metrics` table
into CH; see `docs/architecture/voice-latency-metrics-design.md` §8/§10).
The script needs only the Python standard library (urllib -> CH HTTP :8123),
so it runs anywhere ClickHouse is reachable — typically an ephemeral K8s pod
in the EKS cluster, like `/ops:pg`. **No separate AWS auth needed beyond
`kubectl`.**

## Quick Reference

```bash
PY=/Users/lijinzhao/Developer/pigugu/pigugu-server/scripts/analyze_latency.py

# CH creds come from the cluster secret (same values the agent uses).
set -a && source /Users/lijinzhao/Developer/pigugu/pigugu-server/pigagent/.env && set +a
# .env may not carry CLICKHOUSE_PASSWORD; fall back to the k8s secret:
CH_PW="$(kubectl get secret clickhouse-password -o jsonpath='{.data.password}' | base64 -d)"

# Last 24 hours (default), from a plain python pod:
kubectl run analyze --rm -i --restart=Never --image=python:3.13-alpine \
  --env "CLICKHOUSE_HOST=clickhouse" \
  --env "CLICKHOUSE_PORT=8123" \
  --env "CLICKHOUSE_USER=default" \
  --env "CLICKHOUSE_PASSWORD=$CH_PW" \
  -- python < "$PY"

# Last 7 days / date range / by user / custom percentiles
kubectl run analyze --rm -i --restart=Never --image=python:3.13-alpine \
  --env "CLICKHOUSE_HOST=clickhouse" --env "CLICKHOUSE_PASSWORD=$CH_PW" \
  -- python < "$PY" -- --hours 168
... -- python < "$PY" -- --since 2026-08-18 --until 2026-08-25
... -- python < "$PY" -- --user-id web-123
... -- python < "$PY" -- --percentiles 50,90,99

# Output JSON for BI / further processing
... -- python < "$PY" -- --output /tmp/latency-report.json
```

> `--` separates kubectl's args from the Python args so `--hours 168` reaches
> the script. The script reads `CLICKHOUSE_HOST/PORT/USER/PASSWORD/DATABASE`
> (defaults `clickhouse`/8123/`default`/``/`voice`); the table is always the
> fully-qualified `metrics.turn_latency`.

## What the report shows

- **E2E perceived** — p50 / p90 / p95 / p99 / min / mean / max / std + count.
  True E2E = user_stop → first bot audio (`e2e_perceived_ms`, anchored on the
  device-reported `vad_silence` user-stop; falls back to the server E2E when
  the firmware did not report one). Rows also carry `stt_tail_ms` (user_stop →
  stt_final) and a coverage line showing how many turns had a real device
  anchor.
- **Server E2E** — `stt_final → agent_spk` (stored `segments.e2e`), the
  main-chain reconciliation number (excludes STT, so ≤ perceived).
- **Main-chain segments** (`role="main"` in the stored segments): `agent_init`,
  `orchestrator`, `context`, `llm_prep`, `llm_ttft`, `llm_to_tts`, `tts_ttfb`
- **Diagnostic segments** (`role="diagnostic"`, overlap / can be negative):
  `stt`, `vad`, `server_vad`, `vad_to_recv`, `llm_rest`, `tts`, `turn_end`,
  `ctx_l1/l2/roast`
- **Breakdown by `turn_phase`**: `wake_word` vs `follow_up`
- **Anomalies**: turns missing `server_received_vad_at`, negative or
  outlier E2E (> 60s or < 30ms)

## Gotchas

- **Image is built by CI/CD** — same pipeline as `pigugu-agent`. After
  the first push with `Dockerfile.tools` and the workflow change, the
  image is in ECR. No `pip install` overhead — asyncpg is pre-installed.
- **Image is small** — ~165MB (python:3.13-slim + asyncpg + the two
  scripts). The voice-agent image is ~700MB; this is intentionally
  separate so the ops scripts can be updated without restarting any
  voice agent pod.
- **Time range uses `marks->'agent_spk'->>'unix_ms'`** (the new field
  added by turn.py). Rows without this field are silently skipped. After
  a TRUNCATE / fresh deploy, no rows qualify until the first turn
  finishes — give it a few minutes.
- **`--since` / `--until` are UTC**, ISO 8601 format. If you pass a
  date-only value like `2026-08-20`, it's interpreted as midnight UTC.
- **The script never writes.** It's read-only — safe to run on prod.
- **`kubectl run` requires the same RBAC as deploying** — any user who
  can `kubectl apply` can also run this.

## Common Tasks

### Is the latency improving after a deploy?

```bash
CH_PW="$(kubectl get secret clickhouse-password -o jsonpath='{.data.password}' | base64 -d)"
kubectl run analyze --rm -i --restart=Never --image=python:3.13-alpine \
  --env "CLICKHOUSE_HOST=clickhouse" --env "CLICKHOUSE_PASSWORD=$CH_PW" \
  -- python < "$PY" \
  -- --since "$(date -u -v-2d +%Y-%m-%d)" --until "$(date -u +%Y-%m-%d)"
```

### Spot-check a single user who's been complaining

```bash
... < "$PY" -- --user-id web-abc-123 --hours 168
```

### Generate a weekly JSON digest for the team

```bash
... < "$PY" -- --hours 168 --output /tmp/weekly-latency.json
```

### Investigate a high p99 spike

Run with a narrow time window and custom percentiles, look at the
`Anomalies` section for the per-turn outlier samples:

```bash
... < "$PY" -- --since 2026-08-24T14:00 --until 2026-08-24T16:00 \
  --percentiles 50,90,95,99,99.9
```
