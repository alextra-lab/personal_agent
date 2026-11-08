# FRE-1687 — the delegation tool, its ask rule, and a durable approval decision

ADR-0159 D10.1–D10.5 and D10.7. Phase 5 of the ADR's order of work. The three old adapters are
deleted already (only `delegation/adapters/opencode.py` exists).

## Design (revised after the Codex plan review)

1. **Durable decision (D10.3).** The permission layer (`tools/executor.py`) persists every card
   decision in a new table, `tool_approval_decisions` (migration 0041): `request_id` (primary
   key), `session_id`, `tool_call_id`, `tool`, `args_hash`, `decision`, `trace_id`, `decided_at`.
   - Not a `session_events` row. That table has a 24-hour TTL sweep, and the replay-gap check reads
     its oldest retained `seq`. A decision row kept past the sweep hides a real gap from a
     reconnecting client (Codex finding 3). The table has no TTL and one row per card.
   - `args_hash` is the SHA-256 of the card's arguments as canonical JSON (Codex finding 2).
   - The recorded decision is the effective grant: `approve` (ran once), `approve_session`,
     `deny`, `timeout`, `connection_lost`. An `approve_session` answer to an explicit ask records
     `approve`.
   - A failed write is logged and changes no outcome. Other tools keep their approval behaviour.
     The delegation tool fails closed, because its insert requires the row.
   - The `approve` callable (ADR-0153 D7) records its decisions the same way.
2. **Grant to the executor.** `PermissionGranted.approval` carries an `ApprovalGrant(request_id,
   tool_call_id, args_hash, args)`, only for an approved card. The reserved executor parameter
   `approval` receives it. A model-supplied `approval` is dropped. `execute_tool` and
   `_check_permissions` take `tool_call_id`, and `dispatch_tool_call` passes its own.
3. **The tool (D10.1, D10.5).** `start_dev_job(task, mode, artifact_id=None)` in
   `tools/dev_job.py`, registered only when `dev_agent_supervisor_enabled` is true. The executor
   refuses before any database work when: no grant (`approval_required`); the grant's arguments
   differ from the call's (`approval_mismatch`); no supervisor task runs (`not_available`, Codex
   finding 6); no user or session; invalid arguments. Then `JobStore.create_approved_job`, one
   transaction:
   1. `job_active` if a job holds the one-job slot;
   2. lock the month row of `dev_month_spend` `FOR UPDATE`, as the broker does, and refuse
      `month_cap_reached` at committed + reserved >= cap (no row = zero spend, Codex finding 5);
   3. `INSERT … SELECT … FROM tool_approval_decisions` matching request id, session, tool call id,
      tool, args hash and `approve`. No match: `approval_missing`;
   4. a unique violation on `dev_jobs_approval_request_id_key`: `approval_used`.
   The tool returns at once with the job id, `queued` and "Build started".
4. **Config (D10.2, D10.7).** `permissions.yaml`: an explicit `ask` rule `start_dev_job`, no
   `modes`, so every mode asks. No allow rule. `tools.yaml` entry. No sub-agent grant.
   `delegation_enabled` stays false.
5. `JobStore.create_job` stays for tests. No production code calls it (Codex finding 1).

## Steps

1. Tests first (`tests/test_tools/test_permission_ask_flow.py`, new
   `tests/test_tools/test_approval_decision_persist.py`): the decision event is pushed with the
   request id and tool call id for approve, deny, timeout and connection lost; the executor
   receives `approval` only after an approved card; a model-supplied `approval` is dropped;
   regression: `run_python(network=true)` still shows a card with no session option and runs
   once on approve; a persist failure does not refuse the call.
2. Implement points 1–2 in `transport/events.py`, `transport/agui/adapter.py`,
   `transport/agui/transport.py`, `transport/agui/event_buffer.py`, `tools/executor.py`,
   `orchestrator/tool_dispatch.py`.
3. Tests first for the tool, on the test stack (`tests/personal_agent/dev_agent/
   test_start_dev_job_integration.py`): approved call → one row whose request id matches one
   persisted `approve`; deny, timeout, a reused approval id, a missing event, an event for another
   session or tool → no row; active job → `job_active`, no row; month cap → `month_cap_reached`,
   no row; the call returns in under 10 s with the supervisor absent. Unit tests for the
   argument checks. A config test: the ask rule exists in every mode, no allow rule matches,
   `delegation_enabled` default is false and `.env.example` does not set it true.
4. Implement point 3 (`dev_agent/jobs.py`, `tools/dev_job.py`, `tools/__init__.py`) and point 4.
5. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`, and the dev_agent integration tests on the test stack:
   `uv run pytest tests/personal_agent/dev_agent/test_start_dev_job_integration.py -m integration`.

## Out of scope

The job card and job events (FRE-1688). The paid evaluation (FRE-1689). A direct path that skips
the card (D10.4: v0 has none).
