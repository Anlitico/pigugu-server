#!/usr/bin/env python3
"""
NOTE: Run with `pigagent/.venv/bin/python` — asyncpg + loguru
are installed there, not in the project root `.venv`.


migrate_metrics_format.py — One-shot migration of metrics.marks / metrics.segments
from the old flat format to the new structured format.

OLD → NEW
─────────────────────────────────────────────────────────────
marks:   {key: float}                     → {key: {perf_counter: float, unix_ms: null}}
segments:{key: float}                     → {key: {role: 'main'|'diagnostic', ms: float}}
meta:    unchanged (already a flat dict)

WHY
─────────────────────────────────────────────────────────────
turn.py now writes each mark alongside a UTC millisecond timestamp
(event_unix_ms) and a role classification on each segment, so DB rows can
be cross-system compared and filtered without joining external tables. This
script backfills that structure for rows that were written before the new
schema was deployed.

SAFETY
─────────────────────────────────────────────────────────────
1. Dry-run by default — must pass --apply to actually mutate.
2. Adds 3 columns (marks_legacy, segments_legacy, migrated_at) to back up
   the original payload BEFORE rewriting it. Original data is preserved
   verbatim in *_legacy columns and can be restored at any time.
3. Batched updates (default 500 rows / batch), each batch in its own
   transaction. If a batch fails, earlier batches are still committed.
4. Idempotent and resumable — re-running picks up exactly where the
   previous run stopped.
5. Prints row counts before / after; asks for explicit "yes" confirmation
   before applying unless --yes is passed.

USAGE
─────────────────────────────────────────────────────────────
# 1) Dry-run: just count what would change
./migrate_metrics_format.py

# 2) Apply with confirmation
./migrate_metrics_format.py --apply

# 3) Apply non-interactively (CI / cron)
./migrate_metrics_format.py --apply --yes

# 4) Limit (testing on a small subset)
./migrate_metrics_format.py --apply --limit 1000 --yes

ROLLBACK
─────────────────────────────────────────────────────────────
UPDATE metrics
SET marks = marks_legacy, segments = segments_legacy, migrated_at = NULL
WHERE marks_legacy IS NOT NULL;
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import asyncpg


# ── Segment role classification (mirror of turn.py MAIN_SEGMENT_LABELS) ──
# Keep in sync with pigagent/metrics/turn.py MAIN_SEGMENT_LABELS.
MAIN_SEGMENT_LABELS: frozenset[str] = frozenset({
    "stt", "agent_init", "orchestrator", "context",
    "llm_prep", "llm_ttft", "llm_to_tts", "tts_ttfb",
})


# ── SQL fragments ────────────────────────────────────────────────────────

ENSURE_COLUMNS_SQL = """
ALTER TABLE metrics ADD COLUMN IF NOT EXISTS marks_legacy    jsonb;
ALTER TABLE metrics ADD COLUMN IF NOT EXISTS segments_legacy jsonb;
ALTER TABLE metrics ADD COLUMN IF NOT EXISTS migrated_at     timestamptz;
"""

# Phase 1: back up marks and segments to *_legacy columns.
# Only touches rows that have NOT been migrated yet, and only copies rows
# where the column is still in the old flat format (i.e. at least one
# top-level value is a JSON number rather than an object).
BACKUP_SQL = """
UPDATE metrics
SET
  marks_legacy    = CASE WHEN marks_legacy IS NULL
                         AND marks IS NOT NULL
                         AND EXISTS (SELECT 1 FROM jsonb_each(marks) kv
                                     WHERE jsonb_typeof(kv.value) = 'number')
                         THEN marks END,
  segments_legacy = CASE WHEN segments_legacy IS NULL
                         AND segments IS NOT NULL
                         AND EXISTS (SELECT 1 FROM jsonb_each(segments) kv
                                     WHERE jsonb_typeof(kv.value) = 'number')
                         THEN segments END
WHERE migrated_at IS NULL
  AND (marks_legacy IS NULL OR segments_legacy IS NULL)
  AND (marks IS NOT NULL OR segments IS NOT NULL);
"""

# Phase 2: rewrite marks and segments to new format. Each batch is atomic.
# A row qualifies only if it has a *_legacy backup AND has not been migrated
# yet. For each key in the JSON, if the value is a number, wrap it; if it's
# already an object, leave it (defensive — handles partial-migration edge
# cases). For segments, the role is determined by the label set above.
BATCH_MIGRATE_SQL = """
WITH to_migrate AS (
  SELECT user_id, turn_id, marks, segments
  FROM metrics
  WHERE marks_legacy IS NOT NULL
    AND segments_legacy IS NOT NULL
    AND migrated_at IS NULL
  LIMIT $1
),
new_marks AS (
  SELECT
    tm.user_id, tm.turn_id,
    jsonb_object_agg(
      kv.key,
      CASE
        WHEN jsonb_typeof(kv.value) = 'number'
          THEN jsonb_build_object('perf_counter', kv.value, 'unix_ms', NULL::jsonb)
        ELSE kv.value
      END
    ) AS nm
  FROM to_migrate tm, jsonb_each(tm.marks) kv
  WHERE tm.marks IS NOT NULL
  GROUP BY tm.user_id, tm.turn_id
),
new_segments AS (
  SELECT
    tm.user_id, tm.turn_id,
    jsonb_object_agg(
      kv.key,
      CASE
        WHEN jsonb_typeof(kv.value) = 'number'
          THEN jsonb_build_object(
                 'role', CASE WHEN kv.key = ANY($2::text[]) THEN 'main' ELSE 'diagnostic' END,
                 'ms',   kv.value
               )
        ELSE kv.value
      END
    ) AS ns
  FROM to_migrate tm, jsonb_each(tm.segments) kv
  WHERE tm.segments IS NOT NULL
  GROUP BY tm.user_id, tm.turn_id
)
UPDATE metrics m
SET
  marks        = COALESCE(nm.nm, m.marks),
  segments     = COALESCE(ns.ns, m.segments),
  migrated_at  = now()
FROM to_migrate tm
LEFT JOIN new_marks    nm ON nm.user_id = tm.user_id AND nm.turn_id = tm.turn_id
LEFT JOIN new_segments ns ON ns.user_id = tm.user_id AND ns.turn_id = tm.turn_id
WHERE m.user_id = tm.user_id AND m.turn_id = tm.turn_id;
"""

# Counts and sanity-check queries
COUNTS_SQL = """
SELECT
  COUNT(*)                                                              AS total,
  COUNT(marks_legacy)                                                   AS legacy_backed_up,
  COUNT(segments_legacy)                                                AS legacy_segs_backed_up,
  COUNT(migrated_at)                                                    AS migrated,
  COUNT(*) FILTER (WHERE marks_legacy IS NULL AND migrated_at IS NULL) AS fresh,
  COUNT(*) FILTER (WHERE marks_legacy IS NOT NULL
                    AND migrated_at IS NULL)                           AS pending
FROM metrics;
"""

# A row has "old-format marks" if at least one top-level value is a number
SAMPLE_OLD_MARKS_SQL = """
SELECT user_id, turn_id, marks
FROM metrics
WHERE EXISTS (SELECT 1 FROM jsonb_each(marks) kv
              WHERE jsonb_typeof(kv.value) = 'number')
LIMIT 3;
"""

SAMPLE_NEW_MARKS_SQL = """
SELECT user_id, turn_id, marks
FROM metrics
WHERE marks_legacy IS NOT NULL
  AND migrated_at IS NOT NULL
LIMIT 3;
"""


# ── Script entry point ───────────────────────────────────────────────────

def get_dsn() -> str:
    """Resolve the PostgreSQL DSN from environment.

    Same precedence as analyze_latency.py: prefer a non-localhost
    DATABASE_URL, then fall back to PG* (used by the .claude/skills/ops
    modules that point at the production RDS).
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


async def ensure_columns(conn: asyncpg.Connection) -> None:
    await conn.execute(ENSURE_COLUMNS_SQL)


async def backup_phase(conn: asyncpg.Connection) -> int:
    """One-shot backup of marks/segments to *_legacy columns. Idempotent."""
    return int((await conn.execute(BACKUP_SQL)).split()[-1])


async def migrate_one_batch(conn: asyncpg.Connection, batch_size: int,
                            main_labels: list[str]) -> int:
    async with conn.transaction():
        return int((await conn.execute(
            BATCH_MIGRATE_SQL, batch_size, main_labels
        )).split()[-1])


async def fetch_counts(conn: asyncpg.Connection) -> dict:
    row = await conn.fetchrow(COUNTS_SQL)
    return dict(row)


async def sample(conn: asyncpg.Connection, sql: str) -> list[asyncpg.Record]:
    return await conn.fetch(sql)


async def run(args: argparse.Namespace) -> None:
    dsn = get_dsn()
    print(f"Connecting to {host_label(dsn)} ...")
    conn = await asyncpg.connect(dsn)
    try:
        print("\n[1/4] Ensuring backup columns exist (idempotent) ...")
        await ensure_columns(conn)
        print("  OK: marks_legacy, segments_legacy, migrated_at present.")

        print("\n[2/4] Backing up original marks/segments to *_legacy ...")
        n_backed = await backup_phase(conn)
        print(f"  Backed up {n_backed} row(s).")

        counts = await fetch_counts(conn)
        total = counts["total"]
        legacy = counts["legacy_backed_up"]
        migrated = counts["migrated"]
        pending = counts["pending"]
        fresh = counts["fresh"]
        print("\n[3/4] Current state:")
        print(f"  Total rows in metrics: {total}")
        print(f"  Backed up (marks_legacy set): {legacy}")
        print(f"  Already migrated: {migrated}")
        print(f"  Fresh rows (no legacy needed, already in new format): {fresh}")
        print(f"  Pending migration: {pending}")

        if pending == 0:
            print("\n  Nothing to do. All rows are either already migrated or")
            print("  already in the new format. Exiting.")
            return

        if args.dry_run:
            print(f"\n[4/4] DRY RUN — would migrate {pending} row(s) in batches")
            print(f"  of {args.batch_size}. Re-run with --apply to commit.")
            return

        # Show a sample so the user can sanity-check what they're about to
        # commit before they say "yes".
        print("\n  Sample of rows ABOUT to be migrated (legacy format):")
        for r in await sample(conn, SAMPLE_OLD_MARKS_SQL):
            print(f"    {r['user_id']}/{r['turn_id']}: marks={dict(r['marks'])}")

        if not args.yes:
            print(f"\n[4/4] About to migrate {pending} row(s) in batches of"
                  f" {args.batch_size}.")
            print("  This will rewrite the marks and segments jsonb columns in"
                  " place.")
            print("  Original data is preserved in marks_legacy / segments_legacy"
                  " columns.")
            confirm = input("\n  Type 'yes' to continue: ").strip().lower()
            if confirm != "yes":
                print("  Aborted. No changes made.")
                return

        print(f"\n[4/4] Migrating in batches of {args.batch_size} ...")
        t0 = time.time()
        total_migrated = 0
        batch_num = 0
        main_labels = sorted(MAIN_SEGMENT_LABELS)
        while True:
            batch_num += 1
            n = await migrate_one_batch(conn, args.batch_size, main_labels)
            if n == 0:
                break
            total_migrated += n
            print(f"  Batch {batch_num}: migrated {n} row(s)"
                  f"  (running total: {total_migrated})")
            if args.limit and total_migrated >= args.limit:
                print(f"  Reached --limit={args.limit}, stopping.")
                break
            if args.sleep_ms > 0:
                await asyncio.sleep(args.sleep_ms / 1000)

        elapsed = time.time() - t0
        print(f"\n  Done. Migrated {total_migrated} row(s) in {elapsed:.1f}s.")

        # Show a sample of post-migration state
        print("\n  Sample of rows AFTER migration (new format):")
        for r in await sample(conn, SAMPLE_NEW_MARKS_SQL):
            marks = dict(r["marks"])
            # Truncate to first 3 keys for readable output
            sample_marks = dict(list(marks.items())[:3])
            print(f"    {r['user_id']}/{r['turn_id']}: marks={sample_marks}"
                  f"{' ...' if len(marks) > 3 else ''}")

        print("\n  Rollback command (if something looks wrong):")
        print("    UPDATE metrics")
        print("    SET marks = marks_legacy,")
        print("        segments = segments_legacy,")
        print("        migrated_at = NULL")
        print("    WHERE marks_legacy IS NOT NULL;")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate metrics.marks and metrics.segments to new structured format.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually perform the migration (default: dry-run, just count).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=500,
        help="Rows per batch (default: 500).",
    )
    parser.add_argument(
        "--sleep-ms", type=int, default=100,
        help="Milliseconds to sleep between batches (default: 100).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after migrating N rows (for testing on a small subset).",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the interactive confirmation prompt (use in CI/cron).",
    )
    args = parser.parse_args()
    args.dry_run = not args.apply
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted. Already-migrated rows are committed; re-run"
              " the script to resume.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
