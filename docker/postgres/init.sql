-- Enable pgvector extension for embedding storage
CREATE EXTENSION IF NOT EXISTS vector;

-- Sessions table
CREATE TABLE IF NOT EXISTS sessions (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
    channel VARCHAR(50),
    metadata JSONB DEFAULT '{}',
    messages JSONB DEFAULT '[]'
);

CREATE INDEX idx_sessions_last_active ON sessions(last_active_at DESC);

-- Metrics table (time-series style)
CREATE TABLE IF NOT EXISTS metrics (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trace_id UUID,
    metric_name VARCHAR(100) NOT NULL,
    metric_value DOUBLE PRECISION NOT NULL,
    unit VARCHAR(20),
    tags JSONB DEFAULT '{}'
);

CREATE INDEX idx_metrics_timestamp ON metrics(timestamp DESC);
CREATE INDEX idx_metrics_trace_id ON metrics(trace_id);
CREATE INDEX idx_metrics_name ON metrics(metric_name);

-- Captain's Log captures (fast writes during request)
CREATE TABLE IF NOT EXISTS captains_log_captures (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL UNIQUE,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_message TEXT,
    steps JSONB DEFAULT '[]',
    tools_used TEXT[] DEFAULT '{}',
    duration_ms INTEGER,
    metrics_summary JSONB DEFAULT '{}',
    outcome VARCHAR(50)
);

CREATE INDEX idx_captures_timestamp ON captains_log_captures(timestamp DESC);

-- Captain's Log reflections (written by second brain)
CREATE TABLE IF NOT EXISTS captains_log_reflections (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL REFERENCES captains_log_captures(trace_id),
    reflection_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rationale TEXT,
    entities_extracted TEXT[] DEFAULT '{}',
    connections_found TEXT[] DEFAULT '{}',
    proposed_changes JSONB DEFAULT '[]'
);

CREATE INDEX idx_reflections_timestamp ON captains_log_reflections(reflection_timestamp DESC);

-- API cost tracking
CREATE TABLE IF NOT EXISTS api_costs (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provider VARCHAR(50) NOT NULL,  -- 'anthropic', 'openai', etc.
    model VARCHAR(100) NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd DECIMAL(10, 6) NOT NULL DEFAULT 0,
    trace_id UUID,
    purpose VARCHAR(50),  -- 'user_request', 'second_brain', 'entity_extraction'
    latency_ms INTEGER
);

CREATE INDEX idx_api_costs_timestamp ON api_costs(timestamp DESC);
CREATE INDEX idx_api_costs_provider ON api_costs(provider);

-- Embeddings table (for future semantic search)
-- Uses pgvector for efficient similarity search
CREATE TABLE IF NOT EXISTS embeddings (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_type VARCHAR(50) NOT NULL,  -- 'conversation', 'entity', 'reflection'
    source_id UUID NOT NULL,           -- Reference to source record
    content_hash VARCHAR(64),          -- SHA256 of embedded content (dedup)
    embedding vector(1536),            -- OpenAI ada-002 dimension (adjust as needed)
    metadata JSONB DEFAULT '{}'
);

-- HNSW index for fast approximate nearest neighbor search
CREATE INDEX idx_embeddings_vector ON embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_embeddings_source ON embeddings(source_type, source_id);
CREATE INDEX idx_embeddings_hash ON embeddings(content_hash);

-- ===========================================================================
-- Cost Check Gate (ADR-0065 / FRE-303)
--
-- Atomic Postgres-backed reservation primitive in front of every paid LLM
-- call. v1 policies keyed by (time_window, role); user_id and provider
-- columns present from day 1 (nullable in v1) so v2 per-user / per-provider
-- caps drop in without migration.
-- ===========================================================================

-- Layered policies (D2). All matching caps must approve a reservation; the
-- most restrictive cap wins.
CREATE TABLE IF NOT EXISTS budget_policies (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID,                       -- v1: NULL; v2: per-user policy
    time_window VARCHAR(16) NOT NULL,   -- 'daily' | 'weekly'
    provider VARCHAR(32),               -- v1: NULL; v2: per-provider policy
    role VARCHAR(64) NOT NULL,          -- 'main_inference' | 'entity_extraction' | ... | '_total'
    cap_usd DECIMAL(10, 6) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- NULLS NOT DISTINCT (PG 15+) — v1 always has user_id=NULL and
    -- provider=NULL; without this, concurrent INSERTs would produce
    -- duplicate "unique" rows because Postgres treats NULL != NULL in
    -- unique constraints by default.
    UNIQUE NULLS NOT DISTINCT (user_id, time_window, provider, role)
);
CREATE INDEX IF NOT EXISTS idx_budget_policies_lookup
    ON budget_policies(time_window, role)
    WHERE user_id IS NULL AND provider IS NULL;

-- Running totals — the row that SELECT … FOR UPDATE locks during reservation.
-- window_start normalised to UTC midnight (daily) or UTC Monday midnight
-- (weekly) so windows roll automatically without a cron job: a reservation
-- against a "new" window writes a new row with zero running total.
CREATE TABLE IF NOT EXISTS budget_counters (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID,
    time_window VARCHAR(16) NOT NULL,
    provider VARCHAR(32),
    role VARCHAR(64) NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    running_total DECIMAL(10, 6) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE NULLS NOT DISTINCT (user_id, time_window, provider, role, window_start)
);
CREATE INDEX IF NOT EXISTS idx_budget_counters_lookup
    ON budget_counters(time_window, role, window_start);

-- Active and settled reservations (D1). 90-second TTL; a reaper sweeps stale
-- rows on a 30s cadence and refunds them to the counter (catches caller crash
-- between reserve and commit).
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    counter_id BIGINT NOT NULL REFERENCES budget_counters(id),
    role VARCHAR(64) NOT NULL,
    amount_usd DECIMAL(10, 6) NOT NULL,
    actual_cost_usd DECIMAL(10, 6),     -- populated on commit
    status VARCHAR(16) NOT NULL,        -- 'active' | 'committed' | 'refunded' | 'expired'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,    -- created_at + 90s
    settled_at TIMESTAMPTZ,
    trace_id UUID
);
-- Reaper hot-path: only scan active reservations past their TTL.
CREATE INDEX IF NOT EXISTS idx_budget_reservations_reaper
    ON budget_reservations(expires_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_budget_reservations_trace
    ON budget_reservations(trace_id);

-- Per-attempt telemetry (D6). Covers entity-extraction / promotion retries;
-- event-driven Redis Streams redelivery is observable separately via
-- XPENDING. Joined to chat traces via trace_id.
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

-- Backfill — populate the unscoped (_total) counter rows for the current
-- daily and weekly windows from existing api_costs aggregates so the gate
-- sees existing spend on first start. Per-role backfill isn't possible
-- because api_costs.purpose is freeform and doesn't map cleanly to ADR roles
-- — the gate starts tracking per-role spend going forward.
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
