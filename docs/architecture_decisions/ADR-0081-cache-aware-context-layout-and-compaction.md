# ADR-0081 — Cache-Aware Context Layout & Compaction

**Status:** Proposed — 2026-05-29
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

### D4 — Skill-index split + minimization (extends ADR-0063)

`skill_index` is the primary prefix-churn source (it is routed per turn, so its content varies). Split it:
- **Stable, complete compact index** (id · purpose · when-applicable · version) → cached prefix (SEMI-STATIC).
- **Per-turn selected skill bodies** → volatile tail (or `read_skill` on demand).

Separately, the index is now a permanent prefix resident, so its size/encoding matter for both tokens and attention dilution. A **minimization + format experiment** (compact-markdown vs JSON vs XML; field ablation) is scoped as its own work, gated on a labeled routing-eval set; the canonical index is stored structured (for tooling/versioning/hashing) and **deterministically rendered** to a lean model-facing form — never LLM-restructured (nondeterminism would churn the prefix).

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

- **D4 skill handling:** route-once-per-session (stabilize) vs split-index+late-bodies vs relocate-all. Leaning split (preserves per-turn adaptivity *and* caches the map).
- **D4 index format/size:** Pareto search (routing accuracy vs tokens) — needs a labeled routing-eval set; candidate for DSPy optimization.
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
