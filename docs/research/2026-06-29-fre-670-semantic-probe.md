# FRE-670 — A vocabulary-divergent recall probe that finally separates semantic from lexical

**Date:** 2026-06-29 · **Ticket:** FRE-670 (project "Memory Recall Quality", parent FRE-435)
**Backing ADR:** ADR-0087 §D2 (measurement-first recall gate) · **Builds on:** FRE-489, FRE-655, FRE-656
**Substrate:** isolated FRE-375 test stack only (Neo4j :7688) — never the live production KG.

## The problem this probe fixes

The FRE-489 gate (`bespoke_probe.yaml`) is **lexical masked as semantic**. Its queries are oblique
about entity *names*, but the entity *descriptions* were authored alongside the queries, so they
share ordinary content vocabulary. FRE-656 showed the consequence: a plain BM25 keyword search over
the stored text gets **recall@5 = 1.00**, *beating* the vector path (0.72), and the 0.6B and 4B
embedders score **identically** (0.722 = 0.722) — there is no semantic gap for a better embedder to
close. That probe cannot justify the vector + reranker apparatus or inform the embedder/re-embed
decision (FRE-656/671), which is why no production re-embed should proceed until a harder probe
exists.

## What FRE-670 builds

A 54-case split (`scripts/eval/fre435_memory_recall/semantic_probe.yaml`) whose queries describe their
targets in **imagery / paraphrase**, sharing near-zero surface vocabulary with the stored note text —
so keyword search falls over and the vector path must do real semantic matching. 44 positives across
12 corpus themes (35 imagery, 9 natural; 7 natural/imagery register-pairs; 13 compound multi-fact) +
10 abstention controls (4 over-recall traps). Owner-authored and prod-grounded; the committed file is
paraphrased per the FRE-489 PII discipline (no names, locations, or verbatim transcripts).

Two disciplines are enforced in CI (`tests/evaluation/test_fre670_semantic_probe.py`): **referential**
(a query never names its answer) and **vocabulary-divergent** (for every imagery positive, the
content-token Jaccard between query and note is < 0.15; observed median **0.03**, max **0.10**). The
guard is a floor, not a proof — note faithfulness (the anti-gaming protection) is held out-of-band by
the gitignored owner working file as the auditable provenance record.

## Method — three arms over the identical 54-note corpus

All three rank each query against the **same** co-resident 54-note corpus (so the comparison is
apples-to-apples, not a per-case-isolated vector path facing one note):

- **BM25 keyword** — `keyword_baseline.py` over name + description (the standing lexical-leakage guard).
- **0.6B vector** — Qwen3-Embedding-0.6B via `run_embedder_benchmark.sh 0.6b calibrate`.
- **4B vector** — Qwen3-Embedding-4B via `run_embedder_benchmark.sh 4b calibrate` (tunnelled endpoint).

Recall is fractional over the expected set (matching `metrics.recall_at_k`); control abstention is
measured at the calibrated cosine floor.

## Results

| Arm | recall@1 | **recall@5** | imagery @5 | natural @5 |
|-----|---------:|-------------:|-----------:|-----------:|
| BM25 keyword       | 0.432 | **0.659** | 0.600 | 0.889 |
| 0.6B vector        | 0.705 | **0.989** | 0.986 | 1.000 |
| 4B vector          | 0.761 | **1.000** | 1.000 | 1.000 |

**Sanity check:** the same BM25 instrument on the old FRE-489 probe still reports recall@5 = 1.00 —
reproducing the FRE-656 finding exactly, so the drop to 0.659 here is the probe, not the tool.

### AC2 — the split is genuinely semantic ✅

BM25 recall@5 (0.659) lands **materially below** the vector arms (0.989 / 1.000) — a **+0.33** gap,
widening to **+0.39** on the imagery cases where divergence is real. This is the mirror image of
FRE-489 (where BM25 *won*, 1.00 vs 0.72). The split requires semantic matching.

### The probe now distinguishes embedders (the FRE-656 re-embed input)

Where FRE-489 scored 0.6B and 4B **identically**, this probe resolves a real gap:

- recall@1: 0.6B **0.705** → 4B **0.761** (+0.056);
- imagery recall@5: 0.6B 0.986 → 4B **1.000** (4B recovers the one case 0.6B missed);
- register robustness (recall@1, natural − imagery): 0.6B **+0.092** → 4B **+0.021** — the larger
  embedder degrades *less* on oblique phrasing.

The gains are small but consistent, and — crucially — **visible**, which the prior gate could not make
them. This is the evidence the embedder/re-embed decision (FRE-656, gating the FRE-671 hosting call)
needs. Whether a +0.05–0.06 recall@1 gain and better oblique robustness justify a one-way-door KG
re-embed at 2560 dims is the owner's call; this probe is the scoreboard, not the verdict.

### Abstention / floor

Both vector arms abstain on **9 of 10** controls at the calibrated floor (0.75); the single
non-abstention is an over-recall trap whose tempting near-neighbour is a co-resident positive. The
positive/negative cosine distributions overlap (0.6B: median pos 0.776 vs neg 0.706), so the floor
trades recall for precision realistically (sweep in the gitignored run JSON). BM25 has no floor; over
the same corpus it returns *some* lexical neighbour for 7 of 10 controls — keyword over-recall the
floor-bounded vector path resists.

## Limitations (stated, not hidden)

- BM25 ranks the clean 54-note corpus with no distractor noise — the *easier* condition — so if it
  still loses, the semantic win is conservative.
- recall@5 saturates near 1.0 for both embedders; recall@1 and the register delta are the
  discriminating cut-offs at this corpus size.
- Control abstention depends on the floor; a production floor is FRE-655's calibrated output.

## Artifacts

- Probe: `scripts/eval/fre435_memory_recall/semantic_probe.yaml`
- BM25 arm: `scripts/eval/fre435_memory_recall/keyword_baseline.py --probe …`
- Vector arms: `scripts/eval/fre435_memory_recall/run_embedder_benchmark.sh <0.6b|4b> calibrate --probe-set …`
- Pure report helpers: `scripts/eval/fre435_memory_recall/semantic_report.py`
- Raw run JSON: `telemetry/evaluation/fre435-memory-recall/calibrate-*.json` (gitignored; aggregates above)
