-- ===========================================================================
-- Migration: 0001 — Cost Check Gate schema (ADR-0065 / FRE-303)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_URL -f docker/postgres/migrations/0001_cost_gate_schema.sql
--
-- Fresh installs receive these tables via docker/postgres/init.sql, which
-- only runs on an empty Postgres volume. This file mirrors the gate-related
-- DDL from init.sql so existing prod / dev DBs can be brought current with
-- a single psql invocation.
-- ===========================================================================

BEGIN;

-- Layered policies (D2)
CREATE TABLE IF NOT EXISTS budget_policies (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID,
    time_window VARCHAR(16) NOT NULL,   -- 'daily' | 'weekly'
    provider VARCHAR(32),
    role VARCHAR(64) NOT NULL,          -- 'main_inference' | 'entity_extraction' | ... | '_total'
    cap_usd DECIMAL(10, 6) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, time_window, provider, role)
);
CREATE INDEX IF NOT EXISTS idx_budget_policies_lookup
    ON budget_policies(time_window, role)
    WHERE user_id IS NULL AND provider IS NULL;

-- Running totals — locked via SELECT … FOR UPDATE during reservation
CREATE TABLE IF NOT EXISTS budget_counters (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID,
    time_window VARCHAR(16) NOT NULL,
    provider VARCHAR(32),
    role VARCHAR(64) NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    running_total DECIMAL(10, 6) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, time_window, provider, role, window_start)
);
CREATE INDEX IF NOT EXISTS idx_budget_counters_lookup
    ON budget_counters(time_window, role, window_start);

-- Active and settled reservations (D1)
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    counter_id BIGINT NOT NULL REFERENCES budget_counters(id),
    role VARCHAR(64) NOT NULL,
    amount_usd DECIMAL(10, 6) NOT NULL,
    actual_cost_usd DECIMAL(10, 6),
    status VARCHAR(16) NOT NULL,        -- 'active' | 'committed' | 'refunded' | 'expired'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    settled_at TIMESTAMPTZ,
    trace_id UUID
);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_reaper
    ON budget_reservations(expires_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_budget_reservations_trace
    ON budget_reservations(trace_id);

-- Per-attempt telemetry (D6)
CREATE TABLE IF NOT EXISTS consolidation_attempts (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    role VARCHAR(64) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    outcome VARCHAR(32) NOT NULL,       -- 'success' | 'budget_denied' | 'model_error' | 'extraction_returned_fallback' | 'transient_failure' | 'dead_letter'
    denial_reason VARCHAR(64),          -- 'cap_exceeded' | 'policy_violation' | 'reservation_failed' | 'provider_error' | NULL
    UNIQUE (trace_id, attempt_number, role)
);
CREATE INDEX IF NOT EXISTS idx_consolidation_attempts_trace
    ON consolidation_attempts(trace_id);
CREATE INDEX IF NOT EXISTS idx_consolidation_attempts_outcome
    ON consolidation_attempts(outcome, started_at DESC);

-- Backfill the unscoped (_total) counter rows for the current daily and
-- weekly windows from existing api_costs aggregates so the gate sees
-- existing spend on first start.
INSERT INTO budget_counters (user_id, time_window, provider, role, window_start, running_total)
SELECT
    NULL,
    'weekly',
    NULL,
    '_total',
    date_trunc('week', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
    COALESCE(SUM(cost_usd), 0)
FROM api_costs
WHERE timestamp >= date_trunc('week', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
ON CONFLICT (user_id, time_window, provider, role, window_start) DO NOTHING;

INSERT INTO budget_counters (user_id, time_window, provider, role, window_start, running_total)
SELECT
    NULL,
    'daily',
    NULL,
    '_total',
    date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
    COALESCE(SUM(cost_usd), 0)
FROM api_costs
WHERE timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
ON CONFLICT (user_id, time_window, provider, role, window_start) DO NOTHING;

COMMIT;
