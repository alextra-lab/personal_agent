# FRE-435 — memory-recall quality harness (FRE-488 scaffold)

The reusable instrument for ADR-0087's measurement-first program: given a probe
set, drive each case end-to-end against the **test substrate**, score the D1
metrics, and emit a structured report with a hypothesis-attribution breakdown.
Analog of `scripts/eval/fre433_cache_ab/`. **Phase 1 changes no production
behaviour** — the harness only calls existing `src` APIs.

## What FRE-488 ships (and what it does not)

- ✅ The harness package + the D1 metric core + a **tiny seed** probe set.
- ❌ The real labelled gate set → **FRE-489** (loads through this same schema).
- ❌ LongMemEval adapter → **FRE-490** (`load_longmemeval` is a stub).
- ❌ Baseline numbers / hypothesis verdict / §D5 gate cutoffs → **FRE-491**.
- ❌ LLM-judge description-integrity → a deterministic proxy for now.

## Layout

| File | Role |
|------|------|
| `probes.py` | `ProbeCase`/`ExpectedRecall`/`SeedEntity` schema + `load_probe_set` (YAML) + `load_longmemeval` stub |
| `metrics.py` | Pure D1 scoring: recall@k, precision@k, MRR, nDCG, false-negative, retrieval-miss, k-sweep, write-completeness |
| `attribution.py` | `attribute()` — maps a failed case to a §D4 hypothesis (H1..H6) by metric pattern |
| `scoring.py` | `flatten_recall` (namespace/order/dedup a recall result) + `score_case` (pure) |
| `report.py` | `RunReport`/`CaseResult` + `aggregate` + JSON/markdown rendering |
| `harness.py` | The I/O driver (CLI) |
| `seed_probe.yaml` | Tiny seed set proving the instrument runs (FRE-488) |
| `bespoke_probe.yaml` | **The recall GATE set** — ~21 curated cases mined from the live corpus (FRE-489) |

## Run protocol

```bash
make test-infra-up      # isolated test substrate: Neo4j:7688 / ES:9201 / Postgres:5433

# offline seed (write path bypasses the LLM; default)
uv run python scripts/eval/fre435_memory_recall/harness.py \
    --run-id seed-$(date +%Y%m%d) \
    --probe-set scripts/eval/fre435_memory_recall/seed_probe.yaml \
    --write-mode replay

make test-infra-down
```

Run the **gate** set with `--probe-set scripts/eval/fre435_memory_recall/bespoke_probe.yaml`.

Output: `telemetry/evaluation/fre435-memory-recall/<run-id>.{json,md}` (gitignored —
**raw runs are never committed; curated summaries only**).

### The gate set (`bespoke_probe.yaml`, FRE-489)

The ADR-0087 §D2 primary gate — ~21 cases **grounded in Seshat's own live corpus
but curated/paraphrased** (the repo is public: no verbatim transcripts, no PII).
It spans the owner's real multi-session threads (neuroplasticity, game theory,
consciousness/Orch-OR, the EM spectrum & cosmology, optics, cooking, history, the
agent's own architecture), includes the three §D6 pedagogical sub-types
(active-recall-of-a-due-concept, thread-branch retrieval, cross-domain match),
real false-negative failures, and true-negative abstention controls.

Two disciplines are enforced by `tests/evaluation/test_fre435_bespoke_probeset.py`:
a **PII denylist** over every authored string, and **referential queries** — a
query never names its expected entity (else a dumb substring match passes and the
gate can't discriminate a real recall failure).

### The semantic split (`semantic_probe.yaml`, FRE-670)

The FRE-489 gate is **lexical masked as semantic** — its queries share
description-level vocabulary with the stored text, so BM25 beats the vector path
(recall@5 1.00 vs 0.72) and 0.6B/4B embedders score identically (FRE-656). The
FRE-670 split (54 cases) fixes that: queries are **vocabulary-divergent** (imagery /
paraphrase, near-zero surface overlap with the note text), so keyword search falls
over and the vector path must do real semantic matching. It adds a `register:`
(natural vs imagery) and `type:` (positive vs control) tag taxonomy; disciplines are
enforced by `tests/evaluation/test_fre670_semantic_probe.py` (referential + a
content-token-Jaccard divergence guard, plus the PII denylist).

**Three-arm comparison** (test substrate only; the acceptance bar is BM25 recall@5
landing *materially below* the vector arms on the positives):

```bash
export AGENT_NEO4J_PASSWORD=<test :7688 password>   # from .env NEO4J_PASSWORD
export AGENT_NEO4J_USER=neo4j

# Arm A — BM25 keyword baseline (standing lexical-leakage guard; --probe-agnostic)
uv run python scripts/eval/fre435_memory_recall/keyword_baseline.py \
    --probe scripts/eval/fre435_memory_recall/semantic_probe.yaml

# Arms B/C — vector path, co-resident recall over the same 54-note corpus
scripts/eval/fre435_memory_recall/run_embedder_benchmark.sh 0.6b calibrate \
    --probe-set scripts/eval/fre435_memory_recall/semantic_probe.yaml
# 4B additionally needs CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET (tunnelled slm endpoint)
scripts/eval/fre435_memory_recall/run_embedder_benchmark.sh 4b calibrate \
    --probe-set scripts/eval/fre435_memory_recall/semantic_probe.yaml
```

The `calibrate` pass co-seeds all cases (no per-case wipe) so the vector arm ranks
against the same corpus BM25 does — the comparison is apples-to-apples. Each arm
reports recall@1/@5, the natural-vs-imagery register delta, and control abstention.
Result writeup: `docs/research/2026-06-29-fre-670-semantic-probe.md`.

### Write modes

- `--write-mode replay` (default, offline) — seeds the case's pre-extracted
  entities directly via `MemoryService.create_entity` / `create_relationship`. No
  LLM on the write side.
- `--write-mode extract` (needs the SLM server) — runs the real
  `extract_entities_and_relationships` over each setup turn, lands the entities,
  and promotes them via `run_promotion_pipeline`.

### Backend-aware truth-source (read this before interpreting numbers)

- Retrieval outcomes are read from the **actual** `recall()` return, never a
  proxy log field (FRE-433 discipline).
- The report stamps **`embedding_backend`**: offline `replay` *without* an
  embedding model persists **zero-vector** embeddings, so `query_memory` skips
  vector search and recall degrades to **keyword-only**. A run stamped
  `zero-vector` is measuring degraded-mode recall — not the real vector pipeline.
  For real vector-path measurement use `--write-mode extract` with the SLM up
  (FRE-491).

## D1 metrics (ADR-0087 §D1)

- **Write-completeness** — extraction-fire rate, landing rate, description-integrity
  (proxy), joinability (optional, reuses the ADR-0074 probe — wired in a later
  ticket).
- **Retrieval quality** — recall@k / precision@k with a *k* sweep, **false-negative
  rate** (the headline: relevant context exists but the system returns nothing or
  denies), a distinct **retrieval-miss** rate (returned the wrong context), MRR and
  nDCG.

Ids are namespaced (`entity:` / `episode:`) so the entity and episode id spaces
never collide when scoring.

## Tests

```bash
make test-k K=fre435   # the pure metric/attribution/report/probe/scoring suite
```
The pure core is fully unit-tested; the I/O driver is exercised by the live seed
run above (needs the test substrate, hence run by the integrator, not in CI).
