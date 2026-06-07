-- Migration 0010: Route-trace ledger per-topology key (FRE-513 / ADR-0088 seam)
--
-- The ADR-0088 emission seam writes per-topology rows keyed by (trace_id, task_id).
-- The FRE-452 base table (migration 0009) keyed idempotency on UNIQUE(trace_id) with
-- task_id reserved as a forward slot. This migration promotes that slot to the live key:
-- UNIQUE NULLS NOT DISTINCT (trace_id, task_id) (Postgres 15+; VPS runs PG 17).
--
-- NULLS NOT DISTINCT keeps the turn-level write (task_id IS NULL) de-duplicating per turn
-- (two (trace_id, NULL) rows still conflict), while future per-topology rows de-duplicate
-- per (trace_id, task_id). All existing rows have task_id NULL, so each (trace_id, NULL)
-- stays unique under the new key — the migration is mechanically safe on existing data.
--
-- Idempotent: guarded by IF [NOT] EXISTS / catalog checks so a re-run is a no-op.
-- No Alembic (project policy): schema lives in init.sql + ordered migrations.

DO $$
BEGIN
    -- Preflight (codex Q3): the new constraint cannot be added if duplicate keys already
    -- exist. The key is the PAIR (trace_id, task_id) under NULLS NOT DISTINCT, so check for
    -- duplicate pairs — GROUP BY groups NULL task_ids together, matching NULLS NOT DISTINCT.
    -- In production all existing rows are turn-level (task_id NULL) and trace_id was UNIQUE,
    -- so this is a no-op; it only fires if a manual backfill bypassed 0009's UNIQUE(trace_id).
    -- (Checking trace_id alone would be wrong: legitimate per-topology rows share a trace_id
    -- with distinct task_ids.)
    IF EXISTS (
        SELECT 1 FROM route_traces GROUP BY trace_id, task_id HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'route_traces has duplicate (trace_id, task_id) rows; resolve before applying 0010';
    END IF;
END
$$;

-- Drop the turn-level UNIQUE(trace_id) (auto-named on inline UNIQUE in 0009/init.sql).
ALTER TABLE route_traces DROP CONSTRAINT IF EXISTS route_traces_trace_id_key;

-- Add the per-topology key. NULLS NOT DISTINCT is required so the turn-level NULL task_id
-- rows still collapse to one row per trace_id under ON CONFLICT. Guarded so a re-run is a
-- no-op (ADD CONSTRAINT has no IF NOT EXISTS form).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_route_traces_trace_task'
    ) THEN
        ALTER TABLE route_traces
            ADD CONSTRAINT uq_route_traces_trace_task
            UNIQUE NULLS NOT DISTINCT (trace_id, task_id);
    END IF;
END
$$;
