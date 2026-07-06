-- ===========================================================================
-- Migration: 0017 — sysgraph.signal (realized-value suppression cooldown)
-- (ADR-0105 D7 / FRE-717)
--
-- FRE-717 closes the outcome->source loop: a ticket outcome (shipped /
-- owner-rejected / canceled-as-noise) is recorded and a realized-value
-- signal per (source, category) is read back by the next promotion run.
-- The value itself (v) is computed on read from sysgraph.outcome rows —
-- never persisted, so it never drifts from the 90-day trailing window. Only
-- the suppression cooldown needs persisted state (a fixed-duration timer,
-- parallel to the existing fingerprint suppression in
-- captains_log/suppression.py — not a live recompute).
--
-- Also adds a UNIQUE(ticket_id) constraint on sysgraph.produced: a ticket
-- has exactly one terminal outcome, and record_outcome()'s
-- ON CONFLICT (ticket_id) DO NOTHING depends on this existing so concurrent
-- callers can't each insert a distinct outcome for the same ticket.
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0017_sysgraph_signal.sql
-- ===========================================================================

BEGIN;

SET ROLE sysgraph_role;

CREATE TABLE IF NOT EXISTS sysgraph.signal (
    source           TEXT NOT NULL CHECK (source IN ('statistical_detector', 'reflection')),
    category         TEXT NOT NULL,
    suppressed_until TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, category)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_constraint WHERE conname = 'sysgraph_produced_ticket_unique'
    ) THEN
        ALTER TABLE sysgraph.produced
            ADD CONSTRAINT sysgraph_produced_ticket_unique UNIQUE (ticket_id);
    END IF;
END
$$;

-- Supports get_signal()'s windowed (observed_at) filter without a seq scan
-- as outcome rows grow.
CREATE INDEX IF NOT EXISTS idx_sysgraph_outcome_observed_at ON sysgraph.outcome(observed_at);

RESET ROLE;

COMMIT;
