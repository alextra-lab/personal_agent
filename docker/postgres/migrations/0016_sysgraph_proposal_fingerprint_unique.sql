-- ===========================================================================
-- Migration: 0016 — sysgraph.proposal.fingerprint UNIQUE constraint
-- (ADR-0105 D4 / FRE-716)
--
-- FRE-716 upserts sysgraph.proposal rows keyed on fingerprint
-- (ON CONFLICT (fingerprint) DO UPDATE ...) when writing the PROMOTED_TO
-- linkage. Migration 0014 only indexed fingerprint, not constrained it —
-- safe to add now because nothing writes to sysgraph.proposal in production
-- yet (FRE-716 is the first writer).
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0016_sysgraph_proposal_fingerprint_unique.sql
-- ===========================================================================

BEGIN;

SET ROLE sysgraph_role;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_constraint WHERE conname = 'sysgraph_proposal_fingerprint_key'
    ) THEN
        ALTER TABLE sysgraph.proposal
            ADD CONSTRAINT sysgraph_proposal_fingerprint_key UNIQUE (fingerprint);
    END IF;
END
$$;

RESET ROLE;

COMMIT;
