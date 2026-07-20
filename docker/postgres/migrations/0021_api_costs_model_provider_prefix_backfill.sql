-- ===========================================================================
-- Migration: 0021 — Backfill api_costs.model to the provider-prefixed form
-- (ADR-0121 T4 / FRE-919, AC-8)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0021_api_costs_model_provider_prefix_backfill.sql
--
-- Fresh installs need no backfill — a new api_costs table starts empty.
--
-- Prior to this change, LiteLLMClient.record_api_call wrote the bare model id
-- (e.g. 'claude-sonnet-4-6') into api_costs.model, while the sibling code
-- change (litellm_client.py) makes new writes use the same provider-prefixed
-- string 'model_call_completed' telemetry already used (e.g.
-- 'anthropic/claude-sonnet-4-6') — AC-8 requires cost and telemetry records to
-- name the same model for the same call. Without this backfill, a
-- get_cost_by_model() query spanning the deploy boundary would silently split
-- one model's spend across two dict keys (bare vs prefixed) — caught in
-- code review (correctness, CONFIRMED). Every existing row already carries the
-- correct `provider` column, so the prefix is derived from data already
-- present, not guessed. `NOT LIKE '%/%'` makes re-running a no-op.
-- ===========================================================================

BEGIN;

UPDATE api_costs
SET model = provider || '/' || model
WHERE model NOT LIKE '%/%';

COMMIT;
