-- ===========================================================================
-- Migration: 0013 — budget_reservations identity threading (FRE-693, ADR-0074 §8c)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_URL -f docker/postgres/migrations/0013_budget_reservations_identity.sql
--
-- Fresh installs receive these columns via docker/postgres/init.sql, which only
-- runs on an empty Postgres volume.
--
-- Background: the ADR-0065 cost gate's budget_reservations table only ever
-- carried trace_id. AC-12 (ADR-0101 §8c) requires cost-gate rows to join back
-- to the turn by trace_id + session_id + task_id (task_id NULL at the turn
-- level — see route_traces' own convention). This migration adds both columns
-- and backfills session_id on pre-existing rows from api_costs (ADR-0074 §I4:
-- api_costs.session_id is NOT NULL and keyed by the same trace_id). Without the
-- backfill, the joinability probe's new orphan check (FRE-693) would red-flag
-- every pre-cutoff reservation it happens to sample — a false positive, not a
-- real identity gap.
-- ===========================================================================

BEGIN;

ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS session_id UUID;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS task_id UUID;

CREATE INDEX IF NOT EXISTS idx_budget_reservations_session
    ON budget_reservations(session_id);

-- Backfill: every trace_id maps to exactly one session_id, so DISTINCT ON is a
-- determinism safeguard (in case a trace produced more than one api_costs row
-- across tool-loop iterations), not a correctness requirement.
UPDATE budget_reservations br
   SET session_id = ac.session_id
  FROM (SELECT DISTINCT ON (trace_id) trace_id, session_id FROM api_costs) ac
 WHERE br.trace_id = ac.trace_id
   AND br.session_id IS NULL;

COMMIT;
