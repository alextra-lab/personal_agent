-- ===========================================================================
-- Migration: 0004 — End-to-end traceability identity columns (ADR-0074 / FRE-376)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_URL -f docker/postgres/migrations/0004_traceability_identity.sql
--
-- Fresh installs receive these columns via docker/postgres/init.sql, which
-- only runs on an empty Postgres volume. This file mirrors the schema deltas
-- from init.sql so existing prod / dev DBs can be brought current with a
-- single psql invocation.
--
-- Phase 1 of ADR-0074 closes two of the six joinability gaps:
--   * I4 — api_costs.trace_id NOT NULL; api_costs.session_id added.
--          Pre-cutoff rows are unattributable and are dropped (per ADR §
--          Consequences — backfill explicitly out of scope).
--   * I3 — sessions.primary_model_at_creation, sessions.model_config_path
--          added so the row knows which model config was active when the
--          session opened. Per-message attribution travels in messages[]
--          JSONB and is enforced at the service layer (see
--          SessionRepository.append_message).
-- ===========================================================================

BEGIN;

-- ── api_costs: identity becomes load-bearing ───────────────────────────────
-- Pre-cutoff rows have NULL trace_id (4,077 / 4,077 on prod as of 2026-05-22).
-- They are already useless for attribution; drop, do not backfill.
DELETE FROM api_costs WHERE trace_id IS NULL;

-- session_id is added NULL-able in Phase 1. The application-layer raise in
-- CostTracker.record_api_call enforces presence on every new write; a later
-- phase flips this column to NOT NULL once Phase 2 confirms the local-LLM
-- path also threads identity.
ALTER TABLE api_costs ADD COLUMN IF NOT EXISTS session_id UUID;

-- trace_id becomes NOT NULL now that the legacy NULL rows are gone.
ALTER TABLE api_costs ALTER COLUMN trace_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_api_costs_session_id ON api_costs(session_id);
CREATE INDEX IF NOT EXISTS idx_api_costs_trace_id   ON api_costs(trace_id);

-- ── sessions: row-level model attribution ──────────────────────────────────
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS primary_model_at_creation VARCHAR(120);
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS model_config_path         VARCHAR(255);

COMMIT;
