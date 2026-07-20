-- ===========================================================================
-- Migration: 0020 — Session-scoped model selections (ADR-0121 §4 / FRE-917)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0020_session_model_selections.sql
--
-- Fresh installs receive this table via docker/postgres/init.sql, which only
-- runs on an empty Postgres volume.
--
-- The server-authoritative selection store replaces execution-profile "Path" as
-- the source of truth for which model a role runs (ADR-0079's invariants
-- inherited verbatim; ADR-0121 §4). One row per (session_id, role) names a
-- catalog deployment key; a missing row means "resolve through the role's
-- configured binding default".
--
-- BEHAVIOUR-PRESERVING BACKFILL (AC-7): every existing session gets an EXPLICIT
-- 'primary' selection row equal to the model its stored execution_profile
-- resolved to before this change — NOT left implicit — so a later change to the
-- primary binding default never silently moves a pre-existing session:
--     execution_profile = 'cloud' -> claude_sonnet          (config/profiles/cloud.yaml primary_model)
--     execution_profile = 'local' (or anything else) -> qwen3.6-35b-thinking  (config/profiles/local.yaml)
-- These are the T1 catalog deployment keys (config/models.yaml) and the primary
-- role's binding default (config/model_roles.yaml). ON CONFLICT DO NOTHING keeps
-- the migration idempotent and never overwrites a real user selection.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS session_model_selections (
    session_id     UUID NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role           TEXT NOT NULL,
    deployment_key TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id, role)
);

INSERT INTO session_model_selections (session_id, role, deployment_key)
SELECT
    session_id,
    'primary',
    CASE execution_profile
        WHEN 'cloud' THEN 'claude_sonnet'
        ELSE 'qwen3.6-35b-thinking'
    END
FROM sessions
ON CONFLICT (session_id, role) DO NOTHING;

COMMIT;
