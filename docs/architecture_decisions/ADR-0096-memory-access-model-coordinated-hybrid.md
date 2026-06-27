# ADR-0096 — Memory Access Model: Coordinated Hybrid (Passive Ambient Floor + Active On-Demand Retrieval)

**Status:** Accepted — 2026-06-27 (owner)
**Related (dependency map):**
- **ADR-0087** — memory-recall quality program (FRE-435); the pillar this lands under. This ADR owns the *access posture*; 0087 owns *recall quality*.
- **ADR-0081** — cache-aware **prompt layout** (KV-cache reuse). Passive memory lands in the **volatile tail** / last user message, after the cacheable-prefix capture point; the access model must not perturb that. *(Disambiguation: 0081's "frozen append-only" is **prompt-cache layout** — NOT ADR-0098's retired "first-write-wins" KG-write freeze. Same word, different layer.)*
- **ADR-0095** — delegation boundary; owns the sub-agent tool boundary + grammar-constrained digest. `recall_personal_history` is already in its discovery `_DISCOVERY_TOOL_ALLOWLIST`. This ADR **forward-points** to 0095 for active-retrieval mechanics and does not re-author them.
- **ADR-0026** — the `search_memory` native tool whose default exposure this ADR governs.
- **ADR-0039** — proactive memory; the one *flag-gated* passive source (default off).
- **ADR-0042** — KG freshness via `memory.accessed` / `AccessContext`; the emit-site the access-path attribution rides.
- **ADR-0047 / ADR-0059** — context compaction's `_extract_entity_ids` capture of *dropped* memory is the de-dup seam this ADR reuses (0047); the recall controller (0059) consumes that capture.
- **ADR-0090** — telemetry surface contract; ES mapping discipline for the access-path field.
- **ADR-0074** — identity / joinability; emit-site discipline so per-path attribution joins the turn.

**Hard prerequisite:** FRE-593 (per-request context-window occupancy breakdown — the Phase-2 mix decision input; still *Needs Approval*).
**Reconciles (does not duplicate):** FRE-502 (why passive injection is the deterministic floor — a weak local brain can't be trusted to decide to retrieve) · FRE-577 (long-session occupancy curve — where passive↔active competition bites).
**Sibling forks** (2026-06-26 architecture-review triage, `docs/superpowers/plans/the-following-information-comes-logical-pie.md` §4): ADR-0094 (turn-level execution profile) · ADR-0095 (per-worker delegation boundary).
**Validation:** EVAL-2 (memory access-mode A/B — the context-pressure gate).

---

## Context

The external design review (2026-06-26) framed memory access as a fork — *"active retrieval-as-a-tool (Letta-style) **versus** passive gateway injection"* (ARCH-3) — and recommended picking a default. The review was written **without codebase access**. The live tree refutes the framing: **it is not a versus.** Both paths already exist, both are on by default, and **they are uncoordinated**. ADR-0096's job is therefore not to *build* a capability or *choose one of two*, but to **assign the two live paths distinct roles, coordinate them so they stop competing, and make the default mix tunable on measured data** — the same measurement-first, no-clamp staging ADR-0094 and ADR-0095 adopt.

The standing program priority is **infrastructure + observability first**. This ADR honors it literally: its Phase 1 spends **no quality and no money axis** — it only instruments the access path and stops the two paths from double-sourcing. The posture decision itself (Phase 2) is explicitly gated on data that **does not exist yet** (FRE-593).

### Six facts the live tree establishes (verified 2026-06-26 against `/opt/seshat/.claude/worktrees/adrs`)

Where this diverges from the review's framing, the code wins.

1. **Passive injection is the gateway default, not an opt-in — it fires from deterministic gateway logic, without the brain choosing to retrieve.** `request_gateway/context.py` `_query_memory_for_intent` (`context.py:138-244`) runs broad recall for `MEMORY_RECALL` intents (`recall_broad`, `:168-177`) **unconditionally** once `memory_adapter.is_connected()`, and entity-name-match recall for analysis/other intents (`:192-240`) **whenever the message yields capitalised entity hints** (it returns `None` if none are found, `:194-196`). Both branches are gateway-rule-driven (intent + surface-form), not a model tool-call. Only *two* passive sub-sources are flag-gated: proactive semantic suggestions (`if settings.proactive_memory_enabled:`, `:179`, **default false**) and Captain's-Log reflection recall (`reflection_recall_enabled`, **default true**). The assembled memory lands in `AssembledContext.memory_context`, rendered later by the executor into the **volatile tail** (ADR-0081 — inlined into the last user message, after the cacheable prefix capture).

2. **Active memory-as-tool is always-registered and exposed to the *primary* model — it is not the *experimental* path, nor sub-agent-only.** `register_mvp_tools` registers `search_memory` (ADR-0026) and `recall_personal_history` (FRE-343) in the unconditional "Always-available tools" block (`tools/__init__.py:102-103`; docstring "Always registered", `:81-83`). Neither is flag-gated. The primary call loads them from the same default registry (`get_default_registry().get_tool_definitions_for_llm(mode=ctx.mode)`); sub-agents inherit a *filtered subset*, so the memory tools are a primary-level capability, not a sub-agent specialty.

3. **The two paths are uncoordinated — single-turn double-sourcing of the same fact is structurally possible and unsuppressed.** Passive recall hits `MemoryService` via `recall_broad`/`recall`; the active `search_memory` tool hits the **same** `MemoryService` via `query_memory`/`query_memory_broad`. In one turn the model receives the passively-injected `memory_context` **and** can re-query the same store for the same fact. There is no shared key, no "already injected" suppression, no overlap check. The closest existing primitive is `_extract_entity_ids` (`budget.py:222`), which captures the identifiers of memory **dropped for budget** so the recall controller (ADR-0059) can notice a later reference — i.e. **a "what memory was in play this turn" capture seam already exists**, it is simply not used to de-dup the active path.

4. **Under budget pressure, memory is evicted *before* tools — the competition is real and the priority is hard-coded, not measured.** `apply_budget` (`budget.py:134-294`) trims sequentially: Phase 1 oldest history (`:192-214`), **Phase 2 drops the entire `memory_context`** (`:217-242`), Phase 3 drops tool definitions (`:245-266`). So passively-injected memory is sacrificed one full phase earlier than the tool surface that could re-fetch it. The decision telemetry (`context_budget_applied`, `:270-283`) carries only `has_memory` / `has_tools` **booleans** and a single `total_tokens` — **no per-category split.**

5. **The data the review says to "decide against" does not exist yet.** There is no `memory_tokens` / `tool_tokens` / `reasoning_tokens` emission anywhere; `_total_context_tokens` (`budget.py:41-65`) sums the three buckets into a single integer. That per-category breakdown **is** FRE-593 (filed Needs Approval, Observability Foundation). ADR-0096's posture decision is gated on it. This ADR must not pretend the data is in hand.

6. **The active path *partially* rides ADR-0095's tool boundary already — the unification is real but incomplete, and is 0095's to mechanize.** `recall_personal_history` is already a member of ADR-0095's discovery `_DISCOVERY_TOOL_ALLOWLIST` (`sub_agent.py:97-99`: `{"bash", "read", "read_skill", "web_search", "recall_personal_history"}`), so it can already be fronted by a discovery sub-agent returning a **grammar-constrained digest** (the seam ADR-0095 hardens: retrieved memory enters the root context shape-guaranteed, not as a raw dump). `search_memory` (ADR-0026) is **not** on that allowlist — it is a **primary-only** tool today (`tools/__init__.py:102`). So the unification covers one of the two active tools; whether `search_memory` *also* moves behind the discovery boundary is a Phase-2 open decision (D2), **not** assumed here. This ADR **points at** ADR-0095's machinery for the active path and **does not duplicate it** (owner-settled scope, 2026-06-26).

### Why this ADR exists, and what it deliberately is not

- It is **not greenfield** — both paths ship today. It builds *coordination + attribution*, not a new capability.
- It is **not an either/or** — the right answer is a hybrid with a division of labor, because the two paths have genuinely different virtues (fact-driven below).
- It does **not clamp a live capability in round 1.** Removing the always-on tool exposure, or gating passive injection, *before* the per-category data exists is precisely the "don't prematurely clamp" anti-pattern. Phase 1 changes nothing a user can feel; it only observes and de-dups.
- It does **not author the grammar-constrained-digest mechanism** — ADR-0095 owns it; this ADR forward-points (owner-settled).
- It does **not** let the reasoning brain decide *whether* to use memory at the expense of the deterministic floor. Passive injection stays the gateway-owned reliability floor; active retrieval is additive precision.

**The governing asymmetry (the caveat that shapes every phase).** Passive and active fail in opposite, both-silent ways. Passive injection's failure is **budget competition** — it crowds the window and gets evicted (fact #4), or it injects stale/irrelevant facts that look like context but aren't. Active retrieval's failure is **non-invocation** — a weak local brain (FRE-502) simply never calls the tool, so the right memory is never pulled. A naive "pick active" bet trades a loud-ish problem (window pressure) for a silent one (the brain forgets to remember). The division of labor below is built around that asymmetry: **passive is the floor that fires without the brain's cooperation; active is the precision that the brain (or a discovery sub-agent) reaches for when it knows it needs something specific.**

---

## Decision

Adopt a **coordinated hybrid** memory-access model with an explicit division of labor, recorded **measurement-first**, rolled out in three phases. The gateway keeps owning passive injection; the brain / discovery sub-agent owns active retrieval; a thin **coordination layer** stops the two from double-sourcing; and the **default mix** is decided on measured data (FRE-593 + the access-path attribution this ADR adds), never by up-front assertion.

### The governing division of labor (what passive owns vs what active owns)

- **Passive injection = the ambient floor.** Deterministic, gateway-owned (Stage 6), budget-capped, placed in the ADR-0081 volatile tail. It fires **without** depending on the brain choosing to retrieve — this is its whole value under a weak local model (FRE-502). It carries the *cheap, always-relevant* memory: identity/profile facts and (where measurement justifies it) high-salience proactive suggestions.
- **Active retrieval = on-demand precision.** The brain — or, per ADR-0095, a discovery sub-agent fronting `recall_personal_history` and returning a grammar-constrained digest — pulls *specific* memory when the turn reveals a specific need the ambient floor didn't cover (`search_memory` remains primary-only today; whether it also moves behind this boundary is deferred to Phase 2 / D2). **The tool boundary and the digest shape-guarantee are ADR-0095's; this ADR only routes through them.**
- **Coordination = the missing third piece.** Passive injection **records the set of memory identifiers it placed this turn**; active retrieval **de-dups against that set** (suppress or annotate a re-fetch of already-injected memory). This reuses the `_extract_entity_ids` capture seam (fact #3) rather than inventing a parallel one.
- **The mix is data-gated.** *How much* the floor injects by default, and *whether* the memory tools stay on the primary surface or move behind the discovery sub-agent, is a Phase-2 decision on FRE-593's per-category token split plus this ADR's access-path attribution — not a value baked in now.

### D1 — Phase 1 (ships first): access-path attribution + passive↔active coordination

A **pure observability + reliability win, no quality or money axis** — it makes the *existing* dual-path behavior measurable and non-redundant without changing what any user sees.

1. **Access-path attribution (the field this ADR adds).** FRE-593 emits the *token split* (memory / tool / reasoning); it does **not** say *how* a given memory item entered the window. This ADR adds, per memory item in play for a turn, its **access path** — `injected` (passive Stage-6), `tool_fetched` (active `search_memory` / `recall_personal_history`), or `both` (the redundancy case). Emitted on the existing `memory.accessed` / `AccessContext` emit-site (ADR-0042) with the full ADR-0074 identity envelope so it joins the turn, and mapped per ADR-0090 discipline. **FRE-593 is a hard prerequisite** — the token split is the denominator; the access path is the breakdown.
2. **Coordination / de-dup.** Passive injection publishes the memory-identifier set it placed this turn (reuse `_extract_entity_ids` over the *injected* `memory_context`, not just the dropped one). The active tools consult that set and **suppress or flag** a re-fetch of an already-injected item. This is the direct fix for fact #3's uncoordinated double-sourcing; it spends no extra inference.
3. **Record nothing that picks the posture yet.** Phase 1 adds a *descriptive* field and a *de-dup* guard. It does **not** change the budget eviction order (fact #4), does **not** gate either path, does **not** touch the default mix. The `both`-path redundancy rate and the per-path token cost become queryable — that is the entire deliverable.

**Flag-gated, default off.** The de-dup guard ships behind `memory_access_dedup_enabled` (default false); the attribution emit behind the same flag or FRE-593's. Enabled observe-only first.

**Acceptance (Phase 1):** for a turn that both injects and tool-fetches the same fact, telemetry shows one `access_path="both"` record (the redundancy is *visible*, not silent); with the de-dup guard on, the active tool's re-fetch of an already-injected item is suppressed/flagged and the per-turn memory-token total measurably drops on redundant turns; FRE-593's per-category split is joinable with the per-path attribution; no budget-order change, no posture change, no schema-pick.

### D2 — Phase 2 (flag-gated, on Phase-1 + FRE-593 data): tune the default mix

With the per-category split (FRE-593) and the access-path attribution (D1) in hand, decide the **default posture knobs** on measured context pressure and redundancy — *local-biased, no clamp, manual override preserved*:

- **What the ambient floor injects by default** — e.g. always-inject identity/profile; promote or demote proactive injection (today default-off, ADR-0039) on its measured salience-vs-cost; cap the floor's token share so it cannot crowd the window.
- **Where the memory tools live** — `recall_personal_history` is already dual-available (primary surface + ADR-0095 discovery allowlist); the open calls are (i) whether `search_memory` (primary-only today) also joins the discovery boundary, and (ii) whether the *default* active retrieval routes through the grammar-constrained digest (ADR-0095) rather than returning a raw tool result to the root. Decided on whether the `both`-redundancy rate and the primary's tool-token cost justify the move.
- **Revisit the eviction order** (fact #4) — is dropping memory *before* tools correct, given the per-category cost the data now shows? A data-driven re-ordering, not an assumed one.

Any change that spends money (a cloud-routed retrieval digest) inherits ADR-0094/0095's cost-gate + `$2/session` cap and governance expansion-denial.

### D3 — Phase 3 (research, gated): consolidation quality over raw storage

The review's closing lean — *"consolidation quality over raw storage"* — is a research direction, not a day-one lever. The vehicle is the existing episodic→semantic pipeline (`memory/promote.py` `run_promotion_pipeline`; `second_brain/consolidator.py` `consolidate_recent_captures`). The hypothesis: **higher-quality consolidated semantic facts reduce how much the ambient floor must inject**, which relieves the very budget competition D2 measures and sharpens active retrieval (fewer, denser facts to pull). Gated on D2 showing that injection volume — not retrieval precision — is the binding constraint.

### D4 — What stays authoritative / out of scope

- **ADR-0095 owns the active-retrieval mechanics** — the tool boundary, the `_DISCOVERY_TOOL_ALLOWLIST`, and the grammar-constrained digest. This ADR routes through them and must not fork them.
- **The deterministic decompose/delegate decision stays in the gateway** (`decomposition.py`); nothing here lets the brain self-route.
- **The manual override stays authoritative** — no phase introduces a hidden auto-switch of the memory posture; the floor and the tools remain inspectable and flag-controlled.

---

## Open decisions (data-gated — resolved in Phase 2, not now)

- The default **floor composition** (identity-only vs identity + proactive) and its token cap.
- Whether memory tools stay on the **primary** surface or move behind the **discovery sub-agent** (ADR-0095).
- Whether the **eviction order** (memory before tools) should be re-ranked on measured per-category cost.
- Whether `both`-path redundancy warrants **suppression** (hard de-dup) or only **annotation** (let the brain see "already in context").

## Consequences

**Positive.** The dual-path behavior becomes measurable and non-redundant with no user-facing change and no money spent (D1). The posture decision is made on data, mirroring the program's measurement-first discipline. The active path reuses ADR-0095's hardened boundary rather than forking a second one. Passive stays the reliability floor a weak local model needs (FRE-502).

**Negative / risk.** Adds an attribution field and a de-dup hop to the hot memory path — must respect ADR-0090 mapping discipline and ADR-0074 joinability or it becomes another orphaned signal. Phase 2 is *blocked* on FRE-593 approval+landing; if FRE-593 stalls, the posture decision stalls (by design — better stalled than guessed). The `both`-redundancy de-dup must not suppress a *legitimately different* tool query that merely shares an entity id.

## Verification

EVAL-2 (memory access-mode A/B) is the validation. Gated on D1 + FRE-593: with the per-category split and access-path attribution live, run a fixed task set under the candidate floor/active mixes and report the outcome-quality delta against the per-turn memory-token cost — the same flag→measure→verify→rollout gate ADR-0094/0095 use. Phase 1's own acceptance (above) is verifiable without EVAL-2: the `both`-redundancy record and the de-dup token drop are direct telemetry assertions.
