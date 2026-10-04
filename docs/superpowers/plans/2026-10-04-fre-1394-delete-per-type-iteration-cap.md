# FRE-1394 — delete the per-type tool-iteration cap

**Ticket:** FRE-1394 (cap half only — the routing half is FRE-1515).
**Design intent:** ADR-0142 D1 — the task type stops allocating capability. Every turn resolves to
`orchestrator_max_tool_iterations` (25), plus any grant the user explicitly made.
**Tier:** Standard (touches `src/` logic and the cost envelope) — codex plan-review required.

## Scope

- Delete `orchestrator_max_tool_iterations_by_task_type` from `AppConfig`.
- `_resolve_max_iterations` returns `orchestrator_max_tool_iterations + tool_iteration_bonus +
  grounding_retrieval_grant`, and reads no task type.
- `_apply_matrix` and every routing decision stay unchanged (FRE-1515 owns them).
- The spend-threshold guard (`orchestrator_spend_threshold < _max_iters`) is deleted. It existed
  only for the per-type cap. `AppConfig` refuses a threshold at or above the global ceiling, and a
  grant only raises the ceiling, so the guard is always true after this change.

## Codex plan-review findings (all taken)

| Severity | Finding | Disposition |
|----------|---------|-------------|
| Medium | The plan kept a test for a threshold at the ceiling. The validator (`settings.py`, `_validate_spend_threshold_below_ceiling`) makes that configuration impossible. | Delete the guard and the `TestSpendPauseRespectsPerTaskTypeCeiling` class. The validator keeps its own tests in `tests/test_config/test_settings.py`. |
| Medium | Captain's Log reflection asks for "a cap-raise for this TaskType" (`reflection_dspy.py`, `reflection.py`). | The prompt now proposes a raise of the global ceiling. The task type stays as context. |
| Low | The grant test covers one task type and one grant. | Parametrize over every `TaskType`, with `tool_iteration_bonus` and `grounding_retrieval_grant`. |
| Low | The field-name test does not prove that a stale env var boots. | Set the env var and build `AppConfig()`. |
| Low | Stale comments in `executor.py`, `error_monitor.py`, `route_trace/types.py`, `sub_agent_types.py`, the validator docstring. | Updated. |

## Steps

1. **Failing tests first** — `tests/personal_agent/orchestrator/test_effective_tool_iteration_ceiling.py`:
   - `test_ceiling_is_the_same_for_every_task_type` — for every `TaskType` member, a context with
     that task type resolves to `settings.orchestrator_max_tool_iterations`. Fails today
     (`conversational` → 6, `memory_recall` → 8).
   - `test_grants_add_to_the_uniform_ceiling_for_every_task_type` — for every `TaskType`,
     `tool_iteration_bonus=10` and `grounding_retrieval_grant=3` resolve to the global ceiling + 13
     (AC-1's "explicitly granted").
   - `test_the_per_task_type_setting_is_gone` — with the stale env var set, `AppConfig()` loads and
     has no such field.
   - Run: `make test-file FILE=tests/personal_agent/orchestrator/test_effective_tool_iteration_ceiling.py`
     → all three new tests fail (the grant test reads 6 + 10 = 16, not 25 + 10 = 35).
2. **`src/personal_agent/config/settings.py`** — delete the field. Update the descriptions of
   `orchestrator_spend_threshold` and `sub_agent_max_tool_iterations`, which name it.
3. **`src/personal_agent/orchestrator/executor.py`** — simplify `_resolve_max_iterations` and its
   docstring. Update the spend-threshold comment that says "removed only by FRE-1394".
4. **`tests/personal_agent/orchestrator/test_spend_threshold_pause.py`** — delete the class
   `TestSpendPauseRespectsPerTaskTypeCeiling`, its orphaned helper and imports, and the module
   docstring paragraph that names the per-type cap.
5. **Docs** — `.env.example` comment (line 183); regenerate the AUTOGEN block of
   `docs/reference/CONFIG_INVENTORY.md` with `uv run python scripts/audit/config_inventory.py generate`
   and remove the field from the hand-written list (line 513). Historical ADRs and research
   documents stay unchanged.
6. **Gates** — `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files` · `uv run python scripts/audit/config_inventory.py verify`.

## Acceptance criteria (from the ticket)

| AC | Proof before merge | Proof after deploy |
|----|--------------------|--------------------|
| AC-1 — the ceiling is independent of the task type | Step-1 unit tests over every `TaskType` member | `route_traces` primary rows (`task_id IS NULL`), ≥200 turns, ≥3 task types, grouped by `task_type` and `effective_tool_iteration_ceiling`: one ceiling (25) for every type, except granted turns |
| AC-5 — the cost change is reported | Pre-change baseline (2026-09-04 to 2026-10-04) recorded on the ticket | The same query over an equal post-deploy window, compared with the baseline |

## Baseline (pre-change, 2026-09-04 to 2026-10-04, `route_traces`)

- Primary turns: 224. `cost_live_usd` 13.4541. `cost_authoritative_usd` 18.5454 (80 of 224 reconciled).
  `tool_iteration_count` p50 1, p90 4, max 15.
- Child rows: 193, 1.8940 USD.
- `conversational`: 126 turns, ceiling 6 (one granted turn at 16), p50 1, p90 4, max 7, 5 at the ceiling.

## Risk

The change raises the ceiling for `conversational` (6 → 25) and `memory_recall` (8 → 25). The
replacement bounds are live: the spend-threshold pause at 6 iterations (FRE-1393), the brainstem
fan-out budget (FRE-1382), and the turn deadline and lifetime cap (ADR-0142 D4a).
Diff class: escalated (cost envelope).
