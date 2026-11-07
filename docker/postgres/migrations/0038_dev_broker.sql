-- ===========================================================================
-- Migration: 0038 — the dev-agent broker's ledger and its restricted role
--            (ADR-0159 D4.4, D5.4-D5.9 / FRE-1683)
--
-- dev_job_tokens   One row per job. The gateway writes it and the broker reads it: the hash of the
--                  job token, the job's model, trace and session, a status and a stop reason. The
--                  broker accepts a token only while it is `active` and before `expires_at`.
-- dev_job_spend    The per-job counter: committed and reserved spend in USD.
-- dev_month_spend  The per-UTC-month counter of the dev_agent role.
-- dev_job_calls    One row per call: the per-job ledger. A row stores its month, so a call reserved
--                  at 23:59 UTC on a month's last day settles against that month.
--
-- seshat_dev_broker is the broker's login role. It reaches only these four tables, the cost-gate
-- counters and reservations, and an insert into api_costs (D4.4). It has no grant on `artifacts`,
-- `users`, `sessions` or any other table, and no ALTER DEFAULT PRIVILEGES: a later table, such as
-- dev_jobs (FRE-1684), gets no grant unless a migration names it. It cannot insert a job token.
--
-- ADMIN CREDENTIAL: run this migration as the `agent` SUPERUSER, through AGENT_DATABASE_ADMIN_URL,
-- not the app's AGENT_DATABASE_URL (seshat_app cannot run DDL -- FRE-808):
--   psql "$(echo $AGENT_DATABASE_ADMIN_URL | sed 's|postgresql+asyncpg|postgresql|')" \
--     -f docker/postgres/migrations/0038_dev_broker.sql
--
-- PRODUCTION PASSWORD: this migration creates the role with NO password, so nothing can log in as
-- it until the deploy sets the real secret. Put the same value in /opt/seshat/.env as
-- DEV_BROKER_DB_PASSWORD:
--   ALTER ROLE seshat_dev_broker PASSWORD '<DEV_BROKER_DB_PASSWORD>';
-- (A known password here would let anything on cloud-sim log in as a role that can write the
-- cost-gate counters, until someone remembers the ALTER.) init.sql keeps a dev password, because
-- only fresh local, test and CI stacks run it.
--
-- Idempotent. Fresh installs receive this via docker/postgres/init.sql (empty volume only); this
-- file brings existing prod/dev/test/eval DBs current.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS dev_job_tokens (
    token_hash  TEXT PRIMARY KEY,
    job_id      UUID NOT NULL UNIQUE,
    model_key   TEXT NOT NULL,
    trace_id    UUID NOT NULL,
    session_id  UUID NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('active', 'stopped', 'revoked')),
    stop_reason TEXT CHECK (stop_reason IN ('cap_reached', 'cost_unknown')),
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dev_job_spend (
    job_id        UUID PRIMARY KEY,
    committed_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
    reserved_usd  NUMERIC(12, 6) NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dev_month_spend (
    month_start   DATE PRIMARY KEY,
    committed_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
    reserved_usd  NUMERIC(12, 6) NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dev_job_calls (
    call_id               UUID PRIMARY KEY,
    job_id                UUID NOT NULL,
    model_key             TEXT NOT NULL,
    month_start           DATE NOT NULL,
    prompt_token_bound    INTEGER NOT NULL,
    max_output_tokens     INTEGER NOT NULL,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    reserved_usd          NUMERIC(12, 6) NOT NULL,
    settled_usd           NUMERIC(12, 6),
    status                TEXT NOT NULL CHECK (status IN (
        'reserved', 'settled', 'cost_unknown', 'timed_out', 'cancelled', 'provider_error',
        'failed', 'gate_denied'
    )),
    created_at            TIMESTAMPTZ NOT NULL,
    settled_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dev_job_calls_job ON dev_job_calls (job_id, created_at);

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'seshat_dev_broker') THEN
        -- No password: login fails until the deploy sets one (see PRODUCTION PASSWORD above).
        CREATE ROLE seshat_dev_broker LOGIN;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE personal_agent TO seshat_dev_broker;
GRANT USAGE ON SCHEMA public TO seshat_dev_broker;
GRANT SELECT ON dev_job_tokens TO seshat_dev_broker;
GRANT UPDATE (status, stop_reason, updated_at) ON dev_job_tokens TO seshat_dev_broker;
GRANT SELECT, INSERT, UPDATE ON dev_job_spend, dev_month_spend, dev_job_calls TO seshat_dev_broker;
GRANT SELECT, INSERT, UPDATE ON budget_counters, budget_reservations TO seshat_dev_broker;
GRANT USAGE ON SEQUENCE budget_counters_id_seq TO seshat_dev_broker;
GRANT INSERT, SELECT (id) ON api_costs TO seshat_dev_broker;
GRANT USAGE ON SEQUENCE api_costs_id_seq TO seshat_dev_broker;

COMMIT;
