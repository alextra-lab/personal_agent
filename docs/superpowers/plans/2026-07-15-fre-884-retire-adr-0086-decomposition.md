# FRE-884 — Retire the ADR-0086 artifact-build decomposition path

**Ticket:** FRE-884 (Approved) · **Backing:** ADR-0086 (retiring its artifact-decomposition
section), ADR-0118 (superseding direction) · **Branch:** `fre-884-retire-adr-0086-decomposition`

## Scope confirmed with owner

This is retiring real, working, instrumented code (a fully-built tool-using discovery loop with
governance-integrated dispatch, cost tracking, live-meter progress, and audit capture) — not an
inert stub. The ADR's "stub" language describes pre-implementation design; the FRE-500s range of
follow-up tickets built it into working code after the 2026-06-06 rollout. Owner confirmed
proceeding despite: (a) the ticket's own "Stream-1 routing-fork-collapse ADR" sequencing gate is
unmet (that ADR doesn't exist yet — referenced only as a future ADR inside ADR-0118), and (b) the
actual deletion size is larger than the ticket's "inert branch" framing suggested. Both are noted
in the final ticket comment for master.

**Item 1 from the ticket (flip the prod `.env` flag off + rebuild)** is explicitly an ask-first
deploy action per the ticket text — out of scope for this build session. This plan covers items 2
(delete the code) and 3 (mark ADR-0086's section Retired).

## What stays untouched (confirmed shared infra)

- `request_gateway/decomposition.py` — `ANALYSIS`/`PLANNING` HYBRID/DECOMPOSE cases, the
  resource-pressure guard.
- `orchestrator/expansion_controller.py` — `ExpansionController.execute/_run_planner/_run_dispatch/
  _build_synthesis_context`, the core plan-validation schema, `executor.py:1645` HYBRID/DECOMPOSE
  wiring.
- `orchestrator/expansion_types.py` — `SubAgentMode.PARALLEL_INFERENCE`, `ExpansionPlan`,
  `PlanTask` (fields `mode`/`tools` stay — ADR-0036 general-purpose expansion-controller design,
  not ADR-0086-specific; they just become permanently inert defaults, same as before ADR-0086
  existed).
- `orchestrator/fallback_planner.py` — never implemented a discovery-aware branch; no change.
- `orchestrator/tool_dispatch.py::dispatch_tool_call` — stays; only its module docstring's
  "two callers" claim needs correcting (drops to one: the primary executor).
- `intent.py::_ARTIFACT_BUILD_REGEX` / `_TOOL_INTENT_PATTERNS` — FRE-469's artifact-build →
  TOOL_USE classification fix stays (independent of ADR-0086; confirmed `_TOOL_INTENT_PATTERNS`
  embeds `_ARTIFACT_BUILD_REGEX` directly at intent.py:125, not via the compiled
  `_ARTIFACT_BUILD_PATTERNS` being removed).
- `scripts/eval/fre481_decomposition_ab/`, historical plan/research docs under
  `docs/superpowers/plans/2026-06-05-fre-{479,480,481}-*`, `docs/research/2026-06-06-*` — historical
  record of the A/B that already ran; not imported by any src module (verified), left alone.

## Steps

1. **`src/personal_agent/config/settings.py`** — delete 3 Fields: `artifact_decomposition_enabled`
   (~line 492), `sub_agent_max_tool_iterations` (~line 464), `sub_agent_summary_max_chars`
   (~line 474). All three are exclusively consumed by the code being deleted (confirmed via grep).

2. **`src/personal_agent/request_gateway/intent.py`**:
   - Remove `_ARTIFACT_BUILD_PATTERNS` compiled pattern (line 91) — its only consumer is the bias
     check being removed.
   - Simplify the `TOOL_USE` branch (lines 314-338): drop the `if _ARTIFACT_BUILD_PATTERNS.search(...)`
     branch; always do `complexity = _estimate_complexity(user_message, task_type)` (the plain-lookup
     path becomes unconditional). No `artifact_build` signal is ever appended again.
   - Trim the stale D1 rationale from the `_ARTIFACT_BUILD_REGEX` comment block (lines 75-81) —
     keep the FRE-469/FRE-479 classification rationale, drop the "drives the artifact-build
     complexity sub-signal" line.

3. **`src/personal_agent/request_gateway/decomposition.py`**:
   - `_apply_matrix`: remove the `artifact_decomposition_enabled` kwarg; `TaskType.TOOL_USE` case
     becomes the pre-ADR-0086 unconditional `return DecompositionStrategy.SINGLE, "tool_use_single"`.
   - `assess_decomposition`: drop the `artifact_decomposition_enabled=settings.artifact_decomposition_enabled`
     kwarg from the `_apply_matrix` call.
   - Update `_apply_matrix`'s docstring (drop the ADR-0086 flag paragraph).

4. **`src/personal_agent/orchestrator/expansion_types.py`** — remove
   `SubAgentMode.TOOLED_SEQUENTIAL`; update the enum's docstring and `PlanTask.mode`/`.tools`
   docstrings to drop the TOOLED_SEQUENTIAL mention.

5. **`src/personal_agent/orchestrator/expansion_controller.py`**:
   - Remove `_PLANNER_DISCOVERY_SLICE_GUIDANCE` constant and the `_DISCOVERY_TOOL_ALLOWLIST` import.
   - Remove `_build_planner_system_prompt()`; use `_PLANNER_SYSTEM_PROMPT` directly at its one call
     site in `_run_planner`.
   - `_validate_plan_json`: remove the `discovery_enabled` lookup and the
     `if discovery_enabled and str(t.get("mode",...))...` branch — every task is always
     `PlanTask(..., mode=SubAgentMode.PARALLEL_INFERENCE, tools=[])`, i.e. stop reading `mode`/
     `tools` from the raw JSON entirely.

6. **`src/personal_agent/orchestrator/sub_agent.py`** (the bulk of the deletion):
   - Remove `_DISCOVERY_TOOL_ALLOWLIST`, `_publish_sub_agent_progress`, `_to_openai_tool_calls`,
     `_run_tooled_loop` in full.
   - `run_sub_agent`: collapse the `if spec.mode == SubAgentMode.TOOLED_SEQUENTIAL and spec.tools`
     branch — only the "default: single inference call" path remains, `summary_cap = 2000`
     unconditionally (matches the ADR's own documented rollback guarantee).
   - Remove the `is_tooled`/`empty_digest` special-casing in the success-path `SubAgentResult`
     construction — `success=True` unconditionally on that path (no tooled mode can produce an
     empty-digest failure anymore).
   - Remove the `complete_tooled` variable and the `tooled=complete_tooled` field from the
     `sub_agent_complete` log call (it would only ever log `False` going forward — dead metadata).
     Note in the master handoff: any Kibana panel keyed on `sub_agent_complete.tooled` needs
     separate follow-up (out of scope here).
   - `spec: SubAgentSpec` `tools_used: list[str]` stays as a general field, always `[]` now.

7. **`src/personal_agent/orchestrator/tool_dispatch.py`** — docstring only: correct the "one
   dispatch path, two callers" framing (now one caller: the primary executor); drop the
   `_run_tooled_loop` reference.

8. **`src/personal_agent/orchestrator/sub_agent_types.py`** — docstring only: `mode` field
   description drops "or TOOLED_SEQUENTIAL".

9. **Tests** (TDD: run each affected file red → green):
   - `tests/personal_agent/request_gateway/test_intent.py` — remove
     `test_artifact_build_floors_at_moderate`, `test_artifact_build_allows_complex`; add
     `test_artifact_build_no_longer_biases_complexity` (parametrized over the same 9 fixtures)
     asserting `"artifact_build" not in result.signals`. Keep
     `test_artifact_build_classified_as_tool_use` unchanged (FRE-469 classification, untouched).
   - `tests/personal_agent/request_gateway/test_decomposition.py` — replace
     `TestToolUseMatrix` with a single parametrized test (`SIMPLE`/`MODERATE`/`COMPLEX` → `SINGLE`/
     `"tool_use_single"` unconditionally, no monkeypatch). Replace
     `TestArtifactBuildFlagAndGovernanceGuards` with one test driving the real classifier on the
     `_ARTIFACT_MSG` fixture through `assess_decomposition`, asserting `SINGLE`/`"tool_use_single"` —
     this is the acceptance-criterion proof for the retirement. Remove the now-unused `settings`
     import if nothing else in the file needs it (check first).
   - `tests/personal_agent/orchestrator/test_expansion_types.py` — remove the
     `TOOLED_SEQUENTIAL.value` assertion.
   - `tests/personal_agent/orchestrator/test_expansion_controller.py` — remove
     `test_parses_tooled_mode_when_flag_on`, `test_drops_mutating_tools_when_flag_on`,
     `test_ignores_mode_tools_when_flag_off`, `test_planner_prompt_flag_gated`; replace with one
     test asserting `_validate_plan_json` always produces `PARALLEL_INFERENCE`/`tools=[]` regardless
     of a `mode`/`tools` field in the raw JSON, and that `tooled_sequential` never appears in the
     planner system prompt.
   - `tests/personal_agent/orchestrator/test_sub_agent.py` — remove the entire `TestTooledLoop`
     class (10 tests), `_tooled_spec`/`_tool_call` helpers, and
     `TestSubAgentCost.test_tooled_loop_sums_per_call_cost`. In
     `test_start_and_complete_carry_session_id`, drop the `complete[0]["tooled"] is False`
     assertion (field no longer emitted). Add `assert result.tools_used == []` to
     `test_successful_execution` (folds in the one useful assertion from the deleted
     `test_parallel_inference_unaffected`).
   - `tests/personal_agent/orchestrator/test_fallback_planner.py` — simplify
     `TestToolAssignment.test_research_tasks_get_tools` (currently a vacuous
     `assert isinstance(research_tasks, list)` that references the removed enum member) — drop the
     `SubAgentMode.TOOLED_SEQUENTIAL` reference; assert `plan.tasks[0].mode ==
     SubAgentMode.PARALLEL_INFERENCE` instead (documents actual behavior, no longer vacuous).

10. **`docs/architecture_decisions/ADR-0086-hybrid-decompose-routing-for-artifact-builds.md`** —
    change `**Status:** Proposed — 2026-06-04` to
    `**Status:** Retired — 2026-07-15 (FRE-884) — superseded by ADR-0118's user-selectable
    artifact-builder direction; never cleared its own rollout gate (telemetry showed ~once/90-days
    firing, low value)`. Add a short "## Retirement note (2026-07-15)" section after the header
    summarizing: code removed in FRE-884, the missing Stream-1 routing-fork-collapse ADR gap is
    noted for master/future sequencing.

11. **`docs/reference/CONFIG_INVENTORY.md`** — remove the 3 settings rows/mentions (line 58 table
    row, line 378 list mention).

## Test commands

```
make test-file FILE=tests/personal_agent/request_gateway/test_intent.py
make test-file FILE=tests/personal_agent/request_gateway/test_decomposition.py
make test-file FILE=tests/personal_agent/orchestrator/test_expansion_types.py
make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py
make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent.py
make test-file FILE=tests/personal_agent/orchestrator/test_fallback_planner.py
make test   # full suite
make mypy
make ruff-check
make ruff-format
```

## Acceptance criteria (from the ticket)

1. A high-complexity artifact-build turn falls back to the single-path route (no HYBRID, no
   discovery dispatch) — proved by the new `assess_decomposition` test driving the real classifier.
2. The now-inert decomposition branch code is deleted (steps 1-8 above).
3. ADR-0086's artifact-decomposition section is status-changed to Retired (step 10).
4. Deploying the flag flip in the live `.env` is explicitly NOT this ticket's job — flagged to
   master as an ask-first deploy action.
5. The missing "Stream-1 routing-fork-collapse" ADR gap is surfaced to master, not silently
   resolved.
