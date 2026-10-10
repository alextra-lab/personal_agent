# FRE-1563 — Telemetry tasks stay with the primary

Ticket: FRE-1563 (Approved, stream:build2). Backing design: ADR-0154 D2/D7, ADR-0150 (worker types).
Risk tier: Standard (touches `src/` logic in the expansion controller) — codex plan-review required.

## Problem

The planner can write a task such as "retrieve latency metrics for the last 24 h" and type it
`researcher`. No worker type holds a tool that reads Seshat's logs, metrics or health
(`worker_types.py`: `researcher` = `web_search`; `general` = `run_python`, `search_memory`,
`recall_personal_history`). The worker searches the web until its round cap.

## Mechanism chosen: a post-plan filter in the controller (not a planner prompt change)

Options from the ticket, and the verdict on each:

| Option | Verdict |
|--------|---------|
| Planner instruction | Rejected. Any change to the planner request needs a new D7 run (11/11) in an owner window master gates. That is a cost with no benefit here, because the failure is the model ignoring routing, and an instruction asks the same model to route. |
| Gateway rule | Rejected. It judges the user query by keyword before the planner runs. ADR-0154 moved routing off exactly that ladder (97% of typed turns mislabelled). It also withholds a whole mixed question (telemetry plus web) from expansion. |
| Decline rule | Not available. The planner decline (`SINGLE`) is not wired in production yet (`admit_single` is a probe flag; `planner_may_decline` has no caller). |
| **Post-plan filter on the plan's tasks** | **Chosen.** It acts on a task the planner already wrote, so it removes only the part a worker cannot do. It covers the LLM plan and the fallback plan in one place. The planner request is untouched, so AC-2 holds by the byte-identity branch. |

Failure direction is safe. A false positive (a web task withheld by mistake) returns that task to the
primary, which holds `web_search` and does the work itself — the pre-expansion behaviour. A false
negative is today's behaviour. Neither adds a failure.

## Classifier

A task is a telemetry task when its `name` (underscores read as spaces) plus `goal` matches ONE of:

1. A health check: `health check`, or `<backend|service|system|gateway|agent|orchestrator> health`.
2. A telemetry noun (`logs`, `log lines`, `metrics`, `latency`/`latencies`, `telemetry`, `traces`,
   `error rate(s)`) AND a live-system marker: a recent-window phrase (`last|past|previous N
   second/minute/hour/day/week`, `N h`, `today`, `right now`) or an own-system name (`seshat`, `our`,
   `my`, `this system`, `elasticsearch`, `neo4j`, `postgres`, `gateway`, `orchestrator`, `slm`).

Every worker type is checked, because no worker type holds a telemetry tool. A test pins that
assumption: the tools of all `WORKER_TYPES` are a subset of a known worker-tool set, so a future type
that gains a telemetry tool fails the test and forces a decision.

Constraints are not matched. A constraint that says "do not use the web" is not a telemetry signal.

## Behaviour

New module `src/personal_agent/orchestrator/primary_only_tasks.py`:

- `is_telemetry_task(task: PlanTask) -> bool`
- `withhold_telemetry_tasks(plan: ExpansionPlan) -> tuple[ExpansionPlan, tuple[PlanTask, ...]]`
  returns the plan without those tasks (`dataclasses.replace`) and the withheld tasks.

In `ExpansionController.execute`, between planning and the empty-plan check:

- Nothing withheld: no change at all.
- Some withheld, some remain: dispatch the rest. `ExpansionResult.withheld_tasks` carries the names.
  `_build_synthesis_context` appends one note: these sub-tasks need Seshat's own logs, metrics or
  health, no worker can reach them, answer them with your own tools.
- All withheld: `result.plan` is the empty plan, `result.withheld_tasks` is set, and `execute`
  returns at once. It is NOT a degradation (no `report_degradation`, `degraded` stays False): this is
  a routing outcome, not a failure. The executor already returns to the ordinary tool loop when
  `sub_agent_results` and `skipped_tasks` are both empty (no synthesis message, `LLM_CALL`).
- One structlog event, `expansion_telemetry_tasks_withheld`, with `trace_id`, task names, and
  whether the plan was a fallback plan.

Not changed: `_build_planner_system_prompt`, the planner user message, `planner_response_format`,
`worker_types.py`, `fallback_planner.py`, the gateway.

Known label gap, left alone: `ctx.expansion_strategy` stays `HYBRID` on an all-withheld turn, as it
does on the existing "No valid plan" path. `record_planner_outcome` has no production caller yet, so
the planner-decision clear of ADR-0154 D6 is not live. Noted in the handoff.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 | `tests/personal_agent/orchestrator/test_primary_only_tasks.py`: three telemetry goals (logs for the last hour, latency over 24 h, a backend health check) as `researcher` and as `general` tasks — none survives `withhold_telemetry_tasks`. A web goal as `researcher` survives. Controller-level test: a stub planner returning the telemetry task dispatches no worker (`dispatched_count == 0`, `run_sub_agent` not called) and a mixed plan dispatches only the web task. |
| AC-2 | Planner request unchanged. `test_fre1549_reasoning_discipline.py::TestPlannerPromptUnchanged` already pins the system-prompt hash to main. This PR leaves that test green and adds a user-message check: `build_planner_user_message` output for a fixed input equals the output on main (hash captured from `origin/main` before the change). |
| AC-3 | Live, master or explore after deploy. Runbook in the handoff. |

## Steps (TDD)

1. Write `tests/personal_agent/orchestrator/test_primary_only_tasks.py` (classifier table, withhold,
   worker-tool pin). Run: `uv run pytest tests/personal_agent/orchestrator/test_primary_only_tasks.py -q`
   — expect import failure.
2. Write the module. Re-run — expect pass.
3. Write controller tests in `tests/personal_agent/orchestrator/test_expansion_controller.py` style
   (all withheld → no dispatch, not degraded; mixed → web only dispatched and synthesis note present;
   fallback plan filtered). Run — expect fail.
4. Wire `execute`, add `ExpansionResult.withheld_tasks`, extend `_build_synthesis_context`. Re-run — pass.
5. Add the user-message byte-identity test (hash from `git stash`-free capture: run the builder on
   `origin/main` in a scratch worktree-free way — import from `git show origin/main:` is not needed;
   the hash is computed on this branch before the edit in step 4 and pinned).
6. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
7. Commit, then self-review `git diff origin/main...HEAD` with `feature-dev:code-reviewer`
   (and `security-review` only if the diff touches inputs — it matches regex on planner text only).

## Diff class

Not a production write path, no deletion, no schema, no cost/governance. Self-serve.

## Codex plan review (2026-10-10) — findings, not yet applied

Work on this plan stopped before these were applied. The owner parked FRE-1563 for an ADR on
giving workers more tools. Early-return and fallback-plan coverage were verified correct.

1. High — telemetry only in `constraints` bypasses the filter, and `_maybe_redispatch_on_gap`
   re-dispatches without reclassifying. Match `constraints` too, and classify the replacement spec.
2. High — the classifier misses `errors`/`exceptions` and bare `Seshat health`. A false negative
   still sends a task to a worker, so "safe direction" is wrong for the must-not-send requirement.
3. Medium — false positives on web research ("how Kubernetes backend health checks work",
   "compare Elasticsearch latency metrics"). Fallback plans embed the whole query in every task goal,
   so one telemetry clause can withhold every task. Treating `elasticsearch` as proof of ownership is too broad.
4. Medium — "logs for the last hour" has no numeral; the window regex must accept `last hour`.
5. Low — the worker-tool subset pin detects a new tool name, not an existing tool gaining telemetry reach.
   The single fixed-input user-message hash proves one fixture only.
