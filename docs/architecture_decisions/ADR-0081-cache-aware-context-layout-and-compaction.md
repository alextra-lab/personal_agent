# ADR-0081 — Cache-Aware Context Layout & Compaction

**Status:** Proposed — 2026-05-29 · D1 shipped (FRE-422, PR #120) · **D4 decided 2026-06-01** (skill-index split; owns the cache-GREEN gate D1 could not meet)
**Related:** ADR-0038 (context compressor + prefix ordering), ADR-0061 (within-session head-middle-tail compression), ADR-0063 (skill routing / compact index), ADR-0074 (identity / joinability), ADR-0078 (prompt management & observability — this is its gated composer-redesign phase)

---

## Context

P1 of the prompt-management initiative (ADR-0078, FRE-405) made prompt identity and KV-cache reuse **measurable** for the first time (`static_prefix_hash`, `dynamic_hash`, real `cache_n` / `cached_tokens`). The first real data exposed three coupled problems:

1. **The assembled prompt's volatility order is inverted.** The final system prompt is built by two f-string splices — `executor.py:2193` appends the DYNAMIC `memory_section`, then `executor.py:2218` wraps it as `f"{tool_awareness}\n\n{system_prompt}\n\n{tool_prompt}"`. Net byte order: `tool_awareness` → operator/skill/deployment → **`memory_section` (DYNAMIC, mid-string)** → STATIC tool rules (~975 tok) → decomposition. The largest *static* block sits *after* the *dynamic* one.

2. **The prompt is reconstructed from full raw history every turn, transiently.** `executor.py:1395` rehydrates the entire conversation from Postgres; `executor.py:1574` re-runs `apply_context_window(compressed_summary=get_summary())` each turn, inserting a re-derived summary at index 1. Compaction is **never persisted** — it is a one-shot in-memory artifact (`compression_manager._summaries`, popped on read). So the layout *after the system message changes every turn*, and the prefix churns by construction.

3. **Measured cost.** Prefill is **92–96 % of turn latency** (~8 s for ~8 k-token prompts at ~1050 tok/s, native llama-server). **Cross-turn KV reuse ≈ 0** — every turn fully re-prefills. The only reuse observed was *within* a turn (a continuation call reused 9 851 / 10 238 tokens; prefill collapsed 9 550 → 549 ms). The cache mechanism works; the layout and the transient re-derivation defeat it. The SLM config is already well-tuned (native llama-server, `cache_prompt` on, q8 KV, flash-attn) — the bottleneck is **harness-side**.

The "reconstruct every turn" property (Context #2) is not the bug — it is the *lever*. It gives total freedom to place content deliberately. The bug is that we use that freedom to produce an every-turn-different, cache-hostile layout.

Two attention facts shape the design (primacy/recency, "lost in the middle," attention-sink): the model attends to the **head** (primacy + attention-sink; also the cached region) and **tail** (recency; nearest generation) far more than the **middle**. So the middle is simultaneously the *least cached* and *least attended* region — the correct place to compress; and important content must never be buried there.

---

## Decision

Treat the prompt as a **write-once, append-only log with a volatility gradient**, reconstructed each turn into a cache-shaped, attention-shaped layout.

### D1 — Volatility-gradient layout

Reorder assembly so the byte order is monotone in mutation frequency:

```
[STATIC]      deployment · tool-use rules · decomposition         ── cached, primacy
[SEMI-STATIC] operator · tool-awareness · stable skill index      ── cached
‖ cache boundary ‖
[FROZEN]      append-only narrative summary of cold history       ── cached (see D2)
[VERBATIM]    recent tail (+ pinned, see D6)
[VOLATILE]    recalled memory · salient highlights (D3) · current query   ── recency
```

Fixes the `executor.py:2218` inversion: STATIC fragments move to the front; `memory_section` and conversation move after the boundary. The current query stays **last** (adjacent to generation).

### D2 — Append-only / frozen compaction (replaces transient re-derivation)

Compaction must produce a **stable, persisted, append-only** artifact, not a per-turn-recomputed summary. When a turn ages out of the verbatim tail, summarize *that increment* and **append** it to the frozen narrative; never rewrite the existing summary. A turn aging out then invalidates the cache only at the append point (monotonic forward movement), instead of forcing a full re-prefill. This supersedes the ADR-0061 behavior of re-deriving the whole middle each turn (`compress_in_place` rewriting + `apply_context_window` re-inserting at index 1).

### D3 — Two-object summary (narrative + salient highlights)

`compress_in_place` returns **`(frozen_narrative, salient_highlights)`**:
- `frozen_narrative` → the append-only cached artifact (D2).
- `salient_highlights` → a short, bounded, **volatile** distillation (decisions, constraints, open threads, key entities) placed in the volatile tail **just before the current query**.

This resolves the freeze-vs-freshness tension: the bulk narrative can stay frozen/cached *because* the highlights carry per-turn freshness in the already-volatile zone (so they cost nothing in cache terms). It also exploits the attention asymmetry — compress in the low-attention middle, reinforce salience in the high-attention tail. Highlights are derived as a strict subset of the narrative (single extraction) to avoid contradiction, and hard-bounded (token cap) so they don't erode the compression win.

### D4 — Skill-index split (decided; owns the cache-GREEN gate D1 could not meet)

**Status of this section:** Decided 2026-06-01, post-D1-deploy. D1 (FRE-422, PR #120, merged `2248a22`) moved `memory_section` to the volatile tail, but the post-deploy eval shows `make cache-erosion-status` for `orchestrator.primary` is **still RED**. The residual prefix churn is the skill block. The ADR-0081 "post-D1 residual" question — *is D4 worth resolving?* — is answered: **yes, D4 is required.** The open research questions D4 carried are resolved below.

**Measured residual (ES `agent-logs-*`, `model_call_completed`, after the 07:34 D1 deploy):** a single session (`5af07bc0…`) of **23 `orchestrator.primary` turns produced 6 distinct `prompt_static_prefix_hash` values.** The cache gate needs exactly **1** across ≥5 same-session turns; post-D1 we have 6 across 23. The prefix is still eroding every few turns, and D1 — which only relocated memory — provably cannot account for it. The only remaining per-turn-varying bytes inside the captured prefix are the skill block's volatile fragments (below).

#### The residual churn, located precisely

After D1, the cacheable prefix is captured at `executor.py:2259` as `inner_system_before_memory` — everything assembled before the volatile `memory_section` tail. But the skill block is spliced into `system_prompt` *earlier* (`executor.py:2045-2050`), so it falls **inside** that captured prefix. And the skill block is not uniform in volatility. `_full_skill_injection` concatenates four fragments with two different volatility classes:

| Fragment | Source | Volatility |
|----------|--------|------------|
| Compact skill index | `assemble_skill_index()` (`skills.py:191`) | **STABLE** — deterministic render of the loaded skill catalog; identical every turn until a skill is added/edited |
| `<skill_index_directive>` | `assemble_skill_index_directive()` (`skills.py:249`) | **STABLE** — constant string |
| Selected skill bodies | `_keyword_block` (hybrid) / `_preloaded_bodies` (model_decided) | **VOLATILE** — varies with the user query and `ctx.loaded_skills` |
| `<skill_usage_directives>` | `assemble_skill_usage_directives(ctx.loaded_skills, …)` (`skills.py:264`) | **VOLATILE** — bullet set derived from per-turn `ctx.loaded_skills` |

The VOLATILE fragments sit inside the captured static prefix → `static_prefix_hash` changes every turn → cross-turn `cached_tokens` ≈ 0 → cache gate RED. This is structurally the *same* defect D1 fixed for memory, one layer up; D4 applies the same maneuver to the skill block. (Refinement from design review: `<skill_usage_directives>` churns only when `ctx.loaded_skills` changes — it returns empty when no body is loaded, `skills.py:280` — so it is volatile-when-present rather than every-turn. The selected-bodies fragment is the dominant per-turn churner.)

#### Why the skill block — and not something else — is the churner (component-isolation evidence)

The prefix could in principle be churned by *any* per-turn-varying fragment captured before line 2259, not just skills. A design-review pass (Codex) correctly flagged three other candidates inside the captured prefix: `operator_stanza` (memory-backed, could change on a mid-session memory write), the tool block (`tool_awareness` + prompt-injected tool defs, *skipped* on synthesis turns → a structurally different prefix), and the `decomposition_instructions` block (turn-state-dependent). If any of those were active across the 23-turn session, the split alone would reduce but not eliminate the hash count.

The FRE-405 `prompt_component_ids` stamp lets us test this directly. Across all **6** distinct prefix hashes in the session, the component set is **identical**: `{deployment_context, operator_stanza, skill_index, tool_awareness, tool_use_rules, memory_section}`. Therefore:

- **`decomposition_instructions` never appears** → the decomposition block was not active. Ruled out.
- **`tool_awareness` + `tool_use_rules` appear in all 6** → no synthesis turns dropped the tool block; the tool fragments are structurally constant here. Ruled out.
- The variation is in component *content*, not component *presence* — and the only captured-prefix component whose content varies **by construction** is `skill_index` (the hybrid keyword bodies track the user message). This is the confirmed churner D4 targets.
- **Residual suspect — `operator_stanza` (weighted as a real risk, not a footnote).** A second design-review pass corrected an earlier framing here: the stanza is **not** provisioned once and frozen. `get_owner_stanza()` runs during session init (`executor.py:1425`) and reads Neo4j-backed user-persona facts via `get_or_provision_user_person` (`prompts.py:203`/`231`) that **mutate as memory is written**. So `operator_stanza` is *plausibly volatile by design* — a candidate co-cause of the 6 hashes, not merely an unprovable edge case. The honest claim is therefore conditional: **D4 removes the confirmed structural churner (`skill_index`) and drives the hash count toward 1; it reaches exactly 1 only if `operator_stanza` is independently verified byte-stable across the session.** If the post-D4 count lands >1, isolate `operator_stanza` next — it would need the same before-capture/stable treatment, or its own volatile-tail relocation if it proves session-mutable. `memory_section` appears in the component list but, post-D1, is appended *after* the capture point — it is already on the volatile side and does not enter `static_prefix_hash`.

#### The split

Resolve the standing "route-once vs split-index+late-bodies vs relocate-all" question in favor of **split**: it preserves per-turn routing adaptivity *and* caches the stable map. Concretely, partition `_full_skill_injection` at its volatility seam:

- **Cached prefix (SEMI-STATIC), before the `inner_system_before_memory` capture:** the compact skill index + the constant `<skill_index_directive>`. These become permanent prefix residents and contribute to `static_prefix_hash`.
  - **Required invariant for "stable":** the index is stable only because `assemble_skill_index()` renders `cache.docs.values()` in a deterministic order seeded by `sorted(skills_dir.glob("*.md"))` (`skills.py:120`). That ordering must stay deterministic for the cached fragment to hold its hash — name it as a D4 invariant. A skill added/edited mid-session legitimately shifts the index hash **once** (the intended drift signal), then re-stabilizes; any *non-deterministic* ordering (unsorted glob, FS-order dependence) would churn the cached fragment and defeat the split.
- **Volatile tail, after the capture point (same band as `memory_section`, before the current query):** the selected skill bodies + the per-turn `<skill_usage_directives>`. These contribute to `dynamic_hash`, never to `static_prefix_hash`.

Implementation shape (for the build worktree, not done here): split the single splice at `executor.py:2045-2050` into two — index + index-directive appended into `system_prompt` before the line-2259 capture; bodies + usage-directives deferred and appended into the volatile tail alongside `memory_section` (line 2262-2266). The `skill_index` entry in `_component_ids` (line 2326) stays; add a distinct volatile marker for the bodies so prompt identity attributes them to the dynamic side.

**Volatile-tail ordering (decided):** `[stable prefix] ‖ selected skill bodies → <skill_usage_directives> → recalled memory / D3 highlights`, all at the **end of the system message**. Within the system prompt this keeps the usage-directives adjacent to the bodies they reference and the volatile content after the cache boundary — which is what D4 needs for the cache result.

**Correction (design review) — the cache basis is sound; the "recency/nearest the query" basis is not.** D4's justification is **cache correctness**: these fragments are volatile, so they belong *after* the `static_prefix_hash` capture point. That holds unconditionally. What does **not** hold is any *attention-recency* claim: the system prompt is inserted at message index 0 (`client.py:317`) and the user query is the last entry of `ctx.messages`, so "end of the system prompt" is hundreds of tokens *before* the actual user turn — **not** query-adjacent. An earlier draft (carried over from D1's framing) implied tail fragments gain a recency benefit; they do not, absent a change to the message-array layout, which is **out of D4's scope**. D4 is justified by cache stability alone; it makes no recency claim. (A future item could relocate `salient_highlights`/memory into a trailing user-adjacent message for genuine recency — tracked separately, not part of D4.)

**Two implementation cautions (from design review), to carry into the build ticket:**
- **Separator/empty-fragment symmetry (same bug class D1 fixed for empty-memory):** the volatile-tail join must filter empty fragments *and* emit separators identically whether 0 or N skill bodies are selected, so a "no volatile skills" turn and a "one volatile skill" turn cannot leave different separator/whitespace bytes on the **stable** side of the boundary. Build the stable prefix and the volatile tail with independent joiners; never let the tail's presence affect prefix bytes.
- **`_skill_index_present` flag correctness:** today it is set from any `_full_skill_injection` (`executor.py:2045-2046`), including keyword-only mode where no index exists. After the split it must reflect *actual index presence* only, so the `skill_index` component id (which feeds `static_prefix_hash` attribution) is not falsely stamped on keyword-mode turns that carry only volatile bodies.

#### Interaction with routing modes (ADR-0063 §D7, FRE-373) and `prefer_primitives_enabled`

The entire skill block is gated behind `settings.prefer_primitives_enabled` (default `False`). When off, no skill content is injected and the prefix is already stable — **D4 is a no-op in that state.** When on, the split applies per routing mode (`settings.skill_routing_mode`, default `hybrid`):

- **`hybrid` (production default):** index + `<skill_index_directive>` → cached prefix; `_keyword_block` bodies + `<skill_usage_directives>` → volatile tail. (Keyword bodies vary per query — the dominant residual today.)
- **`model_decided`:** index + `<skill_index_directive>` → cached prefix; router-selected `_preloaded_bodies` + `<skill_usage_directives>` → volatile tail. The routing call itself runs once per request on a separate model (`skill_routing_model_key`, default `claude_haiku`) and never touches the primary prefix, so it is orthogonal to the split.
- **`keyword` (legacy):** emits no index, only `get_skill_block()` bodies. Today those bodies churn the prefix; D4 relocates them to the volatile tail, so keyword mode also stops eroding the cache (it just has no cached catalog to gain).

The split is invariant across modes: **any index → cached prefix; any bodies → volatile tail.**

#### Target byte layout after D4

Against the same bands D1 established:

```
[STATIC]      tool-use rules                                        ── cached, primacy
[SEMI-STATIC] tool-awareness · deployment · operator stanza
              · STABLE SKILL INDEX + <skill_index_directive>        ── cached
              · decomposition-base (autonomous mode)
‖ cache boundary ‖  ── static_prefix_hash covers everything above; nothing volatile precedes it
[VOLATILE]    selected skill bodies + <skill_usage_directives>      ── per-turn
              · recalled memory / D3 salient highlights             ── dynamic_hash
              · current query                                       ── recency
```

The cache boundary moves up to sit immediately after the stable skill index. After D4 there is no volatile fragment left on the cached side of the boundary.

#### Index format/size minimization — deferred sub-experiment (does NOT gate D4)

The index is now a permanent prefix resident, so its size/encoding affect tokens and attention dilution. A **minimization + format experiment** (compact-markdown vs JSON vs XML; field ablation) remains scoped as its own follow-up, gated on a labeled routing-eval set (candidate for DSPy optimization). It is explicitly **not** on the critical path for the cache-GREEN gate — the split removes the confirmed structural churner (subject to the `operator_stanza` contingency in Verification); the format search only trims the now-cached cost. Invariant: the canonical index stays stored structured (for tooling/versioning/hashing) and is **deterministically rendered** to the model-facing form — never LLM-restructured, since nondeterministic rendering would re-introduce prefix churn.

### D5 — Tiered virtual context (cold-tier on-demand retrieval)

Full conversation history is **not** carried in-prompt once cold. It remains in the existing stores (Postgres `session_events`, ES `agent-captains-captures`) and is retrieved on demand via a **`recall_session_history` tool** (semantic + keyword — grep alone is too brittle for prose; we have the embeddings service). This is the MemGPT / virtual-context pattern and the bottom of a three-tier hierarchy:

| Tier | Location | Standing cost | Fidelity |
|------|----------|---------------|----------|
| Hot — salient highlights (D3) | volatile tail | tiny | distilled |
| Warm — frozen narrative (D2) | cached prefix | paid once | lossy |
| Cold — full history | Postgres/ES, on-demand | ~0 | lossless |

**Critical affordance:** the in-prompt summary must explicitly tell the model the full history is searchable (and how), or it will not retrieve facts it does not realize it is missing. Triggered both by threshold (history ages to cold) and by the model (decides it needs an old detail). Reuses the FRE-410 read/grep muscle.

### D6 — Optional pin mechanism

A message may be marked **pinned** → always retained, never compressed. Placement follows attention: **durable-important → head** (cached + primacy); **turn-important → tail** (recency). Pinned content is never left only in the middle. (Today the tail is purely positional/recency; pinning is a new capability.)

### Measurement

Every change is verified against the P1 instruments: `static_prefix_hash` must go **constant across same-session turns**; cross-turn `cache_n` / `cached_tokens` must rise from ~0; and a new **post-compression "forgot-an-earlier-fact" error rate** must not regress (the metric for D3/D5). No change ships without a before/after on these.

---

## Open decisions (researcher / data-gated)

- **D4 skill handling:** ~~route-once-per-session vs split-index+late-bodies vs relocate-all~~ — **RESOLVED 2026-06-01: split** (stable index cached, per-turn bodies + usage-directives to the volatile tail). See §D4; owns the cache-GREEN gate D1 could not meet.
- **D4 index format/size:** Pareto search (routing accuracy vs tokens) — needs a labeled routing-eval set; candidate for DSPy optimization. **Still open**, but de-risked: this is a follow-up cost-trim that does *not* gate the cache-GREEN result (§D4).
- **D5 retrieval:** semantic-only vs hybrid; ranking; how much to auto-inject vs require an explicit tool call.
- **Objective horizon:** maximize single-turn static prefix vs minimize *total* prefill across an N-turn horizon (dynamic content grows monotonically and will eventually dominate regardless of order — argues for the cold tier, D5).

These are posed to external CS/math reviewers; this ADR records the architecture, not the final parameterization.

---

## Consequences

**Positive**
- Cross-turn KV reuse becomes achievable: on a stable prefix, repeat-turn prefill should drop from ~8 s toward the within-turn figure (~0.5 s observed). Prefill is 92–96 % of latency, so this is the dominant win.
- Compaction stops being a per-turn cache-buster and *grows* the cached prefix (D2).
- Lossless recall (D5) removes summarization's worst failure mode.
- Important content lands where the model actually attends (D1/D3/D6).

**Negative / tradeoffs**
- Append-only compaction means the narrative grows; the cold tier (D5) bounds it.
- Cold-tier retrieval adds tool round-trips + the "unknown unknowns" recall risk (mitigated by the D5 affordance + D3 highlights).
- Splitting skill routing (D4) trades a stable cached index against per-turn body re-prefill; the index must be minimal (attention dilution).
- Touches a hot path (every turn) — must be gated behind measurement and rolled out carefully.

---

## Verification

- `static_prefix_hash` constant across ≥5 same-session turns of mixed query types (today: 3 distinct values across 5 turns).
- Cross-turn `cache_n` > 0 on repeat-prefix turns (today: 0); mean `orchestrator.primary` cache-hit rate rises past the ADR-0078 / FRE-406 60 % gate.
- A deliberate skill/tool-desc edit shifts `static_prefix_hash` exactly once (drift signal), then re-stabilizes.
- Post-compression forgot-fact error rate flat or improved with D3 highlights vs summary-only (A/B), at bounded token cost.
- D5: a query needing a cold detail triggers `recall_session_history` and answers correctly without that detail being in-prompt.

### D4-specific acceptance (the cache-GREEN gate D1 could not meet)

D4 is **done** only when `orchestrator.primary` flips GREEN on the FRE-405/406/407 instruments:

- **Prefix stability (FRE-405):** `prompt_static_prefix_hash` constant across ≥5 same-session turns — i.e. `distinct_prefixes == 1` for the session, vs the **6-across-23** measured post-D1.
- **Erosion gate (FRE-406):** `make cache-erosion-status` for `orchestrator.primary` reports Jaccard ≥ 0.90 (`status=stable`), flipping the current RED.
- **Real reuse:** cross-turn `cached_tokens > 0` on repeat-prefix turns (today ≈ 0); the stable skill index now sits in the cached region.
- **Quality unchanged (FRE-407):** the per-turn rating trace stays flat or improves — moving skill bodies from the cached prefix to the end of the system message must not degrade routing/answer quality.
- **`operator_stanza` contingency (named residual, from design review):** the component-isolation evidence rules out decomposition and synthesis/tool variance but cannot prove `operator_stanza` bytes were constant across the session. If post-D4 the hash count lands **>1**, isolate `operator_stanza` next (group surviving hashes by component content; confirm whether a mid-session memory write mutated it). Do **not** mark D4 done on a partial drop (e.g. 6→2) — the gate is exactly 1.

**Verification-tooling prerequisite (FRE-406 amendment).** The cache-erosion monitor (`scripts/monitors/cache_erosion_monitor.py` / `compute_erosion_report`) currently windows on whole calendar days (`window_days`, `calendar_interval: "1d"`). A same-morning deploy + same-session verification (exactly the D4 case — the entire 23-turn dataset above lives inside one morning) is **invisible at day granularity**: it collapses into a single bucket and reports `insufficient_data`. The monitor must gain an **`--hours-ago` window** (sub-day range + an hour/turn-level bucketing) so D4 can be verified in the same session it deploys, per the "post-deploy steps run in the same session as deploy" rule. This is a small build-worktree change, tracked as an explicit sub-item on the D4 ticket; it gates D4's *verifiability*, not its implementation.
