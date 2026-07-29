-- ===========================================================================
-- Migration: 0023 — per-session transport event sequence (FRE-1040 / ADR-0075)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0023_session_events_per_session_seq.sql
--
-- Fresh installs receive this column via docker/postgres/init.sql, which only
-- runs on an empty Postgres volume.
--
-- WHY. session_events.seq was drawn from ONE GLOBAL Postgres sequence
-- (session_events_seq, migration 0005) shared by every session, while the PWA
-- client dispatches only a *contiguous* run of seq values for the session it is
-- attached to. With two conversations alive, the second consumes numbers inside
-- the first's series; the first's client then waits forever for a number that
-- belongs to another socket and will never arrive on its own. The assistant
-- response sits undelivered in the client's pending buffer and only a full
-- session reload recovers it. Per-session numbering makes contiguity mean what
-- the client already assumes.
--
-- WHY A STORED COUNTER RATHER THAN MAX(seq). session_events rows are swept on a
-- 24h TTL. A MAX-derived counter would reset to 0 once a session's rows aged
-- out and re-issue seq values at or below a client's stored watermark, which the
-- client discards as duplicates — a permanent blackout on that conversation.
--
-- THE BACKFILL, AND WHY IT IS GUARDED. Every pre-existing session starts at the
-- current global high-water mark, which is >= every seq ever issued and hence
-- >= every watermark any client has stored. That is what stops an already-open
-- client from swallowing its own new events after this migration. The guard
-- (run only when the column is absent) makes a re-run a genuine no-op: re-running
-- the backfill after the application had begun issuing per-session values would
-- jump every counter forward and manufacture the very hole this fixes.
--
-- session_events.seq KEEPS its legacy DEFAULT nextval('session_events_seq') so
-- that rolling the gateway image back to a build that does not supply seq
-- explicitly still writes. New code always supplies it.
-- ===========================================================================

BEGIN;

DO $$
DECLARE
    high_water BIGINT := 0;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'sessions'
          AND column_name = 'last_event_seq'
    ) THEN
        ALTER TABLE sessions ADD COLUMN last_event_seq INTEGER NOT NULL DEFAULT 0;

        IF to_regclass('public.session_events_seq') IS NOT NULL THEN
            EXECUTE 'SELECT last_value FROM session_events_seq' INTO high_water;
        END IF;

        UPDATE sessions SET last_event_seq = GREATEST(high_water, 0);

        RAISE NOTICE 'FRE-1040: seeded sessions.last_event_seq at high-water %', high_water;
    END IF;
END $$;

COMMIT;
