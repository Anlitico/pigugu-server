#!/usr/bin/env python3
"""
analyze_latency.py — Read metrics from ClickHouse and print latency stats.

Reads the ``metrics.turn_latency`` table over the ClickHouse HTTP interface
(port 8123) using only the Python standard library (urllib), so it runs in
any python image without extra deps — no asyncpg / loguru / image rebuild.
Works on rows in both the old flat format ({key: float}) and the new
structured format ({key: {perf_counter, unix_ms}} / {key: {role, ms}}).

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
import json
import math
import os
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any


# ── Constants ────────────────────────────────────────────────────────────

# Which section a segment is shown under is decided per-row by the segment's
# stored ``role`` (segments JSON). These lists only keep the report's column
# order stable and supply the default for rows written before the role field
# existed. ``stt`` left the main chain when metrics moved to ClickHouse: the
# writer now emits it with role="diagnostic" (server_received_vad_at is a
# device-clock anchor, not part of the serial server E2E chain).
MAIN_SEGMENT_LABELS: list[str] = [
    "agent_init", "orchestrator", "context",
    "llm_prep", "llm_ttft", "llm_to_tts", "tts_ttfb",
]
DIAG_SEGMENT_LABELS: list[str] = [
    "stt", "vad", "server_vad", "vad_to_recv", "llm_rest", "tts",
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


# ── ClickHouse query ─────────────────────────────────────────────────────

CH_TABLE = "metrics.turn_latency"


def _ch_literal(v: str) -> str:
    """Single-quoted ClickHouse string literal."""
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"


def resolve_time_bounds(args: argparse.Namespace) -> tuple[int, int]:
    """agent_spk unix_ms bounds (ms since epoch UTC) for the window."""
    if args.until:
        until_dt = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
        until_ms = int(until_dt.timestamp() * 1000)
    else:
        until_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    if args.since:
        since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        since_ms = int(since_dt.timestamp() * 1000)
    else:
        since_ms = until_ms - int((args.hours or 24) * 3600 * 1000)
    return since_ms, until_ms


def build_query(args: argparse.Namespace, since_ms: int, until_ms: int) -> str:
    """SELECT the window from metrics.turn_latency over ClickHouse HTTP.

    Filtering mirrors the old pgsql query: only rows whose
    ``marks.agent_spk.unix_ms`` falls inside the window qualify; rows without
    that mark are skipped. JSON columns stay as text and are parsed in Python
    (marks/segments/meta are single-line JSON, so TSV round-trips them fine).
    """
    conds = [
        "JSONHas(marks, 'agent_spk', 'unix_ms')",
        f"JSONExtractInt(marks, 'agent_spk', 'unix_ms') >= {int(since_ms)}",
        f"JSONExtractInt(marks, 'agent_spk', 'unix_ms') <= {int(until_ms)}",
    ]
    if args.user_id:
        conds.append(f"user_id = {_ch_literal(args.user_id)}")
    sql = (
        "SELECT user_id, turn_id, marks, segments, meta, stt_tail_ms, "
        "e2e_perceived_ms\n"
        f"FROM {CH_TABLE}\n"
        "WHERE " + "\n  AND ".join(conds) + "\n"
        "ORDER BY JSONExtractInt(marks, 'agent_spk', 'unix_ms')\n"
        f"LIMIT {int(args.limit)}\n"
        "FORMAT TSV"
    )
    return sql


# ── Aggregation ──────────────────────────────────────────────────────────

def aggregate(rows: list[dict], percentiles: list[float]) -> tuple[dict, dict[str, str]]:
    e2e_values: list[float] = []
    server_e2e_values: list[float] = []
    stt_tail_values: list[float] = []
    segment_values: dict[str, list[float]] = {k: [] for k in ALL_SEGMENT_LABELS}
    phase_buckets: dict[str, list[float]] = {}
    fallback_buckets: dict[str, list[float]] = {}
    seg_roles: dict[str, str] = {}
    rows_with_negative_segments: list[dict] = []
    rows_without_device_anchor: list[dict] = []
    rows_with_vad_end_fallback_detect: int = 0
    anchored_turns: int = 0
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

        # New typed columns (0005): stt_tail = user_stop -> stt_final,
        # e2e_perceived = user_stop -> first bot audio. The firmware reports a
        # device user-stop (vad_silence), so rows written by the current writer
        # carry both > 0 when the anchor exists; pre-column rows are 0.
        typed_stt_tail = int(row.get("stt_tail_ms") or 0)
        typed_perceived = int(row.get("e2e_perceived_ms") or 0)
        server_e2e = extract_segment_ms(segments, "e2e")

        if typed_stt_tail > 0 or typed_perceived > 0:
            anchored_turns += 1
        if typed_stt_tail > 0:
            stt_tail_values.append(float(typed_stt_tail))

        # Headline E2E = PERCEIVED (user_stop -> first bot audio), as the
        # writer computed it. Pre-column rows fall back to the stored server
        # E2E (stt_final -> agent_spk) so history stays comparable.
        if typed_perceived > 0:
            e2e_ms = float(typed_perceived)
        else:
            e2e_ms = server_e2e
            if e2e_ms is None:
                agent_spk = extract_perf_counter(marks, "agent_spk")
                if agent_spk is not None:
                    for k in ("stt_final", "server_received_vad_at", "vad_end"):
                        s = extract_perf_counter(marks, k)
                        if s is not None:
                            e2e_ms = (agent_spk - s) * 1000.0
                            break
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

        # Server E2E (stt_final -> agent_spk) for reconciliation with the main
        # chain; excludes STT, so it is naturally <= perceived.
        if server_e2e is not None and MIN_E2E_MS <= server_e2e <= MAX_E2E_MS:
            server_e2e_values.append(server_e2e)

        # Per-segment. The stored role decides the main-vs-diagnostic section
        # at render time; the constant lists only provide order/fallback.
        for k in ALL_SEGMENT_LABELS:
            raw = segments.get(k)
            if isinstance(raw, dict) and isinstance(raw.get("role"), str):
                seg_roles[k] = raw["role"]
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

        if typed_stt_tail == 0:
            rows_without_device_anchor.append({
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

    report = {
        "total_rows": total_rows,
        "new_format_count": new_format_count,
        "old_format_count": old_format_count,
        "anchored_turns": anchored_turns,
        "e2e": stats(e2e_values),
        "server_e2e": stats(server_e2e_values),
        "stt_tail": stats(stt_tail_values),
        "segments": {k: stats(v) for k, v in segment_values.items()},
        "by_phase": {k: stats(v) for k, v in phase_buckets.items()},
        "by_fallback": {k: stats(v) for k, v in fallback_buckets.items()},
        "anomalies": {
            "negative_or_outlier": rows_with_negative_segments[:50],  # cap
            "missing_device_anchor": rows_without_device_anchor[:50],
            "detect_fallback_count": rows_with_vad_end_fallback_detect,
        },
    }
    return report, seg_roles


# ── Rendering ────────────────────────────────────────────────────────────

def effective_segment_role(label: str, seg_roles: dict[str, str]) -> str:
    """Section a segment belongs in: the row's stored role wins, else the
    canonical default from the label lists."""
    r = seg_roles.get(label)
    if r in ("main", "diagnostic"):
        return r
    return "main" if label in MAIN_SEGMENT_LABELS else "diagnostic"


def render_text(report: dict, seg_roles: dict[str, str], percentiles: list[float],
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
        # p may be int (argparse default) or float (parsed from "--percentiles
        # 50,90,99.9") — format without assuming an integral type.
        labels = [f"p{format(p, 'g')}" for p in percentiles]
        width = max(len(x) for x in labels)
        cols = "  ".join(x.rjust(width) for x in labels)
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

    out.append(header("── E2E perceived (user_stop → first bot audio) ──"))
    out.append(row("e2e", report["e2e"]))
    out.append(
        f"  anchored device user-stops: {report['anchored_turns']} / "
        f"{report['total_rows']} ({fmt_pct(report['anchored_turns'], report['total_rows']).strip()})"
    )

    out.append(header("── Server E2E (stt_final → agent_spk) ──"))
    out.append(row("server_e2e", report["server_e2e"]))

    out.append(header("── STT tail (user_stop → stt_final; device-anchored) ──"))
    out.append(row("stt_tail", report["stt_tail"]))

    out.append(header("── Main chain (sum == server E2E in theory) ──"))
    for k in MAIN_SEGMENT_LABELS:
        if effective_segment_role(k, seg_roles) == "main":
            out.append(row(k, report["segments"][k]))

    out.append(header("── Diagnostics (overlap / can be negative) ──"))
    for k in DIAG_SEGMENT_LABELS:
        if effective_segment_role(k, seg_roles) == "diagnostic":
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
    out.append(f"  turns with no device user-stop (server-e2e fallback): "
               f"{len(a['missing_device_anchor'])}  (sample: {a['missing_device_anchor'][:3]})")
    out.append(f"  turns with negative / outlier E2E:    {len(a['negative_or_outlier'])}"
               f"  (sample: {a['negative_or_outlier'][:3]})")
    out.append(f"  turns using detect as vad_end fallback: {a['detect_fallback_count']}")
    out.append("")
    return "\n".join(out)


# ── ClickHouse transport (stdlib urllib) ─────────────────────────────────

def _ch_http_endpoint() -> str:
    host = os.getenv("CLICKHOUSE_HOST", "clickhouse").strip()
    port = os.getenv("CLICKHOUSE_PORT", "8123").strip()
    return f"http://{host}:{port}"


def _tsv_unescape(s: str) -> str:
    """Undo ClickHouse TSV backslash escaping on a single column value."""
    if "\\" not in s:
        return s
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _json_field(s: str) -> dict:
    if not s:
        return {}
    try:
        v = json.loads(s)
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def fetch_rows(sql: str) -> list[dict]:
    """POST the query to ClickHouse HTTP and parse the TSV response."""
    endpoint = _ch_http_endpoint()
    user = os.getenv("CLICKHOUSE_USER", "default").strip()
    password = os.getenv("CLICKHOUSE_PASSWORD", "")
    database = os.getenv("CLICKHOUSE_DATABASE", "voice").strip()
    headers = {"X-ClickHouse-User": user, "Content-Type": "text/plain"}
    if database:
        headers["X-ClickHouse-Database"] = database
    if password:
        headers["X-ClickHouse-Key"] = password
    req = urllib.request.Request(endpoint + "/", data=sql.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        print(f"ERROR: ClickHouse HTTP {e.code}: "
              f"{e.read().decode('utf-8', 'replace').strip()}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR: cannot reach ClickHouse at {endpoint}: {e.reason}",
              file=sys.stderr)
        sys.exit(1)

    rows: list[dict] = []
    for line in raw.split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 7:  # defensive — SELECT lists exactly 7 columns
            continue
        user_id = _tsv_unescape(parts[0])
        try:
            turn_id = int(_tsv_unescape(parts[1]))
        except ValueError:
            turn_id = 0
        marks = _json_field(_tsv_unescape(parts[2]))
        segments = _json_field(_tsv_unescape(parts[3]))
        meta = _json_field(_tsv_unescape(parts[4]))

        def _int_col(raw_col: str) -> int:
            try:
                return int(_tsv_unescape(raw_col))
            except ValueError:
                return 0

        rows.append({
            "user_id": user_id,
            "turn_id": turn_id,
            "marks": marks,
            "segments": segments,
            "meta": meta,
            "stt_tail_ms": _int_col(parts[5]),
            "e2e_perceived_ms": _int_col(parts[6]),
        })
    return rows


# ── Entry point ──────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    since_ms, until_ms = resolve_time_bounds(args)
    sql = build_query(args, since_ms, until_ms)
    print(f"Connecting to {_ch_http_endpoint()} ...", file=sys.stderr)
    rows = fetch_rows(sql)
    print(f"Fetched {len(rows)} row(s).", file=sys.stderr)

    report, seg_roles = aggregate(rows, args.percentiles)

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
        print(render_text(report, seg_roles, args.percentiles, (since_ms, until_ms)))


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
        description="Analyze pigugu voice agent latency from ClickHouse.",
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
    run(args)


if __name__ == "__main__":
    main()
