-- Migration 0033: sessions.revealed_tools (ADR-0156 A5.4, FRE-1595)
--
-- The skill-provided tools that read_skill revealed in a session. The primary sees such a tool
-- only after the reveal, and the reveal lasts for the session: a new user turn, a pause and
-- resume, and a gateway restart all read this record. It is a column of its own, not a key of
-- sessions.metadata, because a client can replace metadata through the PATCH endpoint.
--
-- Run as the `agent` superuser through AGENT_DATABASE_ADMIN_URL (the restricted app role
-- cannot run DDL -- FRE-808). Run it BEFORE the seshat-gateway rebuild: SessionModel names the
-- column, so every ORM read of sessions fails until it exists.
-- Idempotent: ADD COLUMN IF NOT EXISTS, so a re-run is a no-op. Existing rows get '{}'.
-- No Alembic (project policy): schema lives in init.sql + ordered migrations.

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS revealed_tools TEXT[] NOT NULL DEFAULT '{}';
