-- Migration 0036: budget_reservations remembers the counters it incremented (FRE-1690, FRE-1681)
--
-- reserve() now stores the id of every budget_counters row it incremented. commit(), refund()
-- and the reaper adjust exactly those rows. Before this, they re-derived the rows from the window
-- that is current at settle time, so a call that crossed 00:00 UTC (daily) or Monday 00:00 UTC
-- (weekly) left the old window holding the estimate and moved the new window's counter.
--
-- NULL marks a row written before this migration. The gate falls back to the old derivation for
-- such a row. An uncapped reservation stores an empty array: it incremented no counter.
--
-- Run as the `agent` superuser through AGENT_DATABASE_ADMIN_URL (the restricted app role
-- cannot run DDL -- FRE-808). Run it BEFORE the seshat-gateway rebuild: reserve()'s INSERT
-- names the column. Idempotent: ADD COLUMN IF NOT EXISTS, so a re-run is a no-op.
-- Do not deploy within 5 minutes of 00:00 UTC: legacy active rows (NULL) live up to 180 s and
-- still settle against the current window.
-- No Alembic (project policy): schema lives in init.sql + ordered migrations.

ALTER TABLE budget_reservations
    ADD COLUMN IF NOT EXISTS counter_ids BIGINT[];
