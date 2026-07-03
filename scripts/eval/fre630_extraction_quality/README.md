# FRE-630 — pre-write extraction-quality benchmark

The write-side complement to the FRE-435 recall harness. FRE-435 asks *"is what we
stored retrievable?"*; this asks *"did the extractor get the right things out in the
first place?"*. It scores the **output dict** of
`second_brain.entity_extraction.extract_entities_and_relationships` against a curated
gold set — **no Neo4j write** is involved, so extraction quality is measured directly
at the extractor boundary.

> **Altitude — pre-write.** This benchmark observes only the extractor's returned
> `entities` / `relationships` / `stances` / `claims`. It does **not** measure
> embedding-based dedup, the description-correction gate, or write-time validation —
> those are graph-write behaviours. A post-write graph-state benchmark is **deferred
> to Phase 2** (filed as a follow-up).

> **This is a calibration / regression set — not a statistically powered benchmark.**
> ~24 curated cases detect large regressions and the ticket's named failure modes with
> high signal; they are **not** enough to certify a few-point A/B move. Read per-tag and
> per-case, use paired per-case deltas for A/Bs, and grow toward 50–100 cases as
> failures are discovered (codex plan-review P1.3).

## Layout

| File | Role |
|------|------|
| `gold.py` | `GoldCase` schema (versioned) + `load_gold_set` (YAML) + discipline guards |
| `matching.py` | **Pure** tiered entity matcher: normalize → alias → narrow fuzzy |
| `metrics.py` | **Pure** scoring functions (no I/O, no LLM) — the unit-tested core |
| `scoring.py` | `score_case(gold, extracted)` — pure; per-case diffs |
| `report.py` | `RunReport` + `aggregate` (+ per-tag) + JSON/markdown; rich run stamping |
| `harness.py` | I/O driver (CLI): calls the real extractor `--samples N` times, scores, emits |
| `gold_extraction.yaml` | The curated gold set (~24 cases), grounded/paraphrased, no PII |

## Why a tiered matcher (codex P0.2)

LLM extraction is non-deterministic and gold names will not string-match cleanly, and
a naive exact match punishes correct-but-differently-worded extractions — worse, one
entity-name mismatch cascades into false relationship misses (edges are scored over
their endpoints). Each extracted name resolves to at most one gold entity via:

1. **exact** — after deterministic normalization (case-fold, whitespace-collapse,
   punctuation strip, NFKD accent-fold): `Neo4j` == `neo4j`, `Météo France` == `meteo france`.
2. **alias** — the extracted name matches a gold entity's hand-authored accepted form.
3. **fuzzy** — a conservative `difflib` similarity ≥ threshold, only for still-unmatched
   candidates, always recorded as tier `fuzzy` so it is auditable.

Relationships score over the **resolved gold entity ids**, not raw strings.

## Metrics

Entity precision/recall/F1 · entity-type accuracy · knowledge-class accuracy ·
relationship precision/recall · **relationship-type correctness** (right-type-given-
right-endpoints — the residence-vs-visit signal) · hallucination rate ·
forbidden-edge-type rate · **extraction-empty-fallback rate** (the extractor's
timeout/parse-fail path, counted separately so it never masquerades as a plain miss) ·
dedup/normalization convergence · description-integrity (a labeled proxy, never a
headline) · stance/claim emission recall. Metrics return `None` on a vacuous
denominator so they are excluded from aggregates rather than averaged as a misleading
`1.0`.

## Run protocol

The extractor always routes through LiteLLM + the ADR-0065 cost gate, so the harness
points the cost substrate at the **test stack** (FRE-375): benchmark runs never touch
prod cost records. Run it as a **module** (`-m`) so `scripts` resolves as a package:

```bash
make test-infra-up      # isolated cost substrate: Postgres :5433

# AC-3 smoke — one case, proves the driver runs end-to-end + emits a schema-valid report
uv run python -m scripts.eval.fre630_extraction_quality.harness \
    --run-id smoke-$(date +%Y%m%d) --limit 1

# AC-5 baseline — all cases, 3 samples each for a mean±std stability band
uv run python -m scripts.eval.fre630_extraction_quality.harness \
    --run-id baseline-$(date +%Y%m%d) --samples 3

make test-infra-down
```

Output lands in `telemetry/evaluation/fre630-extraction-quality/<run-id>.{json,md}`
(**gitignored** — raw runs are never committed; curated summaries go in the research
doc). The extractor model is resolved from `config/models.cloud.yaml`
(**gpt-5.4-mini**, the prod extractor); every run stamps the model id, provider, prompt
hash, git commit, and matcher/gold-schema versions.

## Backend-aware truth-source (read before interpreting numbers)

The report stamps `extractor_model` / `provider` / `model_config_path`. The prod
extractor is **gpt-5.4-mini** (cloud); the local `config/models.yaml` role is
gpt-5.4-nano — a *different* model, so a run stamped `nano` is not the prod baseline.
`prompt_hash` pins the exact extraction prompt the numbers reflect; a prompt A/B must
compare runs with different `prompt_hash` but the same `matcher_version` and
`gold_schema_version`.

## Tests

```bash
make test-k K=fre630   # the pure matcher/metric/scoring/report/gold suite (no LLM)
```

The pure core is fully unit-tested; the I/O driver is exercised by the smoke run above
(needs the test substrate + cloud creds, hence run by the integrator, not in CI).

## Curation discipline (public repo)

Cases are **grounded but paraphrased** from the live-corpus failure modes (FRE-630
evidence + the FRE-636 taxonomy spike) — never verbatim transcripts, no PII / owner
name / home / vehicle / injected email / deployment identifiers. `test_fre630_gold_set.py`
enforces a PII denylist and failure-mode coverage.
