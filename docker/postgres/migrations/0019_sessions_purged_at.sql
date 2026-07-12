-- ===========================================================================
-- Migration: 0019 — sessions.purged_at retention column (FRE-860 / ADR-0098 D4/D6)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0019_sessions_purged_at.sql
--
-- Fresh installs receive this column via docker/postgres/init.sql, which only
-- runs on an empty Postgres volume.
--
-- Background: the sessions table (1225 rows) has no retention, TTL, or expiry
-- — nothing ages out session history. This adds a soft-prune tombstone:
-- purged_at is set (and messages cleared to '[]') by the scheduled retention
-- sweep (SessionRepository.prune_expired) once a session has been inactive
-- past the retention window. A hard DELETE was rejected because
-- artifacts.session_id and session_events.session_id both FK to
-- sessions(session_id) with NO ACTION on delete.
-- ===========================================================================

BEGIN;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ NULL;

-- Partial index: the retention sweep only ever scans not-yet-purged rows.
CREATE INDEX IF NOT EXISTS idx_sessions_retention_scan
    ON sessions(last_active_at) WHERE purged_at IS NULL;

COMMIT;
