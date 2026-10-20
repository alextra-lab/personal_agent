-- Migration 0034: sessions.seshat_effort (ADR-0157 D2, FRE-1622)
--
-- The session's Seshat effort level: fast, standard or deep. A per-session choice, held by the
-- server, as the model selection is (ADR-0121 §4). The application writes the level explicitly
-- on every session it creates. The default only backfills the rows that exist before this
-- migration, and it matches config/seshat_effort.yaml's default_level.
--
-- Run as the `agent` superuser through AGENT_DATABASE_ADMIN_URL (the restricted app role
-- cannot run DDL -- FRE-808). Run it BEFORE the seshat-gateway rebuild: SessionModel names the
-- column, so every ORM read of sessions fails until it exists.
-- Idempotent: ADD COLUMN IF NOT EXISTS, and the check is added only when absent.
-- No Alembic (project policy): schema lives in init.sql + ordered migrations.

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS seshat_effort VARCHAR(16) NOT NULL DEFAULT 'standard';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_sessions_seshat_effort'
          AND conrelid = 'sessions'::regclass
    ) THEN
        ALTER TABLE sessions
            ADD CONSTRAINT ck_sessions_seshat_effort
            CHECK (seshat_effort IN ('fast', 'standard', 'deep'));
    END IF;
END $$;
