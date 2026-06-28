# FRE-656 — Embedder/Reranker Quality-Ceiling Benchmark: the gate can't measure what it gates

**Date:** 2026-06-28 · **Ticket:** FRE-656 · **Initiative:** Memory Recall Quality (FRE-435) · **ADR:** ADR-0100 (relevance-bounded recall) · **Builds on:** FRE-655 A/B + floor calibration (PR #271), FRE-489 probe, FRE-491 baseline.

## TL;DR

1. **Qwen3-Embedding-4B (Q4_K_M, 2560-dim) shows no measurable gain over the current Qwen3-Embedding-0.6B (f16, 1024-dim)** on the FRE-489 probe — identical recall@5 (0.722), identical best-achievable separation (Youden's J = 0.714), latency-neutral.
2. **But that result is inconclusive by construction.** A plain BM25 keyword search over the same stored text **beats** the vector path (recall@5 1.00 vs 0.72) with zero entity-name leakage. The probe does not exercise semantic vector matching — so it cannot distinguish embedders, nor justify the vector+rerank apparatus.
3. **The one measured, decision-ready win is reranker hosting, not the embedder:** the production reranker on the VPS CPU costs ~4.4 s per recall; the same-workload 4B reranker on the Mac GPU costs ~0.6 s. The reranker is a query-time rescorer (never enters the vector index), so moving it is a clean swap — no re-embed.
4. **Recommendation:** do **not** do the one-way-door 4B embedder re-embed on this evidence. First build a **vocabulary-divergent** probe that actually tests semantic recall, then re-run the embedder A/B. Separately, decide reranker hosting for the latency win.

## What was built (the enabling change)

The benchmark "needs no code change" premise broke once the owner routed the 4B through the Access-gated Mac SLM gateway (`https://slm.frenchforet.com/v1`). Two memory client paths could not reach it and **degraded silently** (zero vector / passthrough), which would have produced garbage numbers:

- `memory/embeddings.py` and `memory/reranker.py` did not inject the CF-Access service token that `llm_client/client.py` injects. Fixed: hostname-gated injection reusing `service/cf_service_token.py`.
- The OpenAI SDK's default `User-Agent: OpenAI/Python` trips a **Cloudflare WAF rule** on the gateway (a 403 "request blocked"; the raw-httpx LLM client is unaffected). Fixed: a benign `User-Agent` for the gated host. Bisected live — the `x-stainless-*` headers and a custom UA are fine; only `OpenAI/Python` is blocked.

Both fixes are forward-correct: any future Mac-hosted embedder needs this auth. Plus `config/models.benchmark-4b.yaml` and a safe runner (`scripts/eval/fre435_memory_recall/run_embedder_benchmark.sh`) with preflight guards (force-set test substrate, assert `neo4j_uri == :7688`, assert a probe embedding is non-zero and correctly-sized before seeding — catching exactly the silent-degradation traps above).

## Method

Test substrate (Neo4j :7688 / ES :9201 / Postgres :5433), FRE-489 probe (21 cases), same FRE-655 harness. One CLI process per (embedder × mode) to avoid the module-global singleton trap (settings / embedding client / model-config cache). The 0.6B was re-run as a same-session control and reproduced the FRE-655 baseline exactly. `ensure_vector_index()` auto-recreated the entity index on the 1024→2560 dimension change.

## Results

### Separation (calibrate — co-resident hard negatives)

| | positives median | negatives median | per-case pos>neg | best-J floor | recall / fpr at best-J |
|---|---|---|---|---|---|
| 0.6B (f16, 1024) | 0.8225 | 0.7096 | 17/21 (81%) | 0.768 | 0.762 / 0.048 |
| 4B (Q4, 2560) | 0.7973 | 0.6789 | 16/21 (76%) | 0.740 | 0.810 / 0.095 |

Identical best-achievable discrimination (**J = 0.714 both**). The 4B's cosines are genuinely different (all 21 cases differ; it sits in a lower regime, so the floor recalibrates 0.768→0.740 — validating ADR-0100's config-driven, embedder-calibrated floor). But it does **not** separate positives from negatives any better. The overlap is intrinsic: the high distractors are genuinely related concepts (the game-theory cluster), and a better model represents that true proximity *more* faithfully, not less.

### Recall (ab — live distractors)

| | entity-path recall@5 (off→on) | broad-path hit (off→on) | misses |
|---|---|---|---|
| 0.6B | 0.0 → 0.722 | 0/21 → 18/21 | ctrl-personal-detail, ctrl-travel-plans, ctrl-unscoped-project |
| 4B | 0.0 → 0.722 | 0/21 → 18/21 | **same three** |

Identical, and the same three control cases miss for both — the residual ceiling is **not embedder-bound** (it is extraction/probe-structure). Every per-case cosine differs between 0.6B and 4B, so the 4B was genuinely engaged; recall@5 is simply robust to a uniform cosine downshift that preserves order.

### Latency (clean, separate processes)

| | embed p50 / p95 | rerank p50 / p95 |
|---|---|---|
| 0.6B local (VPS CPU) | 130 / 162 ms | **4392 / 4632 ms** |
| 4B via tunnel (Mac GPU) | 128 / 173 ms | **591 / 610 ms** |

The 4B-over-tunnel embed ≈ 0.6B-local-CPU embed — the GPU+tunnel path is not slower. The reranker line is the real story: the **local-CPU 0.6B reranker costs ~4.4 s/recall**; the Mac-GPU 4B reranker does the same work in ~0.6 s — a 7× cut, driven by *where it runs*, not model size. (An earlier in-process latency probe wrongly reported both at ~4.4 s — an `importlib.reload` / `lru_cache` state-bleed; the separate-process numbers above are the truth.)

## The pivotal finding — keyword beats vector on this probe

The probe enforces *referential discipline*: a query never contains its expected entity's **name**. So I ran a BM25 baseline over the same co-resident candidate set, varying only the document text:

| Method (same 40 entities, same oblique queries, 18 scored) | recall@1 | recall@5 |
|---|---|---|
| keyword (BM25) query ↔ **entity name only** | 0.167 | 0.389 |
| keyword (BM25) query ↔ **name + description** | **0.944** | **1.000** |
| vector (0.6B *or* 4B) query ↔ name + description | — | 0.722 |

The vector path embeds `f"{name}: {description}"` (service.py:784) — exactly the keyword document — so this is apples-to-apples.

**Mechanism:** against entity *names*, the queries are genuinely oblique (keyword collapses to 0.39 — referential discipline works). But the *descriptions* were authored by the same hand as the queries to depict one scenario, so they share ordinary content vocabulary ("chapter / writing", "leftover radiation / early universe"). Keyword rides that to 1.00; the vector path rides the *same* lexical overlap, just less efficiently. **There is no semantic gap in this probe for a better embedder to close** — which is precisely why 0.6B = 4B, and why keyword ties or beats both.

**Does the probe represent the real world?** Not in the dimension that matters for vectors. Real recall fails when you ask *weeks later in your own words* and the note was written in *different* words — vocabulary drift. This probe has none: query and stored text are lexically aligned by construction. It tests "lexical recall with the label hidden," not "semantic recall."

## Recommendation

- **Do not re-embed the production KG to the 4B on this evidence.** It is a one-way door for no measured recall/separation gain, and the gate that would justify it cannot measure embedder value.
- **Build a vocabulary-divergent probe first** (queries sharing ~no content words with the stored note text — true paraphrase/synonymy). Only that can answer (a) is a bigger embedder worth it, and (b) is the vector path worth its cost over keyword at all. Then re-run this exact A/B harness.
- **Reranker hosting is the real lever** — ~4.4 s → ~0.6 s/recall by moving it to the GPU. Clean swap (no re-embed). Worth its own decision, including whether the reranker earns ~4.4 s on the VPS CPU today at `reranker_top_k=10`.
- **Floor hand-back to FRE-655:** the similarity floor is a weak guard for *both* embedders (intrinsic overlap), and the current calibration comes from a lexically-easy probe — so **do not roll out an aggressive floor** (0.75 costs ~24% recall). Keep the primary guard as top-k + reranker with a conservative floor (~0.6–0.65, dropping only clear non-matches), and **recalibrate the floor on the vocabulary-divergent probe** once it exists.

## Limitations

One probe (n=21), short entity targets, native 2560-dim Q4_K_M only (no Q8 / dimension-truncation sweep), f16-0.6B vs Q4-4B (quantization narrows the gap). The "no gain" result is scoped to *this probe* — which the keyword finding shows is the wrong instrument for the question. BGE-M3 / cloud reference candidates from the original ticket were not run; the locked decision had already collapsed the sweep to the 4B, and the probe limitation makes further candidates premature until the probe is fixed.

## Follow-ups filed

- Vocabulary-divergent (semantic) probe split for FRE-489 — the prerequisite to any embedder or vector-vs-keyword decision.
- Reranker hosting / cost decision — GPU-host vs the ~4.4 s VPS-CPU cost; revisit `reranker_top_k`.
