-- Migration 0031: route_traces pause instrumentation (ADR-0142, FRE-1391)
--
-- ADR-0142 names two observations RouteTraceRow does not carry, both required before
-- the ADR's own acceptance criteria are checkable: the post-grant tool-iteration
-- ceiling the loop actually used (AC-1), and one entry per constraint pause the turn
-- raised (AC-2). Both are nullable / default-empty: existing rows and rows from a
-- turn whose tool loop never ran carry NULL / an empty JSONB array respectively.
--
-- No Alembic (project policy): schema lives in init.sql + ordered migrations.

ALTER TABLE route_traces
    ADD COLUMN IF NOT EXISTS effective_tool_iteration_ceiling INTEGER,
    ADD COLUMN IF NOT EXISTS constraint_resolutions JSONB;
