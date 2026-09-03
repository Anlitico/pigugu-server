-- One-time operational cleanup: drop the pgsql metrics tables that the
-- observability refactor (branch refactor/telemetry-observability) stopped
-- writing. All structured metrics now land in ClickHouse (`metrics.*`).
--
-- pgsql stays as the application OLTP store (context/prompts/users); only the
-- dead metrics tables are removed. Coldstart was dead code (metrics/session.py
-- deleted) — never written in the live voice path.
--
-- NOT wired into alembic: the repo's migration graph is multi-head/tangled
-- here, so attaching a new revision risks landing on the wrong head. The
-- alembic files that create these tables are left in place for history; a
-- fresh DB that runs `upgrade head` recreates empty tables, which this script
-- clears in one shot. Idempotent.
--
-- Run against the RDS pgsql (pigugu-server DATABASE_URL) via the existing
-- /ops pg flow, e.g.:
--   kubectl run ... -- psql "$DATABASE_URL" -f - < scripts/drop_pgsql_metrics_tables.sql

DROP TABLE IF EXISTS metrics;
DROP TABLE IF EXISTS compression_metrics;
DROP TABLE IF EXISTS coldstart_metrics;
