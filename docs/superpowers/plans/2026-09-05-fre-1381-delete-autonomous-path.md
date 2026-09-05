# FRE-1381 round 2 — delete the autonomous orchestration path (scope correction)

Supersedes `2026-09-05-fre-1381-serialize-expansion-fanout.md` (round 1, which serialized
`execute_hybrid` per the ticket description at the time). Master bounced PR #1052: the owner's
actual 2026-09-05 decision was **delete**, not serialize — recorded in a Linear comment while
the ticket description still said "serialize." Per master: "A contradicting comment does not
supersede a description; amend the description" — the description is now amended, and this plan
implements the corrected scope. Evidence for the ruling (orchestration_mode is set in no `.env`,
no compose file, and is absent from the running container; three consecutive tickets improved
only the enforced path) is already established in master's comment — not re-derived here.

## Acceptance criteria (master's 2026-09-05 comment, verbatim scope)

- **AC-1**: the path is gone, not merely unreachable — no remaining definition or call of the
  autonomous fan-out in `src/`. Fails if hidden behind a permanently-false flag.
- **AC-2**: no setting survives with no live reader — `orchestration_mode` and
  `sub_agent_max_tokens` gone, or the PR states exactly what still reads each.
- **AC-3**: the enforced path is unchanged, proven by test — existing `expansion_controller`
  tests pass without modification; FRE-958's client-isolation test and FRE-1390's
  `TestPlannerRoleBinding` still hold.
- **AC-4**: nothing else depended on it — the executor's call site and its `orchestration_mode`
  branch removed cleanly, no orphaned imports or dead branches.

## What "the path" is, mapped to code (found by grep, not assumed)

1. `src/personal_agent/orchestrator/expansion.py` — `execute_hybrid()` + `parse_decomposition_plan()`.
   No caller outside this file and `executor.py`'s autonomous hook (grepped). **Delete the whole
   file** — nothing survives it.
2. `executor.py`'s three autonomous-mode touchpoints:
   - The `if settings.orchestration_mode == "enforced": ... else: # Autonomous mode` branch in
     the HYBRID/DECOMPOSE dispatch gate — flatten to the enforced body, unconditional.
   - The "HYBRID decomposition prompt (autonomous mode only)" system-prompt injection block —
     delete, plus its `_decomposition_added` flag and the `_component_ids.append(...)` it fed.
   - The "--- HYBRID expansion hook (autonomous mode only) ---" post-completion block (parses
     the plan, calls `execute_hybrid`, builds phase-1 synthesis) — delete entirely.
3. `settings.orchestration_mode` — delete (AC-2, named).
4. `settings.sub_agent_max_tokens` — delete (AC-2, named); its only reader was
   `parse_decomposition_plan`, deleted in (1).
5. **Found during this ticket, not named in AC-2 but the same class of finding**:
   `settings.sub_agent_timeout_seconds` — grepped, its only reader was also
   `parse_decomposition_plan`. Deleting it too, and saying so plainly in the PR (AC-2's own
   principle: "no setting survives with no live reader").

## What does NOT move

- `expansion_controller.py` (the enforced path) — untouched except two comments that named the
  now-deleted autonomous path/setting as a reason something "is not deleted" (now false) —
  correcting a comment to stop asserting a survivor that no longer exists is not a logic change.
- `ExecutionContext` fields (`expansion_strategy`, `expansion_constraints`, `sub_agent_results`,
  `expansion_plan`, `expansion_phase_results`) — grepped: read generically by
  `observability/route_trace/*` and `observability/topology/seam.py` regardless of which path
  populated them, and the enforced path (kept) sets all of them itself. Not autonomous-specific.
- `settings.expansion_budget_max` — grepped: also read by `request_gateway/pipeline.py` and
  `service/app.py` for decomposition-depth budgeting, unrelated to `execute_hybrid`'s deleted
  concurrency knob. Stays.
- `PROMPT_COMPONENT_TAXONOMY` in `llm_client/prompt_identity.py` — left in place as retired
  vocabulary (historical telemetry may still carry `"decomposition_instructions"`); only the
  test asserting the literal `_component_ids.append(...)` call site in `executor.py` source is
  updated, since that call site is what's actually gone.

## Tests

- **Delete**: `tests/personal_agent/orchestrator/test_expansion.py` (tests the deleted module
  outright) and `TestHybridExecutionPath` in `test_gateway_integration.py` (simulates the
  deleted hook via `patch("personal_agent.orchestrator.expansion...")`, an import path that no
  longer exists).
- **Fix, mechanical**: four `monkeypatch.setattr(ex.settings, "orchestration_mode", "enforced")`
  calls (`test_gateway_integration.py` ×2, `test_frozen_reset_emit.py` ×2) — the setting no
  longer exists to patch; enforced is now the only behavior, so the line is just removed, not
  replaced.
- **Fix, mechanical**: `test_prompt_identity_taxonomy.py`'s two hardcoded
  `executor_component_ids` lists drop `"decomposition_instructions"` — the sync-guard's own
  purpose (catch drift between the taxonomy and what `executor.py` actually appends) requires it,
  since the `_component_ids.append("decomposition_instructions")` call site is gone.
- **Unchanged, per AC-3**: `test_expansion_controller.py` in full (including
  `TestPlannerRoleBinding`), and `test_gateway_integration.py::TestEnforcedExpansionClientRole`
  (FRE-958 guard).

## Verification

- `make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py`
- `make test-file FILE=tests/personal_agent/orchestrator/test_gateway_integration.py`
- `make test-file FILE=tests/personal_agent/llm_client/test_prompt_identity_taxonomy.py`
- `make test-file FILE=tests/test_orchestrator/test_frozen_reset_emit.py`
- `make test` (full suite)
- `make mypy` / `make ruff-check` / `make ruff-format` / `pre-commit run --all-files`
- `rg "orchestration_mode|sub_agent_max_tokens|sub_agent_timeout_seconds" src/ tests/` → zero
  live-code hits (historical-rationale comments in `sub_agent.py` and `expansion_controller.py`
  are the only survivors, and are not reads)
- `rg "execute_hybrid|parse_decomposition_plan" src/` → zero hits
