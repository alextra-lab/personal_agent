# ADR-0094 — Deterministic Local/Cloud Execution-Profile Routing

**Status:** Proposed — 2026-06-26
**Related (dependency map):**
- **ADR-0079** — server-authoritative execution profile; the session-persisted `local`/`cloud` pill this ADR extends.
- **ADR-0082** — tier-aware model selection; the *orthogonal* axis (which role runs, primary-thinking vs sub_agent-instruct). Its `model_tier` plumbing never shipped.
- **ADR-0033** — multi-provider model taxonomy; defines `primary`/`sub_agent` roles and `provider_type`.
- **ADR-0086** — HYBRID/DECOMPOSE routing for artifact builds; the expansion strategies this profile decision must compose with.
- **ADR-0088** — execution-topology observability contract; the `agent-topology-*` projection that must carry the profile.
- **ADR-0074** — identity / joinability; emit-site discipline.
- **ADR-0090** — telemetry surface contract; ES mapping discipline for the new field.
- **ADR-0065** — cost gate; the `$2/session` cloud cap the fan-out interacts with.

**Reconciles:** FRE-432 (the standing tier-routing gap).
**Validation:** EVAL-3 (brain-vs-architecture A/B).
**Sibling forks** (2026-06-26 architecture-review triage): ADR-0095 (delegation boundary / per-worker sizing — the per-worker refinement this ADR defers to) · ADR-0096 (memory access model).

---

## Context

Seshat can run a turn on one of two **execution profiles** (ADR-0079, `config/profiles/`):

| Profile | Reasoning role (`primary`) | Worker role (`sub_agent`) | Provider | Per-session cost cap | Money |
|---------|---------------------------|---------------------------|----------|----------------------|-------|
| `local` | Qwen3.6-35B-A3B (thinking) | Qwen3.6-35B-A3B-subagent (instruct) | local llama.cpp | none | **free** |
| `cloud` | Claude Sonnet 4.6 | Claude Haiku | LiteLLM/Anthropic | **`$2.00`** (`cloud.yaml`) | **real spend** |

**Today this choice is a manual, session-persisted toggle and nothing else.** Per ADR-0079 (`service/app.py:1830-1874`, `_resolve_session_profile`): for an existing session the stored `sessions.execution_profile` column is authoritative and the request-supplied field is ignored; a new session adopts the client's pill, defaulting to `"local"`. The only mutator is the UI pill via `PATCH /api/v1/sessions/{id}`. Downstream, the LLM factory resolves the local-vs-cloud branch lazily from the active profile's resolved model `provider_type` (`llm_client/factory.py:90-112`), where `resolve_model_key(role)` maps each role to the profile's model and `provider_type == "local"` → `LocalLLMClient`, else `LiteLLMClient`.

There is **no intent-derived, deterministic routing** between local and cloud. The gateway already computes everything such a decision would need — Stage 4 intent → `TaskType`, the decomposition assessment → `Complexity` and strategy (`request_gateway/pipeline.py:94,142`) — and a clean seam exists on `GatewayOutput` (`request_gateway/types.py:165-189`) before the single construction site (`pipeline.py:236`). But that signal never touches the profile. A high-judgment synthesis turn and a one-line "what time is it" turn run on whatever pill the user last left the session on.

### Four facts the live tree establishes (verified 2026-06-26 against `/opt/seshat/.claude/worktrees/adrs`)

These refine the architecture-review triage plan; where the plan and the code diverged, the code wins.

1. **ADR-0082's `model_tier` field never shipped** — repo-wide grep returns **0 hits**. `_determine_initial_model_role()` (`executor.py:1309-1322`) is still a no-op returning `ModelRole.PRIMARY`, and the gateway-driven model-role branch hardcodes `PRIMARY` (`executor.py:2234-2250`). So "carry the routing decision on `GatewayOutput`" has no existing plumbing to extend — it is net-new. The execution-profile axis (this ADR) and the tier axis (ADR-0082) are **orthogonal** — *which provider* vs *which role* — but neither is expressed on `GatewayOutput` today.

2. **The cross-provider escalation config is fully dead.** `DelegationConfig.allow_cloud_escalation` + `escalation_provider` + `escalation_model` (`config/profile.py:103-121`) are defined and populated in `cloud.yaml`, but **never read** — grep for any *read* of these fields returns zero hits. RT-2's "the flag exists, there's just no trigger" actually means the config slot exists and there is **zero escalation code**. Wiring it is greenfield behavior, not flipping a switch.

3. **The profile is a per-turn pair-switch for the whole turn-tree, not a per-call decision.** `set_current_profile` is turn/session-level; the factory resolves *every* model call in a turn — the reasoning root **and** every spawned `sub_agent` worker — through the one active profile. The two roles move together: a `cloud` HYBRID turn fans out a Sonnet root **plus N Haiku workers**, all billed against the single `$2/session` cap. "Cloud brain, local hands" is not representable today.

4. **No durable sink records which profile a *past turn* ran under.** `route_traces` (`docker/postgres/init.sql:100-172`, `model_role` at line 126) and the `agent-topology-*` projection (`observability/topology/es_projection.py:60-93`, `model_role` at 86) both carry `model_role` only — **never `execution_profile`**. The profile lives *only* on the mutable `sessions` row (last `PATCH` wins). **You cannot today answer "which profile did trace X run under?" for any historical turn.** This is the core measurability gap.

### Why this ADR exists, and what it deliberately is not

The standing program priority is **infrastructure + observability first**, and the triage plan's own headline is "the real win is **measurability, not automation**." This ADR honors that: it is **observe-first, route-later, escalate-last**. It does *not* propose a big-bang auto-router on a hot path that spends real money. It closes the measurability gap first (Phase 1), then earns the deterministic decision on that data (Phase 2), then treats true cross-provider escalation as gated research (Phase 3).

**Economics flip vs ADR-0082.** ADR-0082's cheap default was the local *instruct* tier and the local *thinking* tier was a free fallback — both local, both free. Here the axis crosses a **money boundary**: `local` is free, `cloud` is real Anthropic spend under a `$2/session` cap. So the cost-rational optimistic default is the opposite direction: **local-first, cloud-as-escalation** — never cloud-by-default.

---

## Decision

Introduce an **execution-profile decision axis** evaluated deterministically in the gateway, recorded per model-call, and rolled out in three phases. The manual ADR-0079 pill remains authoritative; this axis observes it, then (opt-in) drives it, then (research) escalates across it.

### How it composes with HYBRID / DECOMPOSE / sub-agent tool calls (the governing constraint)

A turn is a **tree of model calls**: SINGLE (~95% of traffic) is just the root reasoning call; HYBRID is the root + N concurrent `sub_agent` workers; DECOMPOSE is a sequence of sub-tasks; DELEGATE hands off a structured package. The gateway runs **once, pre-LLM** — it knows the *strategy* but **the sub-agents and their tool-call depth are discovered mid-turn and do not exist at gateway time.**

This ADR therefore draws a hard boundary:

- **ADR-0094 owns the *turn-level* profile** — one decision for the whole turn-tree: it selects the profile for the reasoning root and is the *inherited default* for any workers the turn later spawns. The turn stays **uniformly local or uniformly cloud** (no mixed-provider turns in 0094).
- **ADR-0094 records the *resolved* profile/provider on every model-call** — the root **and** each `sub_agent` segment (`route_traces` already emits per-segment rows; `assembler.py:184`). So a HYBRID turn is fully attributable as "cloud root + 3 cloud workers."
- **Per-sub-agent / per-tool-class routing ("cloud brain, local hands") is explicitly deferred to ADR-0095** (sub-agent sizing + grammar), because that decision is made at *sub-agent-spawn time* with tool-messiness signal the gateway never sees. Because 0094 records the field **per-call** from the start, 0095 can refine a worker's provider with **no re-plumbing** — the topology simply reads "cloud root + 3 *local* workers" once 0095 lands.

### D1 — Phase 1 (ships first): record the execution profile per model-call, joinably

Close fact #4. Stamp **three resolved fields per model-call** — `execution_profile` (`local`/`cloud`), `provider` (`local`/`anthropic`/…), and `resolved_model_key` (e.g. `claude_sonnet`, `sub_agent`) — onto every durable per-call sink, carrying `session_id` + `trace_id` per ADR-0074. **Critical emit-site constraint (the ADR-0095 hand-off depends on it):** these fields MUST be populated from the *resolved call/client/model context at each individual emit site* (root and each sub-agent), **never** from a session-level scalar copied across the turn. While ADR-0094 keeps the turn uniform (every node resolves to the same profile), recording from the per-call resolution point is what lets ADR-0095 introduce mixed-provider workers with no re-plumbing — the emit sites are already reading the actual call context.

1. **`route_traces` ledger** — add `execution_profile`, `provider`, `resolved_model_key` columns (`VARCHAR(50)`) to the table (`init.sql` + a new idempotent `docker/postgres/migrations/00NN_route_trace_execution_profile.sql`, mirrored per the init-vs-migration sync convention; **no Alembic**) and to `RouteTraceRow` (`observability/route_trace/types.py`). Populate them at row assembly (`assembler.py`) for the root row *and* each `sub_agent` segment row from that node's resolved client, so a fan-out turn records the profile/provider/model of every node.
2. **`agent-topology-*` projection (ADR-0088)** — add `execution_profile` + `provider` + `resolved_model_key` to the projected document (`observability/topology/es_projection.py:60-93`). **ES mapping discipline (ADR-0090):** all three are short keywords — declare them explicitly in the index template's `properties` (not left to a dynamic template) so the default `keyword ignore_above:1024` path is intentional, not accidental. No numeric fields are added here, so the float-`0.0`→`long` trap does not apply; the discipline is still to walk every new field through the template before the first doc lands.
3. **`model_call_completed` event** — emit `execution_profile` + `provider` + `resolved_model_key` alongside the existing `model_role`, so cost (`api_costs`), latency, and (where present) quality can all be sliced by profile/provider without a separate join.

**No backfill — coverage is forward-only.** The mutable `sessions.execution_profile` row cannot reconstruct which profile a *past, pre-Phase-1* turn actually ran under (it records only the *current* pill, and `auto`/mid-session switches make even that lossy). Phase 1 is therefore an additive forward-recording change with **no historical backfill**; the attribution guarantee holds for **post-Phase-1 turns only**.

**Acceptance (Phase 1, the ADR-0074 §3.4 gate):** `scripts/monitors/joinability_probe.py` shows **no orphans** for the three new fields; a HYBRID/DECOMPOSE turn's `route_traces` + `agent-topology-*` rows show `execution_profile`/`provider`/`resolved_model_key` on the root **and** every sub-agent segment; `_field_caps` confirms all three typed as `keyword`. After Phase 1, "which profile (and provider/model) did trace X run under?" is answerable for **every post-Phase-1 turn**, including each node of a fan-out.

### D2 — Phase 2 (flag-gated, on Phase-1 data): deterministic gateway profile recommendation

Add a deterministic, intent-derived **profile recommendation** to `GatewayOutput`, computed after Stage 4 + Stage 5 from signals already in hand (`TaskType`, `Complexity`, decomposition `strategy`). The recommendation field, its emission, and recommended-vs-actual shadow logging **all begin in Phase 2** — they are *not* part of Phase 1 (Phase 1 records only the *resolved* profile that ran, not a recommendation). From the moment Phase 2 lands, the recommendation is **carried and logged on every turn** (recommended-vs-actual), and **drives** the profile only when the session has explicitly opted in.

**Opt-in mechanism — a third pill value `auto`** (preserving ADR-0079 semantics):
- Session on `local` or `cloud` → the user's explicit choice is **authoritative**; the recommendation is recorded as a **shadow** ("gateway would have picked cloud; session ran local") but does not change routing.
- Session on `auto` → `_resolve_session_profile` computes the profile **per-turn** from the gateway recommendation instead of returning a stored scalar (a clean extension of the resolver: `auto` means "delegate to the router"). The user opted into automation explicitly; manual `local`/`cloud` stays authoritative.

**Proposed starting mapping (conservative, local-biased — to ratify; data-gated, see Open decisions):**

| TaskType / signal | Strategy | Recommendation | Rationale |
|-------------------|----------|----------------|-----------|
| `CONVERSATIONAL`, `MEMORY_RECALL` | any | **`local`** | chat/retrieval — no cloud-grade judgment; never pay for it |
| `TOOL_USE` (mechanical: grep/read/extract) | SINGLE | **`local`** | bulk/mechanical — local hands are sufficient |
| `ANALYSIS` / `PLANNING` | moderate+ | **`cloud`** *(candidate)* | judgment / detail / synthesis — the cell cloud is *for* |
| `SELF_IMPROVE` | any | **`cloud`** *(candidate)* | reflection quality matters |
| **any** | **HYBRID / DECOMPOSE** | **`local` unless strong cloud signal** | fan-out × cloud is the cost cliff (D-cost below) |

**Cost guard (the strategy×profile interaction).** A HYBRID/DECOMPOSE turn auto-routed to cloud multiplies the fan-out against the one `$2/session` cap — the single most expensive cell. The rule **must take `strategy` as an input** and bias fan-out toward `local`; a wide cloud fan-out requires an explicit, strong cloud signal, not a default. This mirrors ADR-0082's governance-gate concern: the recommendation must also respect the cost gate (ADR-0065) and any active governance expansion-denial.

**Cost-gate denial / cap-exhaustion fallback (a visible routing outcome, must be logged).** When a turn is on `auto` and the rule recommends `cloud` but the cost gate denies it (session cap reached, or the cap would be breached mid-fan-out), the **recommendation stays `cloud` while the actual profile falls back to `local`** — a deliberate "would-have-gone-cloud, ran-local-on-budget" divergence. The denial reason is recorded on the same recommended-vs-actual shadow log (D1's per-call sink), so cap-driven fallbacks are distinguishable from rule-driven `local` choices and from user-pill choices. A turn on an explicit `cloud` pill that exhausts its cap mid-turn surfaces the cost-gate's existing behavior (ADR-0065) unchanged — this ADR only adds the *attribution* of the denial, not new denial semantics.

**Acceptance (Phase 2):** the recommendation is emitted on every turn and joinable to the actual profile (shadow divergence is queryable); flipping a session to `auto` makes the profile track the recommendation per-turn; no turn on an explicit `local`/`cloud` pill changes routing. Rolled out flag-gated, class-by-class (start with the unambiguous `local` classes — they can only *save* money), measured before widening any `cloud` cell.

### D3 — Phase 3 (research, gated): local-first → detect-insufficiency → escalate

The cost-rational endgame: run the turn on `local` first; if it is insufficient, escalate the *same* turn to `cloud`. This wires the currently-dead `allow_cloud_escalation` / `escalation_provider` / `escalation_model` (fact #2) into real behavior.

This is the **cross-provider dual of ADR-0082's D3** (instruct→thinking escalation) — and strictly harder, because it crosses a provider *and* a money boundary:
- **No insufficiency signal exists today.** The trigger (low confidence / tool-loop non-convergence / explicit "I need to reason" / a quality signal) is greenfield — designing it is the substance of Phase 3, not a detail.
- **State surgery** identical in kind to ADR-0082 D3 but across providers: `ctx.last_response_id` must be scoped/cleared on a provider switch; synthesis gated; partial tool state preserved so the cloud re-run sees the right prefix without double-applying the local attempt's tool calls.
- **Cost-gate exposure:** a local→cloud escalation bills a real cloud inference *after* a free local attempt — the escalation rate must stay bounded or the win inverts into pure added cost. Per-profile cost attribution (D1.3) is what makes this measurable.

Phase 3 ships **only** if Phase-1 measurement shows a worthwhile insufficiency-rate band and a tractable trigger. It is explicitly allowed to remain a documented research result rather than shipped code.

### D4 — The manual pill stays authoritative; nothing here is a hidden auto-switch

Across all phases, an explicit `local`/`cloud` pill is never overridden. Automation is **opt-in** (`auto`), measurement is **always-on** (shadow logging). This is the ADR-0079 invariant preserved.

---

## Open decisions (data-gated)

- **The mapping boundaries (D2).** Which `TaskType × Complexity × strategy` cells recommend `cloud`. The local-biased cut above is a proposal; the labeled signal is the Phase-1 shadow trace (recommended-vs-actual) plus EVAL-3 quality A/B. Candidate probes: does `ANALYSIS/PLANNING moderate` actually need cloud, or does local thinking suffice? Is there *any* HYBRID cell worth the cloud fan-out cost?
- **Insufficiency trigger (D3).** The exact local→cloud escalation condition, and whether a re-run reuses or discards the local partial. Tradeoff: aggressive escalation protects quality but spends real money and erodes the free-local win.
- **`auto` as default for new sessions?** Phase 2 leaves new-session default at `local` (ADR-0079). Whether new sessions should default to `auto` once the recommendation is trusted is a later, data-gated call — not assumed here.
- **Quality floor.** The non-negotiable for any `auto`-routed class: mean per-turn quality must not regress vs the all-local (or the user's-pill) baseline. Define the threshold before widening Phase 2 beyond the cost-only-safe `local` classes.
- **Governance / cost-gate interaction (resolve before Phase 2 ships any `cloud` cell).** A recommendation that routes a `SINGLE` turn to cloud invokes paid inference; it must be subject to the cost gate (ADR-0065) and to governance expansion-denial the same way expansion is. Default to "gated by the same guards" until argued otherwise.
- **Relationship to ADR-0082's tier axis.** Profile (provider) and tier (role) are orthogonal and compose as `tier × profile`. The **per-call routing record introduced here (D1) is the shared join surface** — if/when ADR-0082's tier plumbing is revived, it stamps the role half on the same per-call rows. This ADR does not force a merge of the two fields; it does establish the per-call record both can populate.

---

## Consequences

**Positive**
- Closes the measurability gap (fact #4): after Phase 1, every turn — including every node of a HYBRID/DECOMPOSE fan-out — is attributable to a profile, joinable to cost and quality. The decision becomes *visible* before it becomes *automatic*.
- Local-biased, opt-in automation can only *save* money in its safe classes (the `local` cells); cloud is never the silent default.
- The per-call field shape lets ADR-0095 add "cloud brain / local hands" with no re-plumbing.
- Deterministic, reviewable, no new model call (the recommendation reuses existing gateway classification).
- Revives the dead escalation config (fact #2) as designed behavior with a real trigger and real measurement — or honestly retires it as not-worth-it on data.

**Negative / tradeoffs**
- Adds a routing axis to a hot path that spends real money — must be flag-gated, opt-in, and measured (the entire staging is built around this risk).
- Phase 3 escalation is genuine state surgery across a provider boundary (D3) and can *invert* the cost win if the escalation rate is unbounded; it is correctly gated behind Phase-1 data.
- A new durable column (`route_traces.execution_profile`) + ES field is schema/mapping surface that must be walked through the init↔migration sync and the index template (ADR-0090) before first write — the recurring first-pass-mappings trap.
- The `auto` pill adds a third state to the ADR-0079 resolver and makes a session's profile potentially vary per-turn; the resolver and any UI that displays "current profile" must handle a per-turn value, not a fixed scalar.
- Recommended-vs-actual shadow logging adds emit volume on every turn (one field, low cost) but must itself be joinable or it is noise.

---

## Verification

- **Phase 1 (joinability, gating):** `joinability_probe.py` reports no orphans for `execution_profile` / `provider` / `resolved_model_key`; a forced HYBRID and a forced DECOMPOSE turn each show all three fields stamped on the root **and** every sub-agent segment in both `route_traces` and `agent-topology-*`, populated from each node's resolved call context; `_field_caps` types all three as `keyword`. A query over **any post-Phase-1 turn** answers "which profile/provider/model ran trace X?" end-to-end (no historical backfill — forward coverage only).
- **Phase 2 (recommendation):** the gateway recommendation is emitted and joinable on every turn; recommended-vs-actual divergence is queryable, with cost-gate-denial fallbacks (recommended `cloud`, actual `local`) distinguishable from rule-driven `local`; a session flipped to `auto` routes per-turn by the rule, while sessions on explicit `local`/`cloud` never change routing; the cost gate and governance guards are respected on any `cloud` recommendation.
- **Quality (gate, Phase 2 before widening):** EVAL-3 (brain-vs-architecture A/B; harness already exists per `docker-compose.eval.yml` + `scripts/eval/fre453_canonical_evalset/`) shows per-class quality flat-or-up when an `auto` class routes to its recommended profile vs the baseline. Any regression past the floor reverts that class.
- **Cost:** per-profile cost attribution (`model_call_completed` + `api_costs`, sliceable by `execution_profile`) shows the local-biased routing reduces (or at minimum does not increase) cloud spend per session; no HYBRID/DECOMPOSE cloud fan-out breaches the `$2/session` cap.
- **Phase 3 (if pursued):** local→cloud escalation rate stays below a defined ceiling; escalated turns show the `last_response_id` scoping and no double-applied tool calls; the measured quality lift justifies the added cloud spend, else Phase 3 is documented as retired.

---

*Validation follow-up: **EVAL-3** (brain-vs-architecture A/B) is the controlled eval that validates this ADR; file it as a follow-up eval ticket gated on ADR-0094 approval (harness exists → "run it"), pillar Seshat Inference Architecture, [S].*
