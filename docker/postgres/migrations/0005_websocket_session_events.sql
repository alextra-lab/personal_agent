-- ===========================================================================
-- Migration: 0005 — WebSocket session event buffer (ADR-0075 / FRE-388)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_URL -f docker/postgres/migrations/0005_websocket_session_events.sql
--
-- Fresh installs receive this table via docker/postgres/init.sql, which
-- only runs on an empty Postgres volume.
--
-- The session_events table provides a durable, Postgres-sequenced buffer
-- for AG-UI transport events. On WebSocket reconnect the client sends its
-- last received seq; the server replays all events with seq > last_seq from
-- this table, then switches to the live asyncio.Queue drain.
--
-- A global Postgres sequence (session_events_seq) generates seq values.
-- The (session_id, seq) UNIQUE constraint guarantees per-session ordering.
-- A background cleanup task deletes rows older than 24 hours.
-- ===========================================================================

BEGIN;

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

COMMIT;
