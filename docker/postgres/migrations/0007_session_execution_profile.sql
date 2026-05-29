-- ===========================================================================
-- Migration: 0007 — Server-authoritative session execution profile (ADR-0079 / FRE-416)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_URL -f docker/postgres/migrations/0007_session_execution_profile.sql
--
-- Fresh installs receive this column via docker/postgres/init.sql, which
-- only runs on an empty Postgres volume.
--
-- The execution profile ("local" → Qwen on the Mac SLM; "cloud" → Claude
-- Sonnet) becomes a server-owned, per-session value instead of client-only
-- state. NOT NULL DEFAULT 'local' is an explicit stored value — never a silent
-- request-time fallback. Backfills existing rows to 'local' (the prior implicit
-- default). See ADR-0079.
-- ===========================================================================

BEGIN;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS execution_profile VARCHAR(50) NOT NULL DEFAULT 'local';

COMMIT;
