# FRE-1317 — Repoint primary + sub_agent to qwen3.8-flash-next

**Risk tier:** Standard (config-only, but changes live primary/sub_agent routing and cost —
codex plan-review required per build skill Step 3).

## Context

Live outage: the owner swapped the SLM host to serve only `unsloth/qwen3.8-flash-next`. The
catalog still points `primary` and `sub_agent` at `unsloth/qwen3.6-35-A3B` /
`unsloth/qwen3.6-35-A3B-subagent`, both now `"configured but currently disabled"` on the host.
Every primary-routed turn and every HYBRID sub-agent call fails. Master already worked out and
structurally validated (`check_config: clean`) the exact diff below, then reverted it because
`config/` bakes into the gateway image (no uncommitted deploy) and it breaks 7 name-pinned tests.
This ticket ships that diff with the tests updated.

## Steps

1. **`config/models.yaml`** — add a new catalog key `qwen3.8-flash-next` (new key, not an
   id-swap under the old key — telemetry must not read "3.6" while serving a 3.8 preview).
   Insert after the `qwen3.6-35b-instruct` block, before `# ── Managed Qwen (OVH AI Endpoints)`.
   Fields exactly as specified in the ticket body (provider `slm_local`, id
   `unsloth/qwen3.8-flash-next`, temperature 1.0 / top_p 0.95 / top_k 20 / min_p 0.0 /
   presence_penalty 0.0 / repetition_penalty 1.0 — the card's Thinking Mode preset, NOT the old
   entry's tuned 0.6, per the ticket's explicit "sampling must not be carried over" instruction).
   Verify: `qwen3.6-35b-thinking` and `qwen3.6-35b-instruct` entries are untouched (AC-5).

2. **`config/model_roles.yaml`** — four edits:
   - `roles.primary` → `{ all: qwen3.8-flash-next }`
   - `bindings.primary` → `{ deployment: qwen3.8-flash-next, open: true }`
   - `bindings.sub_agent.deployment` → `qwen3.8-flash-next` (the field that actually resolves)
   - `bindings.sub_agent.defaults_by_primary` — add `qwen3.8-flash-next: qwen3.8-flash-next`
     (self-pair; must-define-on-add, per the existing pattern for every other kind:llm entry)
   Verify: `qwen3.6-35b-thinking`/`qwen3.6-35b-instruct` keys stay in `defaults_by_primary`
   unchanged — they remain valid `kind: llm` deployments (AC-5) so the must-define-on-add /
   dangling-value guard still needs an entry for them.

3. **Update the 7 pinned tests** (TDD: confirm each fails against the *pre-edit* repo state
   before step 1/2 land, i.e. write test edits and run once to see them fail, then land the
   config edits and re-run to confirm green):
   - `tests/personal_agent/config/test_check_config.py::TestRealRepoMatrixShape::test_real_repo_roles_declare_a_bare_all_value`
     — `roles["primary"] == {"all": "qwen3.8-flash-next"}`
   - `tests/personal_agent/config/test_model_loader_roles.py::TestRolesResolveToTheAllValue::test_resolves_to_declared_key[primary-...]`
     — parametrize value `("primary", "qwen3.8-flash-next")`
   - `tests/personal_agent/config/test_resolve_cli.py::TestResolve::test_resolve_primary_returns_qwen`
     — `resolve("primary") == "qwen3.8-flash-next"`
   - `tests/personal_agent/config/test_resolve_cli.py::TestResolve::test_resolve_sub_agent_returns_qwen_instruct`
     — body now asserts `qwen3.8-flash-next`; rename the test (it no longer returns the instruct
     model — a stale name would mislead the next reader) and its docstring to cite FRE-1317.
   - `tests/personal_agent/config/test_sub_agent_defaults_by_primary.py`:
     - `TestDefaultsByPrimarySeededThroughTheRealLoader::test_sub_agent_defaults_by_primary_matches_expected_map`
       — add `_QWEN38_FLASH = "qwen3.8-flash-next"` and its self-pair entry to
       `_EXPECTED_DEFAULTS_BY_PRIMARY`
     - `TestMigrationWindowLeavesFlatBindingOperative::test_deployment_and_open_are_unchanged`
       — `binding.deployment == _QWEN38_FLASH`
   - `tests/personal_agent/config/test_catalog_snapshot.py::test_catalog_behaviour_matches_golden`
     — re-baseline `catalog_snapshot_golden.json` via
     `python -m tests.personal_agent.config.test_catalog_snapshot --write`, then hand-diff the
     result to confirm the ONLY deltas are: `binding|primary` and `binding|sub_agent` resolving
     to the new definition; `runtime.concurrency.models` gaining a `qwen3.8-flash-next` entry
     (existing `qwen3.6-35b-thinking`/`qwen3.6-35b-instruct` entries stay — those deployments
     remain in the catalog per AC-5); `runtime.timeouts.sub_agent` moving 90 → 600 (expected —
     sub_agent now self-pairs to the same single-concurrency deployment as primary, which is
     exactly the ticket's flagged serialization/timeout risk, not a bug). No pricing-table
     change (local model, not litellm-priced).

4. **Verify all 5 ACs directly**:
   - AC-2: `python -m scripts.check_config` exits 0 on the real repo (not a fixture).
   - AC-4: `uv run python -m personal_agent.config.resolve --role sub_agent` (or the test-covered
     `resolve()` call) returns `qwen3.8-flash-next`.
   - AC-5: `grep qwen3.6-35b-thinking config/models.yaml` and `grep qwen3.6-35b-instruct
     config/models.yaml` both still hit.
   - AC-1 (reachability) is a live-deploy check master owns post-merge — not verifiable from
     this worktree. Note this explicitly in the handoff so master knows to check
     `model_call_completed` after deploy, per the ticket's own AC-1 wording.
   - AC-3: run the 7 tests plus the full suite.

5. **Quality gates**: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.

## Codex plan-review findings (addressed before implementing)

1. **ISSUE (fixed in this revision).** The 7-test list from the ticket is incomplete. Two more
   files hardcode the old default and will break:
   - `tests/test_llm_client/test_factory_sub_agent.py::TestSubAgentResolution` — both tests
     assert `resolved_key == "qwen3.6-35b-instruct"`; update to `qwen3.8-flash-next` (rename
     `test_role_resolves_to_qwen_instruct` since it no longer returns the instruct model).
   - `tests/personal_agent/gateway/test_session_api.py` — 5 assertion sites:
     `test_get_session_stale_stored_key_provenance_is_default` (line 475),
     `test_get_session_selection_defaults_when_no_row` (line 533),
     `test_get_session_context_max_defaults_when_no_selection` (line 591, also asserts
     `context_max == 131072` → `qwen3.8-flash-next`'s `context_length`, 131072 — same value,
     no change needed there), the local-provider-down candidates test (lines 796-797, add
     `assert "qwen3.8-flash-next" not in candidates`), and
     `test_get_session_config_no_selection_row_falls_back_to_binding_default` (lines 874, 876).
   Given the size of this ripple, the actual gate is **run `make test` after the config edits
   land and fix every red test it surfaces**, not a hand-audited fixed list — grepping the old
   keys across `tests/` turns up 30 files, most of which use mocked/fixture config unaffected by
   this change; the full suite is the authoritative source of truth, not static grep.

2–4. Confirmed correct: the proposed `qwen3.8-flash-next` entry satisfies
   `check_reasoning_declaration`'s local branch (`thinking_budget_tokens` non-`None` alone
   satisfies `declares_local` — `disable_thinking` is not required); the `defaults_by_primary`
   self-pair addition is sufficient (`config_guard.py` has no separate totality/dangling guard
   for that map — it's test-enforced only, via `test_sub_agent_defaults_by_primary.py`); the
   golden re-baseline approach is correctly scoped.

5. **RISK (accepted, not fixed here — noted for the handoff).** `role_candidates()`
   (`model_loader.py:480`) filters by provider availability, not per-model availability — so
   `qwen3.6-35b-thinking`/`qwen3.6-35b-instruct` stay selectable in the picker whenever
   `slm_local` itself is reachable, even though the *specific* old models are disabled on the
   host. A session with a stored selection pointing at either old key would keep failing after
   this deploy. Fixing this needs per-model (not per-provider) availability probing — a real,
   separate scope, not a one-line fix, and out of this ticket's stated AC's. Flag in the handoff
   as a known gap; a follow-up ticket is the owner's call, not this one's.

## Addendum — OVH sub_agent investigated and rejected (owner decision, 2026-08-28)

Mid-build the owner asked to point `sub_agent` at `qwen3.6-27b-ovh` instead of the self-pair.
Investigated, measured, and **rejected on the evidence**; owner confirmed self-pair. Recorded
here because the finding outlives this ticket.

**`qwen3.6-27b-ovh` has no reasoning control channel at all.** Both halves measured live:

* `reasoning_effort` — litellm's `ovhcloud` provider does not support the parameter. Not
  "unlisted": after `register_model_pricing()` runs, `provider_reasoning_support("Qwen3.6-27B",
  "ovhcloud")` returns `False` (a definite no), and every effort value raises
  `UnsupportedParamsError: ovhcloud does not support parameters: ['reasoning_effort']` —
  tested across `none`/`low`/`medium`/`high`.
* `thinking_budget_tokens` / `disable_thinking` — delivered via `extra_body`, which our
  LiteLLM path does not send. The catalog entry's own comment already records this.

So binding a role there means the model runs at OVH's default thinking behaviour, undirectable
from our config — exactly the condition FRE-1007's guard exists to forbid ("running at the
backend's default by omission is a convention, not a choice"). Consequences by shape:
`reasoning_effort` absent → `reasoning_declaration_missing` (**safety**, refuses boot);
declared → `reasoning_declaration_rejected` (**policy**, blocks CI). Either way **AC-2 fails**.

**A guard exemption was designed and abandoned.** The proposed
`ModelDefinition.reasoning_effort_unverifiable` flag would have keyed on
`provider_reasoning_support(...) is None` — but that `None` is an artifact of **call ordering**,
not a property of the model: it holds only before `register_model_pricing()` registers the
deployment, and flips to `False` after. An exemption hung on registration order would suppress
the CI finding while the runtime path took a different branch entirely. Codex review
independently reached the same place and added two defects the design would have shipped: the
early return drops co-occurring safety findings (`thinking_disable_on_tool_model`), and the flag
would defeat the existing regression test at
`tests/personal_agent/config/test_reasoning_declaration_guard.py:138`, which guards precisely the
stale-registry CI-outage scenario.

**Cost, for the record.** Qwen3.6-27B is a thinking model and OVH bills thinking as completion at
$3.19/MTok, with `extra_body` unable to reach the path to cap it — `max_tokens: 32768` is the
only bound, so ≈$0.10 worst-case per sub-agent call, and HYBRID spawns them concurrently.

**The correct fix, if OVH sub-agents are wanted later:** forward `extra_body` through
`LiteLLMClient` so Qwen's `enable_thinking` / thinking-budget controls reach the wire. That is
`src/` work with real review surface and its own ticket — not a rider on a live-outage fix, and
not a flag that concedes we have no control.

## Explicitly out of scope

- Pointing `sub_agent` at a cloud model instead of self-pairing — new vendor spend, owner's call
  per the ticket, not this ticket's.
- `/health` not surfacing SLM model availability — ticket says "worth a separate ticket."
- The three stray docstring/comment mentions of the old model key in `src/` (concurrency.py:208,
  model_loader.py:208, service_cli.py:166) — illustrative examples only, not functional
  references; out of the ticket's explicit scope and not touched.
