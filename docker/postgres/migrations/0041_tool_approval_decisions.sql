-- ===========================================================================
-- Migration: 0041 — the durable record of each approval card's decision
--            (ADR-0159 D10.3 / FRE-1687)
--
-- tool_approval_decisions  One row per approval card. The permission layer writes it after the
--                          decision: the request id, the session, the tool call id, the tool, a
--                          hash of the arguments that the card showed, the decision and the time.
--                          A dev_jobs row joins to it on approval_request_id = request_id (AC-1).
--
-- Why a table and not a session_events row: session_events is swept on a 24-hour TTL, and the
-- replay-gap check reads its oldest retained seq. A decision row kept past the sweep would hide a
-- real gap from a reconnecting client. This table has no TTL.
--
-- decision is the decision that took effect: 'approve' (the call ran once), 'approve_session' (the
-- call ran and a session allow rule was added), 'deny', 'timeout' or 'connection_lost'.
--
-- The app role seshat_app gets its grants from the ALTER DEFAULT PRIVILEGES FOR ROLE agent of
-- init.sql / 0015. seshat_dev_broker gets no grant (ADR-0159 D4.4).
--
-- ADMIN CREDENTIAL: run this migration as the `agent` SUPERUSER, through AGENT_DATABASE_ADMIN_URL,
-- not the app's AGENT_DATABASE_URL (seshat_app cannot run DDL -- FRE-808):
--   psql "$(echo $AGENT_DATABASE_ADMIN_URL | sed 's|postgresql+asyncpg|postgresql|')" \
--     -f docker/postgres/migrations/0041_tool_approval_decisions.sql
--
-- Idempotent. Additive: one new table. Fresh installs receive this via docker/postgres/init.sql
-- (empty volume only).
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS tool_approval_decisions (
    request_id   TEXT PRIMARY KEY,
    session_id   UUID NOT NULL,
    tool_call_id TEXT,
    tool         TEXT NOT NULL,
    args_hash    TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN (
        'approve', 'approve_session', 'deny', 'timeout', 'connection_lost'
    )),
    trace_id     TEXT NOT NULL,
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMIT;
