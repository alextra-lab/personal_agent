# FRE-481 — ADR-0086 Phase 3: Flag, telemetry/joinability, A/B harness + guard tests

**Ticket:** FRE-481 (Approved, Tier-1:Opus) · project *Turn Cost & Latency Optimization (artifact builds)* · parent FRE-476
**ADR:** ADR-0086 D6+D7 + Verification (`docs/architecture_decisions/ADR-0086-hybrid-decompose-routing-for-artifact-builds.md`)
**Depends on:** FRE-479 (Phase 1, merged PR #165) · FRE-480 (Phase 2, merged PR #167)

## Context — what Phases 1/2 already shipped

This is the **final** phase of a 3-phase ADR. Most of D6/D7's config is already live:

- `settings.artifact_decomposition_enabled` (default **False**) — already gates both the gateway
  routing (`decomposition.py:_apply_matrix`, Phase 1) **and** the dispatch (`expansion_controller.py:85,501`, Phase 2).
- `settings.sub_agent_max_tool_iterations` (default 5) — already consumed by `sub_agent.py:308`.
- `_run_tooled_loop` is a real tool-using loop (Phase 2); it already emits `sub_agent_tooled_iteration`
  and `sub_agent_tooled_ceiling`.
- Routing is already sliceable: `intent_classified` emits `signals` (incl. `artifact_build`);
  `decomposition_assessed` emits `reason` (`tool_use_moderate_hybrid` / `tool_use_complex_hybrid`).
- Unit-matrix tests (`test_decomposition.py::TestToolUseMatrix`) and intent tests
  (`test_intent.py`) already cover the flag at the **synthetic-complexity** level.

## What Phase 3 still needs (this PR)

1. **Joinability (ADR-0074):** the four discovery-sub-agent events
   (`sub_agent_start`, `sub_agent_tooled_iteration`, `sub_agent_tooled_ceiling`, `sub_agent_complete`)
   currently pass only `trace_id`, **not `session_id`**. The codebase convention is to pass `session_id`
   explicitly on every emit (no contextvar auto-bind — verified). Without it, these events are
   **invisible to the joinability walk's session anchor** (`walk.py:_walk_es_agent_logs` queries
   `term session_id == anchor`), so the probe cannot show them as joinable/sliceable. Add `session_id`
   to all four, and add a **digest size** field to the complete event ("complete with digest size", D7).
2. **Verification tests V5/V6 — end-to-end** (real message through `classify_intent` → `assess_decomposition`,
   exercising the genuine artifact-build complexity bias, not synthetic complexity):
   - **Flag guard:** flag **off** → a high-complexity artifact-build message still routes to `SINGLE`.
   - **Governance degradation:** flag **on** but expansion withheld → forced `SINGLE`.
   - Positive control (flag on + permitted → `HYBRID`) so the guard tests are not vacuous.
3. **A/B measurement harness** (`scripts/eval/fre481_decomposition_ab/`): the FRE-433-recipe tool that
   produces the before/after per-round token curve + total-token delta + wall-time, two arms
   (`baseline` = flag off, `decompose` = flag on), backend-aware truth source. **The harness is the
   deliverable; running it is master's post-deploy action.**

## Out of scope (master-owned, post-merge — lifecycle rules)

Deploy, flag-flip (`artifact_decomposition_enabled=true`), the live joinability-probe run, the
executed before/after report, and the final `sub_agent_max_tool_iterations` default tuning from the
A/B. **PR checklist is pre-merge only.** This build session stops at PR.

### Acceptance-criteria map (which Verification item lands where)

| ADR §  | Criterion | This PR (pre-merge) | Master (post-deploy) |
|--------|-----------|---------------------|----------------------|
| §1     | Parent `fresh_in` no longer climbs to ~71 k; per-round + total-token delta | harness ships | **runs** harness, files report |
| §2     | Artifact-quality unchanged (correctness/completeness) | harness **captures paired artifact output**; method = human side-by-side rating of N≥5 baseline-vs-decompose pairs | **runs** the rating, records verdict (codex review action 2) |
| §3     | No simple-tool regression (SIMPLE→single) | covered (existing `test_intent` + `test_decomposition`) | — |
| §4     | Discovery sub-agent actually runs tools | covered (Phase-2 `test_sub_agent::test_tooled_loop_executes_tool`) | — |
| §5     | Flag guard → SINGLE | **new test (Step 3)** | — |
| §6     | Governance degradation → SINGLE | **new test (Step 3)** | — |
| §7     | `joinability_probe.py` no orphans for new events; sliceable | emit-site `session_id` + tests (Step 1/2) | **runs probe** post-deploy (codex review action 3) |
| §8     | Backend-aware truth source | harness reads local/cloud counters correctly | applied when run |
| §9     | `make test`/`mypy`/`ruff` clean | **gate before PR** | — |

---

## Step 1 — TDD: telemetry/joinability tests (write first, confirm red)

**File:** `tests/personal_agent/orchestrator/test_sub_agent.py`

Add to `TestRunSubAgent` and `TestTooledLoop`:

- `test_sub_agent_start_and_complete_carry_session_id` — run `run_sub_agent(..., session_id="sess-1")`
  with a mocked client; assert the captured `sub_agent_start` **and** `sub_agent_complete` events both
  carry `session_id == "sess-1"` and that `sub_agent_complete` carries an int `digest_chars`.
- `test_tooled_iteration_event_carries_session_id` — drive the tooled loop (one tool call then final),
  assert the captured `sub_agent_tooled_iteration` event carries `session_id == "s"`.

Run: `make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent.py` → **expect new tests FAIL**
(session_id / digest_chars absent).

## Step 2 — Implement telemetry/joinability

**File:** `src/personal_agent/orchestrator/sub_agent.py`

- `sub_agent_start` (~line 76): add `session_id=session_id`.
- `sub_agent_complete` (~line 188): add `session_id=session_id`, `digest_chars=len(result.summary)`,
  and `tooled=(spec.mode == SubAgentMode.TOOLED_SEQUENTIAL and bool(spec.tools))` (recomputed at the
  emit site so it is defined in the timeout/exception paths too — `is_tooled` inside the `try` is not
  in scope at the post-`except` emit).
- `sub_agent_tooled_iteration` (~line 391): add `session_id=session_id`.
- `sub_agent_tooled_ceiling` (~line 406): add `session_id=session_id`.

(`session_id` is already a parameter of both `run_sub_agent` and `_run_tooled_loop`, and the call site
`expansion_controller.py:391` already threads it.)

Run the same test file → **expect green**.

## Step 3 — TDD: flag-guard + governance-degradation end-to-end tests

**File:** `tests/personal_agent/request_gateway/test_decomposition.py`

New class `TestArtifactBuildFlagAndGovernanceGuards` (imports `classify_intent`), driving the **real**
classifier so the artifact-build complexity bias is genuinely exercised:

```python
_ARTIFACT_MSG = "Explain the internals of the gateway and build an interactive HTML guide."
```

- `test_real_artifact_build_is_high_complexity` (control) — `classify_intent(_ARTIFACT_MSG)` yields
  `TaskType.TOOL_USE`, `complexity != SIMPLE`, `"artifact_build" in signals`.
- `test_flag_off_high_complexity_artifact_routes_single` — flag **explicitly** off
  (`monkeypatch.setattr(settings, "artifact_decomposition_enabled", False)` — do **not** rely on the
  default, so the guarantee fails loudly if the default ever flips):
  `assess_decomposition(classify_intent(_ARTIFACT_MSG), _governance())` →
  `SINGLE` / `tool_use_single` (explicitly **not** HYBRID). *Verification §5 / ticket flag-guard.*
  (codex review action 1)
- `test_flag_on_expansion_denied_forces_single` — flag **on**, `_governance(expansion_permitted=False)`
  → `SINGLE` / `expansion_denied`. *Verification §6 / ticket governance-degradation.*
- `test_flag_on_zero_budget_forces_single` — flag **on**, `_governance(expansion_budget=0)`
  → `SINGLE` / `zero_budget`.
- `test_flag_on_permitted_routes_hybrid` (positive control, proves the message is high-complexity so
  the guards above are non-vacuous) — flag **on**, default governance →
  `HYBRID` / (`tool_use_moderate_hybrid` or `tool_use_complex_hybrid`).

Run: `make test-file FILE=tests/personal_agent/request_gateway/test_decomposition.py` → green
(no source change needed — these assert already-shipped Phase-1 behaviour end-to-end; they are the
explicit guard tests the ADR's Verification §5/§6 require).

## Step 4 — A/B measurement harness (the deliverable, not run here)

**New dir:** `scripts/eval/fre481_decomposition_ab/`

- `harness.py` — mirror `scripts/eval/fre433_cache_ab/harness.py` structure (httpx → gateway `/chat`,
  ES read-back), but collect the **full per-round** `model_call_completed` series for the turn's
  `trace_id` (not just the first full-context call):
  - Per-round table: round#, role, `input_tokens` (parent `fresh_in`), `cache_read_tokens`,
    `output_tokens`, `latency_ms`.
  - Totals: Σ input, Σ cache_read, Σ output, total wall-time (first→last `@timestamp`), round count.
  - Routing check: pull `decomposition_assessed` (`strategy`/`reason`) and `intent_classified`
    (`signals`) for the trace; assert sliceability.
  - Discovery-sub-agent slice: count `sub_agent_tooled_iteration` / `sub_agent_complete` for the trace
    (joinable by `session_id` now).
  - `--arm {baseline,decompose}` tag (mirrors fre433's `--arm`; the arm is *what flag the gateway is
    deployed with*, the harness only tags + drives).
  - `--profile {local,cloud}`; backend-aware: headline parent-context size keys on
    `max(input_tokens, cache_read_tokens, cache_creation_input_tokens)`; note local `timings.cache_n`
    vs cloud `cache_read_input_tokens` truth source in the README.
  - `--logs-prefix` CLI arg (default `settings.elasticsearch_index_prefix`) so a prod prefix
    divergence can't silently read zero events (codex review action 4).
  - **Capture the artifact output** per arm — record the `/chat` `response` text and any `artifact_id`
    into JSON so the post-deploy human side-by-side quality eval (ADR §2) has paired before/after
    material to score (codex review action 2).
  - Markdown + JSON output under `telemetry/evaluation/fre481-decomposition-ab/`.
- `dataset.yaml` — one session, the `a0a07227`-equivalent artifact prompt(s).
- `README.md` — the run protocol (deploy flag-off → run `--arm baseline`; deploy flag-on →
  run `--arm decompose`; diff), the deterministic vs measurement-gated claims (parent-tail bound is
  deterministic; wall-time near-zero on single-GPU local; net-cost can rise), backend-aware truth
  source, and an explicit **"this is a master post-deploy action"** banner.

(`scripts/` is outside `mypy src/` / `ruff check src/`; still write typed, clean, docstringed code
matching the fre433 harness. No test imports the harness.)

## Step 5 — Docs

- ADR-0086: flip **Status** Proposed → Accepted only if owner directs (status edits are doc-drift
  sensitive; default leave as-is and let master handle on merge). Add a one-line Phase-3 note under
  Verification if helpful. *Decision: leave ADR status untouched; master owns status transitions.*
- No README/skill-doc changes needed (no new commands; harness has its own README).

## Step 6 — Quality gates (all before PR)

```
make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent.py
make test-file FILE=tests/personal_agent/request_gateway/test_decomposition.py
make test          # full unit suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Step 7 — PR, then STOP

Open PR with `.github/PULL_REQUEST_TEMPLATE.md`, **pre-merge checklist only**. Move post-deploy items
(joinability probe run, A/B before/after report, flag-flip) into a Linear comment for master. Push the
branch. **Do not merge / deploy / flip the flag / close the ticket / edit MASTER_PLAN.**

## Risk / halt notes

- One phase = one PR — satisfied (this is the single Phase-3 PR).
- No historical-row drop, no schema change, no migration.
- If `make mypy` shows >5 pre-existing errors I didn't introduce → surface, don't fix here.
- Multi-phase parent FRE-476 stays **In Progress** until this ships (master's call on merge).
