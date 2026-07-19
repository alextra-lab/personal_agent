# ADR-0121: Model Catalog and Selection Layer — providers, deployments, bindings; the user selects the model

**Status:** Proposed
**Date:** 2026-07-19
**Deciders:** Owner (architect), cc-adrs (Opus)
**Tags:** config, model-routing, provider-abstraction, pwa, human-in-the-loop, observability

---

## Context

**What is the issue we're addressing?**

Seshat's model configuration accreted through project history rather than being designed.
`config/models.yaml` conflates three different things in one flat map, and the confusion has
started producing bad work downstream.

**1. The key namespace is half slot-names, half model identities.**

```
primary            -> unsloth/qwen3.6-35-A3B        ← a role name, used as a model key
sub_agent          -> unsloth/qwen3.6-35-A3B-subagent
compressor         -> gpt-5.4-nano (local file) / gpt-5.4-mini (cloud file)
claude_haiku       -> claude-haiku-4-5-20251001     ← an actual model identity
gpt-5.4-mini       -> gpt-5.4-mini
```

`compressor` is not a model — it is nano in one profile and mini in the other. Any catalog of
"models and their attributes" keyed on this namespace would be asserting attributes about a slot.
This is the root defect: **role names leaked into the model namespace**, so a model cannot be named
independently of the job it currently happens to do.

**2. The two catalog files carry almost no divergence.** `config/models.yaml` and
`config/models.cloud.yaml` are identical in **11 of 12 entries**. The single difference is
`compressor` (nano vs mini) — which `config/model_roles.yaml`'s own comment already flags as
"uncorrected drift, unresolved." Meanwhile placement is *already* expressed per-entry by
`provider_type`/`endpoint`. The two-file split, the `active_profiles` path map, and
`AGENT_MODEL_CONFIG_PATH` exist to select between two near-identical files.

**3. There is no provider entity, so one is reconstructed from strings at runtime.**
`endpoint: https://slm.example.com/v1` is copy-pasted onto 5 of 12 entries.
`llm_client/concurrency.py:48` defines `infer_provider_type(endpoint)` — it *parses the URL* to
recover the provider — and keeps `_endpoint_provider_type` keyed by normalized endpoint
(`concurrency.py:202`). `config/profiles/cloud.yaml` independently carries
`delegation.escalation_provider: anthropic`. The provider concept already exists twice, implicitly,
in incompatible forms.

**4. Nothing types a model.** No field says whether an entry is an LLM, an embedding model, or a
reranker; whether it can reason; or what effort levels it supports. Role→model correctness is held
only by convention — nothing structurally prevents the `embedding` role from binding to a chat
model.

**5. Deployment constraints are frozen into role bindings.** `sub_agent` points at a distinct
`-subagent` deployment with `disable_thinking: true`, `max_tokens: 2048`, and a 32768 context. That
is not a design decision — it is "I could not serve two full-strength models simultaneously on a
laptop," baked into the architecture as though sub-agents were an inferior tier. The constraint is
real; its expression is in the wrong place. On cloud it does not exist at all.

**What this cost us.** ADR-0118 and ADR-0119 were written *over* this substrate instead of fixing
it. Both are Proposed, neither is built, and the work they generated was unsound:

- **FRE-880** specified an `artifact_builder_candidates` registry carrying "provider, decoding
  params, known failure modes, and large-output capability" — duplicating fields
  `ModelDefinition` already has, justified by the claim that `ModelDefinition` "carries only
  `max_tokens` and no capability field." That claim is false: `llm_client/models.py:173-187` has an
  established `supports_*` capability convention. Its acceptance gate ("a candidate whose
  `max_tokens` does not clear the threshold fails loader validation") asserts only that a human
  typed a large enough number into YAML — it cannot fail for the reason that matters, because
  FRE-478 was a *provider-side* cap hit mid-generation. And "known failure modes" is prose in
  config that no code reads.
- **ADR-0119 §2/§4** then had to introduce an "override-vs-profile crossing" rule — the default
  respects the profile, an explicit override may cross to cloud — to paper over the fact that
  placement and model choice were the same axis.
- **FRE-879** shipped a flat matrix row for `artifact_builder` that silently routed *local*
  builds to cloud Haiku, caught in code review. The regression was a direct consequence of the
  substrate: there was no coherent place for an open role's binding to live.

**The user-facing half of the same problem.** The PWA exposes a **Path** toggle — local vs cloud —
which is `ExecutionProfile` (ADR-0044/0079) wearing a user-facing name. Path bundles model choice,
placement, concurrency, cost limits, and delegation rules into one two-valued switch. The owner's
requirement is explicit: **stop using Path; let the user select the model by name**, the way
frontier harnesses do. Placement should be a *visible attribute* of the model you pick, not a mode
you enter.

**What needs to be decided:** how models, providers, and role bindings are represented; how a user
selects a model; what happens to Path; and what stays pinned.

---

## Decision

Replace the flat model map with a **three-layer normalized configuration** — providers →
deployments → role bindings — and build a **selection layer** on top of it in which **the user
selects the model by name** and Path is removed.

### 1. Three layers

**Layer 1 — Providers.** The backend you talk to. One entry per provider, not per model.

```yaml
providers:
  slm_local:  { base_url: …, auth: none,                 placement: local, max_concurrency: N, health: … }
  anthropic:  { base_url: …, auth_env: ANTHROPIC_API_KEY, placement: cloud, max_concurrency: N }
  openai:     { … }
  voyage:     { … }
```

A provider owns: endpoint, authentication, **placement** (`local`/`cloud`), total concurrency
capacity, and health. `infer_provider_type`'s URL string-parsing is deleted;
`provider_type` on models and profiles is deleted (it is now derived from
`providers[p].placement`).

**Layer 2 — Deployments (the model catalog).** One entry per *deployment*, keyed by a stable
alias naming the **model**, never the role. Each references a provider.

```yaml
models:
  qwen3.6-35b-thinking: { provider: slm_local, id: unsloth/qwen3.6-35-A3B, kind: llm, … }
  qwen3.6-35b-instruct: { provider: slm_local, id: unsloth/qwen3.6-35-A3B-subagent, kind: llm, … }
  claude_haiku:         { provider: anthropic, id: claude-haiku-4-5-20251001, kind: llm, … }
  qwen3-embedding-0.6b: { provider: slm_local, id: Qwen/Qwen3-Embedding-0.6B, kind: embedding, … }
```

Keyed by *deployment*, not by weights, because two deployments of the same weights are separately
servable, separately sized, and separately available — which is exactly the two-qwen case. The
existing `claude_haiku` → `claude-haiku-4-5-20251001` pattern (stable alias → versioned provider
id) is the correct one and is generalized; versioned provider ids churn on every model bump and
must not be the key that role bindings reference.

**The catalog is profile-independent and single.** `config/models.cloud.yaml`, the
`active_profiles` path map, and `AGENT_MODEL_CONFIG_PATH` are deleted. A catalog that exists once
cannot diverge.

**Layer 3 — Role bindings.** Per role: which deployment, plus the per-use parameters.

```yaml
roles:
  primary:            { open: true,  default: qwen3.6-35b-thinking, effort: high }
  sub_agent:          { open: false, default: qwen3.6-35b-instruct, effort: off, max_tokens: 2048 }
  artifact_builder:   { open: true,  default: claude_haiku }
  entity_extraction:  { open: false, default: gpt-5.4-mini }
  vision:             { open: false, default: … }
```

**Decoding parameters and effort live on the binding, not on the model**, because they are per-use.
This is what dissolves the `primary`/`sub_agent` duplication: they stop being two "models" and
become two bindings of one model at different effort — which is what they always were.

### 2. Deployment metadata — rich enough to select on

The catalog is read by two consumers: a **human picker** and (later, the future sub-agent ADR) the **primary model
choosing a sub-agent**. Both must be able to choose on relevant detail, so each deployment carries:

| Group | Fields |
|---|---|
| Identity | key, `provider`, provider model id, display name, `status` (`active`/`preview`/`deprecated`), knowledge cutoff |
| Kind | `kind: llm \| embedding \| reranker`; `dimensions` for embedding |
| Reasoning | `reasoning_capable`, `effort_levels: […]`, `default_effort`, `can_disable_thinking` |
| Limits | `context_length`, `max_output_tokens` |
| Capabilities | `supports_function_calling`, `supports_vision`, `supports_pdf_document`, `supports_structured_output`, `supports_streaming`, `supports_prompt_caching` |
| Cost | `input_per_mtok`, `output_per_mtok`, `cached_input_per_mtok` |
| Capacity | deployment concurrency sub-limit (under the provider ceiling) |
| Decoding defaults | temperature, top_p, top_k, min_p, penalties — overridable per binding |
| `summary` | one line of intended use, for the picker and for machine selection |

**The line that keeps this from rotting** (and from repeating FRE-880): **the catalog carries
*declared* facts — from the provider's model card and this deployment's configuration. Telemetry
carries *observed* behaviour — latency, truncation, actual spend. The UI and the orchestrator join
the two at read time.** "This model truncated at 40 KB on 2026-07-12" is an observation: it belongs
in telemetry and research, never hand-typed into config where it silently goes stale. No prose
failure-mode fields.

### 3. The user selects the model; Path is removed

**`primary` is user-selected by name.** One control, listing catalog deployments where `kind: llm`,
the role is `open`, and the provider is available. Placement, cost, and context are **displayed**
per option — you choose with the tradeoff visible rather than entering a "local mode."

**Path is deleted as a concept**: the profile pill, `localStorage seshat_profile`,
`config/profiles/{local,cloud}.yaml` as binding sets, `provider_type` on profiles, and the
local/cloud framing throughout. What replaces its *one-tap ergonomics* is not a mode but the fact
that there is now only one control to set.

`GET /api/inference/status?profile=` becomes provider health — which is what it already does
(`useInferenceStatus.ts`: local probes the SLM tunnel, cloud checks provider configuration), merely
keyed on the wrong dimension. Re-keyed to provider, it is also the availability filter the picker
needs. `ClassifiedErrorCard`'s "switch to cloud and re-send" (FRE-399) becomes "retry on \<model\>".

### 4. Selection state is server-authoritative — ADR-0079's rules carried forward verbatim

Model selection is session-scoped selection state, structurally identical to the execution profile
it replaces. **ADR-0079's invariants are not re-derived; they are inherited**, because they were
paid for with a live incident (FRE-416/419) and every one of them transfers:

1. **The server is the single source of truth.** Clients hydrate and reconcile; `localStorage` is a
   cache, never the authority.
2. **Persistence is an explicit stored value**, never a silent parameter fallback.
3. **Resolution is asymmetric by session existence.** Existing session → the stored selection wins
   and a supplied value is advisory and ignored. New session → adopt the supplied value. *Including
   ADR-0079's post-deploy correction*: the variant where the client stops sending and the server
   defaults silently created every Cloud session as local. The client keeps sending; the server
   decides.
4. **The control is a write** — `PATCH /api/v1/sessions/{id}`, requiring `sessions:write`, resolving
   the CF Access user and scoping the repository write to that `user_id`, 404 on mismatch.
5. **Hydration at every entry point** — the HTTP session GET on mount *and* a `STATE_DELTA` on WS
   reconnect (the socket only connects on first send).
6. **Notify the single active socket**, not a broadcast (ADR-0075 evicts prior sockets with 4001).
7. **In-flight turns keep the selection resolved at launch.** A change affects subsequent turns
   only — this is the mid-turn-switch answer, already solved.
8. **Durability over delivery.** Correctness rests on the persisted row plus hydration; the event is
   an optimization that may fail.
9. **An offline change is a failed write** — revert and surface it; never optimistically diverge.
10. **Provenance is emitted** (`server-hydrated` / `localStorage` / `default`) so a flip is
    attributable.
11. **The session row is the authority** — the precondition for the collaborative-sessions north
    star; no choice here assumes a single client forever.

Mechanically this is a **selection store** (session → role → deployment key), the same shape as
ADR-0076's constraint preferences: selection state layered over the canonical binding defaults, not
a second definition of them.

### 5. Role tiers — who chooses, and when

The organizing principle is **selection authority**:

| Role | Authority | When | Surface |
|---|---|---|---|
| `primary` | **User** | Standing (per session) | One picker — replaces Path |
| `artifact_builder` | **User**, over a configured default | **Per build** | DecisionCard (ADR-0122) |
| `sub_agent` | Deployment default now; **orchestrator per dispatch** later (the future sub-agent ADR) | — | None yet |
| `vision` | Deployment | Never | None — pinned |
| Writers — `entity_extraction`, `captains_log`, `insights`, `embedding`, `reranker`, `reranker_fallback` | Deployment | Never | None — pinned |

**`vision` is pinned.** Its prompt and PDF pipeline are model-coupled (ADR-0102), so it is
deployment-configured with no user control. This removes the FRE-886 attachment local/cloud chip —
the last surviving Path instance — rather than leaving the concept alive in one corner.

**`sub_agent` is not user-selectable.** Its model choice belongs to the orchestrator at dispatch
time, alongside effort and thinking/instruct mode — the pattern this project's own master/dispatcher
uses. That requires sub-agent invocation to become model-invoked rather than gateway-assessed
(Stage 5 currently decides `SINGLE/HYBRID/DECOMPOSE/DELEGATE` before the primary runs, and
`expansion.py:109` then hardcodes `get_llm_client(role_name="sub_agent")`). **That change is
deliberately out of scope** — it is a request-pipeline change, not a config change. This ADR lands
the plumbing so it is ready: `sub_agent` resolves through the catalog, effort is per-call
overridable, and `sub_agent_types.py:48` already carries a per-spawn `model_role` field.

**This is not the LLM-router pattern the 2026-07-16 SOTA survey rejected**, and the distinction
should be recorded because it will be raised. A rejected LLM router is an *extra* model call in the
hot path whose only job is to classify and dispatch — latency-adding and non-deterministic. An
orchestrator choosing a sub-agent's model is filling in *parameters of a tool call it was already
making*: zero extra calls, zero added latency. The survey's conclusion stands.

### 6. Guardrail — writers pinned, structurally and fail-closed

A selectable model never reaches a durable-substrate writer. Enforced three ways:

- **Structurally** — `open: false` roles are not consulted against the selection store at all.
- **Fail-closed** — a resolved key that is not a valid, `kind`-compatible, available catalog entry
  falls back to the role's configured default, never to an arbitrary model. This becomes
  load-bearing under the future sub-agent ADR, when a *model* proposes the key.
- **Server-side validation** — the write API rejects a selection naming a pinned role or a
  non-catalog key. Never trust the client.

Authorization is the **intersection** of a model-side and a role-side fact: `kind` compatibility
(intrinsic to the model) ∩ `open` (a blast-radius policy on the role). Both halves are required.
Putting authorization wholly on the model — an `authorized_roles` list per model — was considered
and rejected: it makes "writers are not user-selectable" an emergent property of N model entries,
so a thirteenth model added without omitting `entity_extraction` silently opens the guardrail. The
role-side flag fails closed for every model at once, including ones added later.

### 7. Migration is behaviour-preserving, and proven so

This refactor touches live model resolution. Its failure mode is a role silently resolving to the
wrong model — precisely the FRE-879 regression that triggered this ADR. Therefore the refactor
ships with a **snapshot assertion**: every `(role, profile) → fully-resolved ModelDefinition` is
captured before the change and asserted **byte-identical** after. Any intended delta — notably
`compressor`'s nano/mini drift — must be an explicit, reviewed, separately-committed change, not a
silent side effect of the refactor.

### 8. What this supersedes

- **ADR-0118** (Superseded) and **ADR-0119** (Superseded) — both Proposed, neither built, both
  written over the broken substrate. Their salvageable content is carried forward here and in
  ADR-0122 (see References and ADR-0122 §Context).
- **ADR-0044 D1/D2** (Superseded in part) — profile-based execution configuration and dual-harness
  simultaneous operation. **D3, D4, D5 survive** and are explicitly *not* retired: D3
  (cross-profile escalation) is carried into the future sub-agent ADR, D4 (external agent harnesses as delegation
  targets) is orthogonal to Path and belongs with ADR-0050, D5 (profile-aware observation)
  transforms rather than dies (below).
- **ADR-0079** (Superseded in subject, inherited in substance) — the profile it governs is
  removed, but all eleven invariants in §4 are carried forward to the selection store.
- **ADR-0099** (Amended) — the role→model matrix survives as Layer 3 bindings, but its
  cross-profile divergence guard (`config_guard.py:801-830`) largely becomes *unnecessary* rather
  than updated: a single catalog cannot diverge. The guard's remaining useful job is asserting that
  every binding references an existing catalog key and a `kind`-compatible one.

**D5's telemetry migration is explicit, not incidental.** `TraceContext.profile`
(`telemetry/trace.py:50`, default `"local"`) is a live field feeding per-profile cost dashboards.
Under this ADR the dimension becomes **provider + model**, which is strictly more useful — spend
attributable to the model that incurred it rather than to a two-valued mode — and is the join key
ADR-0120 needs for per-provider cost instrumentation. The field is migrated, and dashboards keyed
on it are updated in the same wave; it is not dropped.

---

## Alternatives Considered

### Option 1: Keep the flat model map; add the candidate registry as specified (ADR-0118/0119)
**Description:** Leave `models.yaml`/`models.cloud.yaml` and the profile mechanism as they are, and
add `artifact_builder_candidates` (later generalized per-open-role) as a sibling config block with
onboarding metadata, per FRE-880.
**Pros:**
- Far smaller diff; no migration risk to live model resolution.
- Ships a visible picker sooner.
**Cons:**
- Builds a management surface over an incoherent catalog — the UI would render slot-aliases as
  though they were models, and `compressor` as though it were one thing.
- The registry duplicates `ModelDefinition` fields, creating a second place model facts live and
  drift.
- It cannot express the two-qwen case honestly, and it leaves the deployment constraint frozen into
  role bindings.
- It requires the "override-vs-profile crossing" rule, which exists only because placement and
  model choice are the same axis.
**Why Rejected:** it polishes the defect rather than fixing it. The owner's explicit direction after
review of FRE-880 was to refactor the substrate rather than build over it.

### Option 2: Normalize the catalog, but keep Path as the user-facing control
**Description:** Do the providers/deployments/bindings refactor, but retain the local/cloud profile
pill as the selection surface; model choice remains implied by placement.
**Pros:**
- One-tap ergonomics preserved with zero new UI.
- No changes to `ExecutionProfile`, session state, or the PWA.
- Substantially smaller blast radius.
**Cons:**
- Keeps a two-valued switch as the interface to an N-model catalog — the picker's whole value is
  choosing *among models*, which Path cannot express.
- Placement stays a mode rather than an attribute, so the catalog's cost/context/capability metadata
  has nowhere to inform a decision.
- The `vision` chip and any future per-role choice keep re-deriving the same toggle.
**Why Rejected:** the stated requirement is model selection by name, matching frontier harnesses.
Path is the thing being removed, not the thing being kept.

### Option 3: Presets over per-role selection (profiles as sugar)
**Description:** Keep a named binding-set concept — "Local", "Cloud", custom — as a one-tap
shortcut that writes several role selections at once, with per-role override underneath.
**Pros:**
- Preserves Path's one-tap ergonomics while making per-role selection the real mechanism.
- A user wanting "everything cheap/offline" gets it in one action.
**Cons:**
- Only `primary` and `artifact_builder` are user-selectable, and `artifact_builder` is chosen
  per-build rather than standing — so a preset would write exactly one standing value. It is a
  shortcut for a single control.
- Reintroduces a named binding-set as a persisted concept, which is what Path is; the confusion it
  causes would return with it.
**Why Rejected:** it solves an ergonomics problem that this role model does not have. Worth
revisiting only if the set of standing user-selectable roles grows.

### Option 4: `authorized_roles` on each model (model-side authorization)
**Description:** Express the guardrail as a per-model list of roles that model may hold, rather than
an `open`/pinned flag on the role.
**Pros:**
- One place answers "what may this model do," which reads well when onboarding a model.
**Cons:**
- Makes "writers are not user-selectable" an emergent property of N model entries — a new model
  added without omitting `entity_extraction` silently opens the guardrail, with no single place to
  see it. Fail-open by omission.
- Conflates two different gates: capability (an embedding model *cannot* be `primary` — intrinsic,
  derivable from `kind`) and policy (a user *may not* select `entity_extraction`'s model — a
  blast-radius decision about the role, not the model).
**Why Rejected:** the policy half must be role-dimensioned to fail closed. `kind` on the model plus
`open` on the role gives the same UI matrix (rendered as a join) with neither half able to drift
open.

### Option 5: A general config editor over AppConfig
**Description:** Surface and edit the full ~150-parameter `AppConfig`.
**Pros:** complete; nothing un-tunable from the UI.
**Cons:** unbounded validation surface and blast radius for arbitrary parameters; most parameters
are never hand-tuned; directly violates the owner's "don't let this get too big."
**Why Rejected:** breadth is the anti-goal. The observe view plus one selectable role captures the
value at a fraction of the risk.

---

## Consequences

### Positive Consequences

- **Models can be named.** A catalog keyed by model identity makes every downstream feature —
  pickers, per-model cost attribution, orchestrator selection — expressible for the first time.
- **The two-qwen case is representable and honest**, and the laptop constraint moves from a frozen
  role binding to provider capacity, where it belongs and where it disappears on cloud.
- **Single source of truth.** One catalog cannot diverge; ADR-0099's divergence guard becomes
  largely unnecessary rather than a thing to maintain.
- **The provider entity exists once**, replacing URL string-parsing and two implicit copies. Health
  and capacity get a natural home, and availability filtering becomes a provider lookup rather than
  a per-candidate liveness concern.
- **Placement becomes information, not a mode** — you pick a model seeing its cost, context, and
  where it runs.
- **Cost attribution improves**: telemetry keyed on provider + model instead of a two-valued
  profile, which is the dimension ADR-0120 needs.
- **The plumbing for orchestrator-chosen sub-agents lands without the pipeline change**, so
  the future sub-agent ADR is a scoped request-pipeline debate rather than a config-and-pipeline debate at once.

### Negative Consequences

- **This is a live-resolution refactor.** Every role's model resolution path changes. The snapshot
  assertion (§7) makes it provable, but the risk is real and the blast radius is the whole harness.
- **Path removal touches the PWA, session state, the gateway session API, and telemetry
  simultaneously** — the profile pill, `localStorage`, `execution_profile` on `sessions`, the
  inference-status endpoint, `TraceContext.profile`, and any Kibana panel keyed on profile.
- **Existing sessions carry an `execution_profile` value that no longer means anything.** Migration
  must map it to an initial `primary` selection rather than dropping it, or in-flight sessions
  change model silently on deploy.
- **Two ADRs are superseded and two amended**, so anything citing ADR-0044/0079/0099/0118/0119 needs
  a reconciliation pass.
- **Richer per-deployment metadata is more config to maintain** — mitigated by the declared-vs-
  observed line (§2), which keeps it to facts a model card supplies rather than accumulated
  experience.
- `sub_agent` is temporarily *less* configurable than ADR-0119 proposed (no user picker), by
  design, until the future sub-agent ADR gives it the right mechanism.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A role silently resolves to a different model after the refactor (the FRE-879 class) | **High** | §7 snapshot assertion: `(role, profile) → resolved ModelDefinition` byte-identical pre/post; AC-1 |
| A user selection reaches a pinned writer role | **High** | Three-layer guardrail — structural (`open: false` never consulted), fail-closed allow-list, server-side write rejection; AC-4 |
| Path removal desyncs client and server (the FRE-416/419 incident class) | **High** | ADR-0079's eleven invariants inherited verbatim (§4); AC-6 asserts the new-session case that the original correction fixed |
| Existing sessions change model on deploy | Medium | Migration maps each session's `execution_profile` to the equivalent `primary` selection; AC-7 |
| A selected model's provider is down and turns fail | Medium | Availability filtering at read time via provider health; unavailable deployments are absent from the picker; AC-5 |
| Telemetry continuity breaks — dashboards keyed on `profile` go blank | Medium | `TraceContext.profile` → provider + model migrated in the same wave with dashboards updated; AC-8 |
| Scope sprawl into the sub-agent pipeline change | Medium | Explicitly out of scope (§5), deferred to the future sub-agent ADR; `sub_agent` keeps a deployment default here |
| Catalog metadata rots into stale prose | Low | Declared-vs-observed line (§2): no failure-mode prose fields; observations live in telemetry |

---

## Implementation Notes

**Files affected:**

- `config/models.yaml` — restructured into `providers:` + `models:` (deployment catalog).
  **`config/models.cloud.yaml` deleted.**
- `config/model_roles.yaml` — becomes Layer 3 role bindings (`open`, `default`, effort, decoding
  overrides); `active_profiles` removed.
- `config/profiles/{local,cloud}.yaml` — **deleted** (Path).
- `src/personal_agent/llm_client/models.py` — `ModelDefinition` gains `provider` (ref), `kind`,
  `reasoning_capable`, `effort_levels`, `default_effort`, `can_disable_thinking`, cost fields,
  `status`, `summary`; `provider_type` removed (derived).
- **New:** provider definitions + loader; `src/personal_agent/llm_client/concurrency.py` —
  `infer_provider_type` deleted, semaphores keyed on provider with per-deployment sub-limits.
- `src/personal_agent/config/model_loader.py` — resolve bindings from the single catalog; validate
  every binding references an existing, `kind`-compatible key.
- `src/personal_agent/config/profile.py` — `ExecutionProfile`, `resolve_model_key`,
  `set_current_profile`, `list_profiles` removed; replaced by binding + selection resolution.
- `src/personal_agent/config/config_guard.py:801-830` — divergence check retired; replaced by
  catalog-reference and `kind`-compatibility checks.
- `src/personal_agent/config/settings.py` — `model_config_path`/`AGENT_MODEL_CONFIG_PATH` removed;
  `attachment_default_processing_target` removed (vision pinned).
- **New:** selection store — table + repository (session → role → key), mirroring
  `constraint_preferences_repository`; migration in `docker/postgres/migrations/` +
  `init.sql`, run as the `agent` superuser via `AGENT_DATABASE_ADMIN_URL` (FRE-808). Migration maps
  existing `sessions.execution_profile` to an initial `primary` selection.
- `src/personal_agent/service/app.py` / `gateway/session_api.py` — config read endpoint (resolved
  bindings, pinned/open, candidates, availability) + selection write (`PATCH`, user-scoped per
  ADR-0079 §3); inference status re-keyed to provider.
- `src/personal_agent/telemetry/trace.py` — `TraceContext.profile` → provider + model.
- `src/personal_agent/orchestrator/expansion.py:109` — `sub_agent` resolved through the catalog
  rather than hardcoded; effort per-call overridable.
- PWA — profile pill replaced by the model picker; `PROFILE_STORAGE_KEY` removed;
  `ClassifiedErrorCard` escalation reworded; observe view (bindings + pinned/open + providers).

**Dependencies:** ADR-0029 (concurrency control), ADR-0033 (provider interface), ADR-0050 (external
delegation targets — ADR-0044 D4's home), ADR-0075 (one socket per session), ADR-0076 (selection-
state precedent), ADR-0079 (invariants inherited), ADR-0099 (matrix — amended), ADR-0102 (vision
model-coupling — why vision is pinned), ADR-0120 (cost attribution consumes provider + model).

**Testing strategy:** the §7 snapshot assertion as the primary safety net; unit tests for catalog
loading, `kind`-compatibility validation, provider-keyed concurrency, selection resolution and
fallback, writer-role override rejection, and availability filtering; a live check on the deployed
stack that selecting a model changes the model that runs the next turn.

**Sequencing (one PR each):**
1. **Catalog + providers refactor, behaviour-preserving.** Three layers, single catalog, provider
   entity, `kind`/reasoning/cost metadata, concurrency re-keyed. Ships with the snapshot assertion
   (AC-1, AC-2, AC-3 provable here). No user-visible change.
2. **Selection store + resolution** (table, repository, migration mapping `execution_profile`,
   fail-closed resolver, server-side validation). AC-4, AC-6, AC-7 provable at API level.
3. **Config read API + availability filtering** off provider health. AC-5.
4. **Telemetry migration** — `TraceContext.profile` → provider + model, dashboards updated. AC-8.
5. **PWA: model picker replaces the profile pill**; observe view; Path removed end to end.
   **(Seam ticket, AC-9.)**

---

## Verification / Acceptance Criteria

- **AC-1 — The refactor changes no model.** *Check:* a snapshot of every `(role, profile) →
  fully-resolved ModelDefinition` (id, provider, endpoint, decoding params, limits) captured on
  `main` before step 1 is asserted **byte-identical** after it, for every role in the matrix.
  *Fails if* any role resolves to a different model or different parameters — including
  `compressor`, whose nano/mini drift must be corrected as a separate reviewed commit, not absorbed
  silently. *(A broken refactor that "loads fine" but rebinds a role passes every import test and
  fails this.)*
- **AC-2 — A role cannot bind to a wrong-kind model.** *Check:* set `entity_extraction`'s binding to
  an `kind: embedding` deployment; config loading **fails** with a kind-mismatch error naming the
  role and key. Likewise binding `embedding` to a `kind: llm` deployment fails. *Fails if* config
  loads successfully — the typing hole that exists today.
- **AC-3 — Concurrency is enforced at the provider, across deployments.** *Check:* with
  `providers.slm_local.max_concurrency = 2`, issue concurrent calls against **two different**
  `slm_local` deployments (`qwen3.6-35b-thinking` and `qwen3.6-35b-instruct`) totalling 4 in
  flight; at most 2 are in flight at the provider at any instant. *Fails if* each deployment gets
  its own independent limit and 4 run concurrently — the current behaviour, which is exactly the
  laptop-contention bug. Additionally, a deployment sub-limit below the provider ceiling is
  respected.
- **AC-4 — A selection cannot reach a pinned role, by any route.** *Check:* (a) insert a selection
  row directly in the store naming each writer `r ∈ {entity_extraction, captains_log, insights,
  embedding, reranker, reranker_fallback}` **and** `vision`, then assert each still resolves to its
  configured default; (b) the write API rejects such a selection 4xx before storage; (c) a
  selection naming a non-catalog key for an *open* role falls back to that role's default, not to
  an arbitrary or empty model. *Fails if* any pinned role's resolution changes when a selection row
  for it exists. *(Injecting the row is what makes this discriminating: an implementation that
  reads selections for all roles and relies only on "pinned roles have no candidates" passes a
  byte-identical check under benign conditions and fails here.)*
- **AC-5 — The picker offers exactly the selectable, available set.** *Check:* the read payload's
  candidate list for `primary` **equals** {catalog deployments where `kind: llm`} minus those whose
  provider is unavailable — asserted in both directions: with the SLM provider down its
  deployments are **absent** while an available cloud deployment is **present**, and no
  non-catalog or wrong-kind key ever appears. *Fails if* the set differs in either direction — a
  leaked key, a retained dead one, or a dropped live one.
- **AC-6 — Selection is server-authoritative, including the new-session case.** *Check:* (a) for an
  **existing** session, a client-supplied `primary` selection on `/chat/stream` is **ignored** and
  the stored value is used; (b) for a **new** session the supplied value **is** adopted and
  persisted; (c) a `PATCH` from a bearer token belonging to a different user returns 404 and does
  not mutate. *Fails if* a stale client can overwrite a stored selection, **or** a new session
  silently persists the default instead of the supplied value — the exact regression ADR-0079's
  post-deploy correction fixed.
- **AC-7 — Existing sessions do not change model on deploy.** *Check:* a session created before the
  migration with `execution_profile = 'cloud'` resolves, after migration and with no user action, to
  the same model it resolved to before (per the AC-1 snapshot). *Fails if* pre-existing sessions
  silently move to a different model.
- **AC-8 — Spend is attributable to the model that incurred it.** *Check:* after two turns on two
  different `primary` selections, cost/telemetry records for each turn carry the **provider and
  model actually used**, and querying spend grouped by model returns non-zero, correctly-split
  values. *Fails if* records still carry only a profile dimension, or carry a model that differs
  from `MODEL_CALL_COMPLETED.model` for that turn.
- **AC-9 (assembled seam) — Path is gone and selection works end to end.** *Check:* on the deployed
  stack, the PWA presents a model picker and **no** local/cloud Path control anywhere (pill,
  attachment chip, error-card escalation); selecting a different `primary` model makes the next turn
  run on it (`MODEL_CALL_COMPLETED.model` matches the selected deployment's resolved id); the
  selection survives a page reload and a WS reconnect via hydration; and a turn already in flight
  when the selection changes completes on the model it launched with. *Fails if* any Path control
  survives, the selection does not take effect, does not survive reload, or mutates an in-flight
  turn.

**Seam owner:** AC-9 is owned by the **PWA picker ticket (step 5)** — the child where the assembled
intent first holds. This ADR does **not** close when the telemetry migration (step 4) merges; it
closes only when AC-9 is proven on the deployed stack. Master asserts AC-9 at the acceptance gate.

---

## References

- ADR-0029 — provider-type concurrency control (the capacity concern re-homed onto providers)
- ADR-0033 — provider interface (Layer 1 builds on it)
- ADR-0044 — Provider Abstraction & Dual-Harness: **D1/D2 superseded here**; D3 carried to the future sub-agent ADR; D4 survives with ADR-0050; D5 transformed (§8)
- ADR-0050 — external agent delegation targets (ADR-0044 D4's home)
- ADR-0065 — cost gate (superseded by ADR-0120; `cost_limit_per_session` on profiles dies with Path)
- ADR-0074 — observability identity requirements
- ADR-0075 — one active socket per session (why notification is not a broadcast)
- ADR-0076 — constraint preferences (the selection-state precedent this store mirrors)
- ADR-0079 — server-authoritative session profile: **subject superseded, all eleven invariants inherited** (§4)
- ADR-0099 — single-source role matrix + validator: **amended** (§8); bindings survive, divergence guard retired
- ADR-0102 — vision document handling (model-coupling: why `vision` is pinned)
- ADR-0118 — user-selectable artifact builder: **superseded**; its DecisionCard analysis carried into ADR-0122
- ADR-0119 — config-management interface: **superseded**; its observe-view intent carried here
- ADR-0120 — cost governance (consumes provider + model attribution from §8)
- ADR-0122 — build-time artifact builder selection (the per-build card this layer feeds)
- Orchestrator-invoked sub-agents — **future ADR, not yet written**: the Stage 5 flip, and ADR-0044 D3's home
- FRE-399 — cloud-escalation error card (reworded to "retry on \<model\>")
- FRE-416 / FRE-419 — the profile desync incident that produced ADR-0079's invariants
- FRE-478 / FRE-495 — artifact-builder output-cap and context-window incidents (why large-output metadata is declared, and why a `max_tokens` YAML check does not prove it)
- FRE-808 — migrations run as the `agent` superuser via `AGENT_DATABASE_ADMIN_URL`
- FRE-869 — `get_llm_client_for_key` budget-lane correctness
- FRE-879 — the local→cloud artifact-builder regression that triggered this refactor
- FRE-880 — the candidate-registry ticket whose defects prompted the substrate review
- FRE-886 — attachment default (retired: `vision` is pinned)
- `config/models.yaml` · `config/models.cloud.yaml` — the 11-of-12 identical catalogs
- `src/personal_agent/llm_client/concurrency.py:48` — `infer_provider_type`, the string-parsed provider
- `src/personal_agent/llm_client/models.py:173-187` — the existing `supports_*` capability convention
- `src/personal_agent/telemetry/trace.py:50` — `TraceContext.profile` (ADR-0044 D5)
- `src/personal_agent/orchestrator/expansion.py:109` — hardcoded `sub_agent` binding
- `seshat-pwa/src/components/StreamingChat.tsx:26` — `PROFILE_STORAGE_KEY` (the Path pill)
- `docs/research/2026-07-16-model-routing-sota-survey.md` — deterministic routing; why orchestrator model-choice is not an LLM router

---

## Status Updates

### 2026-07-19 - Proposed
**Changed By:** cc-adrs (Opus)
**Reason:** Initial proposal, following an owner-led review that halted the ADR-0118/0119 build chain
over FRE-880. That ticket specified a candidate registry duplicating `ModelDefinition`, justified by
a false claim that `ModelDefinition` has no capability field, gated by a `max_tokens` YAML check that
cannot fail for the reason it exists, and carrying prose failure-mode fields no code reads. Review of
the substrate underneath found the actual defect: `models.yaml` conflates slot-aliases with model
identities, duplicates a near-identical catalog across two files, has no provider entity (one is
string-parsed from endpoints at runtime), does not type models, and freezes a laptop capacity
constraint into the `sub_agent` role binding. Owner direction: refactor rather than build over it,
and remove Path in favour of selecting the model by name. Design settled over discussion: three-layer
normalization; `kind` on the model and `open` on the role (authorization as their intersection, after
rejecting model-side `authorized_roles` as fail-open by omission); effort and decoding params on the
binding, per-call overridable; `vision` pinned on ADR-0102 model-coupling grounds, retiring the
FRE-886 chip as the last Path instance; `sub_agent` orchestrator-chosen but deferred to the future sub-agent ADR
because it requires a request-pipeline change; declared-vs-observed metadata line to prevent the
FRE-880 rot; ADR-0079's eleven invariants inherited verbatim; behaviour-preserving migration proven
by snapshot assertion (owner-agreed). Diligence on the ADRs being retired found ADR-0044 is **not**
wholly Path — D3/D4/D5 are carried forward, not deleted — and that ADR-0079 is not really about
profiles at all but about server-authoritative session selection state, so it is inherited rather
than discarded.
