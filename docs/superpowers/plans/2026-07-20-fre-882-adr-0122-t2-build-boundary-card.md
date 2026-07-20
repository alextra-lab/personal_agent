# FRE-882 — ADR-0122 T2: card at the build boundary + fail-closed catalog check

**Ticket:** FRE-882 (Approved). **ADR:** ADR-0122 §2/§4, Sequencing step 2. **Depends on:** FRE-881 (T1, merged — `_maybe_pause_for_constraint`/`resolve_options_and_default` already accept the computed `artifact_builder` path).

## Scope

Raise the artifact-builder decision at the `artifact_draft` build boundary through the
existing ADR-0076 pause path, and wire a selected key into `artifact_draft` fail-closed.
Acceptance criteria: AC-1, AC-2 (regression guard), AC-4, AC-5.

## Key design decision: where the pause call lives

`_maybe_pause_for_constraint` (orchestrator/executor.py:558) is a private helper with
3 existing call sites, all inside executor.py. The generic `ToolExecutionLayer` /
`dispatch_tool_call` path has no channel to thread a resolved key into a specific tool's
executor without either (a) exposing it as an LLM-facing `ToolParameter` (wrong — this is
a human decision, not model-proposed), or (b) adding a field to the shared, frozen
`TraceContext` used by every tool executor (too broad for one role).

Decision: call `_maybe_pause_for_constraint` **inside `artifact_draft_executor`** itself
(`tools/artifact_tools.py`), via a lazy (function-body) import of the private helper from
`orchestrator.executor`. This is the literal build boundary — right where
`get_llm_client(role_name="artifact_builder")` sits today — and needs zero changes to the
generic dispatch machinery. Precedent for this lazy cross-module import of a private
executor.py helper already exists: `gateway/session_api.py:193` does
`from personal_agent.orchestrator.executor import _resolve_context_max # noqa: PLC0415`.
`artifact_tools.py` already imports `get_llm_client` the same way, one line above the
call site being replaced, so both imports (the new `_maybe_pause_for_constraint` and the
existing `get_llm_client`) follow the same convention. No circular-import risk: the import
is deferred to call time, well after all modules have finished loading (orchestrator.executor
imports `personal_agent.tools` at module top; a lazy import the other direction at call time
is safe).

`ctx` passed into `artifact_draft_executor` is a `TraceContext`, which already carries
`user_id`/`session_id`/`trace_id` — everything `_maybe_pause_for_constraint` needs.

## Key design decision: routing signal (ConstraintDecision)

ADR §4 is explicit: "with no decision — headless, no socket, timeout — it keeps the
role-name path [`get_llm_client(role_name="artifact_builder")`, unchanged]... [a] card
decision supplies a key [and] must switch to
`get_llm_client_for_key(key, budget_role="artifact_builder")`." T1's own
`artifact_builder_default_key` docstring says the same: "the timeout dispatch is not
driven by this key... wiring the two together is the FRE-882 seam."

`_maybe_pause_for_constraint` currently returns a plain `str` (the action_id) and
callers cannot tell *how* it was resolved — a stored preference and an interactive pick
both need the key path; a timeout, a dropped connection, or a Stop-button cancel all need
the (already-correct, already-billed-right) role-name path. Changing the function's return
type outright would touch 3 existing call sites and ~15 existing test assertions across
`test_constraint_pause.py`, `test_attachment_cost_gate.py`, `test_executor.py` — all of
which currently do bare `result == "some_action_id"` / mock with
`AsyncMock(return_value="proceed_cloud")`.

Decision: `ConstraintDecision(str)` — a `str` subclass carrying a `.resolution` attribute
(`"preference_applied" | "user_choice" | "timeout_default" | "connection_lost" |
"user_cancel"`). It compares/hashes exactly as the plain action_id string, so every
existing call site and test keeps working unchanged (`ConstraintDecision("continue_10",
"user_choice") == "continue_10"` is `True`). Only the new artifact-builder call site reads
`.resolution` to pick a routing branch. Zero blast radius on the 3 existing callers.

Routing rule in `artifact_draft_executor`: `resolution in ("user_choice",
"preference_applied")` → key path; everything else (including `"user_cancel"`, an
allowlist rather than a denylist — fail-closed) → today's unchanged role-name path.

## Fail-closed catalog check (AC-4)

New `resolve_artifact_builder_key(selected_key, config, *, is_provider_available) -> str`
in `orchestrator/constraint_options.py`, composing two already-shipped primitives rather
than reinventing catalog validation:
- `config.model_loader.is_selectable_binding("artifact_builder", key, config)` — already
  checks existence + `kind: llm` + role-`open` (ADR-0121 §6).
- `build_provider_availability(config, settings)` (T1, already shipped) — the provider
  half AC-4 additionally requires.

Falls back to `artifact_builder_default_key(config)` on any failure. `artifact_draft_executor`
logs `artifact_builder_key_substituted` (warning) when the resolved key differs from what
was requested.

## Changes

1. **`orchestrator/constraint_options.py`** (done): add `ConstraintDecision` and
   `resolve_artifact_builder_key`.
2. **`orchestrator/executor.py`**: `_maybe_pause_for_constraint` return type `str` →
   `ConstraintDecision` at all 3 return points (preference hit, connection_lost, final
   user_choice/timeout_default). No call-site changes needed (str-subclass compat).
3. **`tools/artifact_tools.py`** (`artifact_draft_executor`): replace
   `builder_client = get_llm_client(role_name="artifact_builder")` with:
   - call `_maybe_pause_for_constraint(constraint="artifact_builder", ...)`
   - if `resolution in ("user_choice", "preference_applied")`: fail-closed-check the key
     via `resolve_artifact_builder_key`, log substitution if it differed, then
     `get_llm_client_for_key(key, budget_role="artifact_builder")`
   - else: unchanged `get_llm_client(role_name="artifact_builder")`
4. **`tests/personal_agent/tools/test_artifact_tools.py`**: `_install_fakes` (the base
   fixture used by every test in the file) gets a default `AsyncMock` patch on
   `personal_agent.orchestrator.executor._maybe_pause_for_constraint` returning
   `ConstraintDecision(<anything>, "connection_lost")` — preserves every one of the ~30
   existing `artifact_draft_executor` tests unchanged (they all fall into the unchanged
   role-name branch). New tests override this default per-scenario.
5. **`tests/personal_agent/orchestrator/test_constraint_options_computed.py`**: new tests
   for `resolve_artifact_builder_key` against the file's existing hand-built `ModelConfig`
   fixture — valid key, absent key, embedding-kind key, unavailable-provider key.
6. **`tests/personal_agent/tools/test_artifact_tools.py`**: new tests — pause called with
   `constraint="artifact_builder"`; `user_choice`/`preference_applied` → key path with
   explicit `budget_role="artifact_builder"`; `timeout_default`/`connection_lost`/
   `user_cancel` → role-name path; invalid key → default + substitution log, build still
   completes.

## Explicitly not changed (already shipped, FRE-879/T1)

Role identity, telemetry (`respond(role=ModelRole.ARTIFACT_BUILDER)`,
`artifact_draft_sub_agent_start.model_role`), cost lane
(`_BUDGET_ROLE_BY_FACTORY_NAME["artifact_builder"]`), the Layer-3 `artifact_builder`
binding in `config/model_roles.yaml` (`open: true`, deployment `qwen3.6-35b-instruct`).

## Not in this ticket

PWA card rendering (T3 / FRE-921, seam AC-7). AC-3 (per-model spend separability) is a
telemetry-migration property already delivered by ADR-0121 §8, not new wiring here.
