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
`get_llm_client(role_name="sub_agent")` (`tools/artifact_tools.py:1437`). On the cloud profile
that binding resolves to **Haiku** (`config/profiles/cloud.yaml` `sub_agent_model`). Three
problems follow from that one borrowed binding:

- **Tier-conflation.** "Best at reasoning" is not "best at emitting a 70 KB self-contained
  interactive HTML document." The builder should be selectable independently of the sub-agent
  tier, but today it is hostage to whatever `sub_agent` resolves to.
- **No identity.** Artifact builds carry no cost lane and no telemetry identity of their own —
  they bill and log as `sub_agent`/`main_inference` (`cost_gate/__init__.py:98`), so artifact
  spend and latency cannot be isolated.
- **No user choice.** The owner wants model choice as a first-class, user-facing capability —
  a **standing feature** (you compare builders by *using* them on real artifacts), not an eval —
  surfaced **at artifact-build time, in the conversation**, with a **configurable default**.

Two hard constraints govern the design:

- **Swappability tracks blast-radius × perceptibility.** A user-selectable model must never
  reach a backend *writer* role — the roles pinned in `config/model_roles.yaml`
  (`entity_extraction`, `captains_log`, `insights`, `reranker`, `embedder`) write durable
  substrate (KG, logs) where a wrong model drifts silently into permanent state. Float what you
  feel in real time; pin what corrupts silently.
- **Design constitution (owner):** lean, tight, legible, observable — no dead or orphaned code.
  "Lean" ≠ feature-minimal: every part earns its keep *and* a reader groks it in one pass. The
  cheapest correct mechanism that a reader can follow wins over a bespoke one.

**What needs to be decided:** how a user selects the artifact builder at build time; where the
selectable set and the default live; and how the selection reaches `artifact_draft` without
building a new elicitation mechanism or ever exposing a KG-writer as selectable.

---

## Decision

Introduce a **model-selection layer for *open* roles**, and ship its first consumer — a
**user-selectable artifact builder** — by composing mechanisms that already exist. Five parts:

**1. Extract a first-class `artifact_builder` open role.** Add `ModelRole.ARTIFACT_BUILDER`
(`llm_client/types.py`) and an `artifact_builder` entry in `config/model_roles.yaml` (the
ADR-0099 matrix) with a **default binding of `claude_haiku`** — Haiku stays the default because
it is proven good and grounded on real artifacts and is cheap. The role gets its **own cost lane**
(`cost_gate/__init__.py`: `artifact_builder → artifact_builder`, not `main_inference`) and its
own telemetry `model_role`. This alone dissolves the tier-conflation: the builder stops being
hostage to the `sub_agent` binding.

**2. A vetted candidate registry for the role.** The `artifact_builder` matrix entry carries not
one model but a **default plus a `candidates` list** — the vetted set a user may select. "Vetted"
is a per-model onboarding gate, not a free list-add: each candidate declares provider, decoding
params, known failure modes, and — specific to this role — **large-output capability** (a ~70 KB
artifact ≈ 20 K+ output tokens in one shot; providers differ on max-output / truncation). A model
absent from the registry is not selectable, period.

**3. Surface selection through the existing ADR-0076 DecisionCard, generalized for dynamic
options.** At artifact-build time the executor calls the existing pause helper
(`_maybe_pause_for_constraint`, `orchestrator/executor.py:462`) — the same machinery that powers
tool-approval and the ADR-0101 §8b cloud-attachment cost card. It already provides, for free,
everything the UX needs:

| UX requirement | Existing mechanism (ADR-0076) |
|---|---|
| Card at build time; turn suspends until you click | `ConstraintPauseEvent` → PWA `DecisionCard` → executor blocks on the waiter until `CONSTRAINT_DECISION` posts back (genuine one-turn suspend/resume, durably persisted) |
| Configurable default (settings page) | `user_constraint_preferences` (`service/models.py:419`) + `_load_constraint_preference` — a standing preference **pre-resolves the pause silently** (`constraint_preference_applied`), no card shown |
| "Ask me each build" toggle | the reserved preference value `always_pause` forces the card even when a default exists |
| "Remember this" (a pick becomes the default) | the in-card `remember` flag → `_save_constraint_preference` |
| Safe fallback on timeout / disconnect / headless | the **last option is auto-applied**; for this role the safe default is the configured default (Haiku) — never a zero-artifact stall |
| Guardrail | the card only lists what the options generator emits |

The **one real extension** is that existing decisions have *static, hand-listed* options
(`CONSTRAINT_OPTIONS`, two fixed buttons). The builder's options are **dynamic** — computed from
the registry and **availability-filtered** at pause time (a local-only builder is dropped when the
local SLM server is not servable). So the pause path gains the ability to accept a
**computed option list** instead of only a static `CONSTRAINT_OPTIONS[constraint]` lookup. This
generalizes the ADR-0076 mechanism rather than forking a parallel one (the ADR-0101 §8b
`attachment_cost` card already stretched the "constraint" concept to cover a *choice*, so the
precedent is established).

**4. Wire `artifact_draft` to the resolved builder.** Replace the hardcoded
`get_llm_client(role_name="sub_agent")` (`artifact_tools.py:1437`) with a resolved builder key →
`get_llm_client_for_key(builder_key, budget_role="artifact_builder")` (`factory.py:125` — the
correct call for a caller holding an already-resolved key; `get_llm_client` would mis-bill it,
FRE-869). The key comes from the card decision when present, else from
`resolve_role_model_key("artifact_builder")` (the default binding) for headless / no-WS / timeout
paths.

**5. Guardrail by construction, plus a fail-closed backstop.** The card can only offer registry
candidates, and the registry contains only open roles — a KG-writer is structurally unrepresentable
as an option (invalid states cannot be selected, versus validate-and-reject after the fact). A
second, defensive check: before `artifact_draft` uses a resolved key it asserts the key is in the
`artifact_builder` candidate set; anything else fails **closed to the default builder**, never to
an arbitrary model.

The PWA settings surface writes the standing preference (default builder + ask-each-time) through
the existing `constraint_preferences_repository` — no new store.

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

**Why Rejected:** All net-new cost, zero capability gain over generalizing ADR-0076. The only real
delta (dynamic options) is a small extension to the existing helper, not a new mechanism.

---

## Consequences

### Positive Consequences

- The artifact builder is **independently selectable** and gains its **own cost + telemetry
  identity**; artifact spend/latency become isolable for the first time.
- The `sub_agent` tier-conflation is **dissolved** — one change, two wins (independent binding +
  own budget lane).
- The guardrail holds **by construction**: a KG-writer cannot appear as an option.
- **Small blast radius, high legibility** — the feature is ~one new role + a registry list + a
  ~one-parameter generalization of a proven helper + one tool call site. A reader follows it in one
  pass. This is "lean" as the constitution defines it.
- The layer **generalizes** to the orchestrator selector (Phase 2) with no rework.

### Negative Consequences

- ADR-0076's pause helper must accept **computed options** (the one real mechanism change) — a
  small generalization, but it widens a previously-closed surface (`ConstraintPauseEvent.constraint`
  is a closed `Literal`; it must admit `artifact_builder`).
- **Per-model onboarding is a manual gate** — intended (vetting is the point), but a standing
  maintenance cost: adding a provider is not a one-line list-add.
- **The card only fires on the `artifact_draft` path.** A "build X" request that routes to the
  expansion/HYBRID path (ADR-0086 decomposition) reaches no builder and therefore no card. In
  practice the `artifact_draft` path dominates (expansion fires ~1×/90d; deterministic fallback
  dominates), so the gap is narrow — but it is a real dependency on the routing-fork, tracked
  separately (see Risks; Stream-1 routing-coherence is a distinct future ADR, deliberately out of
  scope here).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A selected model can't emit a 70 KB artifact (truncation) and produces a broken doc | Medium | Onboarding **must** record large-output capability; the registry excludes models that fail the max-output check; FRE-471 truncate-with-warning guard already prevents zero-artifact |
| A user-selectable key reaches a KG-writer | High | Registry contains only open roles (by construction) **plus** a fail-closed assertion in `artifact_draft` that rejects any non-candidate key to the default |
| A dead local builder is offered on cloud and the build fails | Medium | Options are **availability-filtered** at pause time; AC-4 asserts local-only builders are absent when the SLM server is unservable |
| Card interrupts flow / no answer stalls the build | Medium | Standing default pre-resolves silently; `always_pause` is opt-in; timeout/disconnect auto-applies the safe default — the build always completes |
| Routing fork sends elaborate requests away from the builder → no card | Low | Documented limitation; narrow in practice; fixed by separate Stream-1 routing-coherence work, not this ADR |

---

## Implementation Notes

**Files affected (Phase 1):**
- `src/personal_agent/llm_client/types.py` — add `ModelRole.ARTIFACT_BUILDER`.
- `config/model_roles.yaml` — new `artifact_builder` entry: default `claude_haiku` + `candidates`
  list; keep pinned writer roles unchanged.
- `config/models.yaml` — ensure each candidate carries max-output / large-output metadata.
- `src/personal_agent/config/model_loader.py` — `resolve_role_model_key` already resolves roles;
  extend candidate/onboarding validation.
- `src/personal_agent/cost_gate/__init__.py` — map `artifact_builder` to its own budget lane.
- `src/personal_agent/orchestrator/constraint_options.py` + `executor.py` — generalize
  `_maybe_pause_for_constraint` to accept a **computed** option list; add the `artifact_builder`
  decision path with availability filtering.
- `src/personal_agent/transport/events.py` — admit `artifact_builder` on `ConstraintPauseEvent`
  (widen the closed `Literal`).
- `src/personal_agent/tools/artifact_tools.py:1437` — resolve builder key →
  `get_llm_client_for_key(builder_key, budget_role="artifact_builder")`; fail-closed guardrail.
- PWA settings surface — write default-builder + ask-each-time via
  `service/repositories/constraint_preferences_repository.py` (reused, no new store).

**Dependencies:** ADR-0076 (DecisionCard / constraint pause), ADR-0044 (ExecutionProfile),
ADR-0099 (`model_roles.yaml` matrix + `resolve_role_model_key`), ADR-0077 (`artifact_draft`),
ADR-0101 §8b / FRE-691 (selection-card precedent), FRE-869 (budget-lane correctness).

**Testing strategy:** unit tests for registry/default resolution, guardrail rejection of a
non-candidate key (fail-closed to default), dynamic-option generation + availability filter,
preference pre-resolution vs `always_pause`, and safe-default-on-timeout. Live check on the cloud
profile drives a real build with a non-default pick.

**Sequencing (Phase 1 tickets, one PR each):**
1. `artifact_builder` role + cost lane + telemetry identity (default Haiku) — no behaviour change
   yet (still resolves to Haiku, but via its own role/lane).
2. Vetted candidate registry + onboarding metadata (incl. large-output capability).
3. Generalize the ADR-0076 pause helper for computed, availability-filtered options + the
   `artifact_builder` decision path.
4. Wire `artifact_draft` to the resolved builder + fail-closed guardrail. **(Seam ticket.)**
5. PWA settings surface for default + ask-each-time.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 — A non-default pick actually runs on that model.** *Check:* build an artifact after
  selecting a non-Haiku builder; the `artifact_draft` span in ES (`model_call_completed` /
  route-trace) shows the **selected** `model_id`. *Fails if* the span still shows Haiku regardless
  of selection (plumbing not wired — the classic "label changed, call didn't").
- **AC-2 — Default builds bill and log to the `artifact_builder` lane, not `sub_agent`/
  `main_inference`.** *Check:* a default (no-selection) build shows `budget_role="artifact_builder"`
  in the cost-gate ledger / `api_costs` and `model_role="artifact_builder"` in telemetry.
  *Fails if* spend still lands on `main_inference` (role not truly extracted — only renamed).
- **AC-3 — A user-selectable model can never be a backend writer.** *Check:* a test asserts the
  `artifact_builder` candidate set ∩ {`entity_extraction`, `captains_log`, `insights`, `reranker`,
  `embedder`} = ∅, **and** feeding a writer-role key into the builder resolver returns the default
  (fail-closed), not the writer. *Fails if* any writer key is accepted by the builder path.
- **AC-4 — The card offers only currently-servable builders.** *Check:* on the cloud profile with
  the local SLM server unservable, the emitted `ConstraintPauseEvent.options` **exclude** local-only
  candidates. *Fails if* a dead local builder appears as selectable (it would then fail the build).
- **AC-5 — A standing default pre-resolves silently; `always_pause` shows the card.** *Check:* with
  a stored default preference, `constraint_preference_applied` is logged and **no**
  `ConstraintPauseEvent` is emitted for the build; with the preference set to `always_pause`, a
  `ConstraintPauseEvent` **is** emitted. *Fails if* the card shows despite a set default, or never
  shows under `always_pause`.
- **AC-6 — No answer never means no artifact.** *Check:* simulate no-WS / timeout on a build; the
  artifact still renders, produced by the default builder. *Fails if* the build stalls or yields
  zero artifact when the user doesn't answer (regression of the FRE-471 guard).
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
- FRE-869 — `get_llm_client_for_key` budget-lane correctness
- `src/personal_agent/tools/artifact_tools.py:1437` — current hardcoded `sub_agent` builder binding
- `src/personal_agent/orchestrator/executor.py:462` — `_maybe_pause_for_constraint` (reused mechanism)
- explore handoff seed — "Model Selection Layer + Artifact Pipeline Coherence" (2026-07-14)

---

## Status Updates

### 2026-07-14 - Proposed
**Changed By:** cc-adrs (Opus)
**Reason:** Initial proposal. Scoped to Phase 1 (user-selectable artifact builder) after owner
discussion settled on Option B (build-time DecisionCard) + a configurable default (Haiku). Phase 2
(orchestrator selector) documented as deferred. Stream-1 pipeline-coherence work is a separate ADR.
