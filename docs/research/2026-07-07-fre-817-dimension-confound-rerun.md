# FRE-817 corpus A/B — dimension-confound correction (re-run)

> **Date:** 2026-07-07 · **Corrects:** FRE-817 (ADR-0112 AC-4 embedder A/B) · **Backs:** FRE-826 (dimension fix), FRE-821 (embedder adoption)

## Why this re-run

The original FRE-817 A/B concluded "OVH-managed Qwen3-Embedding-8B beats the deployed 0.6B" and drove the
FRE-821 adoption. Two confounds were found afterward (owner catch):

1. **Dimension mismatch.** The 0.6B arm scored at **1024** (its native); the 8B arm scored at OVH's
   **native 4096** — the 8B's `_embed_ovh_batch` sent no `dimensions` param. Not like-for-like, and
   FRE-694's MRL sweep found the 8B's sweet spot is the *middle* (~1024), with native equal-or-worse.
2. **Precision/hardware.** FRE-694's dimension sweep ran the 8B on **local hardware** (a documented Q4
   precision confound — "precision, not size, suppressed it"). OVH serves the 8B on **full-precision
   cloud GPUs**, a different regime — so FRE-694's local dimension optimum could not simply be imported;
   it had to be measured on the real OVH endpoint.

## Method

Reused the FRE-817 harness's tested pieces (`load_probe_set`, `_build_corpus`, `score_arm`,
`ndcg_at_k`) over the same fixed corpus (`semantic_probe.yaml`, 54 queries / 49 notes, Qwen query
prefix). Varied **only** the embedding dimension: 0.6B local truncated to {512, 1024}; 8B via OVH with
the OpenAI `dimensions` param (verified honored server-side) at {512, 1024, 2048, 4096}. Vectors
L2-normalized in `score_arm`.

## Results (nDCG@5)

| arm | dim | nDCG@1 | nDCG@5 |
|-----|----:|-------:|-------:|
| 0.6b-local | 512 | 0.8636 | 0.9176 |
| 0.6b-local | 1024 | 0.8864 | 0.9303 |
| 8b-ovh | 512 | 0.8636 | 0.9377 |
| **8b-ovh** | **1024** | **0.9091** | **0.9585 (peak)** |
| 8b-ovh | 2048 | 0.9091 | 0.9552 |
| 8b-ovh | 4096 | 0.9091 | 0.9566 |

**Fair (matched-dimension) comparison:**
- @1024: 0.6b 0.9303 vs 8b 0.9585 → **Δ +0.0281, 8b wins**
- @512: 0.6b 0.9176 vs 8b 0.9377 → Δ +0.0201, 8b wins
- Original confounded: 0.6b@1024 0.9303 vs 8b@4096 0.9566 → Δ +0.0263

## Findings

1. **8B peaks at 1024, not native 4096** — 0.9585 (1024) > 0.9566 (4096) > 0.9552 (2048). FRE-694's
   middle-dim sweet spot transfers to OVH's full-precision cloud serving. Deploy dimension = **1024**;
   native 4096 costs 4× the vector storage for equal-or-worse recall.
2. **The 8B win survives the fair test — marginally larger.** At matched 1024 the edge is +0.0281 vs
   the confounded +0.0263. The 8B wins at every matched dimension and on nDCG@1 too. The FRE-821
   adoption decision is **vindicated** by the corrected comparison.
3. **Honest caveat:** the margin is modest (~0.028 nDCG@5 at n=54 — near the noise floor FRE-694
   flagged). The 8B is a *consistent but small* improvement, not a landslide.

## Consequences

- **FRE-826** (filed): fix the managed-embed path to request `dimensions=embedding_dimensions` +
  renormalize; operating dimension **1024**, not 4096. Blocks the re-embed.
- **FRE-821** re-embed (master-executed, one-way): re-embed the ~6,126 KG nodes at **1024** after 826
  lands, then prove AC-5/AC-6 live.
