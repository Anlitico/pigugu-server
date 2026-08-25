---
name: metrics
description: Run latency analysis against the production metrics table on AWS RDS
---

# Latency Metrics Analysis

Runs `scripts/analyze_latency.py` against the production `metrics` table on
AWS RDS. The script handles both the old flat format (`{key: float}`) and
the new structured format (`{key: {perf_counter, unix_ms}}`) so it works
on data written by either voice-server version.

Like `/ops:pg`, this module runs inside an ephemeral K8s pod in the EKS
cluster, so the local Mac never touches RDS directly. **No separate AWS
auth needed beyond `kubectl`** (which the project already requires).

## Quick Reference

```bash
PY=/Users/lijinzhao/Developer/pigugu/pigugu-server/scripts/analyze_latency.py
set -a && source /Users/lijinzhao/Developer/pigugu/pigugu-server/pigagent/.env && set +a

TOOLS=537557168531.dkr.ecr.us-west-1.amazonaws.com/pigugu-tools:latest

# Last 24 hours (default)
kubectl run analyze --rm -i --restart=Never --image=$TOOLS \
  --env "PGHOST=$PGHOST" --env "PGUSER=$PGUSER" \
  --env "PGPASSWORD=$PGPASSWORD" --env "PGDATABASE=$PGDATABASE" \
  -- python /scripts/analyze_latency.py

# Last 7 days
... -- python /scripts/analyze_latency.py -- --hours 168

# Specific date range (UTC)
... -- python /scripts/analyze_latency.py -- --since 2026-08-18 --until 2026-08-25

# Filter by user
... -- python /scripts/analyze_latency.py -- --user-id web-123

# Custom percentile set
... -- python /scripts/analyze_latency.py -- --percentiles 50,90,99

# Output JSON for BI / further processing
... -- python /scripts/analyze_latency.py -- --output /tmp/latency-report.json

# Run the migration script (destructive) from the same image
kubectl run migrate --rm -i --restart=Never --image=$TOOLS \
  --env "PGHOST=$PGHOST" --env "PGUSER=$PGUSER" \
  --env "PGPASSWORD=$PGPASSWORD" --env "PGDATABASE=$PGDATABASE" \
  -- python /scripts/migrate_metrics_format.py           # dry-run
...                                                    -- --apply   # actual
```

> The trailing `--` separates kubectl's args from the Python script's
> args, so `--hours 168` etc. reach the script correctly.

## What the report shows

- **E2E (server_received_vad_at → agent_spk)** — p50 / p90 / p95 / p99
  / min / mean / max / std + count
- **Main chain segments** (sum == E2E in theory): `stt`, `agent_init`,
  `orchestrator`, `context`, `llm_prep`, `llm_ttft`, `llm_to_tts`, `tts_ttfb`
- **Diagnostic segments** (overlap / can be negative): `vad`,
  `server_vad`, `vad_to_recv` (network RTT), `llm_rest`, `tts`
- **Breakdown by `turn_phase`**: `wake_word` vs `follow_up` vs
  `first_after_connect`
- **Breakdown by `vad_end_fallback`**: `detect` vs `(none)` — high
  `detect` count means device-side VAD is broken on a lot of units
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
kubectl run analyze --rm -i --restart=Never --image=python:3.13-alpine \
  --env "PGHOST=$PGHOST" --env "PGUSER=$PGUSER" \
  --env "PGPASSWORD=$PGPASSWORD" --env "PGDATABASE=$PGDATABASE" \
  -- sh -c "pip install -q asyncpg && python" < "$PY" \
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

## Migration (one-time, destructive)

The same image also contains `migrate_metrics_format.py`. See the Quick
Reference for the invocation. **Always run with no args (dry-run) first**
to see row counts before adding `--apply`.
