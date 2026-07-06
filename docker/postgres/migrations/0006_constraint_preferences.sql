-- ===========================================================================
-- Migration: 0006 — User constraint governance preferences (ADR-0076 / FRE-389)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0006_constraint_preferences.sql
--
-- Fresh installs receive this table via docker/postgres/init.sql, which
-- only runs on an empty Postgres volume.
--
-- Stores standing per-user preferences for harness constraint pauses. No row
-- for a (user, constraint) pair means `always_pause` (ask every time).
-- `preferred_action` stores a stable action_id (e.g. 'continue_10') or the
-- literal 'always_pause' — never a display label, so button renames never
-- invalidate stored preferences. `source_session_id` records where the
-- preference was first set, for audit.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS user_constraint_preferences (
    user_id           UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    constraint_name   TEXT NOT NULL,
    preferred_action  TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_session_id UUID,
    PRIMARY KEY (user_id, constraint_name)
);

COMMIT;
