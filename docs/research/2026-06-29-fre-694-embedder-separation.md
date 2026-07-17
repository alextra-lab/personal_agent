# FRE-694 — Does a better embedder open a clean floor? No. (0.6B · 4B · 8B f16 · Voyage)

**Date:** 2026-06-29 · **Ticket:** FRE-694 ("Memory Recall Quality"). Continuation of FRE-670/656.
**Backing:** ADR-0087 §D (recall measurement) · ADR-0100 (the floor this gates) · ADR-0099 (config).
**Substrate:** none — the benchmark is pure offline cosine geometry (no Neo4j, no live prod KG).

## The question

The FRE-670 probe proved semantic-over-lexical (vector ≫ BM25). It did **not** show a clean *floor*:
positive (true-match) and negative (no-record) cosine clouds overlap, so no single cosine cutoff
separates "found it" from "nothing here." FRE-670's 4B arm ran at **Q4**, which perturbs the fine
cosine geometry a floor depends on — so "size doesn't help separation" was precision-confounded.
FRE-694 re-tests at **f16 across the board** so size is the only variable:

> Can a higher-quality embedder open a clean floor, or do even the best leave the clouds overlapping?
> If yes → the embedder is the lever (re-embed justified). If no → the lever is downstream (reranker).

## Method

Three arms, all f16, over the 54-case FRE-670 probe (`semantic_probe.yaml`):

| Arm | Model | Native dim | Endpoint |
|-----|-------|-----------:|----------|
| 0.6B | Qwen3-Embedding-0.6B (prod) | 1024 | VPS `:8503` |
| 4B-f16 | Qwen3-Embedding-4B | 2560 | `slm.example.com` |
| 8B | Qwen3-Embedding-8B | 4096 | `slm.example.com:8505` |
| Voyage | voyage-4-large (cloud SOTA) | 2048 | Voyage API |

(The 4B and 8B arms share the Access-gated slm endpoint — run separately as the served model was
swapped; the 8B numbers were captured before the 4B-f16 swap.)

Cosines computed **offline** (embed → first-N truncate → L2 renormalize → cosine), reported in Neo4j
score space (`(cosine+1)/2`) so they are comparable to the production path and the FRE-655 floor.
Entity/query text mirrors production exactly (`"{name}: {description}"` document mode; query mode).
Matryoshka dimension sweep 256/512/1024/native (client-side truncation; validated equivalent to
Voyage's server-side `output_dimension`, cosine ~0.999). Metric = **separation**: per-expected-entity
positives vs each query's strongest non-match negative; overlap counts; robust p5/p95; floor sweep.

### Instrument validation (HARD GATE — passed)

The offline harness was validated against the FRE-670 Neo4j `calibrate` path before any cross-arm
number was trusted: 0.6B@1024 offline medians vs calibrate — pos 0.766 vs 0.776, neg 0.700 vs 0.706,
neg-max 0.779 vs 0.792 (**all Δ ≤ 0.012 < 0.02**). The harness computes the same geometry as production.
*(Caveat: this is an embedding-geometry test, not a Neo4j-HNSW index-fidelity test.)*

## Results — separation at native dimension (full f16 size ladder)

| Arm | native | pos med | neg med | neg max | pos p5 | neg p95 | best Youden's J | clean floor? |
|-----|-------:|--------:|--------:|--------:|-------:|--------:|----------------:|:------------:|
| 0.6B | 1024 | 0.750 | 0.700 | 0.779 | 0.676 | 0.755 | **0.42** (R 0.49 / FP 0.07 @0.75) | **No** |
| 4B-f16 | 2560 | 0.746 | 0.680 | 0.811 | 0.662 | 0.738 | **0.53** (best 0.59 @1024) | **No** |
| 8B | 4096 | 0.738 | 0.662 | 0.775 | 0.649 | 0.733 | **0.53** (best 0.55 @1024) | **No** |
| Voyage | 2048 | 0.749 | 0.651 | 0.804 | 0.647 | 0.751 | **0.59** (best 0.64 @512) | **No** |

- **Recall@5 saturates** at 0.98–1.00 for every arm and dim — recall hides the problem; separation is
  the metric. Recall@1 *is* monotonic with size (0.739 → 0.761 → 0.773 → 0.807).

### Dimension sweep — best dimension is the *middle*, not native (AC2)

Best Youden's J (recall − false-positive at the optimal floor) by dimension:

| Arm | 256 | 512 | 1024 | 2048 | 2560 | 4096 | best dim |
|-----|----:|----:|-----:|-----:|-----:|-----:|:--------:|
| 0.6B | 0.354 | **0.429** | 0.417 | — | — | — | **512** |
| 4B-f16 | 0.478 | 0.519 | **0.594** | 0.512 | 0.532 | — | **1024** |
| 8B | 0.469 | 0.461 | **0.550** | 0.534 | — | 0.534 | **1024** |
| Voyage | 0.571 | **0.642** | 0.605 | 0.589 | — | — | **512** |

More dimensions do **not** improve separation — every arm peaks at a *middle* dimension (512–1024) and
**native is equal-or-worse** (4B-f16 1024 = 0.594 > native 2560 = 0.532; 8B 1024 = 0.550 > native 4096
= 0.534; Voyage 512 = 0.642 > native 2048 = 0.589). The extra dimensions add noise that nudges the
hardest distractors up, not signal. **Practical:** were a re-embed ever done, truncate the MRL
embedding to **~1024 dims** (bigger models) / **~512** (0.6B/Voyage) — best separation *and* recall at
2–4× less vector storage than native. *(Caveat: at n=54 the J gap between neighbouring dims is within
noise; the robust read is "saturates by ~512–1024, native doesn't help," not a precise optimum.)*

## Verdict

**No embedder opens a clean floor.** On every arm at every dimension the positive and negative clouds
overlap — even at the robust percentiles (pos-p5 < neg-p95 throughout), and even for cloud SOTA
(Voyage's best Youden's J is 0.64: keep ~80 % of true matches while admitting ~16 % of distractors —
far from a clean cutoff). The hardest distractors always outscore the easiest true matches.

**Size helps with diminishing returns — it plateaus after 4B — and the Q4 confound is now directly
confirmed.** At native dim the best Youden's J runs 0.42 (0.6B) → 0.53 (4B) → 0.53 (8B) → 0.59
(Voyage): the real gain is **0.6B → 4B (+0.11)**, then **4B and 8B are statistically indistinguishable**
(native 0.532 vs 0.534; the ~0.04 edge 4B shows at the 1024 truncation is within the n=54 noise — not
evidence a smaller model separates better). So within the local Qwen family the separation ceiling is
reached by **~4B; the 8B buys nothing over the 4B.** Voyage (a different architecture) is marginally
highest. Crucially, the *same* 4B model scored ≈ 0.6B in FRE-670 **at Q4** but reaches J ≈ 0.53 at
**f16** — **precision, not size, was suppressing it**, exactly the confound this ticket existed to
clear. The embedder is a **partial lever** (it narrows the overlap, mostly by lowering negative
similarity) but at no size or precision does it close it.

## Recommendation (feeds FRE-655 floor calibration + the re-embed decision)

1. **Do not re-embed for separation.** No embedder — local or cloud SOTA — yields a clean cosine
   cutoff, and recall already saturates at the 0.6B production embedder. The local ceiling (4B-f16 @
   ~1024) reaches only J = 0.59, and even cloud SOTA only 0.64 — still far from a usable floor. A
   one-way-door re-embed is not justified by separation. *(If ever re-embedded for **other** reasons,
   the local separation ceiling is reached by ~4B-f16 — the 8B is statistically no better, so it is not
   worth its larger footprint — and truncating to ~1024 dims loses nothing.)*
2. **The clean-floor lever is downstream.** A bi-encoder cosine cannot separate these clouds; the
   **reranker** (cross-encoder, cross-attention) is where the floor must come from. FRE-655 should
   calibrate against an *overlapping* cosine distribution and lean on the reranker — not expect a clean
   cosine threshold.
3. **If a re-embed is ever done for other reasons** (recall, multilingual, longer context), 8B-f16 is
   the local-family ceiling and modestly improves separation; Voyage is marginally better still on the
   median but has a worse worst-case distractor (neg-max 0.804). Neither changes recommendation (1).
   **Store the truncated MRL embedding at ~512 dims, not native** — the sweep shows 512 gives the best
   (or equal-best) separation and recall at 4–8× less vector storage; native dimensions add cost, not
   quality.

## Limitations (stated)

- Offline geometry test, not a Neo4j-HNSW index-fidelity test (parity-validated for 0.6B).
- n = 44 positives + 10 controls — extrema are outlier-sensitive, hence the robust p5/p95 verdict.
- Each arm runs in its **native** retrieval mode (Qwen instruction prefix vs Voyage `input_type`), so
  an arm difference blends model quality and provider prompt template — "best native mode per arm,"
  not instruction-controlled.

## Artifacts

- Harness: `scripts/eval/fre435_memory_recall/separation_benchmark.py` (+ `separation_report.py`).
- Run: `run_embedder_benchmark.sh <0.6b|8b> separation` · `separation_benchmark.py --arm voyage`.
- Parity gate: `run_embedder_benchmark.sh 0.6b separation --parity`.
- Config: `config/models.benchmark-8b.yaml` (8B f16); `models.benchmark-4b.yaml` retired (Q4-confounded).
- Raw run JSON: `telemetry/evaluation/fre435-memory-recall/separation-*.json` (gitignored).
