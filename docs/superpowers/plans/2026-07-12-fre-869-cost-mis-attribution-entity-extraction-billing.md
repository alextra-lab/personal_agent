# FRE-869 — Cost mis-attribution: entity-extraction classification bills main_inference, not entity_extraction

## Ticket
https://linear.app/frenchforest/issue/FRE-869 — Approved, Tier-2:Sonnet.
Standalone bug fix (ADR-0065 Cost Gate governance bug), no backing ADR — the ticket text itself is
the acceptance-criteria source.

## Root cause (confirmed by reading the code)

`get_llm_client(role_name: str)` (`src/personal_agent/llm_client/factory.py`) does two things with
its single `role_name` argument:

1. Resolves a model definition: `resolve_model_key(role_name)` (profile override, else pass-through)
   → `config.models.get(resolved_key)`. This works whether `role_name` is a literal factory role
   (`"primary"`) or an already-resolved model key (`"gpt-5.4-mini"`), since `config.models` is keyed
   by model key and unrecognized strings pass through `resolve_model_key` unchanged.
2. Derives the budget-gate role: `budget_role_for(role_name)` (`cost_gate/__init__.py`). This ONLY
   works when `role_name` is one of the literal factory names in `_BUDGET_ROLE_BY_FACTORY_NAME`
   (`"primary"`, `"captains_log"`, `"entity_extraction"`, …). It has no knowledge of
   `config/model_roles.yaml`'s role matrix (ADR-0099 D1).

Call sites that resolve a role through `resolve_role_model_key(role)` — the ADR-0099 matrix, the
*authoritative* resolution path — get back a resolved **model key**, then pass that model key into
`get_llm_client(role_name=<model key>)`. Since the model key isn't in
`_BUDGET_ROLE_BY_FACTORY_NAME`, `budget_role_for` silently falls back to `"main_inference"`,
misattributing the spend. The intended cap (`entity_extraction`: $5/day+$7/week, `captains_log`:
$2.50/day) is never charged against; `main_inference` ($15/day) silently absorbs it instead.

## Confirmed affected call sites

Grepped every `resolve_role_model_key` caller repo-wide and checked which feed the result into
`get_llm_client`:

| # | File:line | Role | In ticket? | Status |
|---|---|---|---|---|
| 1 | `second_brain/entity_extraction.py:751` | `entity_extraction` | yes | live bug |
| 2 | `scripts/migrate_fre772_entity_type_v2.py:472` | `entity_extraction` | yes | live bug |
| 3 | `captains_log/feedback.py:125` (`_feedback_llm_complete`), called from `handle_deepen` (L254/265) | `insights` | no | live bug — same class |
| 3b | same function, called from `handle_too_vague` (L301/307) | `captains_log` | no | live bug — same class |
| 4 | `second_brain/session_summary.py:152` | `captains_log` | no | live bug — same class |
| 5 | `orchestrator/context_compressor.py:176` | `compressor` | no | **latent, not live** — `_BUDGET_ROLE_BY_FACTORY_NAME["compressor"]` already equals `"main_inference"`, the same value the unmatched-key fallback produces today. Fixing it is a 1-line robustness improvement (a future change to compressor's intended lane wouldn't silently break), zero current cost impact. |

`consolidator.py` — the ticket names this as an affected call site, but it does **not** call
`get_llm_client`; it only resolves `entity_extraction_role` for logging/tagging (`extractor_model`
metadata written onto the `:Entity` node at consolidator.py:246/727). No code change needed there —
will note this explicitly in the PR/handoff so it isn't read as an unaddressed gap.

**Scope call**: fixing the shared factory function makes the fix "free" at every call site that uses
the same buggy pattern. Leaving 3 of 5 real live instances of the identical bug unfixed after
touching the mechanism would be an inconsistent fix a reviewer would reasonably bounce. Folding all 5
in per Step 5 ("meet the objective... reasonable deviations you discover while building are part of
meeting it") — flagged here explicitly for owner visibility before coding, since it's cost-governance
and materially larger than the ticket's literal text.

## Fix (as implemented — see "Post-implementation code-review finding" below for how this changed
from the original plan)

Route every call site that holds a *resolved model key* (from `resolve_role_model_key`, the
ADR-0099 matrix) through the pre-existing `get_llm_client_for_key(model_key, budget_role=...)`
instead of `get_llm_client(role_name=...)`. `get_llm_client_for_key` already takes the budget role
explicitly and bypasses profile resolution (correct here, since `resolve_role_model_key` already
accounts for the active profile) — no change to `get_llm_client`/`budget_role_for` at all.
`get_llm_client` continues to serve callers that pass a literal factory role name
(`"primary"`, `"router"`, etc.) exactly as before.

### 1. `src/personal_agent/second_brain/entity_extraction.py:751`
`get_llm_client_for_key(entity_extraction_role, budget_role="entity_extraction")`

### 2. `scripts/migrate_fre772_entity_type_v2.py:472`
`get_llm_client_for_key(role, budget_role="entity_extraction")`

### 3. `src/personal_agent/captains_log/feedback.py`
- `_feedback_llm_complete(role_key: str, system: str, user: str, *, budget_role: str)` — add the
  param, thread to `get_llm_client_for_key(role_key, budget_role=budget_role)`.
- `handle_deepen` call site: `_feedback_llm_complete(role_key, system, user, budget_role="insights")`.
- `handle_too_vague` call site: `_feedback_llm_complete(role_key, system, user, budget_role="captains_log")`.

### 4. `src/personal_agent/second_brain/session_summary.py:152`
`get_llm_client_for_key(role_name, budget_role="captains_log")`

### 5. `src/personal_agent/orchestrator/context_compressor.py:176`
`get_llm_client_for_key(compressor_role, budget_role="main_inference")`

## Tests (TDD — failing first)

New module: `tests/personal_agent/llm_client/test_factory_budget_role.py`
- `test_explicit_budget_role_overrides_role_name_lookup` — `get_llm_client(role_name="gpt-5.4-mini",
  budget_role="entity_extraction")` against a mocked cloud `model_def`; assert the constructed
  `LiteLLMClient`'s `budget_role` kwarg is `"entity_extraction"`, not
  `budget_role_for("gpt-5.4-mini")`'s default (`"main_inference"`).
- `test_no_budget_role_falls_back_to_budget_role_for` — no `budget_role` passed, `role_name="captains_log"`
  → still resolves via `budget_role_for` (backward-compat proof).
- `test_ambiguous_model_key_cannot_be_disambiguated_without_explicit_budget_role` (codex finding) —
  `config/model_roles.yaml` resolves both `captains_log` and `insights` to the same model key
  (`claude_sonnet`), so a `budget_role_for(model_key)` fallback fix could never disambiguate them;
  this regression test proves `budget_role="insights"` vs `budget_role="captains_log"` passed
  explicitly for the *same* `role_name="claude_sonnet"` yields two different `LiteLLMClient`
  `budget_role` kwargs — the reason the fix must live at the caller/explicit-param level, not inside
  `budget_role_for`.

Extend existing call-site tests (the acceptance-criterion-bearing tests). Codex flagged that several
existing tests patch `get_llm_client` with kwargs-swallowing lambdas (e.g.
`test_session_summary.py:86`, `lambda **kwargs: client`) — replace those with a capturing
`Mock`/`AsyncMock` so `budget_role` is actually asserted, not silently discarded:
- `tests/test_second_brain/test_entity_extraction_contract.py::TestCloudPathTemperature` — assert
  `mock_get_client.call_args.kwargs["budget_role"] == "entity_extraction"`.
- `tests/scripts/test_migrate_fre772.py` — add a focused unit test of `_build_llm_batch_classifier`
  (currently only exercised indirectly, mocked away in the existing integration-style test) asserting
  it passes `budget_role="entity_extraction"` to `get_llm_client`.
- `tests/test_captains_log/test_feedback_loop.py` — add handler-level tests for `handle_deepen` and
  `handle_too_vague` (codex: assert at the branch points, not just the shared helper) proving
  `budget_role="insights"` / `budget_role="captains_log"` respectively reach `get_llm_client`.
- `tests/personal_agent/second_brain/test_session_summary.py` — extend (with a capturing mock, not a
  kwargs-swallowing lambda) to assert `factory.get_llm_client` receives `budget_role="captains_log"`.
- `tests/test_orchestrator/test_context_compressor.py` — extend to assert `budget_role="main_inference"`
  is passed explicitly. Per codex, this call site is hardening (zero current cost impact, `compressor`
  already falls back to `main_inference` correctly today) rather than AC-critical — included for
  consistency since the mechanism fix makes it free, not because it fixes a live bug.

## Acceptance criteria (ticket is the AC source — no backing ADR)

1. Entity-extraction classification spend is charged to the `entity_extraction` budget role (not
   `main_inference`) at both production/migration call sites — proven by the extended
   `test_entity_extraction_contract.py` and `test_migrate_fre772.py` tests asserting
   `budget_role="entity_extraction"` reaches `get_llm_client`/`LiteLLMClient`.
2. The role-vs-model-key semantics are corrected in the shared factory (`get_llm_client` +
   `budget_role_for`), not per-call-site copy-pasted workarounds — proven by the new
   `test_factory_budget_role.py` tests.
3. No regression to existing correctly-attributed callers — proven by the backward-compat factory
   test + full `make test` pass.
4. (Fold-in, same bug class) `captains_log` insights/refinement spend and session-summary spend are
   also charged to their correct budget roles, not `main_inference` — proven by the extended
   `test_feedback_loop.py` and `test_session_summary.py` tests.

## Quality gates
`make test` (module: `make test-file FILE=tests/personal_agent/llm_client/test_factory_budget_role.py`,
then full) · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Risk tier: Standard/Complex
Touches `src/` cost-governance logic (ADR-0065 Cost Gate budget attribution) across 4 production
modules + 1 migration script. **Codex plan-review required** before coding.

## Codex plan-review (ran before coding)
No blocking issues. Confirmed: root-cause diagnosis correct; model-key→budget-role cannot live inside
`budget_role_for` because model keys are not 1:1 with budget roles (`captains_log` and `insights` both
resolve to `claude_sonnet`); `budget_role` param design is backward-compatible and mirrors
`get_llm_client_for_key`; no additional buggy call sites found beyond the 6 already listed; ruled out
`consolidator.py` (logging only), `migrate_fre865_entity_class_backfill.py` (already works around the
bug directly), `scripts/study/categorizer.py` (already passes `budget_role="study"` explicitly).
Findings folded into the Tests section above (ambiguous-model-key regression test, capturing-mock
fix for kwargs-swallowing lambdas, handler-level feedback.py tests,
`context_compressor.py` reframed as hardening not AC-critical).

## Post-implementation code-review finding — the fix was reworked

After implementing the plan above verbatim (new `budget_role` param on `get_llm_client`) and getting
all tests green, the Step-8 self-review (`code-review --effort high`, workflow-backed) surfaced a
CONFIRMED finding codex's plan-review had not caught: `get_llm_client_for_key(model_key,
budget_role=...)` (same file, pre-existing, already used by `orchestrator/executor.py`) already does
exactly what all 5 call sites need — takes a resolved model key + an explicit budget role — and does
it *more safely*: it raises `ValueError` on an unknown key, whereas `get_llm_client` silently falls
back to `LocalLLMClient` when `model_def` is `None`. Verified independently: every one of the 5 call
sites' `role`/`role_key`/`role_name` value is guaranteed present in `config.models` before the call
(either by `resolve_role_model_key`'s own no-fallback contract, which raises `ModelRoleError` on an
unresolvable key, or by an already-successful `config.models.get(...)` lookup earlier in the same
function) — so switching to `get_llm_client_for_key` is a behavior-preserving simplification for valid
inputs, plus a real robustness fix for the invalid-input case.

Reworked: reverted `get_llm_client`'s signature/docstring to the original (no `budget_role` param —
would have been dead, unused surface area once all 6 call sites moved to
`get_llm_client_for_key`), pointed all 5 real call sites + the `context_compressor.py` hardening
site at `get_llm_client_for_key` instead, updated `get_llm_client`'s and `get_llm_client_for_key`'s
docstrings to cross-reference which one to use, and updated every test's patch target and call-args
assertions to match (`test_factory_budget_role.py`'s three tests were removed — they existed only to
prove `get_llm_client`'s now-reverted `budget_role` param, and `get_llm_client_for_key` already has
its own dedicated unknown-key test at `tests/personal_agent/orchestrator/test_route_skills.py`). All
102 touched-module tests and the full `make test` suite re-verified green after the rework.
