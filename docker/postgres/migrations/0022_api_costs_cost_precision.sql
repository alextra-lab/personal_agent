-- ===========================================================================
-- Migration: 0022 — Widen api_costs.cost_usd precision (FRE-974)
--
-- Idempotent (ALTER COLUMN TYPE to the same wider precision is a no-op on
-- re-run). Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0022_api_costs_cost_precision.sql
--
-- DECIMAL(10,6) rounds a sub-$0.000001 cost to $0.000000 — a real outcome for
-- OVH-embedding (EUR-priced) and Voyage-reranker (per-call) costs, which are
-- denominated far below the LLM-chat cost rows this column was originally
-- sized for. Widening to DECIMAL(18,12) preserves every existing row's value
-- exactly (a strictly wider precision is a lossless cast) and gives six more
-- fractional digits of headroom for the new vendor-cost writes.
-- ===========================================================================

BEGIN;

ALTER TABLE api_costs ALTER COLUMN cost_usd TYPE DECIMAL(18, 12);

COMMIT;
