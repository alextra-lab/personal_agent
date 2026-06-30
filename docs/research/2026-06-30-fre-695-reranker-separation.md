# FRE-695 — Does a cross-encoder reranker open the clean floor the embedder could not?

**Date:** 2026-06-30 · **Ticket:** FRE-695 ("Memory Recall Quality"). Continuation of FRE-694/670/656.
**Backing:** ADR-0087 §D (recall measurement) · ADR-0100 (the floor this gates) · ADR-0099 (config).
**Substrate:** none — offline harness (no Neo4j, no live prod KG). Only the paraphrased, PII-free
`semantic_probe.yaml` is sent to any remote/cloud endpoint.

## The question

FRE-694 proved that **no bi-encoder embedder** — local 0.6B/4B/8B f16 or cloud Voyage — opens a *clean
floor* on the FRE-670 probe: a similarity cutoff that cleanly separates true matches (positives) from
no-record (negatives). Best Youden's J was only **0.42–0.59**; the positive and negative clouds overlap
everywhere. The hypothesised lever is the **cross-encoder reranker**, which reads (query, document)
*together* through cross-attention rather than comparing two pre-computed vectors.

> Does any reranker open a clean floor on the same hard FRE-670 distractors? If yes → the reranker is the
> lever (FRE-655 calibrates the floor on reranker scores). If even rerankers overlap → the floor problem
> is deeper than the retrieval models, and recall quality has to come from structure, not a score cutoff.

## Method

Same 54-case probe (`semantic_probe.yaml`), same 49-note corpus (`"{name}: {description}"`), same metric
as FRE-694: per case, score the query against its true-match note(s) — **positives, per-expected-entity**
— and against the non-expected notes — **negatives, the strongest distractor per query + every control**.
Report best Youden's J (swept at the *observed* scores, since reranker score scales are arbitrary and a
fixed grid understates a compressed band), overlap counts, robust p5/p95, and the clean-floor verdict —
reusing `separation_report.py`. An instrument-sanity gate (a trivial relevant-vs-irrelevant pair must rank
the relevant doc #1) runs before any arm's aggregates are trusted (the FRE-694 discipline).

**Reranker arms** (one `/v1/rerank` request per query over its candidate set; relevance score per pair):
llama.cpp (Qwen3-Reranker 0.6B/4B f16), **MLX** (Qwen3-Reranker 4B mxfp8, 8B mxfp8, 8B bf16), and Voyage
cloud (rerank-2.5, rerank-2.5-lite). Production retrieves with the embedder then reranks its shortlist, so
the bench reranks the embedder's top-15 (∪ the case's expected notes), not the whole corpus.

**Embedder arms** (re-confirming the FRE-694 ceiling on a second runtime + two more quant levels): MLX
Qwen3-Embedding 0.6B/4B/8B at bf16 and 8bit, scored offline (embed → MRL truncate → L2 renormalize →
cosine in Neo4j `(cos+1)/2` space), dimension sweep client-side.

## Results

### Embedders — the FRE-694 ceiling is real (runtime- and quant-robust)

| Embedder | runtime / quant | native J | best J | clean floor? |
|---|---|---:|---:|:---:|
| 0.6B | llama.cpp f16 (prod) | 0.42 | 0.43@512 | No |
| 0.6B | MLX bf16 | 0.417 | 0.447@512 | No |
| 0.6B | MLX 8bit | 0.417 | 0.429@512 | No |
| 4B | llama.cpp f16 | 0.532 | **0.594@1024** | No |
| 4B | MLX 8bit | 0.532 | **0.594@1024** | No |
| 8B | llama.cpp f16 | 0.534 | 0.550@1024 | No |
| 8B | MLX bf16 | 0.534 | 0.552@2048 | No |
| 8B | MLX 8bit | 0.534 | 0.552@2048 | No |
| Voyage-4-large | cloud | — | 0.59–0.64 | No |

- **MLX ≡ llama.cpp**: the 4B lands on **0.594@1024 to three decimals** on both runtimes; the 8B within
  0.002. Embedder separation is **purely model-driven, runtime-agnostic**.
- **8bit ≡ bf16**: 8B bf16 and 8B 8bit are identical (0.534 / 0.552); 0.6B differs by 0.018 (n=54 noise).
  The embedder is **precision-robust at 8-bit and above** — which pins the old FRE-656 confound
  specifically to **Q4 (4-bit)**, not quantization in general.
- Size plateaus after ~4B (0.6B→4B +0.11, then 4B≈8B); the best dimension is the *middle* (512–1024),
  not native — extra dims add distractor noise, not signal (FRE-694).

### Rerankers — the lever, but still no clean floor

| Reranker | runtime / quant | best J | latency/query (15 docs) | complete run? |
|---|---|---:|---:|:---:|
| Qwen3-Reranker-0.6B | llama.cpp f16 | ~0.65 | ~0.45s | partial only (stall) |
| Qwen3-Reranker-4B | llama.cpp f16 | 0.71 | ~1.6s | intermittent (stall) |
| voyage-rerank-2.5-lite | cloud | 0.66 | ~0.22s | ✅ |
| voyage-rerank-2.5 | cloud | 0.73 | ~0.24s | ✅ |
| Qwen3-Reranker-4B | **MLX mxfp8** | 0.747 | ~1.74s | ✅ |
| Qwen3-Reranker-8B | **MLX bf16** | 0.726 | ~4.37s | ✅ |
| **Qwen3-Reranker-8B** | **MLX mxfp8** | **0.785** | ~4.26s | ✅ |

- **The reranker is the strongest single lever**: best embedder J=0.59 → best reranker **J=0.785**, **+0.19**.
  The cross-encoder (cross-attention over the pair) separates materially better than any bi-encoder cosine.
- **But no reranker opens a clean floor either.** Even the best (8B mxfp8, J=0.785) is ~88% recall @ ~9%
  FP — the hardest FRE-670 distractors still outscore the easiest true matches. *The floor problem is
  deeper than the retrieval models.*
- **Cross-runtime/cloud agreement**: the local Qwen3-Reranker-4B (J=0.71) and Voyage (0.73) and MLX-4B
  (0.747) all cluster — so the verdict is not a single-vendor artifact.

### The llama.cpp reranker stall — and the MLX fix

The local Qwen3-Reranker on **llama.cpp** (`--reranking` / causal-LM yes/no-logit path) stalled under
sustained rerank load — a backend 504/524 after a variable number of *distinct* requests. The
investigation ruled out, by controlled repro: pure request count (60 identical requests ran clean), query
variety (54 distinct queries with fixed docs ran clean), document variety alone (a bare distinct-doc loop
ran clean), candidate-set size, context size (8k→32k moved but did not remove it), cache-ram and
kv-offload flags, and concurrency. The owner's server-side monitor confirmed it was **isolated, not a
crash** (249/250 reranks succeeded; the harness's fail-fast turned a transient timeout into an apparent
"wedge"). The behaviour was intermittent and config-sensitive in ways not cleanly modellable, and it does
**not** match a single filed upstream issue (the Qwen3 rerank issues describe *wrong/no output*, not a
stall) — so it is reported as an **empirical, runtime-specific instability**, not attributed to a
specific bug number.

**Switching the same Qwen3-Reranker model from llama.cpp to the MLX runtime eliminated it**: all three
MLX reranker arms completed the full 54-query bench with zero stalls. The fix is the **runtime**, not the
model. (The community seq-cls + ONNX path — `tomaarsen/Qwen3-Reranker-0.6B-seq-cls`,
`onnx-community/bge-reranker-v2-m3-ONNX` — would similarly sidestep the broken llama.cpp causal-rerank
path while keeping data on-device; see Options.)

## Verdict

1. **No retrieval model opens a clean floor on this data** — not the best embedder (J≤0.59), not the best
   reranker (J=0.785). Confirmed across 3 embedder runtimes × 3 quant levels × 3 sizes + cloud, and across
   3 reranker runtimes + cloud.
2. **The reranker is the strongest lever** (+0.19 over the best embedder) and should be the score signal
   FRE-655 calibrates on — but as a **soft, probabilistic operating point** (e.g. ~88% recall @ ~9% FP),
   never a hard cutoff.
3. **The clean separation has to come from structure**, not a score (see below).

### Why there is no clean floor — and when there would be

This is **not a "dirty data" artifact; it is structural.** A clean floor needs the true match to outscore
*every* non-match with a gap. What kills the gap is **topical density** — near-neighbours, items on the
*same topic* as the query but not the actual answer (a "vision" query pulls mantis-shrimp, X-ray-vision,
Rayleigh-scattering, sensory-substitution; they all score high). The true answer and its topical cousins
sit on top of each other. Counter-intuitively, **truly disparate data is *easier*** (no near-neighbours);
a **dense, topically-clustered corpus** — which a rich personal memory is — is the hard case.

A clean floor *does* appear with structured data — but **structure is what provides it, not better
embeddings.** Taxonomy, entity types, metadata, graph relationships, recency windows turn relevance into
a **deterministic filter** (`type = Person AND topic = X AND after = Y`) instead of a fuzzy threshold on a
continuous score. Structure replaces the floor with hard predicates; it does not make the similarity
scores separate any better. **This is exactly why Seshat leans on the knowledge graph** (ADR-0097/0098
taxonomy, entity types, relationships, recency) with the reranker as a soft refinement on top, and why
ADR-0100's relevance-bounded recall must expect an overlapping distribution.

## Architecture options (production reranker + embedder)

Reranking sends the query + candidate **memory content** to whatever runs it, so data sovereignty matters.

| Option | Role | Data path | Quality / latency | Notes |
|---|---|---|---|---|
| **MLX 8B mxfp8 reranker (laptop)** | reranker | on-device (private) | **J=0.785**, ~4.3s | Best quality + reliable; depends on the laptop being up. |
| **MLX 4B mxfp8 reranker (laptop)** | reranker | on-device | J=0.747, ~1.7s | Faster, slightly lower J; good middle. |
| **Voyage rerank-2.5 (cloud)** | reranker | US cloud (egress) | J=0.73, ~0.24s | Fastest + stateless; weakest sovereignty. |
| **ONNX reranker on the VPS** | reranker | on-VPS (private) | TBD; sub-second CPU | `bge-reranker-v2-m3-ONNX` or Qwen3-seq-cls → ONNX; no laptop, no llama.cpp. |
| **OVH embedder** (Qwen3-Emb-8B €0.10/M; BGE-m3 €0.01/M) | embedder only | OVH EU API (GDPR) | n/a to floor | No reranker on OVH; doesn't change the floor. Embedder ops only. |

**Data-sovereignty ranking** (best→worst for keeping memory content off public networks):
MLX-laptop ≈ ONNX-on-VPS ＞ OVH (EU, in-provider, GDPR) ＞ Voyage (US, external).

**Recommendation.** Embedder: **stay on the prod 0.6B** (FRE-694 — no re-embed justified; runtime/quant
robust). Reranker: adopt a cross-encoder as the soft FRE-655 signal; the **MLX 8B mxfp8** is the quality
ceiling and runs reliably on-device, with the tradeoff that recall would then depend on the laptop's
availability — so pair it with a failover (Voyage or an **ONNX-on-VPS** reranker, which is the most
private always-on option). The prod-reranker switch is a deploy-class change → its own ticket, not this
eval.

## Follow-up tickets (filed Needs-Approval)

- **Adopt a production reranker** (MLX-8B-on-laptop with a failover, *or* ONNX-on-VPS) — feeds FRE-655.
- **Bench an ONNX-on-VPS reranker** (`bge-reranker-v2-m3-ONNX` + `Qwen3-Reranker-0.6B-seq-cls`→ONNX) on
  this same separation harness — the always-on private path.
- **(Resolved/deferred)** the local llama.cpp Qwen3-Reranker instability — superseded by the MLX runtime;
  no further llama.cpp debugging warranted.

## Limitations

- Offline geometry/relevance test, not a Neo4j-HNSW index-fidelity test (embedder parity-validated for 0.6B).
- n = 54 (≈57 positives + 54 negatives) — extrema are outlier-sensitive, hence robust p5/p95 alongside J.
- Reranker score scales are arbitrary and not comparable across arms; the floor is per-arm, never
  transferable (FRE-655 calibrates on the chosen reranker's own distribution).
- The 8B-mxfp8 > 8B-bf16 reranker gap (0.785 vs 0.726) is within plausible n=54 noise; not over-read.

## Artifacts

- Harness: `scripts/eval/fre435_memory_recall/separation_benchmark.py` (+ `separation_report.py`).
- Run: `separation_benchmark.py --arm <rr-…|mlx-emb-…|voyage…> [--candidates 15]`.
- Tests: `tests/evaluation/test_fre695_reranker.py` (pure parse/metric units).
- Raw run JSON: `telemetry/evaluation/fre435-memory-recall/separation-*.json` (gitignored).
