-- Migration 0032: route_traces planner decision, inputs, delay and first token (ADR-0154 D6, FRE-1512)
--
-- ADR-0154's acceptance criteria read these fields. Two of them must exist before the routing
-- change (FRE-1515) so a pre-change baseline of SINGLE turns exists: first_token_ms and
-- conversation_history_chars. The planner_* columns stay NULL until the planner writes them.
--
-- Run as the `agent` superuser through AGENT_DATABASE_ADMIN_URL (the restricted app role
-- cannot run DDL -- FRE-808). Idempotent: ADD COLUMN IF NOT EXISTS, so a re-run is a no-op.
-- No Alembic (project policy): schema lives in init.sql + ordered migrations.

ALTER TABLE route_traces
    ADD COLUMN IF NOT EXISTS planner_decision VARCHAR(20),
    ADD COLUMN IF NOT EXISTS planner_failure_reason VARCHAR(40),
    ADD COLUMN IF NOT EXISTS planner_deployment VARCHAR(120),
    ADD COLUMN IF NOT EXISTS planner_mode VARCHAR(40),
    ADD COLUMN IF NOT EXISTS planner_reasoning_chars INTEGER,
    ADD COLUMN IF NOT EXISTS planner_duration_ms REAL,
    ADD COLUMN IF NOT EXISTS planner_prompt_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS planner_completion_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS planner_input_chars JSONB,
    ADD COLUMN IF NOT EXISTS planner_gate_reason VARCHAR(40),
    ADD COLUMN IF NOT EXISTS conversation_history_chars INTEGER,
    ADD COLUMN IF NOT EXISTS expansion_budget INTEGER,
    ADD COLUMN IF NOT EXISTS synthesis_appended BOOLEAN,
    ADD COLUMN IF NOT EXISTS first_token_ms REAL;

-- RouteTraceRow's schema_version default moves 2 -> 3 with this change (same alignment as
-- migration 0031: the DB default follows the DTO default).
ALTER TABLE route_traces ALTER COLUMN schema_version SET DEFAULT 3;
