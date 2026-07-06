-- ===========================================================================
-- Migration: 0003 — Artifact substrate (ADR-0069 / FRE-227)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0003_artifacts_schema.sql
--
-- Fresh installs receive these tables via docker/postgres/init.sql, which
-- only runs on an empty Postgres volume. This file mirrors the substrate
-- DDL from init.sql so existing prod / dev DBs can be brought current with
-- a single psql invocation.
-- ===========================================================================

BEGIN;

-- The artifacts table is the metadata canon for every byte-string the agent
-- (or a user) parks in R2: notes, artifacts, uploads, captures. Bytes live
-- in R2 keyed by r2_key; this table is the authoritative source for
-- ownership, type, tags, and (for notes) the pgvector embedding used by
-- notes_search.
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
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Primary read pattern: list-by-owner with type filter, newest first.
CREATE INDEX IF NOT EXISTS idx_artifacts_owner_type_created
    ON artifacts (user_id, type, created_at DESC);

-- HNSW approximate-NN search over embedding for notes_search.
-- Parameters mirror docker/postgres/init.sql:90-92 (existing embeddings table).
CREATE INDEX IF NOT EXISTS idx_artifacts_embedding
    ON artifacts USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Tag filter: tags && ARRAY['proj-x'] style.
CREATE INDEX IF NOT EXISTS idx_artifacts_tags
    ON artifacts USING gin (tags);

-- Session-scoped lookups (recent artifacts produced in a given session).
CREATE INDEX IF NOT EXISTS idx_artifacts_session
    ON artifacts (session_id)
    WHERE session_id IS NOT NULL;

COMMIT;
