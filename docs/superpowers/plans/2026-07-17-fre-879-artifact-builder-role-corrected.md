# FRE-879 — ADR-0118 T1 (corrected): extract `artifact_builder` role, ExecutionProfile-resolved

**Backing ADR:** ADR-0119 (config-management interface), Sequencing step 0. Supersedes ADR-0118 §1
on the resolution seam only — the cost-lane / telemetry / registry work from ADR-0118 is reused.

## Why this is a re-do

The first cut of FRE-879 (branch `origin/fre-879-artifact-builder-role-cost-lane`, +466, parked)
made `artifact_builder` a flat `divergence: forbidden` row in `config/model_roles.yaml`
(`all: claude_haiku`). That row ignores the session's active `ExecutionProfile`, so a local-profile
build would silently route to cloud Haiku — cost, or a hard-fail with no `ANTHROPIC_API_KEY`. Caught
by code review, corrected in ADR-0119's 2026-07-16 amendment: `artifact_builder` joins `primary` /
`sub_agent` as an **open role**, resolved via `ExecutionProfile` (`resolve_model_key`,
`src/personal_agent/config/profile.py:75`), never the matrix. AC-8 is the regression guard.

**Reused as-is from the parked WIP:** the cost lane (`budget.yaml` + `_BUDGET_ROLE_BY_FACTORY_NAME`),
the `ModelRole.ARTIFACT_BUILDER` enum member, and the telemetry-identity switch in
`artifact_tools.py` (`respond(role=...)` + the `model_role` log field). **Not reused:** the
`config/model_roles.yaml` matrix row and everything downstream of it (`resolve_role_model_key`,
`get_llm_client_for_key`) — replaced with the `ExecutionProfile` seam below.

## Scope

1. `ModelRole.ARTIFACT_BUILDER` enum value.
2. `artifact_builder` cost lane (own `budget_counters` row, not `main_inference`).
3. `ExecutionProfile.artifact_builder_model` + `resolve_model_key` extension — the corrected
   resolution seam. Local profile binds to the local `sub_agent` model key; cloud profile binds to
   `claude_haiku` (no user-visible change on cloud).
4. `artifact_tools.py`'s `artifact_draft_executor` switches off `sub_agent` onto `artifact_builder`
   for both client acquisition and telemetry identity.

**Out of scope (deferred to FRE-880 / ADR-0119 step 1+):** the vetted candidate registry, the
override store, the config read/write API, PWA surface.

## Files touched

| File | Change |
|---|---|
| `src/personal_agent/llm_client/types.py` | `ARTIFACT_BUILDER = "artifact_builder"` |
| `config/governance/budget.yaml` | `roles.artifact_builder` policy + daily $5.00 / weekly $18.00 caps (owner-confirmed 2026-07-17, mirrors `main_inference`'s non-temp-bumped baseline — do not infer a different value) |
| `src/personal_agent/cost_gate/__init__.py` | `_BUDGET_ROLE_BY_FACTORY_NAME["artifact_builder"] = "artifact_builder"` |
| `src/personal_agent/config/profile.py` | `ExecutionProfile.artifact_builder_model: str \| None = None` field; `resolve_model_key` gains an `artifact_builder` branch alongside `primary`/`sub_agent` (fail-loud, see below); docstrings on both updated |
| `config/profiles/local.yaml` | `artifact_builder_model: sub_agent` |
| `config/profiles/cloud.yaml` | `artifact_builder_model: claude_haiku` |
| `src/personal_agent/tools/artifact_tools.py` | `get_llm_client(role_name="artifact_builder")` replaces `get_llm_client(role_name="sub_agent")`; `ModelRole.ARTIFACT_BUILDER` in both the `respond(role=...)` call and the `model_role` log field |

**Deliberately NOT touched:** `config/model_roles.yaml` (no `artifact_builder` matrix row — that is
the regression this ticket corrects) and its two golden-config tests
(`test_model_loader_roles.py`, `test_role_resolution_golden.py`).

## Why `artifact_builder_model` is optional (`str | None = None`), not required — and why the
## resolver still fails loud (codex plan-review correction)

`primary_model`/`sub_agent_model` are required fields on `ExecutionProfile`, but 21+ inline
`ExecutionProfile(...)` constructions across `tests/test_orchestrator/test_executor.py` and
`test_routing.py` don't care about artifact builds. Making the new field required would force
touching all of them for no behavioral reason — outside this ticket's blast radius. The field stays
optional on the Pydantic model.

**Codex plan-review caught a footgun in the original draft:** a plain truthy guard
(`if role_name == "artifact_builder" and profile.artifact_builder_model: ... ; return role_name`)
means an *active* profile that omits the binding silently falls through to the bare string
`"artifact_builder"`, which is not a key in either `models.yaml`/`models.cloud.yaml` — and
`get_llm_client` treats an unresolved model key as local (`model_def is None` → falls through to
`LocalLLMClient()`). A cloud profile missing the binding would silently run local instead of Haiku —
a new asymmetric-fallback bug in the same family as the one this ticket fixes, just undetected because
the two real profile YAMLs both set the field in this PR.

**Fix:** keep the field optional (no blast radius to unrelated tests), but `resolve_model_key` fails
loud instead of passing through when an *active* profile is missing the `artifact_builder` binding —
consistent with this codebase's existing "no silent fallback" convention (ADR-0099 D1's
`ModelRoleError`). When no profile is active at all, `artifact_builder` still passes through
unchanged, identical to `primary`/`sub_agent`/`compressor`'s existing no-profile behavior — that path
is untouched, only "active profile, binding missing" now raises:

```python
if role_name == "artifact_builder":
    if profile.artifact_builder_model:
        return profile.artifact_builder_model
    raise ValueError(
        f"ExecutionProfile {profile.name!r} has no artifact_builder_model binding "
        "(ADR-0119 AC-8 — no silent fallback for an open role)."
    )
```

None of the 21+ existing inline `ExecutionProfile(...)` test constructions call
`resolve_model_key(role_name="artifact_builder")`, so none of them break.

## Tests (TDD — failing first)

1. **`tests/test_llm_client/test_types.py`** — `ModelRole` has 4 members incl. `ARTIFACT_BUILDER`.
2. **`tests/personal_agent/cost_gate/test_budget_role_for.py`** (new) — `budget_role_for("artifact_builder") == "artifact_builder"`, `!= "main_inference"`.
3. **`tests/test_config/test_profile.py`** — add:
   - `test_redirects_artifact_builder_via_active_profile` (mirrors the existing `sub_agent` test)
   - extend `test_returns_role_name_without_active_profile` / `test_does_not_redirect_compressor_role` coverage to include `artifact_builder`
   - `test_real_profiles_artifact_builder_binding` — loads the **real** `config/profiles/{local,cloud}.yaml`, asserts `local_profile.artifact_builder_model == "sub_agent"` and `cloud_profile.artifact_builder_model == "claude_haiku"` (proves the actual shipped config, not just the Pydantic model).
4. **`tests/test_llm_client/test_factory_artifact_builder.py`** (new) — **AC-8 regression guard**:
   under an active **local** `ExecutionProfile`, `get_llm_client(role_name="artifact_builder")`
   returns a `LocalLLMClient`; under **cloud**, returns a `LiteLLMClient` with `budget_role ==
   "artifact_builder"` **and** `model_id` matching the real `claude_haiku` model definition (not just
   the client type). This is the direct proof a local session never crosses to cloud Haiku.
5. **`tests/test_config/test_profile.py`** (added to item 3's file) — `test_active_profile_missing_artifact_builder_binding_raises`:
   an active profile with `artifact_builder_model=None` raises `ValueError` on
   `resolve_model_key("artifact_builder")` — proof of the codex-caught fail-loud fix above.
6. **`tests/personal_agent/config/test_model_loader_roles.py`** (add one test, do not touch the
   existing parametrized cases) — `test_artifact_builder_is_not_matrix_resolved`: `resolve_role_model_key("artifact_builder")`
   raises `ModelRoleError` because `config/model_roles.yaml` declares no such role. Direct proof
   `artifact_builder` stays off the matrix (the exact property distinguishing this corrected cut
   from the parked WIP).
7. **`tests/personal_agent/tools/test_artifact_tools.py`** — AC-1 slice:
   - `test_artifact_draft_uses_artifact_builder_role` — `respond()` called with `role=ModelRole.ARTIFACT_BUILDER`.
   - `test_artifact_draft_start_log_reports_artifact_builder_role` — the `artifact_draft_sub_agent_start` log's `model_role` field is `"artifact_builder"`.
   - `_install_draft_fakes` mocks `personal_agent.llm_client.factory.get_llm_client` (not `get_llm_client_for_key` — that call site no longer exists after this seam change).
8. **`tests/personal_agent/llm_client/test_litellm_gate_wiring.py`** — ticket AC-2 integration test:
   a real `CostGate` call tagged `budget_role="artifact_builder"` debits the `artifact_builder`
   `budget_counters` row and leaves `main_inference` untouched (adapted from the parked WIP's
   `test_artifact_builder_lane_isolated_from_main_inference`, using `get_llm_client_for_key("claude_haiku",
   budget_role="artifact_builder")` directly rather than the retired matrix resolution).

**Note on `get_llm_client_for_key`:** codex confirmed it stays live (callers in
`orchestrator/context_compressor.py`, `orchestrator/executor.py` ×2, `second_brain/entity_extraction.py`,
`captains_log/feedback.py`, `second_brain/session_summary.py`) — item 8 above is a legitimate direct
use, not a call into now-dead code.

## Acceptance criteria → proof

These are the ticket's own AC numbering (FRE-879 body), a strict subset of ADR-0119's full AC list —
**not** identical to ADR-0119 AC-1/AC-2 (which cover the override store and pinned-writer guardrail,
both out of scope here / deferred to FRE-888 step 1). Naming collision noted so master's gate doesn't
read this PR as proving the ADR's AC-2.

- **Ticket AC-1 slice** (telemetry role): tests in item 7; live proof at PR handoff via ES
  `MODEL_CALL_COMPLETED` role field.
- **Ticket AC-2** (own cost lane, `budget_role_for` correctness): items 2 + 8.
- **Ticket AC-8** (local-profile regression guard, = ADR-0119 AC-8): items 3, 4, 5, 6 together — the
  real-config binding, the factory dispatch proof, the fail-loud guard, and the not-matrix-resolved
  guard.

## Risk tier

**Standard** — touches `src/` logic (cost gate, ExecutionProfile resolution, telemetry identity) and
implements a piece of an Accepted ADR (0119 step 0). Codex plan-review required before coding.
