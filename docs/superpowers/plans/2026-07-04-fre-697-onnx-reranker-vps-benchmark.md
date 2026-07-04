# FRE-697 — Benchmark an ONNX reranker on the VPS (always-on private path)

**Date:** 2026-07-04 · **Ticket:** FRE-697 (Approved, Tier-1:Opus, project "Memory Recall Quality").
**Backing:** ADR-0100 (relevance-bounded recall — the floor this measurement feeds via FRE-655).
Follows FRE-670 (probe) → FRE-694 (embedder ceiling, no clean floor, J≤0.59) → FRE-695 (reranker is the
lever, best J=0.785, still no clean floor; named this ticket as the "always-on private path" follow-up).

## The question

FRE-695 established the reranker is the strongest recall lever but no reranker opens a *clean floor*, and
that the local **llama.cpp** Qwen3-Reranker (causal yes/no-logit path) stalls under sustained load while
MLX fixes it *on the laptop*. For an **always-on, fully-private** reranker that does **not** depend on the
laptop being online, does an **ONNX cross-encoder on the VPS CPU** reproduce the FRE-695 reranker
separation, and at what CPU latency — i.e. is the VPS-ONNX path a viable production reranker?

Two arms, same FRE-670 probe + FRE-695 metrics (best Youden's J, overlap counts, robust p5/p95,
clean-floor verdict) **plus CPU latency**:

1. **bge-reranker-v2-m3 (ONNX INT8)** — strong multilingual cross-encoder, ready INT8 CPU export.
2. **Qwen3-Reranker-0.6B *sequence-classification* (ONNX)** — same model family we benched, on the
   **seq-cls** head + ONNX runtime, confirming that path preserves separation while **sidestepping the
   llama.cpp causal-rerank path that stalled**.

## Discovery (done)

- Harness reused verbatim: `scripts/eval/fre435_memory_recall/separation_benchmark.py` +
  `separation_report.py` (`summarize_separation`, `best_separation_at_observed`, `percentile`) +
  `probes.load_probe_set` + `semantic_probe.yaml` (54 cases / 49-note corpus) + `_embedder_shortlist`
  (production top-15 candidate set via the VPS :8503 embedder — no substrate, no laptop).
- VPS is feasible: AVX2+F16C (onnxruntime CPU OK), 122 GB disk free, ~9 GB RAM free, HF reachable.
- **Model sources (torch-free path):**
  - Arm A: `onnx-community/bge-reranker-v2-m3-ONNX` → `onnx/model_int8.onnx` (+ `tokenizer.json`,
    `config.json`). `XLMRobertaForSequenceClassification`, 1 logit. Ready INT8.
  - Arm B: `shawnw3i/Qwen3-Reranker-0.6B-seq-cls-ONNX` → `model.onnx`,
    `architectures=["Qwen3ForSequenceClassification"]` (the exact seq-cls head), fp32. We
    **self-quantize to INT8** with `onnxruntime.quantization.quantize_dynamic` (torch-free) as the
    CPU-viable primary; run fp32 as an optional cross-check.
    - *Rejected:* `onnx-community/Qwen3-Reranker-0.6B-ONNX` is `Qwen3ForCausalLM` — the causal path the
      ticket wants to avoid.
    - *Rejected (for now):* self-export from canonical `tomaarsen/Qwen3-Reranker-0.6B-seq-cls` via
      optimum — requires **torch** + a ~6–8 GB export on the **live** VPS. Avoided to protect live
      services; provenance instead validated by the instrument-sanity gate + agreement with the FRE-695
      Qwen3-Reranker family numbers. (Owner may override to force the canonical self-export.)
- **seq-cls prompt format is correctness-critical** (from the tomaarsen model card — the CrossEncoder
  input the ONNX graph expects):
  - query side: `"<|im_start|>system\nJudge whether the Document meets the requirements based on the
    Query and the Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n
    <|im_start|>user\n<Instruct>: {instruction}\n<Query>: {query}\n"` with the default web-search
    instruction.
  - document side: `"<Document>: {document}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"`
  - tokenized as an (A, B) **pair**; relevance = `sigmoid(logit)` (monotone → verdict-invariant).
  - bge side: plain `(query, document)` pair; relevance = `sigmoid(logit)`.

## Design

**Torch-free deps in a new optional group** so the prod gateway image is never touched:
`[project.optional-dependencies].onnx-eval = ["onnxruntime>=1.19", "transformers>=4.51"]` (transformers
for `AutoTokenizer` returning **numpy**; no torch import path). Run via `uv sync --extra onnx-eval`.

**New module `scripts/eval/fre435_memory_recall/onnx_reranker.py`** — a self-contained, testable
in-process cross-encoder scorer (no substrate, no `personal_agent` import):

- `@dataclass(frozen=True) OnnxArm` — `repo`, `revision` (**pinned commit sha**), `onnx_file`,
  `family` (`"bge"` | `"qwen-seqcls"`), `quantize` (bool), `instruction` (the seq-cls native
  instruction), `engine` label.
- `format_pair(family, query, document, instruction) -> tuple[str, str]` — returns the (A, B) strings
  per family (bge: plain; qwen-seqcls: the exact template above). **Pure — unit-tested.**
- `logit_to_score(logit: float) -> float` — numerically-stable sigmoid (monotone → verdict-invariant).
  **Pure — unit-tested.**
- `squeeze_logits(raw) -> list[float]` — normalize a `[batch]` / `[batch,1]` / `[batch,2]` output to one
  relevance logit per row (**codex #1**: provider output shape varies). For a 2-logit head use the
  positive class; assert the row count matches the input; fail-loud on any other shape. **Pure — unit-tested.**
- `class OnnxCrossEncoder`:
  - `load()` — `hf_hub_download(repo, onnx_file, revision=…)` + tokenizer at the same pinned revision;
    if `quantize`, produce an int8 sibling via `onnxruntime.quantization.quantize_dynamic`
    (**deterministic**, `weight_type=QInt8`) into a gitignored dir; record `sha256` of the fp32 source
    **and** the int8 artifact. **`onnxruntime.SessionOptions` bounds CPU**: `intra_op_num_threads=4`
    (of 8 cores — leave headroom for the live gateway), `inter_op_num_threads=1`,
    `graph_optimization_level=ORT_ENABLE_ALL`. Assert the ONNX graph's **input names**
    (`input_ids`/`attention_mask`[/`token_type_ids`]) and **output rank** at load (**codex #1**).
  - `score(query, documents, *, max_length) -> list[float]` — batch-tokenize the formatted pairs to
    **numpy** with explicit `max_length` + `truncation="only_second"` (never truncate the query side),
    one `session.run`, `squeeze_logits` → `logit_to_score` per row, scores in input order. Fail-loud on
    a row-count/shape mismatch (mirrors `_assert_vectors`).
- `verify_instrument(scorer) -> None` — **stronger than a 2-doc pair (codex #1/#5)**: score the model
  card's own 4-document example (the "Red Planet" set) and assert the Mars document ranks #1 with a
  material gap over the three near-topic distractors; `SystemExit` on failure. This is the polarity +
  template + tokenizer-wiring gate, run before any aggregate is trusted.

**Truncation safety:** the corpus is 49 one-line `"{name}: {description}"` notes (well under any model's
limit); `max_length` = 512 (bge) / 1024 (qwen) with query-side never truncated. `score()` asserts no
formatted document exceeds `max_length` after tokenization → if any expected note would truncate, STOP
(**codex #1**). Recorded in the run record.

**Shared extraction helper (codex #3 — avoid a third drift-prone variant):** extract the per-case
positive/negative selection currently inlined in `_run_reranker` (separation_benchmark.py L760–767) into
a module-level pure function `positives_negatives_for_case(expected, cand_names, scores) ->
tuple[list[float], float | None]`, unit-tested to reproduce the inline expression exactly, and call it
from **both** `_run_reranker` (a 3-line, behaviour-preserving substitution) and the new ONNX path. One
definition of "positive = per-expected-entity, negative = strongest non-expected" for every reranker arm.

**Wiring in `separation_benchmark.py` (mostly additive):**

- `ONNX_RERANKER_ARMS: dict[str, OnnxArm]` — `onnx-bge-int8`, `onnx-qwen-seqcls-int8` (primary) **and
  `onnx-qwen-seqcls-fp32` (required quant-equivalence control, codex #2)**.
- `async def _run_onnx_reranker(arm, args)` — same *reporting* structure as `_run_reranker`: run
  `verify_instrument` (STOP on fail), then per-query rerank of the `_embedder_shortlist` top-N (∪
  expected) using the shared extraction helper, time each `score()` for **CPU latency** (warm-median,
  warm-p95, cold-first), then `summarize_separation` + `best_separation_at_observed`, print the one-line
  verdict, write the same `separation-{arm}.json` shape (gitignored) **plus** a `provenance` block
  (repo, pinned revision, onnx_file, fp32+int8 sha256, quant config, thread config, max_length) and
  `completed_queries`/`partial`. Sync ONNX inference is CPU-bound; `_embedder_shortlist` stays async
  (awaited); the scorer runs via `asyncio.to_thread` with a **per-query wall-clock guard** (abort loudly
  if a single `score()` exceeds ~30 s — a runaway must not pin the live host, codex #4).
- `run()` dispatch: `if args.arm in ONNX_RERANKER_ARMS: return asyncio.run(_run_onnx_reranker(...))`
  placed **before** the existing `RERANKER_ARMS`/embedder branches; extend `--arm choices`. A unit test
  asserts arm-name routing still resolves every existing arm to its original path (dispatch-regression
  guard, codex #3).
- **One arm per process** (like `run_embedder_benchmark.sh`): each `--arm` invocation loads exactly one
  session and frees it at process exit — the "~0.6 GB resident, one at a time" RAM assumption holds by
  construction (codex #4). `--chunk-check` is **not applicable** to these arms (a cross-encoder scores
  each (query, doc) pair independently — no listwise normalization); documented, and confirmed once with
  a delta≈0 spot check.

**Additive, not a full refactor of `_run_reranker`:** the FRE-695 remote arms (laptop-GPU / cloud) cannot
be re-executed from this VPS session, so the ONNX path is added alongside rather than merged into the
HTTP loop. The one exception is the shared extraction helper above — a behaviour-preserving 3-line
substitution with a unit test — which *reduces* drift risk rather than adding it. (Not claiming
"zero-regression": `run()` dispatch and the `--arm` choice list do change; the routing-regression test
covers that, codex #3.)

## TDD steps

1. **`tests/evaluation/test_fre697_onnx_reranker.py`** (pure units, no model, no network — DI the
   session/tokenizer):
   - `format_pair("qwen-seqcls", …)` produces the exact system+instruct+query / document+suffix strings;
     `format_pair("bge", …)` returns the plain pair. → assert exact substrings + ordering.
   - `logit_to_score`: `0.0→0.5`, large `+`→`≈1`, large `-`→`≈0`, monotone, no overflow on `±1000`.
   - `squeeze_logits`: `[batch]`, `[batch,1]`, `[batch,2]`(→ positive class) all normalize to one score
     per row; a row-count mismatch or rank-3 output raises.
   - `positives_negatives_for_case`: reproduces the `_run_reranker` inline expression on a hand table
     (compound → per-expected positives; negative = strongest non-expected; control → all-negative).
   - `OnnxCrossEncoder.score` with a **stub** session + stub tokenizer → scores are `sigmoid(logit)` in
     input order; wrong-shape output raises.
   - **dispatch-routing regression**: every existing `ARMS`/`RERANKER_ARMS` name still routes to its
     original handler after the ONNX branch is added.
   - Run: `make test-file FILE=tests/evaluation/test_fre697_onnx_reranker.py` → all pass.
2. Implement `onnx_reranker.py` + the shared `positives_negatives_for_case` helper to green the units;
   substitute the helper into `_run_reranker` (3-line, behaviour-preserving).
3. Wire the arms + `_run_onnx_reranker` + dispatch into `separation_benchmark.py`.
4. **`uv sync --extra onnx-eval`**, then execute the arms on the VPS **one per process** (RAM discipline):
   - `--arm onnx-bge-int8`
   - `--arm onnx-qwen-seqcls-fp32`  (the quant-equivalence control)
   - `--arm onnx-qwen-seqcls-int8`  (primary)
   - Each: `verify_instrument` **OK** (Mars #1) before aggregates; require `completed_queries == 54`,
     `partial == false`; capture best J, overlap, p5/p95, clean-floor verdict, n_pos/n_neg, and
     warm-median/p95/cold latency; record provenance (revision + hashes + thread/max_length config).
   - **Quant-equivalence gate:** `|bestJ(int8) − bestJ(fp32)| ≤ 0.03` **and** top-1 rank agreement high;
     if it fails, report fp32 as the arm-B result and flag the int8 quant (codex #2).
   - **Family-agreement gate:** the qwen-seqcls arm lands in the reranker band (materially above the
     embedder ceiling J≤0.59 from FRE-694) — else STOP and reconcile before publishing (codex #5).
5. **Research doc** `docs/research/2026-07-04-fre-697-onnx-vps-reranker.md` — method, per-arm separation
   table (best J, overlap, p5/p95, clean-floor, n_pos/n_neg) vs the FRE-695 baselines
   (llama.cpp/MLX/Voyage Qwen3 + bge context), a CPU-latency table, the quant-equivalence result, and the
   **viability verdict + recommendation** (VPS-ONNX as the always-on private reranker / failover for the
   MLX-laptop path). Curated aggregates only; raw JSON stays gitignored.
6. Quality gates: `make test` (module then full) · `make mypy` · `make ruff-check` + `--fix`/format ·
   `pre-commit run --all-files`.

## Acceptance criteria (proof for master's gate)

| # | Criterion (from ticket deliverable + ADR-0100 measurement discipline) | Proof |
|---|---|---|
| AC1 | Both ONNX arms run on the **VPS CPU** through the FRE-670 harness, `completed_queries == 54`, `partial == false` | harness output + `separation-onnx-*.json` (bge-int8, qwen-seqcls-int8) |
| AC2 | **Instrument verification** (4-doc Mars-#1 rank + gap) passes per arm before aggregates; a fail is a hard `SystemExit` | printed `verify: … -> OK` per arm |
| AC3 | Report per arm: best **Youden's J**, overlap counts, robust p5/p95, **clean-floor verdict**, n_pos/n_neg | curated table in research doc + JSON |
| AC4 | Report per arm **CPU latency** (warm-median/p95, cold-first) at the recorded thread config | latency line in output + doc table |
| AC5 | **Quant-equivalence** (int8 vs fp32 arm-B, ΔbestJ ≤ 0.03) recorded, and **family-agreement** with FRE-695 confirmed | equivalence line in doc + fp32 control JSON |
| AC6 | **Recommendation** on VPS-ONNX viability as the always-on private reranker | verdict section in research doc |
| AC7 | Offline / no PII / no prod substrate; raw JSON gitignored, only curated aggregates + **provenance** (revision + hashes) committed | no-substrate harness; `git check-ignore` on JSON; doc |
| AC8 | New pure functions + shared extraction helper + dispatch routing unit-tested; suite green; mypy/ruff clean | `test_fre697_onnx_reranker.py` passing + gates |

## Risks / mitigations

- **Live-VPS resource contention (codex #4)** — torch-free (no heavy export); INT8 primary (~0.5–0.6 GB
  resident); **one arm per process**, freed at exit; onnxruntime `intra_op=4`/`inter_op=1` leaves live-gateway
  headroom; per-query 30 s wall-clock guard aborts a runaway. fp32 arm-B control ~2.4–3 GB resident — run
  alone, monitor RAM against the ~10 GiB envelope; STOP + report if tight.
- **Wrong seq-cls prompt format ⇒ silently meaningless scores (codex #1)** — replicate the model-card
  template exactly; the 4-doc instrument verification (polarity + template + tokenizer wiring) + load-time
  ONNX I/O-name/shape assertion + explicit `squeeze_logits` guard the path; keep the model-**native**
  instruction (its trained distribution) and document that choice.
- **Arm-B ONNX provenance (community fp32 export) (codex #2)** — pin the HF **revision**, record fp32+int8
  **sha256** + quant config in the run record; the required **fp32 control** + **quant-equivalence gate** +
  **family-agreement gate** validate it; owner may force the canonical torch self-export instead.
- **Truncation dropping expected evidence (codex #1)** — tiny one-line corpus; explicit `max_length`,
  query never truncated, and a STOP if any expected note would truncate.

## Out of scope

Production reranker adoption / deploy (its own deploy-class ticket, feeds FRE-655) · any prod substrate
write · embedder changes (FRE-694 settled: stay on prod 0.6B).

## Execution outcome (2026-07-04) — reconciliation with the approved plan

- **Deviation (forced by the environment):** the only seq-cls Qwen3-Reranker ONNX export ships **fp16**,
  not fp32. So the arm-B "fp32 control" is actually **fp16 as-published** (relabelled honestly), and the
  INT8 seq-cls arm is produced locally via a torch-free **fp16→fp32 graph cast** (initializers +
  `Constant`/embedded tensors + `Cast` ops + value-info) then `onnxruntime` dynamic INT8. The cast is
  validated by the instrument gate (Mars #1 still passes) and the quant-equivalence comparison.
- **Results:** all three arms completed 54/54 with **zero stalls**. bge-int8 J=0.503 @ 2.39 s;
  Qwen seq-cls **fp16 J=0.680** @ 7.83 s (arm-B headline, in the FRE-695 reranker band, > embedder 0.59);
  Qwen seq-cls int8-dynamic J=0.395 @ 6.66 s.
- **Quant-equivalence gate FAILED** (fp16 0.680 → int8 0.395, −0.285 ≫ 0.03): per the plan, fp16 is the
  arm-B result and **dynamic-INT8 is flagged non-viable** for this model (documented; a real finding, not
  a wiring bug — instrument gate still passes). **Family-agreement gate passed** for fp16 (0.680 > 0.59).
- **Verdict:** VPS-ONNX is a *functionally* viable always-on private **failover** (correct separation,
  no stalls, on-box), but **latency (2.4-7.8 s/query) blocks it as a primary**. Full writeup:
  `docs/research/2026-07-04-fre-697-onnx-vps-reranker.md`. Follow-ups: **FRE-775** (latency reduction —
  static/QAT INT8, batching, tuning), **FRE-776** (optional canonical seq-cls export).
