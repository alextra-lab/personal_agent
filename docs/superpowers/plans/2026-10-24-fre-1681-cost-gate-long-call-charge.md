# FRE-1681 + FRE-1690 — Cost gate: charge a long call, settle against the reserved window

Tickets: FRE-1681 (primary), FRE-1690 (folded in, owner approval 2026-10-24). One PR closes both.
Diff class: escalated (cost + schema). The owner waived `/code-review ultra` on FRE-1681 (2026-10-24, "Waived").
Backing design: ADR-0065 D1 (reserve / commit / refund / reaper). ADR-0159 D5 works around both defects.

## Root causes

1. `reap_stale` marks an `active` reservation `expired` after `RESERVATION_TTL_SECONDS` (180) and
   subtracts its estimate from the counters. A later `commit()` sees `expired` and raises
   `ValueError`. The caller logs `litellm_commit_failed`. The real cost never reaches
   `budget_counters`.
2. `commit()`, `refund()` and `reap_stale()` re-derive the counter ids from the window that is
   current at settle time (`_counter_ids_for_role`). The reservation incremented the counters of
   the window current at reserve time. A call that crosses UTC midnight or Monday 00:00 corrupts
   both windows.

## Design

- **Schema.** `budget_reservations.counter_ids BIGINT[]`, nullable. It lists every counter that
  `reserve()` incremented. `NULL` marks a row written before this change.
  - `docker/postgres/init.sql`: add the column to `CREATE TABLE`.
  - `docker/postgres/migrations/0036_budget_reservation_counter_ids.sql`:
    `ADD COLUMN IF NOT EXISTS counter_ids BIGINT[]`. No backfill: an `active` legacy row is short
    lived and falls back to the old derivation.
  - `service/models.py` `BudgetReservationModel`: add the column.
- **reserve().** Store `[counter_id for each locked counter]` (sorted). The uncapped path stores
  `'{}'` (it incremented nothing).
- **Helper** `_settle_counter_ids(conn, row)`: return `row["counter_ids"]` when not `NULL`, else
  `_counter_ids_for_role(conn, role=row["role"])` (legacy fallback). `commit`, `refund` and
  `reap_stale` all use it. They select `counter_ids` with the row.
- **commit() on an `expired` row (FRE-1681).** The reaper already took the estimate off the
  counters. So add the full `actual_cost` (not the delta) to the stored counters, mark the row
  `committed`, set `actual_cost_usd`, and log `cost_gate_committed_late`. A `committed` or
  `refunded` row still raises `ValueError`. A late charge is never denied: the money is spent.
- **refund() on an `expired` row.** Unchanged (no-op): the reaper already freed the estimate.
- **reap_stale() (AC-3).** Unchanged rule: an `active` row past `expires_at` is freed. It now
  subtracts from the stored counters.
- No change to `RESERVATION_TTL_SECONDS`. Fix the stale "90s" comments only where this PR edits
  the line.

## Acceptance criteria → tests (`tests/personal_agent/cost_gate/`, marker `integration`)

| AC | Test | Expected |
|----|------|----------|
| 1681 AC-1 | `test_late_commit_charges_real_cost` | reserve 0.40, force expiry, reap, commit 0.34: daily counter = 0.34, row `committed`. Fails on current code (`ValueError`). |
| 1681 AC-2 | existing `test_gate.py` suite | stays green; `test_commit_in_ttl_unchanged` pins estimate 1.00 / actual 0.40 → 0.40 |
| 1681 AC-3 | `test_reaper_still_frees_never_committed_call` | reserve, expire, reap: counter back to 0, row `expired`; a second reap changes nothing |
| 1690 AC-1 | `test_commit_after_daily_boundary_settles_reserved_window` | fake clock: reserve 23:59:30 Sat 2026-10-24, commit 00:00:30 Sun. Old day = pre-reserve + actual. New day counter absent or unchanged. |
| 1690 AC-2 | `..._weekly_boundary_...`, `test_reaper_after_boundary_frees_reserved_window`, `test_refund_after_boundary_...` | same for the weekly window (Sun 2026-10-25 23:59:30 → Mon), for the reaper, and for refund |
| 1690 AC-3 | every boundary test | asserts no counter row has `running_total < 0` for the test role |
| compat | `test_legacy_row_without_counter_ids_falls_back` | row with `counter_ids = NULL` settles against the re-derived counters as today |

Tests use a per-role config with a daily and a weekly cap and no `_total` cap, so they leave no
shared `_total` rows behind. The fake clock patches `personal_agent.cost_gate.gate.datetime`.

## Steps

1. Write the failing tests above → `make test-infra-up`; apply migration 0036 to the test
   Postgres (:5433) as `agent`; run `uv run pytest tests/personal_agent/cost_gate -q`; confirm the
   new tests fail for the stated reason.
2. Schema: `init.sql`, migration 0036, ORM model.
3. `gate.py`: store ids in `reserve()`, add `_settle_counter_ids`, change `commit/refund/reap_stale`.
4. Re-run the cost gate tests → all pass.
5. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
6. Commit, self-review (`feature-dev:code-reviewer`, scoped to `git diff origin/main...HEAD`), PR,
   Linear handoff comment on FRE-1681 (and a pointer on FRE-1690).

## Deploy notes for master

Run migration 0036 as the admin role BEFORE the `seshat-gateway` rebuild: `reserve()` writes
`counter_ids`. The migration is additive and idempotent. Roll back = redeploy the old image
(the extra column is ignored).

## Open points for plan review

- A late commit can push a counter over its cap. This is deliberate (cost is real). Is a log
  warning enough?
- Is `NULL` fallback acceptable for legacy `active` rows, or must the migration backfill them?

## Codex plan review — disposition (2026-10-25)

1. **Lock order (accepted, folded in).** `_lock_counters` locks per cap in config order and sorts
   afterwards, but settlement updates counters in id order. A deadlock aborts a `commit`, and both
   callers swallow that failure, so the cost is lost. Fix: find the ids first (no lock), sort,
   then lock in ascending id order. Test: reserve against settle contention.
2. **Legacy NULL rows during deploy (partly accepted).** No backfill: the counter set depends on
   the config, which is not in the database. The legacy rows live at most 180 s. The runbook says:
   run migration 0036, rebuild at once, and do not deploy within 5 minutes of 00:00 UTC.
3. **Late commit against the reaper (accepted).** Add a test that runs `commit` and `reap_stale`
   together and asserts a committed row and a total of exactly the actual cost.
4. **Uncapped path (accepted).** Add a test that an uncapped reservation stores `{}` and that
   commit and late commit leave the placeholder counter unchanged.
