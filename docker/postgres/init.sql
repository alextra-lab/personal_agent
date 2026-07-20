-- Enable pgvector extension for embedding storage
CREATE EXTENSION IF NOT EXISTS vector;

-- Sessions table
-- primary_model_at_creation + model_config_path: row-level model attribution
-- per ADR-0074 (FRE-376). NULL-able for historical rows; populated on every
-- new session by the service layer.
CREATE TABLE IF NOT EXISTS sessions (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
    channel VARCHAR(50),
    metadata JSONB DEFAULT '{}',
    messages JSONB DEFAULT '[]',
    -- Session owner (FRE-591). Declared NOT NULL + FK in SessionModel and
    -- inserted by SessionRepository.create; the FK is added after the users
    -- table below (sessions is created first). Mirrors
    -- docker/postgres/migrations/0011_sessions_user_id.sql.
    user_id UUID NOT NULL,
    primary_model_at_creation VARCHAR(120),
    model_config_path VARCHAR(255),
    -- Server-authoritative execution profile (ADR-0079 / FRE-416). Explicit
    -- stored value; never a silent request-time fallback.
    execution_profile VARCHAR(50) NOT NULL DEFAULT 'local',
    -- Retention soft-prune tombstone (FRE-860 / ADR-0098 D4/D6). NULL = not
    -- yet purged. Set (and messages cleared to '[]') by the scheduled
    -- retention sweep once a session is inactive past the retention window;
    -- cleared back to NULL if the session is resumed (SessionRepository
    -- append_message / update). Mirrors
    -- docker/postgres/migrations/0019_sessions_purged_at.sql.
    purged_at TIMESTAMPTZ NULL
);

CREATE INDEX idx_sessions_last_active ON sessions(last_active_at DESC);
-- ix_ name matches the prod/SQLAlchemy index so migration 0011 is a no-op there.
CREATE INDEX IF NOT EXISTS ix_sessions_user_id ON sessions(user_id);
-- Retention sweep scan index (FRE-860): only not-yet-purged rows.
CREATE INDEX IF NOT EXISTS idx_sessions_retention_scan
    ON sessions(last_active_at) WHERE purged_at IS NULL;

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
-- trace_id / session_id: identity tuple required by ADR-0074 (FRE-376). The
-- application-layer raise in CostTracker.record_api_call enforces presence on
-- every new write. session_id is NULL-able in Phase 1 and flips to NOT NULL
-- in a later phase once the local-LLM path also threads identity.
CREATE TABLE IF NOT EXISTS api_costs (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provider VARCHAR(50) NOT NULL,  -- 'anthropic', 'openai', etc.
    model VARCHAR(100) NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd DECIMAL(10, 6) NOT NULL DEFAULT 0,
    cache_read_input_tokens INTEGER,         -- FRE-437: Anthropic cache-read tier (NULL = n/a)
    cache_creation_input_tokens INTEGER,     -- FRE-437: Anthropic cache-creation tier (NULL = n/a)
    trace_id UUID NOT NULL,
    session_id UUID,
    purpose VARCHAR(50),  -- 'user_request', 'second_brain', 'entity_extraction'
    latency_ms INTEGER
);

CREATE INDEX idx_api_costs_timestamp ON api_costs(timestamp DESC);
CREATE INDEX idx_api_costs_provider ON api_costs(provider);
CREATE INDEX idx_api_costs_trace_id ON api_costs(trace_id);
CREATE INDEX idx_api_costs_session_id ON api_costs(session_id);

-- Route-trace ledger (FRE-452 / ADR-0088 D6 sink 1): one row per turn capturing what the
-- gateway decided (deterministic-shell label) vs what the harness actually did
-- (orchestration event). Bus-independent durable write (ADR-0088 D8); joins to api_costs on
-- trace_id for authoritative cost (ADR-0088 D3). UNIQUE NULLS NOT DISTINCT (trace_id,
-- task_id) is the ADR-0088 seam key backing ON CONFLICT DO NOTHING: the turn-level write
-- (task_id NULL) de-duplicates per turn, future per-topology rows per (trace_id, task_id).
CREATE TABLE IF NOT EXISTS route_traces (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL,
    session_id UUID,
    task_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schema_version SMALLINT NOT NULL DEFAULT 1,

    -- Stimulus (PII-gated)
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

    -- Result type
    orchestration_event VARCHAR(40) NOT NULL,
    pedagogical_outcomes JSONB,

    -- Synthesis
    final_reply_chars INTEGER NOT NULL DEFAULT 0,

    -- Latency
    latency_total_ms REAL,
    latency_breakdown JSONB,

    -- Cost (ADR-0088 D3)
    cost_live_usd DECIMAL(10, 6) NOT NULL DEFAULT 0,
    cost_authoritative_usd DECIMAL(10, 6) NOT NULL DEFAULT 0,
    cost_reconciled BOOLEAN NOT NULL DEFAULT FALSE,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,

    -- Fallback / error path
    fallback_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    error_type VARCHAR(80),
    error_class VARCHAR(40),

    -- ADR-0088 seam key (FRE-513): per-topology idempotency. NULLS NOT DISTINCT so the
    -- turn-level write (task_id NULL) still collapses to one row per trace_id.
    CONSTRAINT uq_route_traces_trace_task UNIQUE NULLS NOT DISTINCT (trace_id, task_id)
);

CREATE INDEX idx_route_traces_session_id ON route_traces(session_id);
CREATE INDEX idx_route_traces_created_at ON route_traces(created_at DESC);
CREATE INDEX idx_route_traces_task_type ON route_traces(task_type);
CREATE INDEX idx_route_traces_orchestration_event ON route_traces(orchestration_event);

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
    trace_id UUID,
    session_id UUID,                    -- ADR-0074 §I3/FRE-693: turn joinability
    task_id UUID                        -- sub-agent id (NULL at turn level, per route_traces)
);
-- Reaper hot-path: only scan active reservations past their TTL.
CREATE INDEX IF NOT EXISTS idx_budget_reservations_reaper
    ON budget_reservations(expires_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_budget_reservations_trace
    ON budget_reservations(trace_id);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_session
    ON budget_reservations(session_id);

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

-- ===========================================================================
-- User identity (ADR-0064 / FRE-213)
--
-- Populated on first authenticated request via CF Access.  user_id is the
-- durable FK used for artifact/session ownership.  Mirrored by the
-- SQLAlchemy UserModel; create_all and this file must stay in sync.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS users (
    user_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT NOT NULL UNIQUE,
    display_name TEXT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- sessions.user_id FK (FRE-591) — declared here, not inline in the sessions
-- block above, because sessions is created before users. Mirrors prod and
-- docker/postgres/migrations/0011_sessions_user_id.sql.
ALTER TABLE sessions
    ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(user_id);

-- ===========================================================================
-- Artifact substrate (ADR-0069 / FRE-227)
--
-- Metadata canon for every byte-string parked in R2: notes, artifacts,
-- uploads, captures. Bytes live in R2 keyed by r2_key; this table is the
-- source of truth for ownership, type, and (for notes) the pgvector
-- embedding used by notes_search. Mirrored in
-- docker/postgres/migrations/0003_artifacts_schema.sql for existing DBs.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS artifacts (
    id              UUID PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(user_id),
    session_id      UUID NULL REFERENCES sessions(session_id),
    type            TEXT NOT NULL
                        CHECK (type IN ('note', 'artifact', 'upload', 'capture')),
    slug            TEXT NULL,
    title           TEXT NULL,
    summary         TEXT NULL,
    content_type    TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL CHECK (size_bytes >= 0),
    r2_key          TEXT NOT NULL UNIQUE,
    tags            TEXT[] NOT NULL DEFAULT '{}',
    embedding       vector(1024) NULL,
    created_by      TEXT NOT NULL CHECK (created_by IN ('agent', 'user')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    upload_pending  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_artifacts_owner_type_created
    ON artifacts (user_id, type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifacts_embedding
    ON artifacts USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_artifacts_tags
    ON artifacts USING gin (tags);

CREATE INDEX IF NOT EXISTS idx_artifacts_session
    ON artifacts (session_id)
    WHERE session_id IS NOT NULL;


-- ===========================================================================
-- WebSocket session event buffer (ADR-0075 / FRE-388)
--
-- Durable, Postgres-sequenced buffer for AG-UI transport events.
-- On reconnect the client sends last_seq; server replays seq > last_seq.
-- A background cleanup task deletes rows older than 24 hours.
-- ===========================================================================

CREATE SEQUENCE IF NOT EXISTS session_events_seq;

CREATE TABLE IF NOT EXISTS session_events (
    id           BIGSERIAL PRIMARY KEY,
    session_id   UUID NOT NULL REFERENCES sessions(session_id),
    seq          INTEGER NOT NULL DEFAULT nextval('session_events_seq'),
    event_type   TEXT NOT NULL,
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_session_events_replay
    ON session_events (session_id, seq);

-- User constraint governance preferences (ADR-0076 / FRE-389).
CREATE TABLE IF NOT EXISTS user_constraint_preferences (
    user_id           UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    constraint_name   TEXT NOT NULL,
    preferred_action  TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_session_id UUID,
    PRIMARY KEY (user_id, constraint_name)
);

-- Session-scoped model selections (ADR-0121 §4 / FRE-917).
-- Server-authoritative store for which model a role runs in a session — the
-- replacement for execution-profile "Path" as the source of truth (ADR-0079's
-- invariants inherited). One row per (session_id, role) naming a catalog
-- deployment key; a missing row means "resolve through the role's binding
-- default" (the guardrail's fail-closed fallback, ADR-0121 §6). In T2 only
-- role='primary' is populated. Ownership flows through the session, not a
-- column here. Mirrored in migration 0020_session_model_selections.sql.
CREATE TABLE IF NOT EXISTS session_model_selections (
    session_id     UUID NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role           TEXT NOT NULL,
    deployment_key TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id, role)
);

-- ===========================================================================
-- sysgraph schema (ADR-0105 D2/D3 / FRE-714)
--
-- Isolated System-graph store for the self-improvement pipeline (proposals,
-- stats, tickets, outcomes), physically separate from the Neo4j user-memory
-- KG. Isolation proven at the DB permission layer (AC-2): a dedicated
-- sysgraph_role owns this schema exclusively; recall_role stands in for the
-- recall/user-facing connection and is granted nothing here. No pgvector
-- column yet — gated on the FRE-720 separation-probe measurement (D10).
-- Mirrored in docker/postgres/migrations/0014_sysgraph_schema.sql for
-- existing DBs.
-- ===========================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sysgraph_role') THEN
        CREATE ROLE sysgraph_role LOGIN PASSWORD 'sysgraph_dev_password';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'recall_role') THEN
        CREATE ROLE recall_role LOGIN PASSWORD 'recall_dev_password';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE personal_agent TO sysgraph_role, recall_role;

CREATE SCHEMA IF NOT EXISTS sysgraph AUTHORIZATION sysgraph_role;

REVOKE ALL ON SCHEMA sysgraph FROM PUBLIC;
REVOKE ALL ON SCHEMA sysgraph FROM recall_role;
REVOKE ALL ON SCHEMA public FROM sysgraph_role;

SET ROLE sysgraph_role;

CREATE TABLE IF NOT EXISTS sysgraph.proposal (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source      TEXT NOT NULL CHECK (source IN ('statistical_detector', 'reflection')),
    category    TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    what        TEXT NOT NULL,
    why         TEXT,
    how         TEXT,
    seen_count  INTEGER NOT NULL DEFAULT 1,
    scope       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_proposal_source_category ON sysgraph.proposal(source, category);
CREATE INDEX IF NOT EXISTS idx_sysgraph_proposal_source_category_scope
    ON sysgraph.proposal(source, category, scope);

CREATE TABLE IF NOT EXISTS sysgraph.stat (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    value       DOUBLE PRECISION,
    metadata    JSONB NOT NULL DEFAULT '{}',
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_stat_name ON sysgraph.stat(name, observed_at DESC);

CREATE TABLE IF NOT EXISTS sysgraph.ticket (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    linear_issue_id TEXT NOT NULL UNIQUE,
    title           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sysgraph.outcome (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    result      TEXT NOT NULL CHECK (result IN ('shipped', 'owner-rejected', 'canceled-as-noise', 'deferred')),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Supports get_signal()'s windowed (observed_at) filter (ADR-0105 D7 / FRE-717).
CREATE INDEX IF NOT EXISTS idx_sysgraph_outcome_observed_at ON sysgraph.outcome(observed_at);

CREATE TABLE IF NOT EXISTS sysgraph.derives_from (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id UUID NOT NULL REFERENCES sysgraph.proposal(id) ON DELETE CASCADE,
    stat_id     UUID NOT NULL REFERENCES sysgraph.stat(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (proposal_id, stat_id)
);

CREATE TABLE IF NOT EXISTS sysgraph.promoted_to (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id UUID NOT NULL REFERENCES sysgraph.proposal(id) ON DELETE CASCADE,
    ticket_id   UUID NOT NULL REFERENCES sysgraph.ticket(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (proposal_id, ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_promoted_to_ticket ON sysgraph.promoted_to(ticket_id);

-- ticket_id UNIQUE: a ticket has exactly one terminal outcome (ADR-0105 D7 /
-- FRE-717) — subsumes the narrower (ticket_id, outcome_id) pairing FRE-714
-- originally declared.
CREATE TABLE IF NOT EXISTS sysgraph.produced (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id  UUID NOT NULL UNIQUE REFERENCES sysgraph.ticket(id) ON DELETE CASCADE,
    outcome_id UUID NOT NULL REFERENCES sysgraph.outcome(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Realized-value suppression cooldown (ADR-0105 D7 / FRE-717). The value itself
-- (v) is computed on read from sysgraph.outcome rows, never persisted here —
-- see docker/postgres/migrations/0017_sysgraph_signal.sql.
CREATE TABLE IF NOT EXISTS sysgraph.signal (
    source           TEXT NOT NULL CHECK (source IN ('statistical_detector', 'reflection')),
    category         TEXT NOT NULL,
    suppressed_until TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, category)
);

-- CORRELATES_WITH / INFLUENCE: polymorphic Proposal<->Proposal or Proposal<->Stat
-- edges. No DB-level FK across the two possible node tables (heterogeneous
-- target type) — validated at the sysgraph repository layer, not the schema.
CREATE TABLE IF NOT EXISTS sysgraph.correlates_with (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_node_type TEXT NOT NULL CHECK (from_node_type IN ('proposal', 'stat')),
    from_node_id   UUID NOT NULL,
    to_node_type   TEXT NOT NULL CHECK (to_node_type IN ('proposal', 'stat')),
    to_node_id     UUID NOT NULL,
    weight         DOUBLE PRECISION,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_correlates_from ON sysgraph.correlates_with(from_node_type, from_node_id);

CREATE TABLE IF NOT EXISTS sysgraph.influence (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_node_type TEXT NOT NULL CHECK (from_node_type IN ('proposal', 'stat')),
    from_node_id   UUID NOT NULL,
    to_node_type   TEXT NOT NULL CHECK (to_node_type IN ('proposal', 'stat')),
    to_node_id     UUID NOT NULL,
    weight         DOUBLE PRECISION,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_influence_from ON sysgraph.influence(from_node_type, from_node_id);

RESET ROLE;

-- ===========================================================================
-- App role — the live AGENT_DATABASE_URL connection (ADR-0105 T1 / FRE-808)
--
-- Today the app connects as the `agent` bootstrap SUPERUSER, which bypasses
-- every grant — so the sysgraph isolation above would not stop a stray
-- sysgraph query from the app's connection. `seshat_app` is a non-superuser
-- login role scoped to exactly the public-schema DML the app needs; it is
-- granted NOTHING on schema sysgraph, so `SELECT … FROM sysgraph.*` from the
-- app connection raises `permission denied` (the real AC-2 proof, not the
-- recall_role stand-in). Migrations / admin DDL continue to run as the `agent`
-- superuser via AGENT_DATABASE_ADMIN_URL.
--
-- Dev password shipped here; production sets a real SESHAT_APP_PASSWORD secret
-- via `ALTER ROLE seshat_app PASSWORD …` at deploy (see 0015 migration header).
-- ===========================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'seshat_app') THEN
        CREATE ROLE seshat_app LOGIN PASSWORD 'seshat_app_dev_password';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE personal_agent TO seshat_app;
GRANT USAGE ON SCHEMA public TO seshat_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO seshat_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO seshat_app;

-- Future public objects created by the bootstrap superuser (fresh-install
-- init.sql tables below this line, and every later migration run as `agent`)
-- auto-grant to the app role, so new tables never need a manual GRANT.
ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO seshat_app;
ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO seshat_app;

-- Intentionally NO grant on schema sysgraph: seshat_app is not a superuser and
-- has no USAGE there, so the app connection is denied — physical isolation now
-- holds against the actual deployed connection, not just the recall_role proxy.
