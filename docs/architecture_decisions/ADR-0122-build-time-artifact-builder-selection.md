# ADR-0122: Build-time artifact builder selection — choose the model when you know what you're building

**Status:** Proposed
**Date:** 2026-07-19
**Deciders:** Owner (architect), cc-adrs (Opus)
**Tags:** artifact-pipeline, model-selection, human-in-the-loop, config, observability

---

## Context

**What is the issue we're addressing?**

ADR-0121 establishes the model catalog and makes `primary` a standing, user-selected role. The
artifact builder is different in kind, and the difference is the reason this is a separate decision.

**Artifacts are not one thing.** A dense interactive dashboard, a long prose document, a diagram, a
data table, and a single-file web app are different generation problems. A model that is excellent
at one may be mediocre at another, and — critically — **which kind you are building is only known at
build time**, when you have made the request. A standing "my artifact builder is X" setting cannot
express "use the model that is good at *this*."

That inverts the assumption ADR-0119 made. ADR-0119 kept a config-page picker for
`artifact_builder` and deferred the build-time card (ADR-0118 T3/T4) to a "fast-follow." The owner's
position is the reverse: **for the builder, build-time selection is the point, and the configured
default is the fallback that may in practice never be changed.** So the card is the primary
affordance, not a later enhancement.

**Today the builder has no identity at all.** `artifact_draft` — the sole real artifact builder
(ADR-0077; every committed artifact routes through it) — hardcodes
`get_llm_client(role_name="sub_agent")` (`tools/artifact_tools.py:1436-1437`). Three consequences:

- **Tier-conflation.** "Good at reasoning in a sub-agent" is not "good at emitting a 70 KB
  self-contained interactive document." The builder is hostage to whatever `sub_agent` resolves to.
- **No cost lane.** `budget_role_for("sub_agent")` maps to `main_inference`
  (`cost_gate/__init__.py:95-131`), so artifact spend cannot be separated from sub-agent inference.
- **No telemetry identity.** Artifact builds emit `role="sub_agent"`, so "what did artifact
  generation cost and how did it perform" is unanswerable.

Large-output capability is a *measured* failure axis here, not hypothetical: the builder has hit a
provider output cap mid-generation before (FRE-478 — exactly 16,384 output tokens, forcing a slow
continuation call). Which model builds has observed consequences.

**What needs to be decided:** how the user chooses a builder at build time, how that choice reaches
`artifact_draft`, and what happens when no one answers.

---

## Decision

Give the artifact builder a **first-class role identity** and surface its selection **at build time
through the existing ADR-0076 DecisionCard**, over the ADR-0121 catalog. Five parts.

### 1. `artifact_builder` is a real role with its own cost lane and telemetry identity

An `artifact_builder` role binding (ADR-0121 Layer 3, `open: true`) with a configured default.
`ModelRole.ARTIFACT_BUILDER` is added to `llm_client/types.py`, a `roles.artifact_builder` policy is
added to `config/governance/budget.yaml`, and `_BUDGET_ROLE_BY_FACTORY_NAME` maps
`artifact_builder → artifact_builder` rather than `main_inference`.

**Two identities, two edits — they must not be conflated.** `budget_role="artifact_builder"` sets
only the *cost lane*. The *telemetry* `role` on `MODEL_CALL_COMPLETED` comes from the
`respond(role=…)` argument, which `artifact_draft` currently passes as `ModelRole.SUB_AGENT`
(`artifact_tools.py:1468-1470`), alongside a `model_role` field on its
`artifact_draft_sub_agent_start` log. **Both** must switch, or the build still emits
`role="sub_agent"` and AC-1 passes falsely while nothing has actually changed. This distinction was
the most valuable finding of the superseded ADR-0118 and is carried forward deliberately.

### 2. Selection surfaces through the ADR-0076 DecisionCard — reused, not rebuilt

At the artifact-build boundary the executor drives the same pause path that already powers
tool-approval and the ADR-0101 §8b attachment card (`_maybe_pause_for_constraint`,
`orchestrator/executor.py:462`). Everything the UX needs exists already:

| UX requirement | Existing ADR-0076 mechanism, reused as-is |
|---|---|
| Card at build time; the turn suspends until you choose | `ConstraintPauseEvent` → PWA `DecisionCard` → executor blocks on the waiter until `CONSTRAINT_DECISION` posts back (durably persisted suspend/resume) |
| A configured default that skips the card | `user_constraint_preferences` (`service/models.py:418`) + `_load_constraint_preference` pre-resolves silently (`constraint_preference_applied`, `executor.py:518-526`) |
| "Ask me every build" | the reserved preference value `always_pause` (`executor.py:518`) |
| "Remember this choice" | the in-card `remember` flag → `_save_constraint_preference` (`executor.py:614-617`) |
| Safe fallback on timeout / disconnect / headless | the last option auto-applies (`executor.py:566-575, 604-619`); here that is the configured default — never a zero-artifact stall |

### 3. What is genuinely new: a catalog-backed decision type

The reused machinery assumes a **closed, static** constraint set. Admitting a decision whose options
are *computed* requires a coordinated widening of four contracts. None is a new mechanism, and this
is stated plainly rather than hidden behind a "one-line change" claim:

- `orchestrator/constraint_options.py:62` — options are indexed out of a static `CONSTRAINT_OPTIONS`
  dict. `artifact_builder`'s options are computed from the ADR-0121 catalog and
  availability-filtered by provider health at pause time.
- `orchestrator/executor.py:484` — `_maybe_pause_for_constraint` requires `constraint` to be a key
  of `CONSTRAINT_OPTIONS`; it must accept the computed-options path.
- `transport/events.py:139` — `ConstraintPauseEvent.constraint` is a closed
  `Literal["tool_iteration_limit", "context_compression"]` and must admit `artifact_builder`.
  **Pre-existing drift closed in the same pass:** `attachment_cost` already flows through this event
  at runtime behind a `# type: ignore` while absent from the Literal.
- `service/app.py:1501-1504` — settings validation checks `preferred_action ∈ {always_pause} ∪
  option_ids(constraint)`. For `artifact_builder` the valid actions are catalog keys, so validation
  must consult the catalog. This is an API contract change, not "no change."

Because options now come from the ADR-0121 catalog rather than a bespoke registry, the card gets
`summary`, cost, context window, and large-output capability for free — the user chooses on visible
detail rather than on a bare key.

### 4. `artifact_draft` is wired to the resolved builder, fail-closed

Replace the hardcoded `get_llm_client(role_name="sub_agent")` with a resolved builder key →
`get_llm_client_for_key(builder_key, budget_role="artifact_builder")` (`factory.py:125` — the
correct call for a caller holding an already-resolved key; `get_llm_client` would mis-bill it,
FRE-869). The key comes from the card decision when present, otherwise from the role's configured
default for headless, no-socket, and timeout paths.

Before use, the key is checked against the catalog: it must exist, be `kind: llm`, be permitted for
an `open` role, and have an available provider. Anything else **fails closed to the configured
default** — never to an arbitrary model, never to no model.

### 5. Scope

**In scope:** the build-time card, the role identity, the cost lane, the telemetry identity, and the
`artifact_draft` wiring.

**Not in scope:** a standing config-page picker for `artifact_builder`. The ADR-0121 observe view
shows the configured default; changing it is a config edit. If usage shows the default is being
changed often, a picker is a trivial addition to the ADR-0121 config surface — but building it now
would be speculative, and the owner's expectation is that the default rarely moves.

**Known limitation, carried forward from ADR-0118 and not solved here:** the card fires only on the
`artifact_draft` path. A "build X" request routed to the expansion/HYBRID path (ADR-0086, gated by
`artifact_decomposition_enabled`, `config/settings.py:491-497`) reaches no builder and therefore no
card. In practice `artifact_draft` dominates (expansion fires roughly once per 90 days; the
deterministic fallback dominates), so the gap is narrow — but it is real, it is a dependency on the
routing fork, and it is tracked separately rather than quietly ignored.

---

## Alternatives Considered

### Option 1: A standing config-page picker only (the superseded ADR-0119 approach)
**Description:** Expose `artifact_builder` as a role picker on the ADR-0121 config surface; no
build-time card.
**Pros:**
- Zero new mechanism — it is one more panel on a surface being built anyway.
- No widening of the constraint contracts; no pause path in the artifact flow.
- Cheapest possible implementation.
**Cons:**
- Cannot express model-fit per artifact *type*, which is the actual requirement — you would have to
  visit a settings page and change a global before each build, then remember to change it back.
- Puts the affordance where the decision is *not* being made; the user is in a conversation asking
  for an artifact, not in a config screen.
**Why Rejected:** it optimizes the mechanism and loses the capability. Build-time is when the
relevant information (what am I building) exists.

### Option 2: Inline natural-language model naming
**Description:** The user names the model in the request — "build me a dashboard on X with Gemini."
The primary parses it, validates against the catalog, and passes it to the builder.
**Pros:**
- No new UI at all; the picker is natural language.
- Frictionless compare-by-using ("now do the same with Haiku").
**Cons:**
- **Discoverability fails** — the user cannot know the available set without asking, forcing a
  multi-turn detour.
- The guardrail becomes validate-and-reject after the fact rather than invalid-states-
  unrepresentable.
- No clean home for a default or a "remember this" affordance.
**Why Rejected:** the discoverability gap and the multi-turn friction are exactly what a card
removes, and a card enforces the guardrail by construction. Retained on record as a possible
*additional* affordance later, not as the mechanism.

### Option 3: A bespoke model-selection card with its own preference store
**Description:** Build a new interactive card type, waiter, and preference table dedicated to model
selection.
**Pros:**
- Clean semantic separation between "constraint" and "selection."
**Cons:**
- Duplicates suspend/resume, durable pause persistence, safe-default-on-timeout, and a preference
  store that ADR-0076 already provides and ADR-0101 §8b already uses for a *choice*.
- Two mechanisms doing one job is precisely the orphan/duplication the design constitution forbids.
**Why Rejected:** all net-new cost for zero capability gain over widening ADR-0076's decision
surface. The real delta is a computed-options decision type, which is a coordinated widening of
existing contracts.

### Option 4: Let the primary model choose the builder
**Description:** The orchestrator picks the artifact builder per request, as the future sub-agent ADR proposes for
sub-agents.
**Pros:**
- No user interruption; automatic.
- Consistent with the sub-agent direction.
**Cons:**
- The user is the one who knows what they want from *this* artifact — density, length, interactivity,
  fidelity to a style — much of which is not in the request text.
- Removes the compare-by-using loop that is the pedagogic point of exposing the choice.
- Would consume the model-performance history that deliberately does not exist in config (ADR-0121
  §2 declared-vs-observed line).
**Why Rejected:** wrong authority for this role. The builder is a role the owner explicitly wants to
feel and compare by hand; sub-agent dispatch is a role they explicitly do not.

---

## Consequences

### Positive Consequences

- **Model fit can follow artifact type**, which is only knowable at build time — the capability this
  ADR exists for.
- **Artifact spend and performance become isolable for the first time**, via a distinct cost lane
  and telemetry role.
- **The `sub_agent` tier-conflation is dissolved** — the builder stops being hostage to an unrelated
  role's binding.
- **Mechanism reuse is real**: no new pause/resume, no new card framework, no new preference store.
  The net-new surface is a computed-options decision type threaded through four enumerated seams.
- **The card shows real detail** — cost, context, capability, summary — because it reads the
  ADR-0121 catalog rather than a bare key list.
- **The pattern generalizes**: any future role wanting per-invocation selection reuses this decision
  type unchanged.

### Negative Consequences

- **A previously-closed enum surface is opened.** Four static contracts widen at once (options
  source, executor guard, event Literal, settings validation). None is a new mechanism, but this is
  a coordinated multi-file change, not a one-liner.
- **An interaction is added to a flow that previously had none.** Mitigated by the configured
  default pre-resolving silently, but a user who sets `always_pause` accepts a pause per build.
- **The routing-fork gap persists** (§5): expansion-path builds get no card.
- **The card is another turn-suspending surface**, so its timeout and disconnect behaviour must be
  right or a build stalls — covered by AC-5.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A selected model cannot emit a large artifact and truncates mid-document | Medium | The card surfaces `max_output_tokens` from the catalog; the FRE-471 truncate-with-warning guard already prevents a zero-artifact outcome; FRE-478 is the precedent this is drawn from |
| The card is wired but the call still runs on the old model (label-changed, call-didn't) | **High** | AC-1 asserts the emitted `MODEL_CALL_COMPLETED.model`, not the configuration; the two-identities rule (§1) is asserted separately by AC-2 |
| A stale or hallucinated key reaches the client factory | Medium | Fail-closed catalog check in `artifact_draft` (§4); falls back to the configured default; AC-4 |
| No answer stalls the build | Medium | Timeout/disconnect auto-applies the configured default; AC-5 asserts an artifact is still produced |
| Widening the constraint contracts introduces drift (settings API accepts an unsupported action) | Medium | Validation consults the catalog (§3); AC-6 asserts a non-catalog action is rejected 422; the `attachment_cost` Literal drift is closed in the same pass |

---

## Implementation Notes

**Files affected:**
- `src/personal_agent/llm_client/types.py` — add `ModelRole.ARTIFACT_BUILDER`.
- `config/model_roles.yaml` — `artifact_builder` binding, `open: true`, configured default
  (ADR-0121 Layer 3).
- `config/governance/budget.yaml` — new `roles.artifact_builder` policy. **Without it no
  `budget_counters` row is ever created and AC-2 cannot hold.** The cap value is an owner decision
  per that file's confirmed-values convention, and must be reconciled with ADR-0120, which abolishes
  hard caps in favour of visibility and consent.
- `src/personal_agent/cost_gate/__init__.py` — `_BUDGET_ROLE_BY_FACTORY_NAME`: `artifact_builder →
  artifact_builder`.
- `src/personal_agent/orchestrator/constraint_options.py` — catalog-backed, availability-filtered
  options provider alongside the static `CONSTRAINT_OPTIONS`.
- `src/personal_agent/orchestrator/executor.py` — accept the computed-options path; add the
  `artifact_builder` decision at the artifact-build boundary.
- `src/personal_agent/transport/events.py` — widen `ConstraintPauseEvent.constraint`; close the
  `attachment_cost` drift.
- `src/personal_agent/service/app.py` — settings validation consults the catalog for
  `artifact_builder` actions.
- `src/personal_agent/tools/artifact_tools.py:1436-1437` — resolved key →
  `get_llm_client_for_key(..., budget_role="artifact_builder")` + fail-closed check; **and**
  `:1468-1470` — switch `respond(role=…)` and the `model_role` log field to `ARTIFACT_BUILDER`.
- PWA — `DecisionCard` renders the builder options with cost/context/summary (no new card type).

**Dependencies:** **ADR-0121** (catalog, role bindings, provider availability — this ADR cannot start
before its step 1–2 land), ADR-0076 (DecisionCard, preferences), ADR-0077 (`artifact_draft`),
ADR-0101 §8b / FRE-691 (selection-card precedent), ADR-0120 (cost-lane policy reconciliation),
FRE-869 (budget-lane correctness), FRE-471 (truncate-with-warning guard), FRE-478/495 (output-cap
precedent).

**Testing strategy:** unit tests for computed-option generation and availability filtering, the
fail-closed catalog check, `budget_role_for("artifact_builder")` returning a distinct lane, settings
rejection of a non-catalog action, preference pre-resolution versus `always_pause`, and
safe-default-on-timeout. A live build on the deployed stack with a non-default pick.

**Sequencing (one PR each):**
1. `artifact_builder` role identity — `ModelRole`, binding, cost lane, telemetry identity switch,
   and `artifact_draft` wired to the role's configured default. No user-visible change; AC-1 (at the
   default), AC-2, AC-3 provable here.
2. Computed-options decision type — options provider, executor guard, event Literal (+
   `attachment_cost` drift), settings validation. AC-6 provable.
3. Card at the build boundary + fail-closed check + preference behaviour. AC-4, AC-5.
4. PWA card rendering with catalog detail. **(Seam ticket, AC-7.)**

---

## Verification / Acceptance Criteria

- **AC-1 — A non-default pick actually runs on that model.** *Check:* build an artifact after
  selecting a non-default builder in the card; the `MODEL_CALL_COMPLETED` telemetry for the
  `artifact_draft` span shows the **selected** deployment's resolved provider/model id. *Fails if*
  the emitted `model` is the default regardless of the selection — the classic "label changed, call
  didn't."
- **AC-2 — The build carries the builder's own identity on both axes.** *Check:* for the same build,
  (a) `MODEL_CALL_COMPLETED.role == "artifact_builder"` (not `sub_agent`), **and** (b) the cost-gate
  reservation debits the `artifact_builder` `budget_counters` row while `main_inference` is
  untouched (`budget_reservations`/`budget_counters`, `docker/postgres/init.sql:245-280`). *Fails
  if* either axis still reports `sub_agent`/`main_inference` — wiring only the client factory
  changes the lane but leaves the telemetry role, and wiring only `respond(role=…)` does the
  reverse. *(Note: `api_costs` has no `budget_role` column; the lane is asserted in cost_gate.)*
- **AC-3 — Artifact spend is separable.** *Check:* after one artifact build and one ordinary
  sub-agent turn, querying spend grouped by role returns non-zero, **distinct** values for
  `artifact_builder` and the sub-agent lane. *Fails if* the two are indistinguishable — the state
  today.
- **AC-4 — An invalid builder key fails closed to the default, never onward.** *Check:* drive the
  decision path with (a) a key absent from the catalog, (b) a catalog key with `kind: embedding`,
  and (c) a catalog key whose provider is unavailable. In every case the build completes on the
  **configured default** and the substituted key is logged. *Fails if* any of the three reaches the
  client factory, or the build errors instead of falling back.
- **AC-5 — No answer never means no artifact.** *Check:* simulate no-socket and timeout on a build;
  the artifact still renders, produced by the configured default, with `resolution` recorded as
  `connection_lost` or `timeout_default`. *Fails if* the build stalls or yields zero artifact when
  the user does not answer (a regression of the FRE-471 guard).
- **AC-6 — The card and the settings API offer exactly the valid, available set.** *Check:*
  `set(ConstraintPauseEvent.options)` **equals** the availability-filtered set of catalog
  deployments where `kind: llm` — asserted in both directions (no non-catalog key leaks in; a
  deployment whose provider is down is absent; an available one is present). Separately, the
  settings API rejects a `preferred_action` naming a non-catalog key with 422. *Fails if* the option
  set differs in either direction, or the API accepts an unsupported action.
- **AC-7 (assembled seam) — the whole loop, live.** *Check:* on the deployed stack a real "build me
  an artifact about X" request surfaces the card showing per-option cost and context; a **non-default**
  pick renders a correct, grounded artifact on the chosen model; AC-1 and AC-2 telemetry corroborate
  both the model and the lane; and with a stored preference set instead, no card appears, yet the
  build still runs on the *preferred* model (the preference is a config key, so the check maps it
  through the catalog to its resolved model id before comparing — comparing the raw key to the
  emitted `model` would be a dimension error). *Fails if* any leg breaks — card → decision →
  resolved key → tool → correct model → correct lane — **or** if a stored preference is logged and
  swallowed while the build silently falls back to the default.

**Seam owner:** AC-7 is owned by the **PWA card-rendering ticket (step 4)** — the child where the
assembled intent first holds. This ADR does not close when the decision type merges; it closes only
when AC-7 is proven on the deployed stack. Master asserts AC-7 at the acceptance gate.

---

## References

- ADR-0076 — constraint governance / DecisionCard: the suspend/resume, preference, and
  safe-default machinery reused here unchanged
- ADR-0077 — `artifact_draft`, the sole real artifact builder
- ADR-0086 — expansion/HYBRID decomposition (the routing-fork limitation in §5)
- ADR-0089 — artifact execution security (the sealed-box the built artifact runs in)
- ADR-0101 §8b / FRE-691 — cloud-attachment cost DecisionCard, the live precedent for a *choice*
  on this machinery
- ADR-0118 — superseded; its DecisionCard-reuse analysis and the two-identities finding are carried
  forward here
- ADR-0119 — superseded; its config-picker-only approach is Option 1 above, rejected
- ADR-0120 — cost governance (the `artifact_builder` budget policy must reconcile with it)
- ADR-0121 — model catalog and selection layer: the catalog this card reads, and a hard prerequisite
- Orchestrator-invoked sub-agents — **future ADR, not yet written** (Option 4's mechanism, for a role where it does fit)
- FRE-471 — truncate-with-warning guard (why a truncating model never yields zero artifact)
- FRE-478 / FRE-495 — output-cap and context-window incidents; why large-output capability is shown
  on the card
- FRE-869 — `get_llm_client_for_key` budget-lane correctness
- `src/personal_agent/tools/artifact_tools.py:1436-1437` — the hardcoded `sub_agent` builder binding
- `src/personal_agent/tools/artifact_tools.py:1468-1470` — the `respond(role=…)` telemetry identity
- `src/personal_agent/orchestrator/executor.py:462` — `_maybe_pause_for_constraint`
- `src/personal_agent/orchestrator/constraint_options.py:62` — the static options dict to widen
- `src/personal_agent/transport/events.py:139` — the closed `ConstraintPauseEvent.constraint` Literal
- `src/personal_agent/cost_gate/__init__.py:95-131` — `budget_role_for`, currently mapping the
  builder to `main_inference`

---

## Status Updates

### 2026-07-19 - Proposed
**Changed By:** cc-adrs (Opus)
**Reason:** Initial proposal, splitting the build-time artifact-builder card out of the superseded
ADR-0118/0119 pair and re-basing it on the ADR-0121 catalog. The owner's framing inverted ADR-0119's:
because different artifact *types* may suit different models, and the type is known only at build
time, the card is the primary affordance and the configured default is the fallback that may never
be changed — where ADR-0119 kept a config picker and deferred the card as a fast-follow. Carried
forward from ADR-0118: the DecisionCard-reuse analysis, the honest enumeration of the four contracts
that must widen, the `attachment_cost` Literal drift to close in passing, FRE-869's billing
correctness, and the two-identities finding (`budget_role` cost lane versus the `respond(role=…)`
telemetry role — both must switch or AC-1 passes falsely). Dropped from ADR-0118: the bespoke
`artifact_builder_candidates` registry, whose fields duplicated `ModelDefinition` and whose
`max_tokens` loader gate could not fail for the reason it existed (FRE-880) — options now come from
the ADR-0121 catalog. A standing config picker for this role is explicitly deferred as speculative.
