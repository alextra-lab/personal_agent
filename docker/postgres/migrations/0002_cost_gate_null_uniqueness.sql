-- ===========================================================================
-- Migration: 0002 — Fix NULL uniqueness on cost-gate tables (FRE-304 follow-up)
--
-- Postgres treats NULL != NULL in unique constraints by default, which means
-- the constraints from migration 0001 do NOT actually enforce uniqueness on
-- (user_id NULL, time_window, provider NULL, role, window_start) tuples — v1
-- always passes user_id=NULL and provider=NULL, so concurrent INSERTs created
-- duplicate rows. Concurrent reservations then locked different rows and the
-- gate's mutual exclusion broke.
--
-- Fix: drop the bad unique constraints and recreate them WITH NULLS NOT
-- DISTINCT (PG 15+ feature). Postgres 17.x is in use.
--
-- Cleanup is needed because the bug already produced duplicate rows in the
-- DB. We:
--   1. Delete all test_* rows (test pollution; safe to drop).
--   2. Consolidate any duplicate (role, time_window, window_start) groups by
--      keeping the row with the largest running_total and repointing any
--      budget_reservations FK references to that survivor.
--   3. Then drop + recreate the constraints.
-- ===========================================================================

BEGIN;

-- ------------------------------------------------------------------
-- 1. Test pollution cleanup
-- ------------------------------------------------------------------
DELETE FROM budget_reservations
 WHERE role LIKE 'test_%';

DELETE FROM budget_counters
 WHERE role LIKE 'test_%';


-- ------------------------------------------------------------------
-- 2. Consolidate duplicate counter rows (real ones, not test_)
--
-- For each (user_id, time_window, provider, role, window_start) group with
-- more than one row, keep the row with the highest running_total (most
-- conservative — preserves visible spend), repoint any reservations to it,
-- and delete the other rows.
-- ------------------------------------------------------------------
WITH ranked AS (
    SELECT id,
           user_id, time_window, provider, role, window_start,
           running_total,
           ROW_NUMBER() OVER (
               PARTITION BY user_id, time_window, provider, role, window_start
               ORDER BY running_total DESC, id ASC
           ) AS rn
      FROM budget_counters
),
survivors AS (
    SELECT user_id, time_window, provider, role, window_start, id AS keep_id
      FROM ranked
     WHERE rn = 1
),
losers AS (
    SELECT r.id AS drop_id, s.keep_id
      FROM ranked r
      JOIN survivors s
        ON  r.user_id IS NOT DISTINCT FROM s.user_id
        AND r.time_window = s.time_window
        AND r.provider IS NOT DISTINCT FROM s.provider
        AND r.role = s.role
        AND r.window_start = s.window_start
     WHERE r.rn > 1
)
UPDATE budget_reservations br
   SET counter_id = l.keep_id
  FROM losers l
 WHERE br.counter_id = l.drop_id;

WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY user_id, time_window, provider, role, window_start
               ORDER BY running_total DESC, id ASC
           ) AS rn
      FROM budget_counters
)
DELETE FROM budget_counters
 WHERE id IN (SELECT id FROM ranked WHERE rn > 1);


-- ------------------------------------------------------------------
-- 3. Drop + recreate constraints with NULLS NOT DISTINCT
-- ------------------------------------------------------------------
ALTER TABLE budget_policies
    DROP CONSTRAINT IF EXISTS budget_policies_user_id_time_window_provider_role_key;
ALTER TABLE budget_policies
    ADD  CONSTRAINT budget_policies_user_id_time_window_provider_role_key
         UNIQUE NULLS NOT DISTINCT (user_id, time_window, provider, role);

ALTER TABLE budget_counters
    DROP CONSTRAINT IF EXISTS budget_counters_user_id_time_window_provider_role_window_start_key;
ALTER TABLE budget_counters
    ADD  CONSTRAINT budget_counters_user_id_time_window_provider_role_window_start_key
         UNIQUE NULLS NOT DISTINCT (user_id, time_window, provider, role, window_start);

COMMIT;
