# FRE-879 — ADR-0118 T1: extract `artifact_builder` role (cost lane + telemetry identity)

**Ticket:** FRE-879 (Approved) · **ADR:** docs/architecture_decisions/ADR-0118-artifact-builder-model-selection.md, Decision §1 (sequencing item 1 of 5) · **Branch:** `fre-879-artifact-builder-role-cost-lane`

## Scope (from ticket + ADR Decision §1)

`artifact_draft` (the sole real artifact builder, ADR-0077) currently borrows the `sub_agent`
role wholesale: `get_llm_client(role_name="sub_agent")` + `respond(role=ModelRole.SUB_AGENT)`.
This means artifact spend is invisible inside `main_inference`'s cost lane and artifact
telemetry is indistinguishable from generic sub-agent calls. This ticket extracts a first-class
`artifact_builder` role with its own cost lane and telemetry identity — **no user-visible
behaviour change** (still resolves to Haiku on the cloud profile). Everything past this
foundation (candidate registry, DecisionCard selection, settings UI) is out of scope — deferred
to ADR-0118 sequencing items 2–5.

**Owner-confirmed budget policy (asked 2026-07-16):** mirror `main_inference`'s baseline —
daily cap **$5.00**, weekly cap **$18.00**, `on_denial: raise`.

## Acceptance criteria (this ticket's slice, from the Linear ticket body)

- **AC-1 slice** — an artifact build emits `MODEL_CALL_COMPLETED` with `role=artifact_builder`
  and `model=claude_haiku` (ES / route-trace on the `artifact_draft` span).
- **AC-2** — (a) unit: `budget_role_for("artifact_builder") == "artifact_builder"` (not
  `main_inference`); (b) integration: a default build's cost-gate reservation debits the
  `artifact_builder` `budget_counters` row and leaves `main_inference` untouched.

## Key research finding (changes the ticket's literal file list)

`get_llm_client(role_name=...)` (factory.py:56) resolves `role_name` via
`config/profile.py:resolve_model_key()` — which only special-cases `"primary"`/`"sub_agent"`
and otherwise looks the name up **directly as a key in `models.yaml`**. It does **not** consult
`config/model_roles.yaml`. So `get_llm_client(role_name="artifact_builder")` would silently fall
through to `LocalLLMClient()` (no `artifact_builder` key exists in `models.yaml`) — wrong
provider, and `budget_role_for("artifact_builder")` would need a `_BUDGET_ROLE_BY_FACTORY_NAME`
entry to not silently default to `main_inference`.

Every existing ADR-0099 matrix role (`entity_extraction`, `captains_log`, `insights`,
`compressor`) instead uses the established two-step pattern (e.g.
`second_brain/entity_extraction.py:715` and `orchestrator/context_compressor.py:152,176`):

```python
role_key = resolve_role_model_key("artifact_builder")   # config/model_loader.py — matrix-driven
client = get_llm_client_for_key(role_key, budget_role="artifact_builder")  # factory.py:125
```

This ticket's call-site change at `artifact_tools.py:1436-1437` uses this same established
pattern — matching the ADR's own part-1 text ("wiring only the cost side would still emit
`role sub_agent`", i.e. the cost side *is* wired here) while stopping short of ADR-0118 part 4's
full "resolved builder key from the DecisionCard" wiring (still deferred).

## Codex plan review (2026-07-16)

Reviewed via `codex:rescue` before implementation. Findings incorporated below:

- **Confirmed** the call-site change (`get_llm_client` → `resolve_role_model_key` +
  `get_llm_client_for_key`) is required and correct — matches the ADR's own part-1 text and the
  established pattern for every other matrix role; no lighter-weight alternative exists.
- **Confirmed** scoping to default-resolution wiring only (no registry/DecisionCard) is safe —
  the ADR's own sequencing treats `get_llm_client_for_key(builder_key, budget_role=...)` as
  correct once a resolved key is in hand regardless of where the key comes from; the later seam
  ticket only changes the key source, not the client API. No rework risk.
- **Changed**: the AC-2b integration test must NOT delete real shared-role `budget_counters`
  rows by role name (that would destroy other legitimate state, since `reserve()`/`commit()` also
  touch the shared weekly `_total` row). Redesigned below to snapshot-and-restore exact
  `running_total` values for the real `artifact_builder`/`main_inference` rows (mirroring the
  existing `_total`-row snapshot/restore pattern already used in
  `test_litellm_gate_wiring.py`'s `_cleanup_after` fixture), and to delete only this test's own
  `budget_reservations` row by its own `reservation_id` (never by role name).
- **Added** three test files the original ticket bullet list missed, found by tracing existing
  coverage that hardcodes role counts/lists:
  - `tests/test_llm_client/test_types.py:23-26` — `test_model_role_exactly_three_members` asserts
    `len(list(ModelRole)) == 3`; must bump to 4 and assert
    `ModelRole.ARTIFACT_BUILDER == "artifact_builder"`.
  - `tests/personal_agent/config/test_model_loader_roles.py:36-45` — add
    `("artifact_builder", "claude_haiku")` to `TestForbiddenRolesResolveToTheAllValue`'s
    parametrize list.
  - `tests/personal_agent/config/test_role_resolution_golden.py:27-34` — add `"artifact_builder"`
    to the `_FORBIDDEN_ROLES` tuple so `TestForbiddenRolesResolveIdenticallyAcrossProfiles` covers
    it (same-key-and-definition-across-profiles check). Not adding a bespoke
    `TestConsumerRaisesWhenMatrixMissing` case for `artifact_builder` — that pattern is specific
    to `entity_extraction`'s own prior ticket requirement, not a general golden-test obligation,
    and no other forbidden role (e.g. `embedding`, `reranker`) has one either.
- **Clarified**: the $5.00/day, $18.00/week caps are the owner's explicit 2026-07-16 confirmation
  of "mirror main_inference's baseline" — i.e. the ADR-0065 non-temp-bumped baseline, not a
  mechanical copy of `budget.yaml`'s current temp-bumped `main_inference` values ($15/$40, flagged
  in that file as a temporary eval-run bump). The YAML comments below say this explicitly.
- **Noted, not actioned**: `docs/plans/MASTER_PLAN.md` reconciliation for ADR-0118 status is
  master's process concern at the merge gate, not part of this code change.

## Steps

1. **`src/personal_agent/llm_client/types.py`** — add `ARTIFACT_BUILDER = "artifact_builder"` to
   the `ModelRole` enum (after `COMPRESSOR`). No other changes to the file.

2. **`config/model_roles.yaml`** — add a new declared row under `roles:`, after
   `reranker_fallback`:
   ```yaml
   artifact_builder:   { divergence: forbidden, all: claude_haiku }  # ADR-0118 T1 (FRE-879)
   ```
   `claude_haiku` is already defined in both `models.yaml` and `models.cloud.yaml`, so this
   passes the existing `check_forbidden_role_divergence_and_dangling_refs` guard with no other
   changes.

3. **`config/governance/budget.yaml`** — add under `roles:` (mirrors `main_inference`'s
   `RoleConfig` values per owner confirmation):
   ```yaml
   artifact_builder:
     default_output_tokens: 1024
     safety_factor: 1.2
     on_denial: raise        # user-facing build path — surface failures like main_inference
   ```
   and under `caps:`:
   ```yaml
   - {time_window: daily,  role: artifact_builder, cap_usd: 5.00}   # ADR-0118 T1 (FRE-879) — mirrors main_inference baseline, owner-confirmed 2026-07-16
   - {time_window: weekly, role: artifact_builder, cap_usd: 18.00}  # ADR-0118 T1 (FRE-879) — mirrors main_inference baseline, owner-confirmed 2026-07-16
   ```
   (`default_output_tokens: 1024` mirrors `main_inference`'s pre-check estimator input; the real
   `artifact_draft` call always passes an explicit `max_tokens=32768` override, so this value
   only matters as the estimator's `min(max_tokens, default_output_tokens)` ceiling for any
   future caller that omits `max_tokens` — same relationship `main_inference` already has.)

4. **`src/personal_agent/cost_gate/__init__.py`** — add to `_BUDGET_ROLE_BY_FACTORY_NAME`
   (`artifact_builder → artifact_builder`, own lane, not `main_inference`):
   ```python
   "artifact_builder": "artifact_builder",
   ```
   Placed as its own entry (not under "Executor / orchestrator roles" or "Background consumers"
   — it's neither; add a one-line grouping comment).

5. **`src/personal_agent/tools/artifact_tools.py`** (`artifact_draft_executor`, lines
   ~1386–1477):
   - Swap the deferred import at line 1389 from `get_llm_client` to `get_llm_client_for_key`,
     and add `from personal_agent.config.model_loader import resolve_role_model_key`.
   - Replace line 1436-1437:
     ```python
     # --- Acquire sub-agent client (profile-driven: D2) ---
     sub_agent_client = get_llm_client(role_name="sub_agent")
     ```
     with:
     ```python
     # --- Acquire artifact-builder client (matrix-resolved: ADR-0099, ADR-0118 T1) ---
     builder_model_key = resolve_role_model_key("artifact_builder")
     sub_agent_client = get_llm_client_for_key(builder_model_key, budget_role="artifact_builder")
     ```
     (Keep the `sub_agent_client` variable name and all `sub_agent_*` log-event/field names
     unchanged — renaming those is out of this ticket's scope per the ticket's own file list,
     which names only the two identity switches below.)
   - Line ~1461: `model_role=ModelRole.SUB_AGENT.value` → `model_role=ModelRole.ARTIFACT_BUILDER.value`.
   - Line ~1470: `role=ModelRole.SUB_AGENT` → `role=ModelRole.ARTIFACT_BUILDER`.

6. **Tests — `tests/personal_agent/tools/test_artifact_tools.py`**:
   - Update the module docstring line 13 (`get_llm_client` → `get_llm_client_for_key`, note
     ADR-0118 T1).
   - `_install_draft_fakes` (line 812) and the two other inline patches (lines 1427, 1454):
     change `monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client", lambda
     role_name="primary": client)` to patch `get_llm_client_for_key` instead, with a signature
     matching the real one: `lambda model_key, budget_role="skill_routing": client`.
   - Add a new assertion test `test_artifact_draft_uses_artifact_builder_role` (near the existing
     happy-path tests, ~line 886): after a draft call, assert
     `client.respond_calls[0]["role"] == ModelRole.ARTIFACT_BUILDER` (AC-1 slice, unit level).
   - Add a test asserting the `model_role` log field: extend/add a test using `_spy_artifact_log`
     (pattern at line 846) asserting the `artifact_draft_sub_agent_start` event's
     `model_role == "artifact_builder"`.

7. **Tests — new unit test, `tests/personal_agent/cost_gate/test_budget_role_for.py`** (no
   existing file covers `budget_role_for` directly — confirmed via research):
   ```python
   from personal_agent.cost_gate import budget_role_for

   def test_artifact_builder_has_own_lane() -> None:
       assert budget_role_for("artifact_builder") == "artifact_builder"
       assert budget_role_for("artifact_builder") != "main_inference"
   ```
   Pure function test, no DB, no `integration` marker.

8. **Tests — integration, extend `tests/personal_agent/llm_client/test_litellm_gate_wiring.py`**
   (AC-2b, the "leaves `main_inference` untouched" claim). New test
   `test_artifact_builder_lane_isolated_from_main_inference`:
   - Uses the **real** `config/model_roles.yaml` + `config/governance/budget.yaml` (does **not**
     mock `load_budget_config` — this is the one test in the file proving the *real* declared
     policy, not a synthetic role) so a regression that removes the YAML rows fails this test too.
   - Snapshots the real `artifact_builder` and `main_inference` daily `budget_counters` rows
     (`running_total`) before the test (may not exist yet → treat missing as `None`, matching the
     existing `_counter_total`/`_cleanup_after` helpers' None-handling convention).
   - Resolves `resolve_role_model_key("artifact_builder")` for real, builds a `LiteLLMClient` via
     `get_llm_client_for_key(key, budget_role="artifact_builder")`.
   - Mocks only `litellm.acompletion` + `litellm.completion_cost` (same pattern as the existing
     three tests in this file) and calls `.respond(role=ModelRole.ARTIFACT_BUILDER, ...)`.
   - Asserts: `artifact_builder` daily counter increased by the reservation amount;
     `main_inference` daily counter delta is exactly `0`.
   - **Cleanup (snapshot-and-restore, never delete-by-role)**: delete only this test's own
     `budget_reservations` row by its captured `reservation_id`. For `budget_counters`, restore
     the exact pre-test `running_total` for `artifact_builder` (daily), `main_inference` (daily),
     and `_total` (weekly) via `UPDATE ... SET running_total = $pre_value` — or `DELETE` only if
     the pre-test snapshot was `None` (row didn't exist before this test created it). This never
     touches `main_inference`'s or `_total`'s pre-existing state, only reverts the delta this test
     itself introduced — mirroring the existing `_total`-row handling already in this file's
     `_cleanup_after` fixture, applied to all three rows instead of just `_total`.

9. **Tests — bump hardcoded role-count/list assertions** (found via codex review, not in the
   original ticket bullet list):
   - `tests/test_llm_client/test_types.py:23-26` — `test_model_role_exactly_three_members`:
     change `len(list(ModelRole)) == 3` → `== 4`; add
     `assert ModelRole.ARTIFACT_BUILDER == "artifact_builder"`. Rename the test to
     `test_model_role_exactly_four_members` (the name asserts the count; leaving it stale would
     mislead the next reader) and update its docstring.
   - `tests/personal_agent/config/test_model_loader_roles.py:36-45` — add
     `("artifact_builder", "claude_haiku")` to `TestForbiddenRolesResolveToTheAllValue`'s
     parametrize list.
   - `tests/personal_agent/config/test_role_resolution_golden.py:27-34` — add
     `"artifact_builder"` to the `_FORBIDDEN_ROLES` tuple.

## Explicitly out of scope (deferred to ADR-0118 sequencing items 2–5)

- Candidate registry (`artifact_builder_candidates`), onboarding metadata, large-output
  validation.
- DecisionCard / constraint-pause wiring (`constraint_options.py`, `executor.py`,
  `transport/events.py`, `service/app.py` settings API).
- Resolving the builder key from a user selection instead of always the matrix default.
- PWA settings surface.

## Verification

```bash
make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py
make test-file FILE=tests/personal_agent/cost_gate/test_budget_role_for.py
make test  # full unit suite
NEO4J_PASSWORD=... NEO4J_USER=... pytest tests/personal_agent/llm_client/test_litellm_gate_wiring.py -m integration  # needs test-substrate Postgres (make test-infra-up)
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Risk tier

**Standard** — touches `src/` logic (`llm_client/types.py`, `cost_gate/__init__.py`,
`tools/artifact_tools.py`), cost-gate config, and implements an ADR decision section. Codex
plan-review required before implementation per the build skill.
