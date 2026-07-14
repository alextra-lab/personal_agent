# ADR-0117: Model-selection layer for open roles — user-selectable artifact builder (Phase 1)

**Status:** Proposed
**Date:** 2026-07-14
**Deciders:** Owner (architect), cc-adrs (Opus)
**Tags:** model-routing, artifact-pipeline, human-in-the-loop, config, observability

---

## Context

**What is the issue we're addressing?**

Artifact generation today borrows the `sub_agent` role. `artifact_draft` — the sole real
artifact builder (ADR-0077; all committed artifacts route through it) — hardcodes
`get_llm_client(role_name="sub_agent")` (`tools/artifact_tools.py:1436-1437`). On the cloud profile
that binding resolves to **Haiku** (`config/profiles/cloud.yaml` `sub_agent_model`). Three
problems follow from that one borrowed binding:

- **Tier-conflation.** "Best at reasoning" is not "best at emitting a 70 KB self-contained
  interactive HTML document." The builder should be selectable independently of the sub-agent
  tier, but today it is hostage to whatever `sub_agent` resolves to. Large-output capability is a
  *measured* failure axis here, not a hypothetical: the artifact builder has hit provider output
  caps mid-generation before (FRE-478 — exactly 16,384 output tokens, forcing a slow continuation
  call), so "which model builds" has real, observed consequences.
- **No identity.** Artifact builds carry no cost lane and no telemetry identity of their own —
  `budget_role_for("sub_agent")` maps to `main_inference` (`cost_gate/__init__.py:95-131`), so
  artifact spend cannot be isolated from primary sub-agent inference.
- **No user choice.** The owner wants model choice as a first-class, user-facing capability —
  a **standing feature** (you compare builders by *using* them on real artifacts), not an eval —
  surfaced **at artifact-build time, in the conversation**, with a **configurable default**.

Two hard constraints govern the design:

- **Swappability tracks blast-radius × perceptibility.** A user-selectable model must never
  reach a backend *writer* role — the roles pinned in `config/model_roles.yaml`
  (`entity_extraction`, `captains_log`, `insights`, `reranker`, `reranker_fallback`, `embedding`)
  write durable substrate (KG, logs) where a wrong model drifts silently into permanent state.
  Float what you feel in real time; pin what corrupts silently.
- **Design constitution (owner):** lean, tight, legible, observable — no dead or orphaned code.
  "Lean" ≠ feature-minimal: every part earns its keep *and* a reader groks it in one pass. The
  cheapest correct mechanism a reader can follow wins over a bespoke one — but "cheap" must be
  *honest*: every seam the change touches is named, not hidden behind a "one-line" claim.

**What needs to be decided:** how a user selects the artifact builder at build time; where the
selectable set and the default live; and how the selection reaches `artifact_draft` without
building a new elicitation mechanism or ever exposing a KG-writer as selectable.

---

## Decision

Introduce a **model-selection layer for *open* roles**, and ship its first consumer — a
**user-selectable artifact builder** — by **reusing the ADR-0076 DecisionCard machinery wholesale**
and adding a **registry-backed decision type** on top of it. Six parts:

**1. Extract a first-class `artifact_builder` open role.** Add `ModelRole.ARTIFACT_BUILDER`
(`llm_client/types.py`) and an `artifact_builder` role in the ADR-0099 matrix
(`config/model_roles.yaml`) with a **default binding of `claude_haiku`** — Haiku stays the default
because it is proven good and grounded on real artifacts and is cheap. `primary`/`sub_agent` appear
in the matrix as intent-only rows *by convention* (the file comment; at runtime they resolve through
the ExecutionProfile, not this matrix) — but `resolve_role_model_key` itself has no special-case
exclusion, it resolves any declared row. `artifact_builder` is added as a real declared row like
`entity_extraction`, so `resolve_role_model_key("artifact_builder")` returns its default key
directly. The role gets its **own cost lane** — a new `roles.artifact_builder` policy in
`config/governance/budget.yaml` (with no policy there, no `budget_counters` row is ever created for
it) plus `_BUDGET_ROLE_BY_FACTORY_NAME`: `artifact_builder → artifact_builder`, not `main_inference`.
The cap *value* is an owner decision (that file's confirmed-values convention: ask before setting);
as a user-facing build it likely mirrors `main_inference`'s `on_denial: raise`. This dissolves the
tier-conflation: the builder stops being hostage to the `sub_agent` binding.

**2. A vetted candidate registry — new config, not a single binding.** The matrix binds a role to
*one* key; a *selectable set* cannot live in a single-binding entry. So the candidate registry is a
**new config block** (`artifact_builder_candidates` — a sibling map in `model_roles.yaml` or its own
file): the vetted keys a user may select, each with onboarding metadata — provider, decoding params,
known failure modes, and — specific to this role — **large-output capability** (a ~70 KB artifact ≈
20 K+ output tokens in one shot; providers differ on max-output / truncation; see FRE-478/FRE-495).
"Vetted" is a per-model onboarding gate, not a free list-add. A model absent from the registry is
not selectable, period. The default (part 1) must itself be a registry member.

**3. Surface selection through the existing ADR-0076 DecisionCard — reused, not rebuilt.** At
artifact-build time the executor drives the same pause path that powers tool-approval and the
ADR-0101 §8b cloud-attachment card (`_maybe_pause_for_constraint`, `orchestrator/executor.py:462`).
Everything the UX needs already exists and is reused **unchanged**:

| UX requirement | Existing mechanism (ADR-0076), reused as-is |
|---|---|
| Card at build time; turn suspends until you click | `ConstraintPauseEvent` → PWA `DecisionCard` → executor blocks on the waiter until `CONSTRAINT_DECISION` posts back (genuine one-turn suspend/resume, durably persisted) |
| Configurable default (settings page) | `user_constraint_preferences` (`service/models.py:418`; migration `0006`) + `_load_constraint_preference` — a standing preference **pre-resolves the pause silently** (`constraint_preference_applied`, `executor.py:518-526`), no card shown |
| "Ask me each build" toggle | the reserved preference value `always_pause` forces the card even when a default exists (`executor.py:518`) |
| "Remember this" (a pick becomes the default) | the in-card `remember` flag → `_save_constraint_preference` (`executor.py:614-617`) |
| Safe fallback on timeout / disconnect / headless | the **last option auto-applies** (`executor.py:566-575, 604-619`); for this role the safe default is the configured default (Haiku) — never a zero-artifact stall |
| Guardrail | the card only lists what the options generator emits |

**What is genuinely new is a *registry-backed decision type*** — and, honestly stated, admitting it
requires a **coordinated widening of every contract that currently assumes a closed, static
constraint set**. This is not one line; it is a small set of named seams (none of them a new
mechanism):
- `orchestrator/constraint_options.py` — options are looked up from the static `CONSTRAINT_OPTIONS`
  dict and indexed `CONSTRAINT_OPTIONS[constraint]` (`:62`). `artifact_builder`'s options are
  **computed** from the registry and **availability-filtered** at pause time (a local-only builder
  is dropped when the local SLM server is unservable). Needs a registry-backed options provider,
  not a static list.
- `orchestrator/executor.py:484` — `_maybe_pause_for_constraint` requires `constraint` to be a key
  of `CONSTRAINT_OPTIONS`. Must accept the computed-options path.
- `transport/events.py:139` — `ConstraintPauseEvent.constraint` is a closed
  `Literal["tool_iteration_limit", "context_compression"]`. It must admit `artifact_builder`.
  (Pre-existing drift to fix in passing: `attachment_cost` already flows through this event at
  runtime with a `# type: ignore` yet is absent from the Literal — the widening should close that
  gap too.)
- `service/app.py:1501-1504` — the settings API validates `constraint_name in CONSTRAINT_OPTIONS`
  and `preferred_action in {always_pause} ∪ option_ids(constraint)`. For `artifact_builder` the
  valid actions are the **candidate keys** (dynamic), so this validation must consult the registry,
  not the static option set. So the settings surface reuses the *store* but does require an **API
  contract change** — not "no change."

**4. Wire `artifact_draft` to the resolved builder.** Replace the hardcoded
`get_llm_client(role_name="sub_agent")` (`artifact_tools.py:1436-1437`) with a resolved builder key
→ `get_llm_client_for_key(builder_key, budget_role="artifact_builder")` (`factory.py:125` — the
correct call for a caller holding an already-resolved key; `get_llm_client` would mis-bill it,
FRE-869). The key comes from the card decision when present, else from
`resolve_role_model_key("artifact_builder")` (the default) for headless / no-WS / timeout paths.
**Two identities, two edits — do not conflate them:** `budget_role="artifact_builder"` sets only the
*cost lane*. The *telemetry* `role` on `MODEL_CALL_COMPLETED` is populated from the `respond(role=…)`
argument, which `artifact_draft` currently passes as `ModelRole.SUB_AGENT`
(`artifact_tools.py:1468-1470`), alongside a `model_role` field on its `artifact_draft_sub_agent_start`
log. Both must switch to `ModelRole.ARTIFACT_BUILDER` — wiring only the client factory would still emit
`role="sub_agent"` and silently fail AC-1.

**5. Guardrail: writer bindings are immutable under selection, plus a fail-closed backstop.** The
real invariant is not "no writer *role name* in the candidate list" (writer roles resolve to model
*keys* like `gpt-5.4-mini`, so a name-based check is mis-dimensioned). It is: **a builder selection
changes only the `artifact_draft` call's model — every pinned writer role continues to resolve to
its pinned key regardless of any selection.** This holds by construction (the selection writes only
the `artifact_builder` preference key; no writer-role resolution reads user-settable state) and is
asserted directly (AC-3). Defensively, before `artifact_draft` uses a resolved key it checks the key
is in the `artifact_builder` candidate allow-list; anything else fails **closed to the default
builder**, never to an arbitrary model.

**6. Settings surface.** A PWA control writes the standing preference (default builder +
ask-each-time) through the existing `constraint_preferences_repository` (reused store) via the
widened settings API (part 3).

**Phase 2 (deferred, documented — NOT in this ADR's build scope):** "Select your orchestrator"
reuses the identical registry + card + preference mechanism for the `primary` role. It ships only
once the artifact-builder pilot is trusted, because the orchestrator is every-turn, always-on
stakes. It is named here so the layer is designed to generalize; its build is a separate ADR/ticket
wave.

---

## Alternatives Considered

### Option 1: Inline natural-language model naming
**Description:** The user names the model in the request — "build me an artifact on X with Gemini."
The primary parses it, validates against the registry, passes it to the builder; unstated → default.
**Pros:**
- Zero new UI; the "picker" is just natural language.
- Frictionless compare-by-using ("now do it again with Haiku").
**Cons:**
- **Discoverability fails** — the user cannot know the vetted set without asking, which forces a
  multi-turn detour ("what models can I use?" → list → re-request).
- Guardrail is *validate-and-reject* (a typo or a writer-role name is caught after the fact), not
  invalid-states-unrepresentable.
- No clean surface for a configured default or a "remember" affordance.

**Why Rejected:** The owner explicitly weighed this and chose the card: the discoverability gap and
the multi-turn friction are exactly what a card removes, and the card enforces the guardrail by
construction. Kept on record as a possible *additional* affordance later, not the mechanism.

### Option 2: Persistent `artifact_builder_model` field on the ExecutionProfile (extend ADR-0044)
**Description:** Add a builder field to `ExecutionProfile` alongside `primary_model`/`sub_agent_model`,
set per session/profile.
**Pros:**
- Reuses the profile resolution path (`resolve_model_key`) with no new role.
- One place binds all of a turn's models.
**Cons:**
- The profile is **server-owned and session-scoped** (ADR-0079), not a per-build user choice — it
  cannot surface a decision *at build time in the conversation*, which is the requirement.
- Re-creates the tier-conflation: the builder choice would be coupled to the local↔cloud profile
  toggle rather than selected on its own axis.
- No natural home for a vetted candidate *set* + onboarding metadata (a profile field is a single
  scalar).

**Why Rejected:** Wrong altitude and wrong lifetime — a profile is a placement toggle, not a
per-artifact user selection; it defeats the "in the conversation" goal.

### Option 3: A bespoke model-selection card + its own preference store
**Description:** Build a new interactive card type, waiter, and preference table dedicated to model
selection.
**Pros:**
- Clean semantic separation ("selection" vs "constraint").
**Cons:**
- Duplicates suspend/resume, safe-default-on-timeout, durable pause persistence, and a preference
  store that ADR-0076 already provides and ADR-0101 §8b already uses for a *choice*.
- Two mechanisms doing the same job is precisely the orphan/duplication the design constitution
  forbids.

**Why Rejected:** All net-new cost, zero capability gain over widening ADR-0076's decision surface.
The delta (a registry-backed decision type) is coordinated widening of existing contracts, not a new
mechanism.

### Option 4: Mode/governance-policy gating (extend `config/governance/tools.yaml`)
**Description:** Express "which builder" through the existing mode-aware governance layer that already
gates *tool availability* per mode (`config/governance/tools.yaml`).
**Pros:** reuses an existing config-driven policy surface.
**Cons:** governance gates *whether a tool is allowed in a mode*, not *which model a role binds to*;
it has no per-build user-choice surface and no candidate/onboarding schema. Bending it to model
selection conflates two orthogonal axes (capability policy vs model binding).
**Why Rejected:** wrong axis — it answers "may this tool run" not "which model builds," and offers no
build-time user surface.

---

## Consequences

### Positive Consequences

- The artifact builder is **independently selectable** and gains its **own cost + telemetry
  identity**; artifact spend/latency become isolable for the first time.
- The `sub_agent` tier-conflation is **dissolved** — one change, two wins (independent binding +
  own budget lane).
- The guardrail holds **by construction**: a builder selection cannot alter any writer-role binding.
- **Mechanism reuse is real**: no new pause/resume, no new card framework, no new preference store.
  The net-new surface is a registry + a decision type threaded through existing seams — legible in
  one pass *because* those seams are enumerated (Decision §3), not hidden.
- The layer **generalizes** to the orchestrator selector (Phase 2) with no rework.

### Negative Consequences

- Admitting a registry-backed decision type touches **several static contracts at once** (the
  options source, the executor guard, the `ConstraintPauseEvent` Literal, and the settings-API
  validation — Decision §3). None is a new mechanism, but this is a coordinated multi-file widening,
  not a one-liner, and it opens a previously-closed enum surface.
- **Per-model onboarding is a manual gate** — intended (vetting is the point), but a standing
  maintenance cost: adding a provider is not a one-line list-add.
- **The card only fires on the `artifact_draft` path.** A "build X" request that routes to the
  expansion/HYBRID path (ADR-0086, gated by `artifact_decomposition_enabled`,
  `config/settings.py:491-497`) reaches no builder and therefore no card. In practice the
  `artifact_draft` path dominates (expansion fires ~1×/90d; deterministic fallback dominates), so the
  gap is narrow — but it is a real dependency on the routing-fork, tracked separately (Stream-1
  routing-coherence is a distinct future ADR, deliberately out of scope here).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A selected model can't emit a 70 KB artifact (truncation → broken doc) | Medium | Onboarding **must** record large-output capability (FRE-478 is the precedent); the registry excludes models failing the max-output check; the FRE-471 truncate-with-warning guard already prevents zero-artifact |
| A builder selection perturbs a KG-writer binding | High | Writer bindings are independent of selection **by construction** (selection writes only the `artifact_builder` preference); asserted by AC-3; plus a fail-closed allow-list check in `artifact_draft` |
| A dead local builder is offered on cloud and the build fails | Medium | Options are **availability-filtered** at pause time; AC-4 asserts local-only builders are absent from `ConstraintPauseEvent.options` when the SLM server is unservable |
| Widening the closed constraint contracts introduces drift (e.g. the settings API accepts an action no builder supports) | Medium | Settings-API validation consults the registry (Decision §3); a unit test asserts an unknown builder key is rejected 422; the `attachment_cost` Literal drift is closed in the same pass |
| Card interrupts flow / no answer stalls the build | Medium | Standing default pre-resolves silently; `always_pause` is opt-in; timeout/disconnect auto-applies the safe default — the build always completes |

---

## Implementation Notes

**Files affected (Phase 1):**
- `src/personal_agent/llm_client/types.py` — add `ModelRole.ARTIFACT_BUILDER`.
- `config/model_roles.yaml` — new matrix role `artifact_builder` (default `claude_haiku`) **and** a
  new `artifact_builder_candidates` block (vetted keys + onboarding metadata incl. large-output).
  Pinned writer roles unchanged.
- `config/models.yaml` / `config/models.cloud.yaml` — ensure each candidate carries max-output /
  large-output metadata.
- `src/personal_agent/config/model_loader.py` — `resolve_role_model_key` already resolves matrix
  roles; add candidate-registry load + onboarding validation.
- `src/personal_agent/cost_gate/__init__.py` — add `artifact_builder` to
  `_BUDGET_ROLE_BY_FACTORY_NAME` (own lane).
- `config/governance/budget.yaml` — new `roles.artifact_builder` policy (cap value = owner decision;
  without it no `budget_counters` row exists and AC-2 cannot hold).
- `src/personal_agent/orchestrator/constraint_options.py` — registry-backed, availability-filtered
  options provider for `artifact_builder` (alongside the static `CONSTRAINT_OPTIONS`).
- `src/personal_agent/orchestrator/executor.py` — `_maybe_pause_for_constraint` accepts the
  computed-options path; add the `artifact_builder` decision at the artifact-build boundary.
- `src/personal_agent/transport/events.py` — widen `ConstraintPauseEvent.constraint` to admit
  `artifact_builder` (and close the `attachment_cost` Literal drift).
- `src/personal_agent/service/app.py` — settings API validates `artifact_builder` actions against
  the candidate registry, not the static option set.
- `src/personal_agent/tools/artifact_tools.py:1436-1437` — resolve builder key →
  `get_llm_client_for_key(builder_key, budget_role="artifact_builder")`; fail-closed allow-list check;
  **and** switch `respond(role=…)` + the `model_role` log field (`:1468-1470`) to `ARTIFACT_BUILDER`.
- PWA settings surface — write default-builder + ask-each-time via the widened settings API +
  `service/repositories/constraint_preferences_repository.py` (reused store).

**Dependencies:** ADR-0076 (DecisionCard / constraint pause), ADR-0044/0079 (ExecutionProfile),
ADR-0099 (`model_roles.yaml` matrix + `resolve_role_model_key`), ADR-0077 (`artifact_draft`),
ADR-0101 §8b / FRE-691 (selection-card precedent), FRE-869 (budget-lane correctness), FRE-478/495
(large-output caps precedent).

**Testing strategy:** unit tests for registry/default resolution, `budget_role_for("artifact_builder")`
returning a distinct lane, writer-binding immutability under selection, dynamic-option generation +
availability filter, settings-API rejection of a non-candidate action, preference pre-resolution vs
`always_pause`, and safe-default-on-timeout. Live check on the cloud profile drives a real build with
a non-default pick.

**Sequencing (Phase 1 tickets, one PR each):**
1. `artifact_builder` role + cost lane (`budget.yaml` policy + `_BUDGET_ROLE_BY_FACTORY_NAME`) +
   telemetry identity (the `respond(role=…)`/`model_role` switch, default Haiku) — resolves to Haiku
   via its own role/lane; no user-visible behaviour change, but AC-1/AC-2 already provable here.
2. Vetted candidate registry + onboarding metadata (incl. large-output capability) + loader
   validation.
3. Widen the ADR-0076 decision surface (options provider, executor guard, event Literal, settings
   API) for the registry-backed `artifact_builder` decision type + availability filtering.
4. Wire `artifact_draft` to the resolved builder + fail-closed allow-list. **(Seam ticket.)**
5. PWA settings surface for default + ask-each-time.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 — A non-default pick actually runs on that model.** *Check:* build an artifact after
  selecting a non-Haiku builder; the `MODEL_CALL_COMPLETED` telemetry for the `artifact_draft`
  span (`role="artifact_builder"`) shows the **selected** `model` id (ES `agent-logs-*` /
  route-trace). *Fails if* the emitted `model` is still Haiku regardless of selection (plumbing not
  wired — the classic "label changed, call didn't").
- **AC-2 — Artifact builds bill to their own cost lane, not `main_inference`.** *Check:* (a) a unit
  assertion that `budget_role_for("artifact_builder") == "artifact_builder"` (not the
  `main_inference` default); (b) an integration assertion that a build's cost-gate reservation
  debits the `artifact_builder` `budget_counters` row and leaves `main_inference` untouched
  (`budget_reservations`/`budget_counters`, `docker/postgres/init.sql:245-280`). *Fails if* the
  reservation still lands on `main_inference` (role not truly extracted — only renamed). *(Note:
  `api_costs` has no `budget_role` column — the lane is asserted in cost_gate, not there.)*
- **AC-3 — A builder selection cannot perturb any writer-role binding.** *Check:* set a non-default
  `artifact_builder` selection, then assert `resolve_role_model_key(r)` for every
  `r ∈ {entity_extraction, captains_log, insights, embedding, reranker, reranker_fallback}` returns
  **byte-identical** to its baseline (pinned) key; and that no writer-role resolution path reads any
  user-settable preference. *Fails if* any writer role's resolved key changes as a function of a
  builder selection. *(This replaces a naive candidate∩writer-role-name check, which is
  mis-dimensioned — writers resolve to model keys, so a name intersection can pass while the wrong
  model is still selected.)*
- **AC-4 — The card offers exactly the vetted, currently-servable builders — no more, no less.**
  *Check:* assert `set(ConstraintPauseEvent.options)` **equals** the availability-filtered candidate
  registry for the active context — i.e. (a) every option is a registry key (**no** non-registry
  leakage), (b) with the local SLM server unservable on the cloud profile, local-only candidates are
  **absent**, and (c) a candidate whose required secret/endpoint is missing is **absent** while a
  known-servable one is **present**. *Fails if* the option set differs from the availability-filtered
  registry in any direction — a leaked non-registry key, a retained dead candidate, or a dropped live
  one. (A one-scenario "local excluded" check would pass a filter that still leaks dead cloud
  candidates; set-equality closes that.)
- **AC-5 — A standing default pre-resolves silently *and the chosen model actually builds*;
  `always_pause` shows the card.** *Check:* with a stored `artifact_builder` preference set to a
  **non-Haiku** builder, `constraint_preference_applied` is logged, **no** `ConstraintPauseEvent` is
  emitted, **and** the resulting `artifact_draft` build runs on that preferred model
  (`MODEL_CALL_COMPLETED.model` == the preference, per AC-1 instrumentation); with the preference set
  to `always_pause`, a `ConstraintPauseEvent` **is** emitted. *Fails if* the card shows despite a set
  default, never shows under `always_pause`, **or** the preference is logged-and-swallowed while the
  build silently falls back to Haiku (preference resolved but never threaded to the tool).
- **AC-6 — No answer never means no artifact.** *Check:* simulate no-WS / timeout on a build; the
  artifact still renders, produced by the default builder (`resolution` is `connection_lost` or
  `timeout_default`, `model="claude_haiku"`). *Fails if* the build stalls or yields zero artifact
  when the user doesn't answer (regression of the FRE-471 guard).
- **AC-7 (assembled seam) — the whole loop works end to end.** *Check:* on the cloud profile a real
  "build me an artifact about X" request surfaces the card, a **non-default** pick renders a correct,
  grounded artifact on the chosen model, and AC-1/AC-2 telemetry corroborate the model + lane.
  *Fails if* any leg (card → decision → resolved key → tool → correct model → correct lane) breaks.

**Seam owner:** AC-7 is owned by the **`artifact_draft` wiring ticket (Phase-1 step 4)** — it is the
child where the assembled intent first holds. The ADR does **not** close when step 5 (settings UI)
merges; it closes only when AC-7 is proven on the live cloud profile. Master asserts AC-7 at the
acceptance gate.

---

## References

- ADR-0044 — ExecutionProfile (local↔cloud model binding for `primary`/`sub_agent`)
- ADR-0076 — Constraint governance / DecisionCard (suspend/resume + standing preferences)
- ADR-0077 — `artifact_draft` tool (the real artifact builder)
- ADR-0079 — Profile is server-owned and per-session (amends ADR-0044 D2)
- ADR-0099 — `model_roles.yaml` role→model matrix + `resolve_role_model_key`
- ADR-0101 §8b / FRE-691 — cloud-attachment cost DecisionCard (live selection-card precedent)
- ADR-0086 — expansion/HYBRID decomposition (context for the routing-fork limitation; Stream-1, out of scope)
- FRE-478 / FRE-495 — artifact-builder output-cap / context-window incidents (large-output onboarding precedent)
- FRE-869 — `get_llm_client_for_key` budget-lane correctness
- `src/personal_agent/tools/artifact_tools.py:1436-1437` — current hardcoded `sub_agent` builder binding
- `src/personal_agent/orchestrator/executor.py:462` — `_maybe_pause_for_constraint` (reused mechanism)
- `src/personal_agent/config/settings.py:491-497` — `artifact_decomposition_enabled` (routing-fork switch)
- explore handoff seed — "Model Selection Layer + Artifact Pipeline Coherence" (2026-07-14)

---

## Status Updates

### 2026-07-14 - Proposed
**Changed By:** cc-adrs (Opus)
**Reason:** Initial proposal. Scoped to Phase 1 (user-selectable artifact builder) after owner
discussion settled on Option B (build-time DecisionCard) + a configurable default (Haiku). Phase 2
(orchestrator selector) documented as deferred. Stream-1 pipeline-coherence work is a separate ADR.
Revised after codex review round 1: honest enumeration of the full set of contract widenings
(options source, executor guard, event Literal, settings-API validation); AC-2 re-based on cost_gate
`budget_counters` (`api_costs` has no `budget_role` column); AC-3 re-framed as writer-binding
immutability (a candidate∩role-name check was mis-dimensioned); role name corrected to `embedding`;
Option 4 (governance gating) added. Round 2: added the `config/governance/budget.yaml` policy
dependency (no policy → no counter → AC-2 can't hold); separated the two identities (`budget_role` =
cost lane vs the `respond(role=…)`/`model_role` telemetry field — both must switch or AC-1 fails
silently); corrected the §1 claim that the resolver excludes `primary`/`sub_agent` (it doesn't —
they're intent-only by convention only); tightened AC-4 to set-equality against the availability-
filtered registry; tightened AC-5 to assert the preferred model actually builds, not just that the
card is suppressed.
