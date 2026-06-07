-- Migration 0009: Route-trace ledger (FRE-452 / ADR-0088 D6 sink 1)
--
-- The route_traces table is the *direct durable write* of the ADR-0088 execution-topology
-- observability contract: one row per turn capturing what the gateway decided (the
-- deterministic-shell label) alongside what the harness actually did (the orchestration
-- event). It is bus-independent (ADR-0088 D8) and joins to api_costs on trace_id for the
-- authoritative cost SUM (ADR-0088 D3).
--
-- UNIQUE(trace_id) is the turn-level idempotency key backing ON CONFLICT DO NOTHING. When
-- the ADR-0088 seam later emits per-topology rows, the key migrates to (trace_id, task_id);
-- task_id is reserved here as the forward slot.
--
-- No Alembic (project policy): schema lives in init.sql + ordered migrations.

CREATE TABLE IF NOT EXISTS route_traces (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL UNIQUE,
    session_id UUID,
    task_id UUID,                                  -- forward slot for the ADR-0088 seam
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schema_version SMALLINT NOT NULL DEFAULT 1,

    -- Stimulus (PII-gated: preview only when route_trace_store_preview is enabled)
    user_message_chars INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    user_message_sha256 VARCHAR(16),
    user_message_preview TEXT,

    -- Gateway classification (deterministic shell)
    task_type VARCHAR(40),
    complexity VARCHAR(20),
    intent_confidence REAL,
    decomposition_strategy VARCHAR(20),
    decomposition_reason TEXT,
    degraded_stages TEXT[],
    mode VARCHAR(40),
    channel VARCHAR(40),
    gateway_label VARCHAR(120) NOT NULL DEFAULT 'unknown/unknown',

    -- Model path
    model_role VARCHAR(40),
    thinking_enabled BOOLEAN,
    routing_history JSONB,

    -- Tools / skills
    tool_iteration_count INTEGER NOT NULL DEFAULT 0,
    tools_used TEXT[],
    skills_loaded TEXT[],

    -- Delegation
    sub_agent_count INTEGER NOT NULL DEFAULT 0,
    sub_agents JSONB,
    expansion_strategy VARCHAR(40),
    delegate_result_passed_to_synthesis BOOLEAN NOT NULL DEFAULT FALSE,

    -- Result type (orchestration layer programmatic; pedagogical layer deferred to M3)
    orchestration_event VARCHAR(40) NOT NULL,
    pedagogical_outcomes JSONB,

    -- Synthesis
    final_reply_chars INTEGER NOT NULL DEFAULT 0,

    -- Latency
    latency_total_ms REAL,
    latency_breakdown JSONB,

    -- Cost (ADR-0088 D3: live carry-on-event vs SUM(api_costs) authoritative)
    cost_live_usd DECIMAL(10, 6) NOT NULL DEFAULT 0,
    cost_authoritative_usd DECIMAL(10, 6) NOT NULL DEFAULT 0,
    cost_reconciled BOOLEAN NOT NULL DEFAULT FALSE,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,

    -- Fallback / error path
    fallback_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    error_type VARCHAR(80),
    error_class VARCHAR(40)
);

CREATE INDEX IF NOT EXISTS idx_route_traces_session_id ON route_traces(session_id);
CREATE INDEX IF NOT EXISTS idx_route_traces_created_at ON route_traces(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_route_traces_task_type ON route_traces(task_type);
CREATE INDEX IF NOT EXISTS idx_route_traces_orchestration_event
    ON route_traces(orchestration_event);
