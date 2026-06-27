# ADR-0095 — Delegation Boundary: Per-Worker Execution-Profile Routing + Grammar-Constrained Sub-Agent Output

**Status:** Proposed — 2026-06-26
**Related (dependency map):**
- **ADR-0094** — deterministic local/cloud execution-profile routing; owns the **turn-level** profile and defers the **per-worker** "cloud brain / local hands" refinement to *this* ADR. Its Phase-1 per-call record is the plumbing this ADR refines.
- **ADR-0082** — tier-aware model selection; the orthogonal *role* axis (`model_tier` never shipped).
- **ADR-0033** — multi-provider model taxonomy; defines `primary`/`sub_agent` roles, `provider_type`, and the `get_llm_client_for_key` profile-bypass seam this ADR routes through.
- **ADR-0086** — HYBRID/DECOMPOSE + the `TOOLED_SEQUENTIAL` discovery sub-agent surface and `_DISCOVERY_TOOL_ALLOWLIST` this ADR sizes per-tool-class.
- **ADR-0036** — sub-agent execution modes (`PARALLEL_INFERENCE` vs `TOOLED_SEQUENTIAL`).
- **ADR-0088** — execution-topology observability contract; `report_degradation` is the seam a salience/shape failure must call; the `agent-topology-*` per-segment row carries the per-worker provider.
- **ADR-0090** — telemetry surface contract; mapping discipline for attribution fields.
- **ADR-0074** — identity / joinability; emit-site discipline for per-worker attribution.
- **ADR-0065** — cost gate; `get_llm_client_for_key` carries `budget_role`, so a cloud-routed worker is reservation-attributable.
- **ADR-0029 / ADR-0023** — local-server concurrency + thinking/instruct serving; the constraint that today's local `primary` and `sub_agent` are the *same* weights.

**Reconciles (does not duplicate):** FRE-502 (schema-fail → tool-less fallback; the downstream symptom this ADR's grammar guarantee fixes upstream) · FRE-492 (HITL dynamic allow-gate — successor to the static allowlist) · FRE-495 (local sub_agent `context_length` — orthogonal context sizing) · FRE-432 (standing tier-routing gap).
**Sibling forks** (2026-06-26 architecture-review triage, `docs/superpowers/plans/the-following-information-comes-logical-pie.md` §4): ADR-0094 (turn-level profile, the axis this refines per-worker) · ADR-0096 (memory access model).
**Validation:** EVAL-4 (sized-worker quality A/B — the salience gate).

---

## Context

ADR-0094 established the **execution-profile axis** — `local` (free Qwen3.6-35B-A3B) vs `cloud` (Sonnet/Haiku under a `$2/session` cap) — and drew a deliberate hard boundary at the turn level:

> *ADR-0094 owns the turn-level profile — one decision for the whole turn-tree … the turn stays uniformly local or uniformly cloud (no mixed-provider turns in 0094). Per-sub-agent / per-tool-class routing ("cloud brain, local hands") is explicitly deferred to ADR-0095 … Because 0094 records the field per-call from the start, 0095 can refine a worker's provider with no re-plumbing.*

This ADR is that refinement. A turn is a **tree of model calls**: the reasoning root plus, for HYBRID, N concurrent `sub_agent` workers each fronting a set of tools. ADR-0094 routes the *whole tree* to one profile; this ADR decides **what each worker runs on, at sub-agent-spawn time**, using a signal the gateway never sees — *which tools the worker fronts first* — and hardens the worker's output boundary so a (possibly smaller, possibly local) worker can be **trusted**.

The owner-settled framing (2026-06-26) **unifies two axes the triage listed separately** (ARCH-1 + ARCH-2), because they are inseparable: you may only safely *size a worker down* if its output *shape* is guaranteed, and even a shape-guaranteed worker can still be *too weak to be salient*. Shape is the precondition; salience is the residual risk.

### Six facts the live tree establishes (verified 2026-06-26 against `/opt/seshat/.claude/worktrees/adrs`)

Where this diverges from the triage plan, the code wins.

1. **The per-worker routing seam already exists and is profile-independent.** `get_llm_client_for_key(model_key, budget_role)` (`llm_client/factory.py`) resolves a *specific* model key **bypassing** the active `ExecutionProfile`, and already takes a `budget_role` for cost-gate attribution (ADR-0065). It exists today for the skill router ("must use a remote model even when the primary runs locally"). A per-worker routing decision therefore needs **no new dispatch primitive** — it needs a *decision* and a place to attach it on the spawn path.

2. **There is no distinct "small local model" yet — the local sizing axis is degenerate.** Both `primary` and `sub_agent` in `config/models.yaml` are the **same weights** (`unsloth/qwen3.6-35-A3B`), served as two llama.cpp instances differing only in thinking-on (root) vs `disable_thinking` (worker). The only genuinely smaller models configured are *cloud* (`gpt-5.4-nano` for entity/log/insights roles). So ARCH-2's "small model + grammar for mechanical tools, capable model for messy tools" has, *today*, only **three reachable size points**: local-thinking, local-instruct (same weights, thinking off), and cloud-Haiku — **not** a small-local↔large-local pair. A true small local distiller (e.g. an 8B) is a **server-side prerequisite** (separate MLX/llama.cpp repo), not a config flip. This ADR must not assume a model that is not served.

3. **Grammar is effectively unwired; tool-call parsing is permissive post-hoc with silent drops.** Only `response_format={"type":"json_object"}` is used, by a **single** caller (`expansion_controller.py:311`). `response_format` plumbs cleanly to the llama-server payload (`adapters.py:628`), and `extra_body` is the established nested path for non-standard extensions (`top_k`, thinking control) — so a GBNF/`json_schema` grammar has a real wire path. But sub-agent *tool-call* extraction (`tool_call_parser.py`, `_parse_relaxed_json_object`; `sub_agent.py:417-466`) is **best-effort regex/relaxed-JSON that drops malformed calls** with a `warning` + `continue`. On llama.cpp the `qwen3_coder` native tool parser is unavailable, so today nothing *guarantees* a worker's tool-call or summary shape.

4. **The only per-worker knob today is `model_role`, and it is always `SUB_AGENT`.** `SubAgentSpec` (`sub_agent_types.py:48`) carries `model_role: ModelRole = SUB_AGENT`; the spawn site (`expansion_controller.py:308`) hardcodes `role=ModelRole.SUB_AGENT`. There is no per-worker *model-key* or *profile* selection on the spec.

5. **`_DISCOVERY_TOOL_ALLOWLIST` is already flagged as a placeholder for per-tool governance.** `sub_agent.py:93-96` (owner steer, 2026-06-05): *"this static allowlist is a placeholder for a future [decision] … the same `execute_tool` action-boundary governance as the primary executor."* The tool-class taxonomy this ADR needs is the same one FRE-492's dynamic allow-gate needs — they must share it, not fork it.

6. **ADR-0094 Phase 1 already records the per-worker provider — the hand-off is real.** `route_traces` emits one row per segment (root + each `sub_agent`; `assembler.py:184`), and ADR-0094 P1 (FRE-601) stamps `execution_profile` / `provider` / `resolved_model_key` on **each** from that node's resolved client. So "cloud root + 3 *local* workers" is already *recordable* the moment 0094 P1 lands — what is missing is the *decision* that makes a worker local-or-cloud, and the *attribution of why*.

### Why this ADR exists, and what it deliberately is not

The standing program priority is **infrastructure + observability first**, and ADR-0094's own headline is *"the real win is measurability, not automation."* This ADR honors that, mirroring 0094's staging: **shape-first (free), size-later (gated), escalate-last (research).** It does **not** propose a per-worker cost-spending router on day one. It first makes a local worker's output *trustworthy* (grammar — a pure reliability win, no money axis), then earns the per-tool-class sizing decision on measured data, then treats *salience-aware escalation* of a too-weak worker as gated research.

**The salience principle (the governing caveat).** *Grammar fixes shape, not salience.* A schema-constrained 35B-instruct (or an 8B, when served) will return JSON that **validates** — and may still be a **clean-but-lossy** summary that drops the signal the root needed. Shape failures are loud (parse/validation errors); salience failures are **silent and look like success**. Every phase of this ADR is built around that asymmetry: Phase 1 eliminates the loud failures; Phase 2 only sizes down where measurement shows salience holds; Phase 3 detects the silent failures and escalates.

---

## Decision

Introduce a **per-worker delegation-boundary decision** evaluated at sub-agent-spawn time, recorded per model-call on ADR-0094's existing per-segment sink, and rolled out in three phases. The turn-level profile (ADR-0094) and the deterministic decompose/delegate decision (the gateway matrix, `decomposition.py`) both remain authoritative; this ADR refines *which model each worker runs on* and *guarantees the worker's output shape*.

### The governing boundary (what 0094 owns vs what 0095 owns)

- **ADR-0094 owns the turn-level profile** — the inherited default for the whole tree, and the *only* thing the gateway (pre-LLM, once-per-turn) can decide, because sub-agents and their tool depth are discovered mid-turn.
- **ADR-0095 owns the per-worker refinement** — at spawn time (`expansion_controller` → `SubAgentSpec` → `run_sub_agent`), a worker may be routed to a **different** model key than the turn default via the existing `get_llm_client_for_key` seam (fact #1), driven by the **tool-class it fronts first** (fact #5). The turn default is the fallback; a worker is only *overridden* on an explicit, measured signal — never silently.
- **The decision is recorded per-call** on ADR-0094's per-segment row (fact #6). A mixed turn reads back as "cloud root + 3 local workers"; the *attribution of why* (tool-class + sizing decision) is the one new field this ADR may add (D1.3, data-gated).
- **The delegation decision itself stays in the gateway.** This ADR does **not** let the reasoning brain choose whether to delegate (that is already the deterministic `decomposition.py` matrix — triage §5 ARCH-1: *do not file a "make it delegate" ticket*). It refines *how a delegated worker is provisioned and bounded*, not *whether* delegation happens.

### D1 — Phase 1 (ships first): grammar-constrained shape guarantee for local sub-agent output

Close fact #3. Make a local worker's output **shape-guaranteed**, so the permissive post-hoc parser (`_parse_relaxed_json_object`) is a *fallback*, not the primary contract. This is a **pure reliability win with no money axis** — it makes the *existing* free local workers trustworthy, independent of any sizing or cloud routing.

1. **Wire constrained decoding into the local call path.** Extend the `response_format` pass-through (already reaching the llama-server payload, `adapters.py:628`) to carry a **`json_schema` response format and/or a GBNF `grammar`** (via the established `extra_body` extension path) for sub-agent calls whose output contract is known — tool-call envelopes (`TOOLED_SEQUENTIAL`) and structured digests (`PARALLEL_INFERENCE` summaries). The schema is **derived from the existing tool definitions / result shapes**, not hand-maintained in parallel.
2. **Make the silent-drop path observable and rare.** When constrained decoding is active, a parse/validation miss is a **shape failure** that MUST call `report_degradation(...)` (ADR-0088) — *not* a silent `warning + continue`. This is the direct upstream fix for FRE-502's planner schema-fail → tool-less fallback: the fallback still exists, but it is now **loud and attributed** instead of a silent drop.
3. **Record nothing new on the schema yet.** Phase 1 changes the *call*, not the durable record. The grammar-on/off state and shape-failure rate ride existing telemetry (`model_call_completed` + the degradation ledger). No `route_traces` column, no ES mapping change — so Phase 1 carries **no migration and no first-pass-mappings risk**.

**Flag-gated, default off, local-only.** Constrained decoding is wired behind a flag (`sub_agent_constrained_decoding_enabled`, default false), enabled for **local** sub-agent calls first (cloud providers already enforce tool schemas natively — fact #3 is a *local-server* gap). Rolled out class-by-class starting with the `TOOLED_SEQUENTIAL` discovery surface, where shape failures are most frequent and most attributable.

**Acceptance (Phase 1):** with the flag on, a forced HYBRID discovery turn shows **zero silent tool-call drops** — every malformed-output event is a `report_degradation` entry with `expected_vs_actual`, not a bare warning; the shape-failure rate on the discovery surface is queryable and measurably lower than the flag-off baseline; no schema/mapping surface was touched.

### D2 — Phase 2 (flag-gated, on Phase-1 data): per-tool-class sub-agent sizing/routing

Add a deterministic, **tool-class-derived** per-worker routing decision at spawn time, attached to `SubAgentSpec` and resolved through `get_llm_client_for_key` (fact #1). The decision reads **which tools the worker fronts first** against a **tool-class taxonomy shared with FRE-492's allow-gate** (fact #5) — it does not fork a second classifier.

**Proposed starting mapping (conservative, local-biased — to ratify; data-gated):**

| Tool-class the worker fronts | Example tools | Routing | Rationale |
|------------------------------|---------------|---------|-----------|
| **Mechanical / read-only** | `bash`, `read`, `web_search`, `recall` (the `_DISCOVERY_TOOL_ALLOWLIST`) | **local-instruct + grammar (D1)** | bulk discovery — local hands are sufficient *once shape is guaranteed*; never pay for it |
| **Messy / synthesis-bearing** | distillation, cross-source synthesis, judgment summaries | **local-thinking, escalate to cloud only on a strong signal** | salience matters; same weights with thinking on is the cheapest lever before cloud |
| **(Reserved) genuinely small-local** | mechanical, once an 8B is served | **small-local + grammar** | the true ARCH-2 cell — **gated on the server-side small-model prerequisite (fact #2); not assumed here** |

**The model the worker resolves to is the turn default unless the tool-class explicitly overrides it.** A `cloud` turn's mechanical worker MAY be pinned **down** to local ("cloud brain, local hands" — the canonical case); a `local` turn's worker is **never** silently pinned **up** to cloud without the same explicit, cost-gated signal ADR-0094 D2 requires for a turn-level cloud recommendation.

**Cost guard (inherited from ADR-0094).** Any worker routed to cloud invokes paid inference under the `$2/session` cap and MUST go through the cost gate (`get_llm_client_for_key` already carries `budget_role`, fact #1) and respect governance expansion-denial. A *cap-denied* up-route falls back to the turn default and records the denial on the per-call sink — the same "would-have-gone-cloud, ran-local-on-budget" attribution ADR-0094 D2 defines.

**Optional attribution field (the one possible schema change, data-gated).** ADR-0094's per-call record already captures the *resolved* `provider`/`resolved_model_key` per worker (fact #6) — so a sized-down worker is *already* visible. The only thing not captured is **why** (which tool-class drove the sizing). If Phase-1 data shows the resolved fields are insufficient to debug a salience regression, add a single short-keyword `sizing_decision` (or `tool_class`) field to the per-segment record — declared explicitly in the ES template per ADR-0090, mirrored init↔migration per the no-Alembic convention. **Default: do not add it** until the resolved fields prove insufficient.

**Acceptance (Phase 2):** the per-worker routing is deterministic from the tool-class taxonomy and joinable to the resolved per-call record; a HYBRID `cloud` turn with mechanical workers reads back as "cloud root + N local workers"; no `local`-turn worker is up-routed to cloud without a cost-gate-respecting signal; the sizing decision shares FRE-492's taxonomy (no second classifier).

### D3 — Phase 3 (research, gated): salience-aware escalation of a too-weak worker

The endgame addresses the **silent** failure mode the salience principle names: a shape-valid worker output that is *clean but lossy*. Run the worker at its sized-down point first; if its output is **insufficiently salient**, escalate that *worker* (not the turn) to a more capable model and re-run.

This is the **per-worker dual of ADR-0094 D3** (turn-level local→cloud escalation) and shares its hard parts:
- **No salience signal exists today.** The trigger (root rejects/re-asks the worker's digest; a downstream tool-loop fails to converge on the worker's output; an explicit quality probe) is **greenfield** — designing it is the substance of Phase 3, not a detail. Shape validation does **not** detect salience loss.
- **State surgery** at the worker boundary: the escalated re-run must see the worker's input context without double-applying its tool calls; partial tool state preserved.
- **Cost-gate exposure:** a re-run bills a more capable inference after a cheap attempt — the escalation rate must stay bounded or the sizing-down win inverts into pure added cost. Per-worker cost attribution (ADR-0094 D1.3, sliceable by `resolved_model_key`) is what makes this measurable.

Phase 3 ships **only** if Phase-1/Phase-2 measurement shows a worthwhile salience-loss band and a tractable trigger. It is explicitly allowed to remain a documented research result rather than shipped code.

### D4 — Invariants preserved

- The **turn-level profile** (ADR-0094) and the **delegation decision** (gateway `decomposition.py`) are never overridden by this ADR — it refines *worker provisioning*, not *whether/what to delegate*.
- **Sizing-down is the default direction; up-routing to cloud is always explicit, cost-gated, and never silent** (the ADR-0094 local-bias, applied per-worker).
- The **action-boundary governance** of `_DISCOVERY_TOOL_ALLOWLIST` / FRE-492 is unchanged — this ADR *reads* the tool-class signal for sizing; it does not loosen what a worker is allowed to invoke.
- **Measurement is always-on; automation is flag-gated and staged** (grammar default-off → sizing flag-gated → escalation research-gated).

---

## Open decisions (data-gated)

- **The sizing boundaries (D2).** Which tool-classes route to which size point. The local-biased cut above is a proposal; the labeled signal is Phase-1 shape-failure data + EVAL-4 salience A/B. Candidate probe: does a mechanical discovery worker on local-instruct+grammar match its local-thinking quality, or does grammar-induced lossiness show up even on bulk tools?
- **The salience trigger (D3).** The exact "worker output too weak" condition, and whether the escalated re-run reuses or discards the sized-down attempt. Tradeoff: aggressive escalation protects quality but spends real money and erodes the sizing-down win.
- **The small-local-model prerequisite (fact #2).** Whether to stand up a true small local model (e.g. an 8B distiller) on the SLM server — a **cross-repo** decision (separate MLX/llama.cpp repo) that unlocks the genuine ARCH-2 cell. Until then, "sizing" is the three available points (local-thinking / local-instruct / cloud-Haiku). This ADR does **not** assume the small model; it leaves the cell reserved.
- **Grammar source of truth.** Whether the constrained-decoding schema is derived from tool definitions at call time or pre-generated and cached. Tradeoff: derive-at-call is always correct but adds per-call cost; pre-generate is fast but can drift from the tool registry.
- **Attribution depth (D1.3).** Whether ADR-0094's resolved per-call fields suffice to debug a salience regression, or a `sizing_decision`/`tool_class` field is warranted. Default: do not add until proven necessary.
- **Quality floor.** The non-negotiable for any sized-down tool-class: mean per-worker output quality (as judged by the root's acceptance / downstream convergence) must not regress vs the all-local-thinking baseline. Define the threshold before widening Phase 2 beyond the cost-only-safe mechanical class.

---

## Consequences

**Positive**
- Makes the **existing free local workers trustworthy** (Phase 1) — a reliability win that lands before any sizing or spend, and the direct upstream fix for FRE-502's silent schema-fail fallback.
- Realizes ADR-0094's deferred "cloud brain / local hands" with **no re-plumbing** — the per-call record (fact #6) and the dispatch seam (fact #1) already exist; this ADR adds only the *decision* and (optionally) the *why*.
- Local-biased, flag-gated sizing can only *save* money in its safe class (mechanical workers pinned to local); cloud is never the silent default at the worker level either.
- Shares FRE-492's tool-class taxonomy instead of forking a classifier; turns the `_DISCOVERY_TOOL_ALLOWLIST` placeholder into a governed taxonomy.
- Names the **salience risk explicitly** and stages around it, rather than assuming grammar makes a small worker safe.

**Negative / tradeoffs**
- Adds a per-worker routing axis on a path that *can* spend real money — must be flag-gated, local-biased, and measured (the entire staging is built around this).
- **Grammar fixes shape, not salience** — the core residual risk; a shape-valid worker can be clean-but-lossy, and that failure is silent. Phase 3 exists precisely because Phases 1–2 cannot eliminate it.
- The genuine ARCH-2 cell (small-local + grammar) is **blocked on a cross-repo server-side prerequisite** (fact #2); until then the sizing axis is degenerate (three points, not a small/large local pair). Shipping value before that prerequisite means Phase 2's win is "pin mechanical workers to local-instruct," not "run them on a cheap 8B."
- Constrained decoding adds per-call schema-derivation cost and a new failure surface (over-constrained grammar that rejects valid output) — mitigated by default-off, local-only, class-by-class rollout.
- A possible new attribution field (D1.3) is schema/mapping surface subject to the init↔migration sync and ADR-0090 template discipline — avoided unless the resolved fields prove insufficient.

---

## Verification

- **Phase 1 (grammar / shape):** with `sub_agent_constrained_decoding_enabled` on, a forced HYBRID discovery turn shows zero silent tool-call drops — every malformed output is a `report_degradation` entry (ADR-0088) with `expected_vs_actual`, not a bare `warning + continue`; the discovery-surface shape-failure rate is queryable and lower than the flag-off baseline; `_field_caps` shows **no new fields** (no schema touched).
- **Phase 2 (sizing / routing):** the per-worker routing is deterministic from the shared tool-class taxonomy; a forced HYBRID `cloud` turn with mechanical workers reads back in `route_traces` + `agent-topology-*` as "cloud root + N local workers" (using ADR-0094's per-segment provider record); no `local`-turn worker is up-routed to cloud without a cost-gate-respecting signal; the cost gate (`budget_role`) and governance guards are respected on any worker up-route.
- **Salience (gate, Phase 2 before widening):** EVAL-4 (sized-worker A/B) shows per-tool-class worker quality — judged by the root's acceptance / downstream convergence — flat-or-up when a mechanical class is pinned to local-instruct+grammar vs the local-thinking baseline. Any regression past the floor reverts that class.
- **Cost:** per-worker cost attribution (`model_call_completed` + `api_costs`, sliceable by `resolved_model_key`) shows worker-level sizing reduces (or at minimum does not increase) cloud spend per session; no HYBRID cloud fan-out breaches the `$2/session` cap.
- **Phase 3 (if pursued):** salience-escalation rate stays below a defined ceiling; escalated workers show correct state scoping and no double-applied tool calls; the measured quality lift justifies the added spend, else Phase 3 is documented as retired.

---

*Validation follow-up: **EVAL-4** (sized-worker quality A/B — the salience gate) is the controlled eval that validates this ADR's Phase-2 sizing; file it as a follow-up eval ticket gated on ADR-0095 approval **and** ADR-0094 Phase-1 data (the per-worker record EVAL-4 slices on), pillar Seshat Inference Architecture, [S].*
