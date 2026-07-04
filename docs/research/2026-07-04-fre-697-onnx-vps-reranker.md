# FRE-697 — Is an ONNX reranker on the VPS CPU a viable always-on private path?

**Date:** 2026-07-04 · **Ticket:** FRE-697 ("Memory Recall Quality"). Follow-on to FRE-670/694/695.
**Backing:** ADR-0100 (relevance-bounded recall — the floor FRE-655 calibrates) · ADR-0087 §D (recall
measurement).
**Substrate:** none — offline harness (no Neo4j, no live prod KG). Only the paraphrased, PII-free
`semantic_probe.yaml` is scored; all inference is in-process on the VPS CPU (nothing leaves the box).

## The question

FRE-695 established the cross-encoder **reranker** as the strongest recall lever (best J=0.785) but showed
**no** reranker opens a *clean floor* on the FRE-670 probe, and that the local **llama.cpp**
Qwen3-Reranker (causal yes/no-logit path) **stalls** under sustained load — MLX fixes it, but only on the
laptop. For an **always-on, laptop-independent, fully-private** reranker, does an **ONNX cross-encoder on
the VPS CPU** reproduce that separation, and at what CPU latency?

Two arms, same 54-case probe / 49-note corpus / FRE-694-695 metrics (best Youden's J swept at observed
scores, overlap counts, robust p5/p95, clean-floor verdict) **plus CPU latency**:

1. **bge-reranker-v2-m3** — a strong multilingual cross-encoder with a ready **INT8** CPU export.
2. **Qwen3-Reranker-0.6B *sequence-classification*** — the same family we benched, on the **seq-cls**
   head + ONNX, testing whether that path preserves separation while **sidestepping the llama.cpp
   causal-rerank path** that stalled.

## Method

Torch-free (owner-approved): `onnxruntime` CPU inference + a `transformers` tokenizer only (numpy; no
torch). The `onnxruntime` session is thread-bounded (`intra_op=4`/`inter_op=1` of 8 cores) to leave the
live gateway headroom on the shared VPS; one arm per process. Each query reranks the production embedder's
**top-15** shortlist (∪ its expected notes) — production reranks the embedder shortlist, not the whole
corpus. Relevance = `sigmoid(logit)` (monotone → verdict-invariant).

**Instrument verification (FRE-694/695 discipline, stronger than a 2-doc pair):** before any aggregate is
trusted, each arm scores the model card's own 4-document "Red Planet" example and must rank **Mars #1**
over three near-topic distractors — a combined polarity + prompt-template + tokenizer-wiring gate. All
three arms passed. The Qwen3 seq-cls arm replicates the model-card prompt template exactly
(`<|im_start|>system … <Instruct>: … <Query>: …` / `<Document>: … <think>…`), kept on the model's
**native** web-search instruction (its trained distribution).

**Provenance / reproducibility:** HF **revisions are pinned commit shas**; each run records the source and
(where quantized) int8 **sha256**, thread config, and `max_length`. Raw run JSON is gitignored; only these
curated aggregates are committed.

**Arm sourcing (a torch-free constraint that shaped the result).** bge ships a ready static-INT8 export
(`onnx-community/bge-reranker-v2-m3-ONNX@6f5ff65`, `model_int8.onnx`). The **only** seq-cls Qwen3-Reranker
ONNX export (`shawnw3i/Qwen3-Reranker-0.6B-seq-cls-ONNX@e5d273d`, `Qwen3ForSequenceClassification`) ships
**fp16** — so the seq-cls arm ran at **fp16 as-published**, and the INT8 seq-cls arm was produced
**locally**: a torch-free fp16→fp32 graph cast (initializers + `Constant`/embedded tensors + `Cast` ops +
value-info) followed by `onnxruntime` dynamic INT8 quantization. (The `onnx-community` Qwen3-Reranker ONNX
is `Qwen3ForCausalLM` — the causal path we are avoiding — so it was rejected. The canonical
`tomaarsen/…-seq-cls` ships no ONNX; a canonical self-export needs torch + a ~6-8 GB export on the live
VPS, deferred per owner for live-host safety.)

## Results

All three arms completed the full 54-query bench (`completed_queries=54`, `partial=false`) with **zero
stalls** — the llama.cpp instability FRE-695 hit does not appear on the ONNX runtime (as predicted).
57 positive / 54 negative samples per arm.

| Reranker (VPS CPU, ONNX) | precision | best J | R / FP @ bestJ | clean floor? | latency / query (15 docs) |
|---|---|---:|---:|:---:|---:|
| bge-reranker-v2-m3 | int8 (pre-exported, static) | **0.503** | 0.61 / 0.11 | No (OVERLAP) | warm-med **2.39 s** (p95 2.91, cold 3.52) |
| Qwen3-Reranker-0.6B seq-cls | **fp16** (as published) | **0.680** | 0.75 / 0.07 | No (OVERLAP) | warm-med **7.83 s** (p95 9.14, cold 9.38) |
| Qwen3-Reranker-0.6B seq-cls | int8-dynamic (self-quantized) | 0.395 | 0.56 / 0.17 | No (OVERLAP) | warm-med 6.66 s (p95 7.48, cold 7.93) |

For context, the FRE-695 baselines on the *same probe*: embedder ceiling **J ≤ 0.59**; Qwen3-Reranker-0.6B
llama.cpp f16 ~0.65 (stalled), 4B 0.71 / MLX 0.747, 8B MLX-mxfp8 **0.785** (best); Voyage rerank-2.5 0.73,
lite 0.66.

### Reading the numbers

- **The seq-cls + ONNX path preserves the family's separation.** Qwen3-Reranker-0.6B seq-cls **fp16**
  lands at **J=0.680** — squarely in the FRE-695 reranker band, matching the 0.6B llama.cpp arm (~0.65)
  and Voyage-lite (0.66), and **materially above the embedder ceiling (0.59)**. So the seq-cls head on the
  ONNX runtime reproduces the cross-encoder lever *without* the llama.cpp causal-rerank stall. **This is
  the arm-B headline result.**
- **No clean floor here either** — every arm is OVERLAP / robust-overlap, exactly as FRE-694/695 found.
  This is structural (topical density), not a runtime artifact; nothing about ONNX changes it.
- **Dynamic INT8 wrecks the Qwen seq-cls arm.** The quant-equivalence gate **fails hard**: fp16
  J=0.680 → int8-dynamic **J=0.395**, a **−0.285** collapse (threshold 0.03) that drops it *below the
  embedder ceiling* — and it barely helped latency (7.83 s → 6.66 s, ~15%). The instrument gate still
  passes (Mars ranks #1, 0.997 vs 0.787), so this is genuine **quantization damage**, not a wiring bug:
  weight-only dynamic INT8 is a bad trade for this model. **Do not use dynamic-INT8 on the Qwen seq-cls
  reranker.** (bge's *static* INT8 export holds up far better — precision method matters more than the
  bit-width label.)
- **bge-reranker-v2-m3 INT8 under-separates on this probe** (J=0.503, ~embedder-level, below the 0.65-0.785
  reranker band) but is **3× faster** (2.39 s vs 7.83 s). A speed/quality corner, not a free win.

### Latency is the viability blocker

Functionally the VPS-ONNX path is sound (correct separation, zero stalls, fully private). **Latency is the
problem:** 2.4-7.8 s to rerank a *single* 15-candidate query on the shared VPS CPU (fp16 seq-cls runs as
fp32 internally — AVX2 has no native fp16 kernels — hence ~7.8 s). Compare FRE-695: MLX-laptop 1.7-4.3 s
(GPU) and Voyage **0.24 s** (cloud). At interactive recall latency this is 10-30× the cloud and 2-5× the
laptop.

## Verdict

1. **Yes — an ONNX seq-cls cross-encoder on the VPS CPU reproduces the FRE-695 reranker separation**
   (Qwen3-0.6B seq-cls fp16, **J=0.680**, in-band) and **sidesteps the llama.cpp causal-rerank stall**
   (54/54, zero stalls). The always-on private path is *functionally* viable.
2. **It is not viable as a primary interactive reranker** on this hardware: **2.4-7.8 s/query** on the
   shared VPS CPU is far slower than the MLX-laptop or Voyage. It is a credible **always-on private
   failover** (better than no reranker when the laptop is down; fully on-box), not the primary.
3. **Precision method dominates.** Static INT8 (bge) preserves usefulness; **dynamic INT8 on the Qwen
   seq-cls model destroys separation** (0.680→0.395) for a ~15% speedup — avoid it. The best VPS-ONNX
   quality point is **Qwen3-0.6B seq-cls fp16 (J=0.680)**; the fastest is **bge-int8 (2.39 s, J=0.503)**.

### Recommendation (feeds the production-reranker adoption follow-up + FRE-655)

- **Primary reranker: keep the MLX-laptop path** (FRE-695: 4B/8B mxfp8, J=0.747-0.785, reliable) as the
  quality/latency leader.
- **Always-on private failover: Qwen3-0.6B seq-cls ONNX at fp16** on the VPS (J=0.680, fully on-box), used
  when the laptop is offline — accepting the multi-second latency as a degraded-mode cost. **Not**
  dynamic-INT8 (it collapses separation).
- **If VPS latency must come down** (a real blocker for a primary), the levers are a smaller candidate set,
  request batching, ONNX graph optimization/threads, or **static/QAT INT8** (not dynamic) — a scoped
  follow-up, not this eval.
- **FRE-655 floor calibration** should continue to expect an **overlapping** score distribution and a
  **soft** operating point (no clean floor on any VPS-ONNX arm), consistent with FRE-694/695.

## Follow-up tickets (filed Needs-Approval)

- **VPS-ONNX reranker latency reduction** — static/QAT INT8 (dynamic-INT8 is ruled out here), candidate-set
  / batching / thread tuning, to test whether the on-box path can reach interactive latency.
- **(Optional) canonical seq-cls ONNX export** — a torch self-export of `tomaarsen/Qwen3-Reranker-0.6B-seq-cls`
  to remove the community-export dependency, if the VPS-ONNX path is adopted.

## Limitations

- Offline geometry/relevance test, not a Neo4j-HNSW index-fidelity test (as FRE-694/695).
- n = 54 (57 pos / 54 neg) — extrema are outlier-sensitive; robust p5/p95 reported alongside J.
- The seq-cls **fp16** arm ran the community export as-published; the INT8 seq-cls arm is a **local**
  torch-free fp16→fp32→dynamic-INT8 build. Both are gated by the instrument verification (Mars #1) and the
  quant-equivalence comparison; the INT8 collapse is corroborated by the still-passing instrument gate
  (i.e. it is quantization, not a broken cast).
- Reranker score scales are arbitrary and not comparable across arms; the floor is per-arm (FRE-655
  calibrates on the chosen reranker's own distribution).
- Latency is on the **shared** live VPS (thread-capped to 4 cores); absolute numbers would shift on a
  dedicated box, but the qualitative multi-second verdict holds.

## Artifacts

- Harness: `scripts/eval/fre435_memory_recall/separation_benchmark.py` (ONNX arms) +
  `onnx_reranker.py` (in-process scorer + fp16→fp32 cast) + `separation_report.py` (metrics).
- Run: `PYTHONPATH=. uv run python scripts/eval/fre435_memory_recall/separation_benchmark.py --arm
  <onnx-bge-int8|onnx-qwen-seqcls-fp16|onnx-qwen-seqcls-int8>` (needs `uv sync --extra onnx-eval`).
- Tests: `tests/evaluation/test_fre697_onnx_reranker.py` (pure format/logit/extraction/dispatch units).
- Raw run JSON: `telemetry/evaluation/fre435-memory-recall/separation-onnx-*.json` (gitignored).
