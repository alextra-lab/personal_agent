# FRE-694 — Does a better embedder open a clean floor? No. (0.6B-f16 · 8B-f16 · Voyage)

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
| 8B | Qwen3-Embedding-8B | 4096 | `slm.frenchforet.com:8505` |
| Voyage | voyage-4-large (cloud SOTA) | 2048 | Voyage API |

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

## Results — separation at native dimension

| Arm | pos med | neg med | neg max | pos p5 | neg p95 | best Youden's J | clean floor? |
|-----|--------:|--------:|--------:|-------:|--------:|----------------:|:------------:|
| 0.6B @1024 | 0.750 | 0.700 | 0.779 | 0.676 | 0.755 | **0.42** (R 0.49 / FP 0.07 @0.75) | **No** |
| 8B @4096 | 0.738 | 0.662 | 0.775 | 0.649 | 0.733 | **0.53** (R 0.72 / FP 0.19 @0.70) | **No** |
| Voyage @2048 | 0.749 | 0.651 | 0.804 | 0.647 | 0.751 | **0.59** (R 0.74 / FP 0.15 @0.70) | **No** |

- **Recall@5 saturates** at 0.98–1.00 for every arm and dim — recall hides the problem; separation is
  the metric.
- The **dimension sweep** barely moves separation (e.g. 8B Youden's J ≈ 0.53 from 512→4096): more
  dimensions saturate, they do not open a floor.

## Verdict

**No embedder opens a clean floor.** On every arm at every dimension the positive and negative clouds
overlap — even at the robust percentiles (pos-p5 < neg-p95 throughout), and even for cloud SOTA
(Voyage's best Youden's J is 0.59: keep 74 % of true matches while admitting 15 % of distractors —
far from a clean cutoff). The hardest distractors always outscore the easiest true matches.

**But size genuinely helps — the Q4 confound is cleared.** At f16, a bigger embedder measurably
improves separation (best Youden's J 0.42 → 0.53 → 0.59 across 0.6B → 8B → Voyage), mostly by pushing
the *negative* median down (0.700 → 0.662 → 0.651). So FRE-670's "4B doesn't help separation" was
indeed a precision artifact; at equal precision, the embedder is a **partial lever** — it narrows the
overlap but does not close it.

## Recommendation (feeds FRE-655 floor calibration + the re-embed decision)

1. **Do not re-embed for separation.** No embedder — local or cloud SOTA — yields a clean cosine
   cutoff, and recall already saturates at the 0.6B production embedder. A one-way-door re-embed
   (8B → 4096-dim storage; Voyage → an external dependency) buys a J of 0.53–0.59, still far from a
   usable floor. The cost is not justified by separation alone.
2. **The clean-floor lever is downstream.** A bi-encoder cosine cannot separate these clouds; the
   **reranker** (cross-encoder, cross-attention) is where the floor must come from. FRE-655 should
   calibrate against an *overlapping* cosine distribution and lean on the reranker — not expect a clean
   cosine threshold.
3. **If a re-embed is ever done for other reasons** (recall, multilingual, longer context), 8B-f16 is
   the local-family ceiling and modestly improves separation; Voyage is marginally better still on the
   median but has a worse worst-case distractor (neg-max 0.804). Neither changes recommendation (1).

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
