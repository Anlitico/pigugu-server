#!/usr/bin/env python3
"""
NOTE: Run with `pigagent/.venv/bin/python` — asyncpg + loguru
are installed there, not in the project root `.venv`.


analyze_latency.py — Read metrics from PostgreSQL and print latency stats.

Reads from the same DATABASE_URL the voice server uses. Works on rows in
both the old flat format ({key: float}) and the new structured format
({key: {perf_counter, unix_ms}} / {key: {role, ms}}), so it can be run
before AND after running migrate_metrics_format.py.

USAGE
─────────────────────────────────────────────────────────────
  # Last 24 hours (default)
  ./analyze_latency.py

  # Last 7 days
  ./analyze_latency.py --hours 168

  # Explicit date range
  ./analyze_latency.py --since 2026-08-20 --until 2026-08-25

  # Filter by user
  ./analyze_latency.py --user-id web-123

  # Save raw per-turn data as JSON for further processing
  ./analyze_latency.py --output report.json

  # Custom percentile set
  ./analyze_latency.py --percentiles 50,90,99

OUTPUT (text mode)
─────────────────────────────────────────────────────────────
For each metric (E2E, main segments, diagnostics) prints p50/p90/p95/p99/max
+ count + mean + std. Also prints a breakdown by turn_phase and
vad_end_fallback, and an anomaly section showing turns with negative
segments or missing required marks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg


# ── Constants ────────────────────────────────────────────────────────────

MAIN_SEGMENT_LABELS: list[str] = [
    "stt", "agent_init", "orchestrator", "context",
    "llm_prep", "llm_ttft", "llm_to_tts", "tts_ttfb",
]
DIAG_SEGMENT_LABELS: list[str] = [
    "vad", "server_vad", "vad_to_recv", "llm_rest", "tts",
]
ALL_SEGMENT_LABELS: list[str] = MAIN_SEGMENT_LABELS + DIAG_SEGMENT_LABELS

# Tunables: minimum E2E to consider "complete" (in ms). 30ms is a generous
# lower bound that filters out obviously broken records (e.g. an empty
# turn where every mark fires within microseconds).
MIN_E2E_MS: float = 30.0
# Maximum plausible E2E in ms. Anything bigger is treated as a stuck/aborted
# turn and reported but excluded from percentile stats.
MAX_E2E_MS: float = 60_000.0


# ── Format helpers ───────────────────────────────────────────────────────

def extract_perf_counter(marks: dict | None, key: str) -> float | None:
    """Extract a perf_counter value from a marks dict, handling both formats.

    New format: marks[key] = {perf_counter: float, unix_ms: int|null}
    Old format: marks[key] = float
    """
    if not marks:
        return None
    v = marks.get(key)
    if v is None:
        return None
    if isinstance(v, dict):
        pc = v.get("perf_counter")
        return float(pc) if pc is not None else None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def extract_segment_ms(segments: dict | None, key: str) -> float | None:
    """Extract a segment value in ms, handling both formats.

    New format: segments[key] = {role: str, ms: float}
    Old format: segments[key] = float
    """
    if not segments:
        return None
    v = segments.get(key)
    if v is None:
        return None
    if isinstance(v, dict):
        ms = v.get("ms")
        return float(ms) if ms is not None else None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def extract_meta(meta: dict | None, key: str) -> str | None:
    if not meta:
        return None
    v = meta.get(key)
    if v is None:
        return None
    return str(v)


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolation percentile. p in [0, 100]."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def fmt_ms(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:>8.1f}ms"


def fmt_int(v: int | None) -> str:
    if v is None:
        return "—"
    return f"{v:>6d}"


def fmt_pct(num: int, denom: int) -> str:
    if denom == 0:
        return "  0.0%"
    return f"{(num / denom * 100):>5.1f}%"


# ── SQL ──────────────────────────────────────────────────────────────────

# Pull only what we need; everything else is in marks/segments jsonb.
FETCH_SQL_TEMPLATE = """
SELECT
  user_id,
  turn_id,
  meta,
  marks,
  segments,
  -- epoch milliseconds (turn-finish-ish; created_at style — falls back to NULL)
  EXTRACT(EPOCH FROM now()) * 1000 AS now_ms
FROM metrics
WHERE ($1::text IS NULL OR user_id = $1)
  AND ($2::timestamptz IS NULL OR (marks->'agent_spk'->>'unix_ms')::bigint >= $3)
  AND ($4::timestamptz IS NULL OR (marks->'agent_spk'->>'unix_ms')::bigint <= $5)
ORDER BY (marks->'agent_spk'->>'unix_ms')::bigint NULLS LAST
LIMIT $6;
"""


def build_query(args: argparse.Namespace) -> tuple[str, list]:
    """Build the SQL and parameters for the latency query.

    Time range is expressed as agent_spk unix_ms bounds (in milliseconds
    since epoch UTC). If --since / --until are not given, the upper bound
    is "now" and the lower bound is "now - hours".
    """
    until_ms: int
    if args.until:
        until_dt = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
        until_ms = int(until_dt.timestamp() * 1000)
    else:
        until_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    if args.since:
        since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        since_ms = int(since_dt.timestamp() * 1000)
    elif args.hours:
        since_ms = until_ms - args.hours * 3600 * 1000
    else:
        since_ms = until_ms - 24 * 3600 * 1000

    # We re-use the same query template, but inject the bounds. Simpler to
    # inline the SQL with the computed bounds than to thread more params
    # through the (slightly awkward) template above.
    sql = f"""
    SELECT
      user_id,
      turn_id,
      meta,
      marks,
      segments
    FROM metrics
    WHERE ($1::text IS NULL OR user_id = $1)
      AND (
        (marks->'agent_spk'->>'unix_ms')::bigint IS NOT NULL
        AND (marks->'agent_spk'->>'unix_ms')::bigint >= {since_ms}
        AND (marks->'agent_spk'->>'unix_ms')::bigint <= {until_ms}
      )
    ORDER BY (marks->'agent_spk'->>'unix_ms')::bigint
    LIMIT $2;
    """
    return sql, [args.user_id, args.limit]


# ── Aggregation ──────────────────────────────────────────────────────────

def aggregate(rows: list[asyncpg.Record], percentiles: list[float]) -> dict:
    e2e_values: list[float] = []
    segment_values: dict[str, list[float]] = {k: [] for k in ALL_SEGMENT_LABELS}
    phase_buckets: dict[str, list[float]] = {}
    fallback_buckets: dict[str, list[float]] = {}
    rows_with_negative_segments: list[dict] = []
    rows_missing_server_received: list[dict] = []
    rows_with_vad_end_fallback_detect: int = 0
    total_rows: int = 0
    new_format_count: int = 0
    old_format_count: int = 0

    for row in rows:
        total_rows += 1
        marks = row["marks"] or {}
        segments = row["segments"] or {}
        meta = row["meta"] or {}

        # Detect format by sniffing a known key
        if isinstance(marks.get("agent_spk"), dict):
            new_format_count += 1
        else:
            old_format_count += 1

        # Count detect-fallback usage at the row level — independent of
        # whether E2E was valid. A turn that used detect as the fallback
        # is interesting even if its E2E is broken.
        fb = extract_meta(meta, "vad_end_fallback") or "(none)"
        if fb == "detect":
            rows_with_vad_end_fallback_detect += 1

        # E2E: prefer recompute from marks (consistent definition regardless
        # of when the row was written). Fall back to stored segments.e2e
        # if the marks aren't available.
        agent_spk = extract_perf_counter(marks, "agent_spk")
        srv_recv = extract_perf_counter(marks, "server_received_vad_at")
        if agent_spk is not None and srv_recv is not None:
            e2e_ms = (agent_spk - srv_recv) * 1000.0
        else:
            e2e_ms = extract_segment_ms(segments, "e2e")
        if e2e_ms is None:
            continue
        if e2e_ms < 0:
            rows_with_negative_segments.append({
                "user_id": row["user_id"],
                "turn_id": row["turn_id"],
                "e2e_ms": round(e2e_ms, 1),
                "reason": "negative E2E",
            })
            continue
        if e2e_ms < MIN_E2E_MS or e2e_ms > MAX_E2E_MS:
            # Likely broken / stuck turn. Note but exclude from percentiles.
            rows_with_negative_segments.append({
                "user_id": row["user_id"],
                "turn_id": row["turn_id"],
                "e2e_ms": round(e2e_ms, 1),
                "reason": f"E2E out of [{MIN_E2E_MS:.0f}, {MAX_E2E_MS:.0f}]ms",
            })
            continue
        e2e_values.append(e2e_ms)

        # Per-segment
        for k in ALL_SEGMENT_LABELS:
            v = extract_segment_ms(segments, k)
            if v is not None:
                if v < 0:
                    rows_with_negative_segments.append({
                        "user_id": row["user_id"],
                        "turn_id": row["turn_id"],
                        "segment": k,
                        "ms": round(v, 1),
                    })
                else:
                    segment_values[k].append(v)

        # Buckets (only for rows with valid E2E so the stats are meaningful)
        phase = extract_meta(meta, "turn_phase") or "(none)"
        phase_buckets.setdefault(phase, []).append(e2e_ms)
        fallback_buckets.setdefault(fb, []).append(e2e_ms)

        if srv_recv is None:
            rows_missing_server_received.append({
                "user_id": row["user_id"],
                "turn_id": row["turn_id"],
            })

    def stats(values: list[float]) -> dict:
        if not values:
            return {"n": 0, "min": None, "max": None, "mean": None, "std": None,
                    "percentiles": {}}
        return {
            "n": len(values),
            "min": min(values),
            "max": max(values),
            "mean": statistics.fmean(values),
            "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
            "percentiles": {p: percentile(values, p) for p in percentiles},
        }

    return {
        "total_rows": total_rows,
        "new_format_count": new_format_count,
        "old_format_count": old_format_count,
        "e2e": stats(e2e_values),
        "segments": {k: stats(v) for k, v in segment_values.items()},
        "by_phase": {k: stats(v) for k, v in phase_buckets.items()},
        "by_fallback": {k: stats(v) for k, v in fallback_buckets.items()},
        "anomalies": {
            "negative_or_outlier": rows_with_negative_segments[:50],  # cap
            "missing_server_received": rows_missing_server_received[:50],
            "detect_fallback_count": rows_with_vad_end_fallback_detect,
        },
    }


# ── Rendering ────────────────────────────────────────────────────────────

def render_text(report: dict, percentiles: list[float],
                time_range: tuple[int, int]) -> str:
    out: list[str] = []
    since_iso = datetime.fromtimestamp(time_range[0] / 1000, tz=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    until_iso = datetime.fromtimestamp(time_range[1] / 1000, tz=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")

    out.append("=" * 78)
    out.append("Pigugu Voice Agent — Latency Analysis")
    out.append(f"Time range: {since_iso}  →  {until_iso}  (UTC)")
    out.append(f"Total rows: {report['total_rows']}"
               f"  (new format: {report['new_format_count']},"
               f" old format: {report['old_format_count']})")
    out.append("=" * 78)

    def header(name: str) -> str:
        cols = "  ".join(f"p{p:>3d}" for p in percentiles)
        return f"\n{name}\n  {'n':>6s}  {'min':>9s}  {'mean':>9s}  {'max':>9s}  {'std':>9s}  {cols}"

    def row(name: str, s: dict) -> str:
        if s["n"] == 0:
            return f"  {name:24s}  (no data)"
        p_str = "  ".join(
            fmt_ms(s["percentiles"].get(p)) for p in percentiles
        )
        return (f"  {name:24s}  {fmt_int(s['n'])}  "
                f"{fmt_ms(s['min'])}  {fmt_ms(s['mean'])}  "
                f"{fmt_ms(s['max'])}  {fmt_ms(s['std'])}  {p_str}")

    out.append(header("── E2E (server_received_vad_at → agent_spk) ──"))
    out.append(row("e2e", report["e2e"]))

    out.append(header("── Main chain (sum == E2E in theory) ──"))
    for k in MAIN_SEGMENT_LABELS:
        out.append(row(k, report["segments"][k]))

    out.append(header("── Diagnostics (overlap / can be negative) ──"))
    for k in DIAG_SEGMENT_LABELS:
        out.append(row(k, report["segments"][k]))

    out.append("\n── Breakdown by turn_phase ──")
    for phase, s in sorted(report["by_phase"].items(), key=lambda x: -x[1]["n"]):
        out.append(f"  {phase:24s}  n={s['n']:>5d}  "
                   f"p50={fmt_ms(s['percentiles'].get(50))}  "
                   f"p95={fmt_ms(s['percentiles'].get(95))}  "
                   f"p99={fmt_ms(s['percentiles'].get(99))}  "
                   f"max={fmt_ms(s['max'])}")

    out.append("\n── Breakdown by vad_end_fallback ──")
    for fb, s in sorted(report["by_fallback"].items(), key=lambda x: -x[1]["n"]):
        out.append(f"  {fb:24s}  n={s['n']:>5d}  "
                   f"p50={fmt_ms(s['percentiles'].get(50))}  "
                   f"p95={fmt_ms(s['percentiles'].get(95))}  "
                   f"p99={fmt_ms(s['percentiles'].get(99))}  "
                   f"max={fmt_ms(s['max'])}")

    a = report["anomalies"]
    out.append("\n── Anomalies ──")
    out.append(f"  turns missing server_received_vad_at: {len(a['missing_server_received'])}"
               f"  (sample: {a['missing_server_received'][:3]})")
    out.append(f"  turns with negative / outlier E2E:    {len(a['negative_or_outlier'])}"
               f"  (sample: {a['negative_or_outlier'][:3]})")
    out.append(f"  turns using detect as vad_end fallback: {a['detect_fallback_count']}")
    out.append("")
    return "\n".join(out)


# ── Entry point ──────────────────────────────────────────────────────────

def get_dsn() -> str:
    """Resolve the PostgreSQL DSN from environment.

    Order of preference:
    1. ``DATABASE_URL`` if set and points to a non-localhost host (i.e. the
       production RDS endpoint). This is what the voice server uses.
    2. ``PGHOST``/``PGUSER``/``PGPASSWORD``/``PGDATABASE`` set by
       ``.claude/skills/ops/pg`` pointing to the same RDS — used when
       ``DATABASE_URL`` is bound to ``localhost`` (local dev) and we want
       to query production from this script.
    3. As a last resort, whatever ``DATABASE_URL`` says (will fail loudly
       if the connection is unreachable).
    """
    dsn = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")
    if dsn and "localhost" not in dsn and "127.0.0.1" not in dsn:
        return dsn
    host = os.getenv("PGHOST")
    user = os.getenv("PGUSER")
    password = os.getenv("PGPASSWORD")
    database = os.getenv("PGDATABASE", "pigugu")
    port = os.getenv("PGPORT", "5432")
    if host and user and password:
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    if dsn:
        return dsn
    print("ERROR: DATABASE_URL or PGHOST/PGUSER/PGPASSWORD must be set.",
          file=sys.stderr)
    sys.exit(1)


def host_label(dsn: str) -> str:
    return dsn.split("@", 1)[-1] if "@" in dsn else dsn


async def run(args: argparse.Namespace) -> None:
    dsn = get_dsn()
    print(f"Connecting to {host_label(dsn)} ...", file=sys.stderr)
    conn = await asyncpg.connect(dsn)
    try:
        sql, params = build_query(args)
        rows = await conn.fetch(sql, *params)
        print(f"Fetched {len(rows)} row(s).", file=sys.stderr)

        # Compute time range for header
        until_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if args.until:
            until_ms = int(datetime.fromisoformat(args.until)
                           .replace(tzinfo=timezone.utc).timestamp() * 1000)
        since_ms = (int(datetime.fromisoformat(args.since)
                        .replace(tzinfo=timezone.utc).timestamp() * 1000)
                    if args.since
                    else until_ms - (args.hours or 24) * 3600 * 1000)

        report = aggregate(rows, args.percentiles)

        if args.output:
            # JSON output includes raw aggregates; CSV-style per-turn data
            # is intentionally omitted to keep file small — use the
            # migrate script's --output or a separate query for that.
            report["time_range"] = {
                "since_utc": datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).isoformat(),
                "until_utc": datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc).isoformat(),
            }
            with open(args.output, "w") as f:
                json.dump(report, f, indent=2, default=str)
            print(f"Wrote {args.output}", file=sys.stderr)
        else:
            print(render_text(report, args.percentiles, (since_ms, until_ms)))
    finally:
        await conn.close()


def parse_percentiles(s: str) -> list[float]:
    try:
        out = [float(x.strip()) for x in s.split(",") if x.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad percentile list: {s!r}")
    if not all(0 <= p <= 100 for p in out):
        raise argparse.ArgumentTypeError("percentiles must be in [0, 100]")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze pigugu voice agent latency from PostgreSQL.",
    )
    parser.add_argument(
        "--hours", type=float, default=24,
        help="Look back N hours from now (default: 24). Ignored if --since is set.",
    )
    parser.add_argument(
        "--since", type=str, default=None,
        help="Start of time range, ISO date or datetime (UTC). e.g. 2026-08-20 or 2026-08-20T12:00:00.",
    )
    parser.add_argument(
        "--until", type=str, default=None,
        help="End of time range, ISO date or datetime (UTC). Default: now.",
    )
    parser.add_argument(
        "--user-id", type=str, default=None,
        help="Filter to a single user_id.",
    )
    parser.add_argument(
        "--limit", type=int, default=100_000,
        help="Max rows to fetch (default: 100000).",
    )
    parser.add_argument(
        "--percentiles", type=parse_percentiles, default=[50, 90, 95, 99],
        help="Comma-separated percentile list (default: 50,90,95,99).",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Write JSON report to this file instead of printing text.",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
