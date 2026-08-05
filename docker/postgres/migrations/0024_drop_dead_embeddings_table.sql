-- ===========================================================================
-- Migration: 0024 — drop dead `embeddings` table (FRE-597)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0024_drop_dead_embeddings_table.sql
--
-- Fresh installs no longer create this table at all — removed from
-- docker/postgres/init.sql by this same ticket.
--
-- WHY. `embeddings` was provisioned speculatively ("for future semantic
-- search", per its original init.sql comment) with a pgvector HNSW index,
-- but no application code has ever read or written it — semantic search was
-- built against Neo4j instead (personal_agent.memory.embeddings). FRE-597
-- audit confirmed zero references anywhere in src/ (no SQLAlchemy model, no
-- raw-SQL INSERT/SELECT, no repository). Dropping it is safe because it is
-- guaranteed empty in every environment: nothing has ever written a row.
--
-- The `vector` extension itself is NOT dropped — `artifacts.embedding`
-- (docker/postgres/init.sql, artifacts table) depends on it and stays live.
-- ===========================================================================

BEGIN;

DROP TABLE IF EXISTS embeddings;

COMMIT;
