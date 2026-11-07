-- ===========================================================================
-- Migration: 0039 — the dev-agent job record and its fenced-action log
--            (ADR-0159 D9.1-D9.5 / FRE-1684)
--
-- dev_jobs         One row per job. The gateway's job supervisor owns it. A supervisor takes a job
--                  by writing a lease: an owner, an expiry and a lease number (lease_epoch) that
--                  grows by one on each take. Every state change requires the current lease
--                  number, so a supervisor that lost its lease cannot change the job.
-- dev_jobs_one_active
--                  A partial unique index: at most one job in queued, running, checking or
--                  delivering (D9.3, "one job at a time").
-- dev_job_actions  The fenced-action log. One row per external action, written in the same
--                  transaction as the lease renewal that allowed it. That transaction holds the
--                  job row's lock until the action returns, so recovery cannot take the job while an
--                  action runs. A refused action writes no row.
--
-- The app role seshat_app gets its grants from the ALTER DEFAULT PRIVILEGES FOR ROLE agent of
-- init.sql / 0015, because `agent` creates these tables. The broker's role seshat_dev_broker gets
-- no grant (ADR-0159 D4.4).
--
-- ADMIN CREDENTIAL: run this migration as the `agent` SUPERUSER, through AGENT_DATABASE_ADMIN_URL,
-- not the app's AGENT_DATABASE_URL (seshat_app cannot run DDL -- FRE-808):
--   psql "$(echo $AGENT_DATABASE_ADMIN_URL | sed 's|postgresql+asyncpg|postgresql|')" \
--     -f docker/postgres/migrations/0039_dev_jobs.sql
--
-- Idempotent. Fresh installs receive this via docker/postgres/init.sql (empty volume only); this
-- file brings existing prod/dev/test/eval DBs current.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS dev_jobs (
    id                  UUID PRIMARY KEY,
    user_id             UUID NOT NULL,
    session_id          UUID NOT NULL,
    trace_id            UUID NOT NULL,
    approval_request_id TEXT NOT NULL UNIQUE,
    mode                TEXT NOT NULL CHECK (mode IN ('build', 'fix')),
    task_text           TEXT NOT NULL,
    source_artifact_id  UUID,
    repo                TEXT,
    base_commit         TEXT,
    result_commit       TEXT,
    result_artifact_id  UUID,
    model_id            TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN (
        'queued', 'running', 'checking', 'delivering', 'succeeded', 'failed', 'failed_check',
        'cap_reached', 'cost_unknown', 'timed_out', 'cancelled'
    )),
    spend_usd           NUMERIC(12, 6),
    check_result        JSONB,
    lease_owner         TEXT,
    lease_epoch         BIGINT NOT NULL DEFAULT 0,
    lease_expires_at    TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL,
    started_at          TIMESTAMPTZ,
    ended_at            TIMESTAMPTZ,
    failure_reason      TEXT,
    CHECK (mode <> 'fix' OR source_artifact_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS dev_jobs_one_active ON dev_jobs ((true))
    WHERE state IN ('queued', 'running', 'checking', 'delivering');

CREATE TABLE IF NOT EXISTS dev_job_actions (
    id          BIGSERIAL PRIMARY KEY,
    job_id      UUID NOT NULL REFERENCES dev_jobs (id),
    lease_epoch BIGINT NOT NULL,
    lease_owner TEXT NOT NULL,
    action      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    -- Set when the action returns, in the same transaction: 'done', or 'error' if it raised.
    outcome     TEXT CHECK (outcome IN ('done', 'error')),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dev_job_actions_job ON dev_job_actions (job_id, id);

COMMIT;
