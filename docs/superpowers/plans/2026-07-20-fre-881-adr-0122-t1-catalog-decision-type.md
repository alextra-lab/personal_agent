# FRE-881 — ADR-0122 T1: catalog-backed decision type (widen the four static contracts)

**Ticket:** FRE-881 (Approved, Tier-1:Opus, stream:build2)
**Backing ADR:** ADR-0122 §3 + Sequencing step 1
**Depends on (merged):** ADR-0121 T1 (FRE-916 — catalog/providers), T2 (FRE-917 — selection store)
**Proves:** AC-6

## Objective

Admit a decision **whose options are computed from the ADR-0121 catalog** into the reused
ADR-0076 constraint-pause machinery, which today assumes a **closed, static** option set. This is
purely the *machinery* widening — the actual `artifact_builder` pause **call site at the build
boundary is step 2 (FRE-882)** and is NOT added here. Four contracts move together:

1. **Options provider** (`orchestrator/constraint_options.py`) — compute `artifact_builder` options
   from the catalog: deployments of `kind: llm`, availability-filtered by provider health, carrying
   the display detail the card needs (cost, context window, max output, one-line summary).
2. **Executor guard** (`orchestrator/executor.py` `_maybe_pause_for_constraint`) — accept the
   computed-options path instead of only `CONSTRAINT_OPTIONS[constraint]`.
3. **Pause event** (`transport/events.py`) — widen `ConstraintPauseEvent.constraint` from a closed
   2-value `Literal` to admit `artifact_builder` **and** close the pre-existing `attachment_cost`
   drift (add it to the literal + remove the type-ignore it rode).
4. **Settings validation** (`service/app.py` `update_constraint_preference`) — validate an
   `artifact_builder` `preferred_action` against the catalog, not the static registry.

## Availability model (scope boundary vs FRE-918)

ADR-0121 §3 defines provider health as: **cloud → provider configured (credential present); local →
SLM tunnel probe**. The *live* per-provider-health config-read API is **FRE-918 (ADR-0121 T3), not
merged and NOT a blocker of this ticket**. So this ticket supplies the minimal, config-derived
availability predicate AC-6 needs:

- provider with `auth_env is None` (the `slm_local` no-auth tunnel) → **available** (no synchronous
  credential to check; the live SLM health probe is FRE-918's job, layered on later).
- provider with `auth_env` set → **available iff** `getattr(settings, auth_env)` is truthy
  (matches the existing `/api/inference/status` cloud check, `service/app.py:2440`).
- unknown/dangling provider → **unavailable** (fail closed).

The predicate is **injectable** (`is_provider_available: Callable[[str], bool]`) so tests drive both
directions hermetically; the live path builds it from the loaded catalog + live settings.

## File-by-file changes

### 1. `src/personal_agent/orchestrator/constraint_options.py`
Add (static `CONSTRAINT_OPTIONS` + `option_ids` + `default_action_id` unchanged):

- `ComputedConstraintOption` — frozen dataclass: `action_id` (deployment key, persisted in prefs),
  `label`, `summary`, `input_cost_per_token`, `output_cost_per_token`, `context_length`,
  `max_output_tokens`.
- `COMPUTED_OPTION_CONSTRAINTS: frozenset[str] = {"artifact_builder"}` — the constraints whose
  options are computed, not looked up.
- `_catalog_llm_keys(config) -> list[str]` — deployment keys where `kind is ModelKind.LLM`
  (insertion order).
- `build_provider_availability(config, settings) -> Callable[[str], bool]` — the live predicate
  above.
- `compute_artifact_builder_options(config, *, is_provider_available) -> list[ComputedConstraintOption]`
  — kind-llm ∧ provider-available, mapping catalog fields → the dataclass.
- `artifact_builder_default_key(config) -> str` — `resolve_role_target("artifact_builder",
  config=config)[0]` (the configured default; `qwen3.6-35b-instruct` today).
- `resolve_options_and_default(constraint) -> tuple[list[str], str]` — the executor's single entry:
  computed path for `artifact_builder` (loads live catalog + settings, builds predicate), else
  `(option_ids(c), default_action_id(c))`.
- `is_known_constraint(constraint) -> bool` — `in CONSTRAINT_OPTIONS or in COMPUTED_OPTION_CONSTRAINTS`.
- `valid_preference_actions(constraint) -> set[str]` — `{"always_pause", *_catalog_llm_keys(config)}`
  for the computed path (catalog membership, **not** availability-filtered — a saved preference
  survives a transient outage), else `{"always_pause", *option_ids(constraint)}`.

Catalog/settings imports are lazy (inside funcs) + `TYPE_CHECKING` for annotations — no import cycle,
matches the module's leaf position.

### 2. `src/personal_agent/transport/events.py`
- Add `ConstraintName = Literal["tool_iteration_limit", "context_compression", "attachment_cost",
  "artifact_builder"]`.
- `ConstraintPauseEvent.constraint: ConstraintName` (was the 2-value literal). Update the docstring
  to name the two additions and cite the `attachment_cost` drift-close + ADR-0122 §3.

### 3. `src/personal_agent/orchestrator/executor.py`
- `_maybe_pause_for_constraint(..., constraint: ConstraintName, ...)` (was `str`); import
  `ConstraintName` from `transport.events`.
- Replace `opts = option_ids(constraint); default_id = default_action_id(constraint)` with
  `opts, default_id = resolve_options_and_default(constraint)` (import swap).
- **Remove** `# type: ignore[arg-type]` on `constraint=constraint` (line 643) — now type-valid;
  strict mypy `warn_unused_ignores` would fail if left.
- No new call site is added (build-boundary trigger is FRE-882).

### 4. `src/personal_agent/service/app.py` — `update_constraint_preference`
- Import `is_known_constraint, valid_preference_actions` (drop `CONSTRAINT_OPTIONS, option_ids`).
- `if not is_known_constraint(data.constraint_name): 422 unknown`.
- `valid_actions = valid_preference_actions(data.constraint_name)`; unchanged 422-on-miss.

## Tests (TDD — failing first)

- **`tests/personal_agent/orchestrator/test_constraint_options_computed.py`** (new):
  - `compute_artifact_builder_options` on a hand-built `ModelConfig` (3 llm across 2 providers + 1
    embedding): **both directions** — set == available llm keys; embedding key absent; a key whose
    provider predicate is `False` absent; an available key present (**AC-6 core**).
  - detail population: cost/context/max_output/summary copied from the catalog definition.
  - `build_provider_availability`: `auth_env None` → True; `auth_env` present+truthy → True;
    present+empty → False; dangling provider → False.
  - `artifact_builder_default_key` → the binding's deployment.
  - `is_known_constraint` / `valid_preference_actions` for `artifact_builder` (catalog llm keys,
    unfiltered) and a static constraint (unchanged).
  - `resolve_options_and_default("tool_iteration_limit")` unchanged; `("artifact_builder")` returns
    catalog-derived (monkeypatch `load_model_config` + settings).
- **`tests/personal_agent/orchestrator/test_constraint_pause.py`** (extend): drive
  `_maybe_pause_for_constraint(constraint="artifact_builder")` with a monkeypatched catalog +
  captured transport push; assert the emitted `ConstraintPauseEvent.options` == the computed
  available-llm key set (**AC-6 "emitted on the pause event"**), and that the static paths still work.
- **`tests/personal_agent/service/test_constraint_preference_api.py`** (new, `TestClient` +
  `dependency_overrides` for `get_request_user`; 422 fires before any DB write):
  - `artifact_builder` + non-catalog `preferred_action` → **422** (**AC-6 API clause**).
  - `artifact_builder` + a real catalog llm key → not 422 (reaches storage; stub the repo/db).
  - unknown constraint → 422.
- **`tests/personal_agent/transport/test_constraint_governance.py`** (extend): assert
  `set(get_args(ConstraintName))` ⊇ `{"attachment_cost", "artifact_builder", "tool_iteration_limit",
  "context_compression"}` — runtime proof the `attachment_cost` drift is closed in the literal. (The
  type-ignore removal is proven by `make mypy` green.)

## Quality gates
`make test-file` on each new/changed test → module run → `make test` → `make mypy` (proves the
type-ignore removal is valid) → `make ruff-check` + `make ruff-format` → `pre-commit run --all-files`.
Self-review: `code-review` at **high** (src logic + API contract change) + `security-review`
(settings-validation input boundary).

## Codex plan-review resolutions (2026-07-20)

Verdict REVISE → resolved:
- **Availability predicate / AC-6 (pt 1):** confirmed sound; injectable predicate proves AC-6
  hermetically. No change.
- **Hidden caller `ws_harness.py:378` (pt 2):** its `# type: ignore[arg-type]` on
  `ConstraintPauseEvent.constraint` **stays** — `_inject_constraint_pause(constraint: str)` passes a
  `str` variable into the (now-`ConstraintName`) field, so the ignore remains *necessary*
  (`str → Literal` always needs it) and `warn_unused_ignores` will not fire. The harness is
  static-only by construction (`CONSTRAINT_OPTIONS[constraint]` KeyErrors on `artifact_builder`). Only
  the **executor**'s ignore (param tightened to `ConstraintName`) is removed. No harness edit.
- **`default_option` independence (pt 3):** confirmed — keep it separate from `options`; never append.
- **Settings-validation split (pt 4):** confirmed — unfiltered catalog keys for the preference API,
  availability-filtered for the pause options. Correct per AC-6 (ADR-0122:362 vs :365).
- **Rich `ComputedConstraintOption` detail (pt 5):** **kept** despite the YAGNI flag — the ticket body
  explicitly mandates the options "carry the display detail the card needs: cost, context window,
  maximum output, and the one-line summary." It is the mechanism by which step-3's card gets that
  detail "for free" (ADR-0122 §3). Small, pure catalog projection, unit-tested. Flag + rationale go in
  the master handoff.

## Out of scope (later steps, do NOT build)
- The `artifact_builder` pause **call site** at the build boundary + fail-closed catalog check +
  preference behaviour → **FRE-882 (step 2, AC-4/AC-5)**.
- Plumbing the rich option detail onto the event / PWA card rendering → **FRE-883 (step 3, AC-7)**.
- Live per-provider-health config-read API → **FRE-918 (ADR-0121 T3)**.
- `artifact_draft` wiring to a resolved key → step 2.
