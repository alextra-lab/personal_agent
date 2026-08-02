# FRE-1117 — main_inference silence detector

## Investigation summary (premise correction)

FRE-1117 was filed on the premise that `api_costs` losing all `main_inference`
rows after 2026-07-28 while turns kept being served indicates a broken
cost-booking path. Direct verification against the live substrate (Postgres +
Elasticsearch on this VPS, `cloud-sim-postgres` / `agent-logs-*`) falsifies
that premise:

1. `api_costs` has zero `main_inference` rows 07-29→07-31 — confirmed.
2. `budget_reservations` (the pre-call reservation ledger — written
   unconditionally by `LiteLLMClient.respond()` before any API call, for
   every budget-gated role) **also has zero `main_inference` rows** in that
   window — not even `active`/`refunded` rows. This proves no cloud call for
   this lane was ever *attempted*, let alone dropped after the fact. There is
   no commit-path bug to fix.
3. `session_model_selections` (server-authoritative) shows `primary` has been
   pinned to the local deployment (`qwen3.6-35b-thinking`) across every
   session for weeks — not a new regression.
4. ES logs (`personal_agent.llm_client.client`, `provider=slm_local`)
   directly show `role=primary` calls executing on the local model on the
   affected days.
5. The Anthropic/OpenAI cloud models the ticket cites as evidence
   ("paid models are demonstrably serving those turns") are real in the log
   corpus for that window, but tagged `role=skill_routing` /
   `entity_extraction` / `captains_log` — not `primary`. Those lanes bill
   correctly and were never silent.
6. The few genuine historical `main_inference` rows (07-24, 07-25, 07-28) all
   trace to occasional `vision`/`artifact_builder` cloud escalations (which
   also bill to the `main_inference` lane by design) — none of which recurred
   07-29→07-31. That alone accounts for the zero rows.

**Conclusion:** no code fix restores anything, because nothing is broken.
`main_inference` is at zero because primary genuinely ran free/local the
whole window and no other `main_inference`-lane escalation fired. Owner
confirmed (via AskUserQuestion in-session) to proceed with only the ticket's
second, independent ask.

## Codex plan review (2026-08-01) — incorporated

Codex reviewed this plan adversarially before any code was written (its sandbox
had no network access to re-run the live Postgres/ES queries, so it reviewed
the code paths and the query design on their merits). Disposition: *approve
with required changes*. Findings and how each is folded into the design below:

1. **Premise-falsification: no hole found.** `CostGate.reserve()`
   (`cost_gate/gate.py`) does its INSERT inside a transaction with no
   catch-and-swallow — a failure there propagates and rolls back, unlike
   `cost_tracker.record_api_call`'s broad `except Exception: return None`.
   Codex flagged one caveat worth qualifying: `gateway/chat_api.py`'s
   standalone direct-Anthropic path can proceed without a reservation when no
   gate is registered. Already checked and moot: `personal_agent.gateway.chat_api`
   has **zero** log entries of any kind in the 07-29→07-31 ES indices — that
   endpoint received no traffic in the window, so its bypass never fired.
2. **Confirmed, blocking design flaw:** the planned reservation check
   (`SELECT count(*) FROM budget_reservations WHERE role = 'main_inference' ...`)
   is a **global count on a shared lane**. `main_inference` is the budget
   role for `primary`, `sub_agent`, `compressor`, `vision`, `router`,
   `reasoning`, and `standard` alike (`cost_gate/role_map.py`). A reservation
   from any OTHER role sharing that lane on the same day would silently
   suppress the finding for every candidate session — the exact failure mode
   the detector exists to catch. **Fixed**: the reservation check is now
   scoped to `session_id` (`budget_reservations.session_id` exists and is
   indexed), so each candidate session is checked against its own
   reservations, not the lane's aggregate.
3. **Confirmed limitation:** `session_model_selections` has no history — one
   mutable row per `(session_id, role)`, overwritten in place by `upsert()`.
   There is no way to reconstruct "what was selected as of day D" once the
   row has since changed. **Mitigation, not a full fix** (full fix would
   require adding selection history, out of scope for an advisory monitor):
   a candidate session is only used as evidence when its current selection
   row's `updated_at <= end_of_day(D)` — i.e., the selection was already in
   that state by the end of the day being checked, so it is at least
   consistent with (though not provably identical to) that day. A session
   whose selection changed *after* day D is skipped rather than guessed at
   either way. This residual risk (a same-day flip after the turn but before
   day-end) is accepted and documented in the function's docstring — the
   monitor is an advisory WARNING signal a human reads, not an automated
   action, so an occasional missed or delayed flag is the acceptable failure
   mode, not a silent wrong action.
4. **Noted, accepted limitation:** `route_traces` persistence is best-effort
   (`observability/topology/seam.py`) and can under-count. Accepted as-is:
   the failure direction is under-alerting (fewer candidate sessions found),
   which is the safe direction for an advisory monitor — it never causes a
   false alarm, only a possible missed one. Fixing route_traces' own
   reliability is a separate, pre-existing observability concern (already
   partially covered by FRE-1116's corpus-completeness findings), not this
   ticket's.
5. **Reaper race: no issue, confirmed.** `reap_stale()` transitions rows
   in place to `status='expired'`; it never deletes rows or rewrites
   `created_at`. An unfiltered `count(*)` by day sees them either way.
6. **Scope-narrowing and separate-ticket calls: both confirmed as correct**,
   no changes needed.

## Scope actually being built

The ticket's own "What to change" section names a second, standalone-valid
ask regardless of the above: *"make the silence itself detectable... because
[an empty counter during activity] is indistinguishable today from spending
nothing."*

Scoped narrowly to `main_inference`/`primary` (the ticket's actual subject —
not generalized to every budget-gated role, which would be speculative
generality nothing here asked for):

**Add a daily check that flags exactly the anomaly the ticket is worried
about** — a session had `primary`-role turns AND that session's *current*
model selection for `primary` resolves to a **cloud** deployment, yet
`budget_reservations` recorded **zero** `main_inference` rows for that day.
That combination is real: it means a cloud primary call should have at least
attempted a reservation and didn't, or the client never got constructed the
way selection says it should. The all-local day we're living through today
must NOT trip this (that's the false-positive trap a naive "zero rows" check
would fall into — confirmed by hand: `study` and `artifact_builder` are also
silent this entire window and are not anomalies, they're just unused).

## Files

- **New**: `src/personal_agent/cost_gate/silence_monitor.py`
  - `@dataclass(frozen=True) class SilentInferenceFinding` — `day: date`,
    `cloud_selected_sessions: tuple[UUID, ...]`.
  - `async def find_silent_main_inference_day(pool: asyncpg.Pool, model_config: ModelConfig, *, day: date) -> SilentInferenceFinding | None`
    1. `SELECT DISTINCT session_id FROM route_traces WHERE model_role = 'primary' AND created_at >= $1 AND created_at < $2 AND session_id IS NOT NULL` (day bounds, UTC). If empty, return `None` — no turns served, correctly quiet.
    2. For each `session_id`, `SELECT deployment_key, updated_at FROM session_model_selections WHERE session_id = $1 AND role = 'primary'`. Skip the session (not evidence either way) if: no row (binding default applies — local), or `updated_at > end_of_day($2)` (selection changed after the day being checked — can't attribute it retroactively, per the codex-review finding that this table carries no history).
    3. For each remaining session, resolve the deployment key's placement via `model_config.placement_of(deployment_key)`; skip if `Placement.LOCAL`.
    4. **Per-session reservation check (codex-review fix — not a lane-wide count):** for each surviving cloud-selected session, `SELECT count(*) FROM budget_reservations WHERE role = 'main_inference' AND session_id = $1 AND created_at >= $2 AND created_at < $3`. If `0`, this session is silent-and-cloud-selected — add it to the finding. A nonzero count for *this session* means its own booking is fine, even if another session/role shares the lane.
    5. If any session was added, return `SilentInferenceFinding(day=day, cloud_selected_sessions=tuple(...))`; else `None`.
  - `async def run_silence_monitor(gate: CostGate, model_config: ModelConfig, *, interval_seconds: float = 3600.0) -> None` — mirrors `reaper.py`'s `run_reaper` shape (`while True: ... sleep; suppress CancelledError`). Tracks the last UTC calendar day it already checked in a local variable; once per loop iteration, if "yesterday" (the last fully-elapsed UTC day) hasn't been checked yet, calls `find_silent_main_inference_day` for it and logs:
    - `log.warning("main_inference_silent_with_cloud_selection", day=..., session_count=..., session_ids=[str(s) for s in ...])` when a finding is returned.
    - Nothing (no log) when the day is clean — this is a monitor, not a heartbeat; a clean day produces no line, consistent with `cost_recording_failed`-style error-only logging elsewhere in this module.
  - Never raises out of the loop (catch + `log.error` per iteration, same as `run_reaper`).

- **Edit**: `src/personal_agent/service/app.py` — in the lifespan hook, alongside the existing `asyncio.create_task(run_reaper(gate))`, add `asyncio.create_task(run_silence_monitor(gate, model_config))` (model_config already loaded nearby for `register_model_pricing`). Cancel it at shutdown next to the reaper's own cancellation.

- **New test**: `tests/personal_agent/cost_gate/test_silence_monitor.py` (isolated Postgres per FRE-375 — `tests/CLAUDE.md` substrate redirect)
  - `test_no_finding_when_reservations_exist` — seed a `main_inference` reservation for the day → `find_silent_main_inference_day` returns `None`.
  - `test_no_finding_when_no_primary_turns` — no `route_traces` rows for the day → `None`.
  - `test_no_finding_when_selection_is_local` — seed a `route_traces` primary row + a `session_model_selections` row pointing at `qwen3.6-35b-thinking` (local), zero reservations → `None`. **This is the regression guard for today's real state** — must not flag the current, correct, all-local operation.
  - `test_finding_when_selection_is_cloud_and_silent` — seed a `route_traces` primary row + a `session_model_selections` row pointing at `claude_sonnet` (cloud), zero reservations → returns a `SilentInferenceFinding` naming that session.
  - `test_missing_selection_row_defaults_to_local_no_finding` — a session with primary turns but no `session_model_selections` row at all (binding default = local) → `None`.
  - `test_other_session_reservation_does_not_suppress_finding` — **regression test for the codex-review finding.** Session A: primary turns, cloud selection, zero reservations (should be flagged). Session B: a *different* session with a committed `main_inference` reservation the same day (e.g. from `vision` or `sub_agent` sharing the lane). Asserts session A still appears in the finding — proves the check is per-session, not a lane-wide count that session B's activity would incorrectly clear.
  - `test_selection_changed_after_day_is_skipped` — a session's current `session_model_selections` row for `primary` has `updated_at` *after* the end of the day being checked → that session contributes no evidence (not flagged, not cleared) — proves the historyless-table mitigation from the codex review.
  - Mutation check during self-review: flip the `placement_of(...) is not Placement.LOCAL` condition and confirm `test_no_finding_when_selection_is_local` then fails (proves the assertion discriminates, not just "passes").

## Not in scope (filed separately, not folded in)

While tracing `cost_reconciled` in `route_traces` (the ticket's own secondary
evidence — "reconciled flag unset after 07-28"), found a **separate, real**
defect: `assemble_route_trace`'s `cost_live_usd` is sourced from
`ctx.turn_cost_usd`, which only accumulates cost from the executor's own
direct LLM call loop (`executor.py:4787`). `skill_routing`'s call
(`orchestrator/skills.py`) goes through a *separate* `LiteLLMClient` instance
obtained via `get_llm_client_for_key` and never touches that accumulator —
so its cost lands correctly in `api_costs` (authoritative) but never in
`cost_live_usd`, making `cost_reconciled` structurally unable to be `true`
for any turn that includes a skill-routing decision whose own primary call
was free. This was previously masked by tolerance when primary's own cost
dominated the trace total; it became visible once primary went all-local.
This is genuinely separate, sequenceable work (touches the route-trace
assembler's cost-aggregation contract, not the cost-gate booking path) —
filing a new Needs-Approval ticket per Step 5 rather than folding it in here.

## Acceptance criteria (this ticket's own, not ADR-0065/ADR-0120's)

- AC-1: A day where a cloud-selected primary session produced turns but zero
  `main_inference` reservations exist is detected and logged at `warning`.
  Evidence: `test_finding_when_selection_is_cloud_and_silent` passes.
- AC-2: The current, correct, all-local operating state (today's reality)
  produces **no** finding — proven by
  `test_no_finding_when_selection_is_local` passing AND failing under the
  mutation check (condition flip) described above.
- AC-3: A day with committed `main_inference` reservations is never flagged
  regardless of selection — `test_no_finding_when_reservations_exist`.
- AC-4: The monitor never raises out of its loop on a query failure (mirrors
  the reaper's `except Exception: log.error; continue` contract) — a unit
  test drives a pool that raises and asserts the loop logs and keeps running.

## Risk tier

Standard — touches `src/` cost-gate logic and a startup wiring point in
`service/app.py`. Codex plan-review before implementation per the build
skill.
