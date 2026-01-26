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
    purpose VARCHAR(50)  -- 'user_request', 'second_brain', 'entity_extraction'
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
