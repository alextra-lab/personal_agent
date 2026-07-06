-- ===========================================================================
-- Migration: 0018 — sysgraph.proposal.scope (ADR-0105 D9/D10 / FRE-721)
--
-- FRE-721 (T7) generation-time read-before-emit matches an "equivalent"
-- awaiting proposal on (source, category, scope) rather than (source,
-- category) alone — category-only would treat every awaiting proposal in a
-- category as equivalent to every other one, over-suppressing distinct
-- ideas. `scope` already exists on ProposedChange for both producers, so
-- this is a zero-extraction-cost widening of the match key, not a new
-- taxonomy. Nullable: historical rows written by the promotion-only path
-- (FRE-714/FRE-717) predate this column and carry no scope.
--
-- `is_kind_decided`/`get_signal` (FRE-717, already shipped) intentionally
-- stay at (source, category) grain — that is a coarser, rarer, stronger
-- signal ("this whole category is decided") and widening its grain is a
-- separate, riskier change this ticket does not make.
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0018_sysgraph_proposal_scope.sql
-- ===========================================================================

BEGIN;

SET ROLE sysgraph_role;

ALTER TABLE sysgraph.proposal ADD COLUMN IF NOT EXISTS scope TEXT;

CREATE INDEX IF NOT EXISTS idx_sysgraph_proposal_source_category_scope
    ON sysgraph.proposal(source, category, scope);

RESET ROLE;

COMMIT;
