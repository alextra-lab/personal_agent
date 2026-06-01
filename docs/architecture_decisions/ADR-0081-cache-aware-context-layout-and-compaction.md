# ADR-0081 — Cache-Aware Context Layout & Compaction

**Status:** Proposed — 2026-05-29 · D1 shipped (FRE-422, PR #120) · D4 decided 2026-06-01 (skill-index split) · **D2/D3 settled 2026-06-01 — implementation-grade, gated behind `cache_frozen_layout_enabled`; build implements as FRE-434 once approved** (frozen append-only layout + cache-aware compaction scheduler; supersedes the original high-level D2/D3 proposal per the FRE-433 backend finding)
**Related:** ADR-0038 (context compressor + prefix ordering), ADR-0061 (within-session head-middle-tail compression), ADR-0063 (skill routing / compact index), ADR-0074 (identity / joinability), ADR-0078 (prompt management & observability — this is its gated composer-redesign phase)
**Evidence for D2/D3:** FRE-433 diagnostic + A/B (`docs/superpowers/plans/2026-06-01-fre-433-crossturn-kv-reuse-diagnostic.md`), design brief (`docs/superpowers/plans/2026-06-01-fre-433-d2d3-cache-aware-compaction-brief.md`), A/B harness (`scripts/eval/fre433_cache_ab/`)

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

> **D2/D3 settled (2026-06-01) — what changed from the original proposal.** The two short sub-sections that previously sat here were the *high-level* D2/D3 proposal, written before the local backend's reuse rule was measured. FRE-433 measured it and the proposal does not survive contact with the constraint: it assumed compaction could **continuously append** a summarized increment to a frozen narrative and have that count as "forward motion." It cannot, for the local backend (proof below), because the narrative band sits *behind* the recency band — appending to it shifts every later token, which is a mid-sequence change, which zeroes the whole KV cache. The sections below replace the proposal with the settled, implementation-grade design: a **frozen append-only layout** (Part A) plus a **cache-aware compaction scheduler** that batches the unavoidable rewrite into a rare, cost-optimal **reset** (Part B). The six decisions the brief enumerated are settled inline and cross-referenced. Everything here is gated behind a new `cache_frozen_layout_enabled` flag (default `False`, no-op when off); D4/D5/D6 are unchanged.

### The measured constraint that forces this design (FRE-433)

The local SLM (`:8502`, Qwen3.6-35B-A3B, llama.cpp) reuses KV cache **only on an exact / forward-extension prefix** — any byte change at offset *N* invalidates the entire cache from 0, *including the identical head before the divergence point* (diagnostic Test 1). The partial-reuse knob (`--cache-reuse`) is **architecturally unavailable** for this attention architecture across all 7 configs tried — do not design around a backend knob. Cross-turn reuse was **0** because the per-turn volatile block (recalled memory + selected skill bodies) is appended into the **system message** (`executor.py:2275-2285`), which `client.py:318-319` inserts at **message index 0** — the head of the wire sequence — and that block changes every turn. D1/D4 made the *measured* `static_prefix_hash` constant, but that hash covers only a substring of message[0]; the actual wire bytes of message[0] still churned, so local reuse stayed 0. The A/B (diagnostic §A/B) confirmed: relayout *alone* fixes **cloud** (reuse 13.9k → 17–20k, and it *improves* Sonnet caching, does not break it) but **not local**, which additionally needs the prior turns **frozen byte-identical in place**. The decisive construction:

```
turn 2 = [system STABLE][user VOL_V1+Q1][assistant g1][user VOL_V2+Q2]
         └─ prefix [STABLE][VOL_V1+Q1][g1] BYTE-IDENTICAL to turn-1's cached KV ─┘ + new tail
→ strict forward extension → cache_n 6771 / prompt_n 277   (vs head-layout: cache_n 0 / prompt_n 6799)
```

Two properties are jointly necessary and individually insufficient: **(1)** volatile rides its own user turn (out of the system head); **(2)** every prior turn replays byte-identically (frozen append-only). Property (1) is the layout move; property (2) is what makes it persist. D2 delivers both.

---

### D2 — Frozen append-only layout (Part A)

**The wire sequence becomes a strict forward-extension log.** Per turn:

```
[0] system   = [STATIC: tool-use rules] [SEMI-STATIC: tool-awareness · deployment · operator stanza
               · STABLE skill index + <skill_index_directive>] [decomposition-base]   ← byte-identical every turn
‖ cloud cache_control breakpoint sits at the end of message[0] and after the last tool (litellm_client.py:60-75) ‖
[1..M] conversation history — each past user turn carries its OWN frozen volatile block
       (the recall + skill bodies + usage-directives that were live when that turn ran), then its assistant reply.
       These bytes never change again. ← frozen append-only
[M+1] current user turn = [fresh volatile: recalled memory · selected skill bodies · <skill_usage_directives>
       · D3 salient highlights] + [user query] (+ /no_think suffix)   ← the only new bytes this turn
```

**What moves, concretely (build implements; anchors are current main):**

1. **Stop appending volatile to the system message.** Delete the `_skill_bodies_tail` and `memory_section` appends at `executor.py:2275-2285`. After D2, message[0] is exactly `inner_system_before_memory` (captured at `executor.py:2270`) and **nothing else** — it is byte-stable across the session (subject to the D4 determinism invariants for the skill index and the `operator_stanza` byte-stability contingency, both unchanged here).
2. **Attach the per-turn volatile to the current user message.** The block (recall + selected skill bodies + `<skill_usage_directives>` + D3 highlights) is rendered into the **current** user turn — prepended to the user content as a fenced block, or carried as a dedicated message placed immediately before the user query at the tail. Placement is at the very end of the sequence, the newest position, where new bytes belong for a forward extension.
3. **Persist the volatile *into* `session.messages`.** Today `ctx.messages = list(session.messages)` (`executor.py:1398`) replays history and `session_manager.update_session(..., messages=ctx.messages)` (`executor.py:3323`) saves it — but the volatile lives in the re-derived system prompt, **not** in `ctx.messages`, so it is recomputed fresh every turn and never frozen. D2 requires the volatile block to be written into the **persisted user message** for that turn, so the next turn's `list(session.messages)` replay reproduces it byte-for-byte in its original position. This is the single change that converts property (1) into property (2).

**Decision 1 — what gets frozen: BOTH recalled memory AND selected skill bodies (+ usage-directives + D3 highlights).** This is forced, not chosen. The local backend reuses only exact forward extensions, so the alternative ("re-select skills / refresh recall and rewrite them in place each turn") is precisely a mid-sequence rewrite → 0 reuse — it is the bug we are removing. Therefore any volatile content that is to survive into the cached region must be frozen in place at the turn it was produced; only the **newest** turn carries fresh volatile. Consequence (the accumulation cost): turn-3's recall and skill guidance persist verbatim into turn-7's context — monotonic token growth plus *stale* recall/skill guidance lingering. This staleness is real and is the price of reuse; it is **bounded** by three valves: the stable skill **index** is always in the cached prefix (so the model always sees the full live catalog regardless of which bodies are frozen), the D3 salient highlights refresh per turn in the newest turn's block, and the D5 cold-tier retrieval recovers anything summarized away. The accumulation itself is bounded by the Part B scheduler, which periodically resets the frozen prefix. (This partially reverses D4's just-shipped "skill bodies in the system-message tail" placement: D4 put bodies in the *system* tail for cache-hash hygiene; D2 moves them to the *sequence* tail riding the user turn, which is the placement local reuse actually requires. The D4 split of index-vs-bodies is preserved — index stays cached in message[0]; bodies move to the user turn. Gate on FRE-407 quality, §Verification.)

**Decision 5 — frozen-prefix re-establishment (post-reset sequence structure).** After a Part B reset at turn *N*, the persisted sequence is:

```
[0] system  (unchanged — still byte-identical; on cloud it survives the reset entirely, breakpoint sits before history)
[1] frozen_narrative  — one persisted system message: the cumulative narrative of cold turns 1…N−K
[2..K+1] the last K turns kept VERBATIM, each still carrying its own frozen volatile block
[K+2] current user turn + fresh volatile        ← forward extension resumes here
```

Properties that make the next run reuse again:
- The `frozen_narrative` message is **persisted into `session.messages`** (replacing the transient `compression_manager._summaries` mechanism, see Decision 4) so it replays byte-identically; it is itself frozen until the *next* reset.
- The cumulative narrative = previous narrative + newly-summarized increment, so no cold context is lost across resets (the "append the increment" intent of the original proposal is preserved — but **materialized at reset time as one rewrite**, not as a live mid-sequence append, because the latter is impossible for the local backend, see the boxed note above).
- `K` (verbatim recent turns kept) reuses the existing tail-floor concept (`within_session_min_tail_ratio`, default 0.25 → ~24k of 96k); it gives recency continuity so the model is not handed a pure summary.
- Turn *N+1* then forward-extends `[system][frozen_narrative][K verbatim turns][turn-N user+vol][assistant N][turn-N+1 user+vol]`, whose prefix through assistant *N* is byte-identical to what was cached on turn *N*'s first post-reset call → reuse resumes. This is the **rising edge of the sawtooth**; the reset itself is the falling edge (one full re-prefill on local — the cost Part B amortizes).

**Within a run, history is strictly append-only — zero rewrites, full reuse. The reset is the single, scheduled, amortized rewrite exception.** This is the precise reconciliation of "never rewrite" (original D2) with "must bound accumulation": never-rewrite holds *within a run*; the scheduler decides *when* to pay one rewrite.

**Implementation invariant — persist the EXACT wire bytes, or the cache silently dies.** Frozen append-only only yields byte-identical replay if every per-turn transform applied to a message is either persisted with it or applied identically on every replay. Two live hazards on the current path:
- **`/no_think` suffix** (`executor.py:2297-2300`, `_append_no_think_to_last_user_message`) is appended only to the *current last* user message before sending. A message that was last on turn *N* (got the suffix) is *not* last on turn *N+1* (suffix targets the new last) — so unless the suffix is persisted, position *N*'s bytes differ between turns → mid-sequence divergence → local reuse 0. **Required:** either persist the suffixed form into `session.messages`, or move thinking control out of the message bytes (generation param / template kwarg) so it never perturbs the frozen prefix. Build must pick one and verify byte-stability.
- **`_validate_and_fix_conversation_roles`** (`executor.py:2303`) may rewrite role alternation just before send. Any such fix must be applied **before** persistence (so the persisted form equals the sent form), not only to the transient `request_messages`.
This invariant is the same class of bug D1/D4 fought (empty-fragment separator churn): a transform that touches the cached region must be deterministic and persisted. It is the most likely cause of a "frozen layout still shows 0 reuse" failure and must be the first thing build instruments.

**Decision 6 — disposition of `codex/fre-433-layout-tail-arm`: SUBSUME; do not ship independently; do not introduce its `AGENT_CACHE_VOLATILE_TAIL_LAYOUT` env var.** The arm implements property (1) only (volatile to a trailing **ephemeral** message) — validated **cloud-only** (reuse 17–20k, confirmed it improves Sonnet), **zero** local benefit because the ephemeral message is not persisted/frozen (property 2 absent), so turn *N+1* diverges mid-sequence. Shipping it as a cloud-only flag would create a second, divergent layout path that the full D2/D3 must then unify, give nothing to the primary target (local), and risk a measurement-confusing intermediate state. Its value — *proof that property (1) alone fixes cloud* — is already captured in the diagnostic. Build implements the full design (properties 1+2) behind the single `cache_frozen_layout_enabled` flag governing **both** backends; the arm branch is closed unmerged. (Recorded in Open decisions.)

### D3 — Two-object summary & cache-aware compaction scheduler (Part B)

**Two-object summary (unchanged in intent, repositioned for the frozen model).** A reset's `compress_in_place` produces **`(frozen_narrative, salient_highlights)`**:
- `frozen_narrative` → the persisted, cumulative cold-history summary (Decision 5); frozen until the next reset.
- `salient_highlights` → a short, **hard-bounded** distillation (decisions, constraints, open threads, key entities), derived as a strict subset of the narrative in a single extraction (no contradiction). It is **not** frozen into history: it rides the **current** user turn's fresh volatile block (Decision 1) and is regenerated each turn — only the newest turn's highlights are live; prior turns' highlights freeze inert in place like their recall. This is what lets the bulk narrative stay frozen/cached while per-turn freshness still reaches the model at the newest position, at zero cache cost (it is already in the volatile region).

**Decision 3 — compaction-trigger model: fire at the cost/quality optimum, not a fixed token ratio.** Replace the reactive triggers — the soft `context_compression_threshold_ratio = 0.65` (`compression_manager.maybe_trigger_compression`, fired every eligible turn at `executor.py:3403`) and the hard `within_session_hard_threshold_ratio = 0.85` (`needs_hard_compression`, `executor.py:1808`) — with a scheduler that resets when the marginal cost of *holding* (not compacting) one more turn exceeds the amortized cost of *resetting*:

```
fire reset when:   marginal_hold_cost(N)  ≥  R_backend / L_current
where
  marginal_hold_cost(N) = Q_slope · stale_tokens(N)        # quality cost of staleness (FRE-407-calibrated)
                        +  accum_token_penalty(N)           # budget-pressure cost of carrying the grown prefix
  R_backend             = backend-asymmetric one-time reset (re-prefill) cost   # see below
  L_current             = turns elapsed in the current run since the last reset
```

The determinism the frozen layout buys is what makes every term computable:
- **Reuse savings `S(N)`** — monotonic in run length; banked prefill = `cached_tokens · prefill_cost_per_token` (local prefill is 92–96 % of turn latency, ~1050 tok/s, so this is the dominant win). Longer runs bank more; this is *why* we resist resetting.
- **Accumulation cost** — frozen volatile grows by a **known, bounded increment per turn** `Δ_turn ≈ recall + skill_bodies + usage_directives + highlights + query + reply`. `total_tokens(N) ≈ total_tokens(N−1) + Δ_turn` is predictable; `accum_token_penalty` rises as the running total approaches the budget ceiling (Decision 2).
- **Quality cost `Q`** — staleness penalty. Calibrate `Q_slope` (rating loss per accumulated stale token) from the **FRE-407 per-turn rating trace** that the A/B harness already produces. Until that fit exists, build ships a conservative proxy: `Q_proxy(N) = q_per_turn · stale_turns(N)` with `q_per_turn` set so the predicted loss never crosses the FRE-407 flat-or-up gate (Decision 2); the scheduler logs predicted-vs-actual so the slope is fit online.

**Backend-asymmetric reset cost (same scheduler, one backend-aware term):**
- **Cloud:** the stable system+tools segment survives a reset (Anthropic re-reads from the `cache_control` breakpoint at the end of message[0] + last tool, `litellm_client.py:60-75`); a reset only re-creates the rewritten-history portion → **`R_cloud` small** → `R/L` small → fire **sooner**, compact **tighter/fresher**.
- **Local:** any mid-history change → **full re-prefill** of the new prefix (Test 1) → **`R_local ≈ total_tokens · prefill_cost_per_token`, large** → fire **later**, compact **looser** (let runs grow long before paying the reset). Same formula, the asymmetry lives entirely in `R_backend`.

Concrete defaults for build (env-overridable, tuned post-deploy against the harness): `cache_reset_min_run_turns` — local `12`, cloud `4` (anti-thrash floor; never reset more often); the `0.85` hard ratio is **retained only as an overflow backstop** (if the scheduler somehow hasn't fired and the running total nears the context ceiling, hard compaction still prevents overflow); the `0.65` soft every-turn trigger is **removed** (the scheduler subsumes it).

**Decision 2 — accumulation-vs-reuse bound (the ceilings that cap the optimum).** The scheduler fires at the cost optimum but **never later** than two hard ceilings:
- **Token ceiling:** accumulated frozen context must stay under `cache_frozen_accum_max_ratio · context_window_max_tokens` (default `0.50` → ~48k of 96k), reserving headroom for the system prefix, the current volatile, and generation. Hitting it forces a reset regardless of the optimum.
- **Quality ceiling (FRE-407):** the per-turn rating trace must stay **flat-or-up** vs. the head-layout baseline. The staleness budget (`Q_slope · stale_tokens`) is calibrated so the predicted quality loss never crosses this gate; if the *measured* FRE-407 trace dips, the scheduler tightens (resets sooner) until it recovers. This ceiling is the **primary rollout gate** — relocating recall/skill bodies out of the head must not degrade answers.

The operating bound is `reset_at = min(cost_optimum, token_ceiling, quality_ceiling)`, floored by `cache_reset_min_run_turns`.

**Decision 4 — reconcile `within_session_compression`: it becomes the scheduled reset, not an ad-hoc rewrite.** Today `compress_in_place` (`within_session_compression.py`) rewrites history (head + LLM summary + tail) — which **is** a cache reset — and fires reactively (soft via `compression_manager`, hard mid-orchestration), while its output is **transient**: stored in `compression_manager._summaries`, re-inserted by `apply_context_window(compressed_summary=get_summary())` (`executor.py:1575-1584`) at a fixed position each turn, and **popped on read** (`compression_manager.get_summary`, one-shot). That transient re-derivation is itself a per-turn cache-buster (this ADR's Context #2). Under D2/D3:
- `compress_in_place` is invoked **only** when the Decision-3 scheduler decides to reset — never reactively per turn.
- Its output is **persisted into `session.messages`** as the canonical new history (Decision 5): the `frozen_narrative` becomes a real, durable message, not a re-inserted artifact. The `compression_manager._summaries` / `get_summary` / `apply_context_window(compressed_summary=…)` re-insertion path is **removed**; `apply_context_window` keeps only its pure truncation/`_sanitize_tool_pairs` role.
- The existing head-middle-tail machinery (`_extract_head`, `_extract_tail`, `_pre_pass_tool_outputs`, `summarize_middle`) is reused verbatim *inside* the reset; only its *scheduling* and *persistence* change. The `WithinSessionCompressionRecord` dual-write (ADR-0054 §D4) continues, with `trigger` extended to `"scheduled_reset"`.

This makes the one rewrite event deliberate, persisted, and amortized — exactly the sawtooth Decision 5 re-establishes.

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
- **Observability / joinability (ADR-0074):** the split adds a distinct volatile component id for the relocated bodies and keeps `skill_index` on the cached side — both ride the existing `model_call_completed` / `PromptIdentity` stamp, which already carries `session_id` + `trace_id`, so D4 introduces **no new un-joined emit site**. The only requirement is that the new component id enters the `component_ids` taxonomy consistently, so the FRE-406 cache-erosion monitor and prompt-identity queries continue to attribute prefix vs. dynamic bytes correctly. (D4's entire acceptance is read from these joined instruments — it is observability-native by construction.)

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

Every change is verified against the P1 instruments: `static_prefix_hash` must go **constant across same-session turns**; cross-turn `cache_n` / `cached_tokens` must rise from ~0; and a new **post-compression "forgot-an-earlier-fact" error rate** must not regress (the metric for D3/D5). No change ships without a before/after on these. **Local-truth caveat (FRE-433):** for the local backend, the authoritative reuse signal is the SLM-server `timings.cache_n` / `prompt_n` read on the turn's first full-context call — **not** the ES `cache_read_tokens` aggregate, which can mislead. A constant `static_prefix_hash` is *necessary but not sufficient* for local reuse: it hashes only a substring of message[0]; local reuse additionally requires the full wire sequence to be a byte-identical forward extension (D2).

---

## Open decisions (researcher / data-gated)

- **D2/D3 what-gets-frozen:** ~~recall only / skill bodies only / both~~ — **RESOLVED 2026-06-01: both** (forced by the local exact-forward-extension rule; the alternative is a mid-sequence rewrite → 0 reuse). See §D2 Decision 1.
- **D2/D3 compaction model:** ~~continuous append-to-narrative vs batched reset~~ — **RESOLVED 2026-06-01: batched scheduled reset (sawtooth)**. Continuous append is incompatible with local reuse (the narrative sits behind the recency band; appending shifts later tokens = mid-sequence change). See the boxed note at the head of D2 and §D3 Decision 4.
- **D2/D3 trigger:** ~~reactive 0.65 soft / 0.85 hard token ratios~~ — **RESOLVED 2026-06-01: cost/quality optimum** (`marginal_hold_cost ≥ R_backend / L`), backend-asymmetric reset cost, floored by `cache_reset_min_run_turns`, capped by token + FRE-407 quality ceilings; `0.85` retained as overflow backstop, `0.65` removed. See §D3 Decision 3. **Parameterization (`Q_slope`, ceilings, min-run) is data-gated** — fit online from the FRE-407 trace via the FRE-433 harness; ADR records the model, build tunes the constants.
- **`codex/fre-433-layout-tail-arm` disposition:** ~~ship cloud-only flag vs subsume~~ — **RESOLVED 2026-06-01: subsume** into the full D2/D3 layout; branch closed unmerged; `AGENT_CACHE_VOLATILE_TAIL_LAYOUT` not introduced; the single `cache_frozen_layout_enabled` flag governs both backends. See §D2 Decision 6.
- **D4 skill handling:** ~~route-once-per-session vs split-index+late-bodies vs relocate-all~~ — **RESOLVED 2026-06-01: split** (stable index cached, per-turn bodies + usage-directives to the volatile tail). See §D4; owns the cache-GREEN gate D1 could not meet.
- **D4 index format/size:** Pareto search (routing accuracy vs tokens) — needs a labeled routing-eval set; candidate for DSPy optimization. **Still open**, but de-risked: this is a follow-up cost-trim that does *not* gate the cache-GREEN result (§D4).
- **D5 retrieval:** semantic-only vs hybrid; ranking; how much to auto-inject vs require an explicit tool call.
- **Objective horizon:** maximize single-turn static prefix vs minimize *total* prefill across an N-turn horizon (dynamic content grows monotonically and will eventually dominate regardless of order — argues for the cold tier, D5).

The remaining open items (D4 index format, D5 retrieval, objective horizon, and the D3 parameter fit) are posed to external CS/math reviewers and tuned against the harness; this ADR records the architecture, not the final parameterization.

---

## Consequences

**Positive**
- Cross-turn KV reuse becomes achievable: on a stable prefix, repeat-turn prefill should drop from ~8 s toward the within-turn figure (~0.5 s observed). Prefill is 92–96 % of latency, so this is the dominant win.
- Compaction stops being a per-turn cache-buster and *grows* the cached prefix (D2).
- Lossless recall (D5) removes summarization's worst failure mode.
- Important content lands where the model actually attends (D1/D3/D6).

**Negative / tradeoffs**
- **Frozen volatile accumulates** (D2 Decision 1): turn-K's recall + skill bodies persist verbatim into later turns — monotonic token growth plus *stale* recall/skill guidance lingering in-context. Bounded by the Part B scheduler (resets), the always-cached live skill index, per-turn D3 highlights, and the D5 cold tier — but it is the real price of local reuse.
- **The cache is byte-fragile** (D2 implementation invariant): any per-turn transform that touches the frozen region and is not persisted (e.g. the `/no_think` suffix, role-alternation fixes) silently re-zeroes local reuse. This is the most likely failure mode and must be instrumented first.
- **The reset is a real cost** (the sawtooth trough): on local it is a full re-prefill once per run; the scheduler exists precisely to make runs long enough to amortize it (backend-asymmetric).
- Cold-tier retrieval adds tool round-trips + the "unknown unknowns" recall risk (mitigated by the D5 affordance + D3 highlights).
- Splitting skill routing (D4) trades a stable cached index against per-turn body re-prefill; the index must be minimal (attention dilution).
- Touches a hot path (every turn) — gated behind `cache_frozen_layout_enabled` (default off, no-op when off) and rolled out behind the FRE-405/406/407 instruments.

---

## Verification

- `static_prefix_hash` constant across ≥5 same-session turns of mixed query types (today: 3 distinct values across 5 turns).
- Cross-turn `cache_n` > 0 on repeat-prefix turns (today: 0); mean `orchestrator.primary` cache-hit rate rises past the ADR-0078 / FRE-406 60 % gate.
- A deliberate skill/tool-desc edit shifts `static_prefix_hash` exactly once (drift signal), then re-stabilizes.
- Post-compression forgot-fact error rate flat or improved with D3 highlights vs summary-only (A/B), at bounded token cost.
- D5: a query needing a cold detail triggers `recall_session_history` and answers correctly without that detail being in-prompt.

### D2/D3-specific acceptance (FRE-434 — the local cache-reuse gate)

Verified with the FRE-433 A/B harness (`scripts/eval/fre433_cache_ab/`, both `--profile {local,cloud}`), frozen layout ON vs the D1/D4 head-layout baseline. The **local** truth metric is the SLM `timings.cache_n` / `prompt_n` (read on the first full-context call of each turn), **not** ES `cache_read_tokens` aggregates; verify across two *separate* turns, never a within-turn continuation (which already reuses → false PASS).

D2/D3 is **done** only when all hold:

- **Local reuse (the headline gate):** `cache_read_tokens > 0` (backend `timings.cache_n > 0`) on the **first full-context call of every turn ≥ 2** — today **0**.
- **Local prefill collapse:** turn-≥2 `prompt_n` drops from ~8k to **~the new-tail size** (the construction shows 6799 → 277; accept ≈ the per-turn `Δ_turn` increment).
- **Cloud reuse holds:** cross-turn cloud reuse **≥ the 17–20k arm-B baseline** (and cloud cache-creation per turn collapses toward ~0.1–2.2k), confirming the unified layout does not regress what the arm proved.
- **Quality flat-or-up (primary rollout gate, FRE-407):** per-turn rating trace **flat or improved** vs. the head-layout baseline — relocating recall + skill bodies out of the system head must not degrade answers. A measured dip blocks the rollout and tightens the scheduler.
- **Determinism / scheduler:** measured per-turn token growth **matches the predicted `Δ_turn` increment**; compaction **fires at the computed optimum** (not the old 0.65/0.85 ratios) and **re-establishes a reusable frozen prefix** — reuse resumes on the turn *after* a reset (the sawtooth rising edge is observable in `timings.cache_n`).
- **Byte-identity guard:** the persisted turn-*N* message bytes equal the wire bytes sent on turn *N* (no `/no_think`- or role-fix-induced divergence); a deliberate probe that perturbs one frozen byte must drop reuse to 0 (proving the instrument is live).
- **Flag hygiene:** with `cache_frozen_layout_enabled=False` the system is byte-for-byte the current D1/D4 behavior (no-op verified).

### D4-specific acceptance (the cache-GREEN gate D1 could not meet)

D4 is **done** only when `orchestrator.primary` flips GREEN on the FRE-405/406/407 instruments:

- **Prefix stability (FRE-405):** `prompt_static_prefix_hash` constant across ≥5 same-session turns — i.e. `distinct_prefixes == 1` for the session, vs the **6-across-23** measured post-D1.
- **Erosion gate (FRE-406):** `make cache-erosion-status` for `orchestrator.primary` reports Jaccard ≥ 0.90 (`status=stable`), flipping the current RED.
- **Real reuse:** cross-turn `cached_tokens > 0` on repeat-prefix turns (today ≈ 0); the stable skill index now sits in the cached region.
- **Quality unchanged (FRE-407):** the per-turn rating trace stays flat or improves — moving skill bodies from the cached prefix to the end of the system message must not degrade routing/answer quality.
- **`operator_stanza` contingency (named residual, from design review):** the component-isolation evidence rules out decomposition and synthesis/tool variance but cannot prove `operator_stanza` bytes were constant across the session. If post-D4 the hash count lands **>1**, isolate `operator_stanza` next (group surviving hashes by component content; confirm whether a mid-session memory write mutated it). Do **not** mark D4 done on a partial drop (e.g. 6→2) — the gate is exactly 1.

**Verification-tooling prerequisite (FRE-406 amendment).** The cache-erosion monitor (`scripts/monitors/cache_erosion_monitor.py` / `compute_erosion_report`) currently windows on whole calendar days (`window_days`, `calendar_interval: "1d"`). A same-morning deploy + same-session verification (exactly the D4 case — the entire 23-turn dataset above lives inside one morning) is **invisible at day granularity**: it collapses into a single bucket and reports `insufficient_data`. The monitor must gain an **`--hours-ago` window** (sub-day range + an hour/turn-level bucketing) so D4 can be verified in the same session it deploys, per the "post-deploy steps run in the same session as deploy" rule. This is a small build-worktree change, tracked as an explicit sub-item on the D4 ticket; it gates D4's *verifiability*, not its implementation.
