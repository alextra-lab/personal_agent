# ADR-0119: Config-management interface (Phase 1) — observe + open-role model selection

**Status:** Proposed
**Date:** 2026-07-16
**Deciders:** Owner (architect), cc-adrs (Opus)
**Tags:** config, model-selection, observability, pwa, human-in-the-loop

---

## Context

**What is the issue we're addressing?**

ADR-0099 gave Seshat a single-source role→model matrix (`config/model_roles.yaml`) plus a
cross-config validator — the config *substrate*. What it never got is a *surface*. Today you
tune the harness by editing YAML/env and rebuilding, and there is **no pattern to expose a
config value to the UI at all**: FRE-886 had to *hardcode* a PWA label for the attachment
default because none existed. This ADR is the missing capstone surface on ADR-0099.

The realization that shapes it: **"model selection" is not a feature — it is config.** Which
model builds an artifact, orchestrates a turn, or drives a sub-agent is a role→model binding —
exactly what the matrix governs. A picker is a *config surface over the matrix*, not a bespoke
gadget. Ship bespoke pickers (an artifact-builder picker, later an orchestrator picker, the
attachment chip beside them) and each re-implements the same expose→validate→persist plumbing.
Build the interface once and every role-selection is a panel.

Two axes must be kept separate (conflating them is what has tangled this before):
- **Delegation/isolation** (structural) — *should this work run in a context-isolated sub-agent?*
  Driven by noise. Artifacts and deep-research isolate; the sub-agent is a context firewall.
  This ADR does **not** touch that axis — sub-agents stay.
- **Model-per-role** (config) — *which model runs each role?* This is the selectable config,
  and the subject of this ADR.

Design constraints (owner, binding):
- **"Don't let this get too big."** Observe-first; a bounded editable set; defer the routing
  programs. If it sprawls in review, cut it back.
- **Frontier-flexible, low-fear.** Frontier harnesses let you pick the model for any open role
  and switch whenever (`/model`-style). We match that — no phasing editability by "stakes."
  The one real line: a user-selectable model must never reach a role that writes durable
  substrate (the KG/logs), where a wrong model corrupts permanent state silently.
- **Reuse ADR-0099** — read/write *through* the matrix + validator; no parallel source of truth.

A separate 2026-07-16 SOTA survey (`docs/research/2026-07-16-model-routing-sota-survey.md`)
settled the routing question that sits *behind* this: the only non-deterministic, latency-adding
routing pattern is a full LLM-in-the-loop router (GPT-5 style); every other production system
routes deterministically off the hot path. That confirms the config layer here is the right
foundation and that orchestrator-driven routing (Phase 2) is a *deterministic gateway
extension*, not an LLM router — but Phase 2 is out of this ADR's scope.

---

## Decision

Ship **Phase 1 of the config-management interface: an observe-first read view plus a bounded
editable set (open-role model selection + the FRE-886 attachment default), over one
expose→validate→persist pattern.** Seven parts:

**1. Observe-first read view.** Render the *resolved* config: the full role matrix (every
role→model binding for the active profile), the active ExecutionProfile, and a **curated short
list** of key live settings (the attachment default, cost caps, a few key flags) — **not** an
editor over the ~150 AppConfig params. Each role is marked **pinned vs open** so the
swappability principle is legible in the UI itself (you *see* that the KG-writers are pinned and
the open roles are selectable). "Observe-first" means *you can see the config instead of
grepping YAML* — it does **not** mean editability is gated or phased.

**2. Open-role model selection, frontier-flexible.** For every **open** role — `primary`
(orchestrator), `sub_agent`, `artifact_builder` — a picker over that role's vetted,
availability-filtered candidates; freely switchable, **effective on the next turn**. No phasing
by stakes: the orchestrator is as editable as the artifact builder (picking your primary model
is safe and normal — it orchestrates the turn, it does not write durable substrate).

**3. The FRE-886 attachment default.** Expose `attachment_default_processing_target` (the
Auto/local-vs-cloud vision default) through the same surface — the second editable domain, and
the first thing that proves the expose→persist pattern replaces FRE-886's hardcoded label.

**4. One write mechanism — a thin override the resolvers prefer.** A small override store records
two kinds of *selection state*, and the resolvers prefer it over the file/env default:
- **role→model selections** for open roles, consulted by both resolution paths so the user never
  sees which is behind a panel: matrix roles (`artifact_builder`, …) via `resolve_role_model_key`
  (`src/personal_agent/config/model_loader.py:144`) and profile roles (`primary`/`sub_agent`) via
  `resolve_model_key` (`src/personal_agent/config/profile.py:75`, ADR-0044). Note the two default
  sources differ by path: `artifact_builder`'s file default is the matrix; `primary`/`sub_agent`'s
  is the active ExecutionProfile (the resolver *can* read a `primary` matrix row if called
  directly, but by convention those roles default through the profile — the override sits in front
  of whichever path the role uses).
- **whitelisted setting overrides** — currently just the FRE-886 attachment default
  (`attachment_default_processing_target`, `src/personal_agent/config/settings.py:857`, read at
  `orchestrator/executor.py:1649`), which is env/AppConfig-backed today. The store holds the
  UI-set value and the read of that setting prefers the override over the env default — so a
  settings-value panel and a role panel share **one** store and **one** API (the reusable
  expose→validate→persist pattern), with no env mutation and no runtime YAML rewriting.
The matrix stays **canonical** for role→model (candidate space + pinned/open); the override is
*selection state* layered on top, not a redefinition — not a parallel source of truth (same shape
as ADR-0076 constraint preferences).

**5. Vetted candidate registry per open role** (generalize ADR-0118 T2's
`artifact_builder_candidates`): the selectable set + onboarding metadata (provider, decoding,
and — for the builder — large-output capability), **availability-filtered** at read time (a
local-only model is dropped when the SLM server is unservable; a candidate missing its secret is
dropped). A role's candidate list existing = the role is open; absence = pinned.

**6. Guardrail — writers pinned, by construction + fail-closed.** User-selectable models never
reach the durable-substrate writers (`entity_extraction`, `captains_log`, `insights`,
`embedding`, `reranker`, `reranker_fallback`). Enforced structurally (writers have no candidate
list → the override shim never consults an override for them) **and** by a fail-closed backstop
(a resolved key not in the role's candidate allow-list falls back to the file default) **and** by
server-side validation on the write API (never trust the client). This is the one real safety
line; everything the owner feels in real time is freely selectable.

**7. Config API — the reusable expose→validate→persist pattern, built once.** A read endpoint
(resolved bindings for all roles + pinned/open + candidates + effective value + availability +
the curated settings) that drives the view; a write endpoint (validate open-role + vetted +
available → override store) that every panel uses. This is the pattern whose absence forced
FRE-886 to hardcode.

**Explicitly deferred (documented, NOT in this ADR's scope):**
- **Phase 2 — orchestrator-driven routing.** A *deterministic* extension of the intent gateway
  (task-type/role → model), per the SOTA survey — **not** an LLM router. It *consumes* this
  layer's config; the routing programs (ADR-0082/0094/0095) are its home. Gated on their own
  measurement (FRE-432/516).
- **The build-time in-conversation model card** (ADR-0118 T3/T4) — a fast-follow that adds
  in-the-moment per-build override on top of the config default. Tickets kept, out of this ADR.
- **A general editor over all ~150 AppConfig params** — never; the read view is the matrix + a
  curated list, and only the two named domains are editable.

---

## Alternatives Considered

### Option 1: Bespoke pickers per role (the ADR-0118 T5 approach)
**Description:** Ship a standalone artifact-builder picker now, an orchestrator picker later, the
attachment chip separately.
**Pros:** each is small in isolation; fastest to a first visible picker.
**Cons:** every one re-implements expose→validate→persist to the PWA — the exact pain that made
FRE-886 hardcode a label. Produces throwaway UI the config interface would immediately unify and
replace, and no single place to *observe* config.
**Why Rejected:** it optimizes the first panel and pessimizes the third. Build the pattern once;
model-selection is its first consumer.

### Option 2: A general config editor over AppConfig
**Description:** Surface and edit the full ~150-param AppConfig.
**Pros:** complete; nothing is un-tunable from the UI.
**Cons:** enormous surface, most of it never tuned by hand; validation and blast-radius for
arbitrary params is unbounded; directly violates "don't get too big."
**Why Rejected:** observe-first + two demanded editable domains captures the value at a fraction
of the risk. Breadth is the anti-goal.

### Option 3: A per-user preference store separate from the matrix
**Description:** Keep model choice in its own preference table, independent of ADR-0099.
**Pros:** decoupled from config files.
**Cons:** a parallel source of truth for role→model — exactly what ADR-0099 exists to prevent;
the pinned/open distinction and candidate space would drift between the two.
**Why Rejected:** the override is *selection state on the canonical matrix*, not a second
definition. The matrix must remain the one source for what's allowed and what's pinned.

### Option 4: An LLM-in-the-loop router for model selection
**Description:** Let a model decide the per-turn model choice dynamically.
**Pros:** fully automatic; no user action.
**Cons:** the 2026-07-16 SOTA survey is decisive — this is the only non-deterministic,
latency-adding routing pattern, and the frontier avoids it. Non-reproducible and slow.
**Why Rejected:** wrong tool. Deterministic config + explicit override is the SOTA pattern for a
latency-sensitive single-user harness; automatic routing, if ever wanted, is a *deterministic*
gateway extension (Phase 2), not an LLM router.

---

## Consequences

### Positive Consequences
- **One expose→validate→persist pattern**, built once; every future role-selection is a panel,
  and FRE-886's hardcoded label is retired.
- **You can see and tune the harness** without editing YAML and rebuilding — the owner's stated
  goal.
- The matrix stays **canonical**; the guardrail (writers pinned) holds **by construction**.
- Frontier-flexible model choice on every open role, with the pedagogic "compare by using" loop.
- It is the **config foundation** a deterministic Phase-2 router would consume — not a dead end.

### Negative Consequences
- The override store + resolver shim must span **two** resolution paths (matrix + ExecutionProfile)
  — modest but real plumbing. The profile path is a handful of source sites, not a sprawl
  (`get_llm_client` ×4, `resolve_model_key` ×~8), and the override read is best injected inside the
  two resolver functions so callers are unchanged.
- A read/write config API and a PWA surface are net-new (though small and bounded).
- Per-candidate availability checking adds a liveness concern to the read path.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A selection reaches / perturbs a KG-writer binding | High | Writers have no candidate list (override never consulted) + fail-closed allow-list + server-side write validation; asserted by AC-2 |
| A selected model is unavailable (local down / missing secret) and breaks turns | Medium | Candidates availability-filtered at read time; AC-4 asserts dead candidates aren't offered |
| Scope creep into a full config editor | Medium | Observe-first + exactly two editable domains + explicit deferrals; cut back if review sprawls |
| Override store becomes a parallel source of truth | Medium | Override is selection-state only; the matrix remains canonical for candidate space + pinned/open; AC-6 asserts fallback to file default when cleared |

---

## Implementation Notes

**Files affected (Phase 1):**
- `config/model_roles.yaml` — generalize the candidate registry beyond `artifact_builder`
  (open-role candidate lists + onboarding metadata); writer roles unchanged (no candidate list).
- `src/personal_agent/config/model_loader.py` (`resolve_role_model_key`) and
  `src/personal_agent/config/profile.py` (`resolve_model_key`) — consult the override store,
  fail-closed to file default for non-candidate / pinned roles.
- **New:** override store — a small table + repository (role → selected key), mirroring
  `constraint_preferences_repository`. Migration in `docker/postgres/`.
- `src/personal_agent/service/app.py` — config read + write endpoints (validate against the
  registry; reject a pinned role or non-candidate key server-side).
- PWA — the config screen: observe view (matrix + pinned/open + curated settings) + open-role
  pickers + the attachment-default control; writes through the config API.
- **Absorb ADR-0118 T1/T2 — they are not yet built.** The `artifact_builder` role extraction
  (FRE-879) and candidate registry (FRE-880) are Needs-Approval, not done: `llm_client/types.py`
  has no `ARTIFACT_BUILDER` and `tools/artifact_tools.py:1437` still binds `sub_agent`. ADR-0119
  folds those two tickets in as its first sequencing step (re-homed under this chain), supersedes
  ADR-0118's UI half (T5), and defers the ADR-0118 build-time card (T3/T4). Reuse (already
  shipped): FRE-886's `attachment_default_processing_target` setting — surfaced, not re-implemented.

**Dependencies:** ADR-0099 (matrix + validator + `resolve_role_model_key`), ADR-0044/0079
(ExecutionProfile + `resolve_model_key`), ADR-0118 (artifact_builder role/registry — reused),
FRE-886 (attachment default — reused), the 2026-07-16 routing survey (Phase-2 posture).

**Testing:** unit tests for override resolution + fallback, writer-binding immutability under
selection, availability filtering, server-side rejection of a pinned/non-candidate write; a live
check that changing an open-role model takes effect on the next turn.

**Sequencing (Phase 1 tickets, one PR each):**
0. **(Absorbed from ADR-0118)** `artifact_builder` role extraction — `ModelRole.ARTIFACT_BUILDER`
   + matrix role + cost lane + telemetry identity + wire `artifact_draft` off `sub_agent` (FRE-879);
   vetted candidate registry for `artifact_builder` (FRE-880). Prerequisite — the role must exist
   before it can be a panel.
1. Override store (table + repo + migration) + the resolver-shim **inside** both
   `resolve_role_model_key` and `resolve_model_key` (fail-closed) + the attachment-setting override
   read. No UI yet; AC-1/AC-2/AC-6 provable at the API/resolver level.
2. Generalize the candidate registry from `artifact_builder` to the other open roles
   (`primary`, `sub_agent`) + availability filtering.
3. Config read/write API (validate through the registry; server-side guardrail rejecting
   pinned/non-candidate writes).
4. PWA observe view (matrix + pinned/open + curated settings).
5. PWA open-role pickers + attachment-default control (the editable panels). **(Seam ticket, AC-7.)**

---

## Verification / Acceptance Criteria

- **AC-1 — A selection takes effect.** *Check:* set a non-default model for an open role
  (e.g. `primary`); the next turn's `MODEL_CALL_COMPLETED` for that role shows the selected model
  id. *Fails if* the resolution ignores the override.
- **AC-2 — Overrides are ignored for pinned writer roles (the guardrail).** *Check:* inject an
  override row directly in the store naming a writer role (e.g. `entity_extraction → claude_haiku`),
  then assert `resolve_role_model_key('entity_extraction')` still returns its pinned key — and the
  same for every writer `r ∈ {entity_extraction, captains_log, insights, embedding, reranker,
  reranker_fallback}`. Separately, the write API rejects such a row 4xx before it can be stored.
  *Fails if* a writer role's resolution changes when an override for it is present. *(This is the
  discriminating form: a broken impl that reads overrides for all roles and leans only on "writers
  have no candidate list" would pass a byte-identical check under a benign selection, but fails here
  when an override for a writer actually exists.)*
- **AC-3 — Pinned roles are visible but not editable.** *Check:* the read payload marks every
  writer role `pinned` and exposes **no** candidate list / write path for it; a write attempt
  against a pinned role is rejected 4xx server-side. *Fails if* a writer is editable or absent
  from the view.
- **AC-4 — The candidate set is exactly the vetted, available set.** *Check:* for an open role,
  the offered candidates **equal** its registry list minus unavailable ones (local-only excluded
  when the SLM server is down; a secret-less candidate excluded), asserted on the read payload.
  *Fails if* a non-registry, dead, or missing-secret candidate is offered.
- **AC-5 — The attachment default is settable and drives behavior.** *Check:* set the attachment
  default to cloud via the surface → an Auto (no-override) image routes to Sonnet; set it back to
  local → the same image routes to Qwen (FRE-886's own AC, now driven from the UI). *Fails if* the
  setting doesn't change routing.
- **AC-6 — Overrides persist and fall back to the *per-path* canonical default.** *Check:* an
  override persists across turns/sessions; clearing it restores the resolver's canonical default —
  the **matrix file default** for `artifact_builder`, the **active ExecutionProfile default** for
  `primary`/`sub_agent` (they resolve via the profile, not the matrix). *Fails if* the override
  doesn't persist, or clearing it restores the wrong source (e.g. a matrix lookup for `primary`).
- **AC-7 (assembled seam) — the whole loop.** *Check:* open the config view, change the
  orchestrator model, the next turn runs on it (AC-1 telemetry), the writer bindings are unchanged
  (AC-2), and the picker showed only available candidates (AC-4). *Fails if* any leg breaks.

**Seam owner:** AC-7 is owned by the **PWA pickers ticket (step 5)** — the child where the
assembled intent first holds. The ADR does not close when the observe view (step 4) merges; it
closes only when AC-7 is proven live. Master asserts AC-7 at the acceptance gate.

---

## References

- ADR-0099 — single-source role matrix + validator (the config substrate this surfaces)
- ADR-0044 / ADR-0079 — ExecutionProfile + `resolve_model_key` (the `primary`/`sub_agent` path)
- ADR-0076 — constraint preferences (precedent: selection-state store the resolver prefers)
- ADR-0118 — user-selectable artifact builder (role extraction + candidate registry reused; its
  bespoke picker is superseded by this interface's panel; its build-time card deferred to a fast-follow)
- ADR-0082 / ADR-0084 — tier-aware selection + pedagogical counterweight (Phase-2 routing home)
- FRE-886 — attachment default setting (the second editable domain; retires its hardcoded PWA label)
- `docs/research/2026-07-16-model-routing-sota-survey.md` — routing SOTA (Phase-2 = deterministic, not an LLM router)
- `src/personal_agent/config/model_loader.py:144` (`resolve_role_model_key`) · `config/profile.py` (`resolve_model_key`)

---

## Status Updates

### 2026-07-16 - Proposed
**Changed By:** cc-adrs (Opus)
**Reason:** Initial proposal. Scoped Phase 1 (observe view + open-role model selection + FRE-886
attachment default) after extended owner discussion: model-selection reframed as config over the
ADR-0099 matrix; frontier-flexible editability (no stakes-phasing) with KG-writers pinned as the
one real line; sub-agents retained as context firewalls (model-per-role is the selectable axis);
orchestrator routing and the ADR-0118 build-time card explicitly deferred. Routing determinism/
latency concern settled by the 2026-07-16 SOTA survey. Reconciles ADR-0118 FRE-878–883: T1/T2
absorbed as this chain's first step (they are not yet built), T5 bespoke picker superseded, T3/T4
card deferred. Revised after codex review round 1: AC-6 corrected to per-path canonical default
(`primary`/`sub_agent` fall back to the ExecutionProfile, not the matrix); AC-2 rewritten to a
runnable guardrail check (inject a writer override → assert it is ignored); the write mechanism now
covers the FRE-886 attachment setting (env-backed) as a whitelisted setting-override, not only
role→model rows; and the ADR-0118 T1/T2 reconciliation corrected from "reuse as-is" to "absorb as
step 0" (they are Needs-Approval, not shipped).
