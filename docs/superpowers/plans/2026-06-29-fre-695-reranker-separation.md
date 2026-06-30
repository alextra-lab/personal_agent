# FRE-695 — Reranker separation on the FRE-670 probe: does the cross-encoder open the floor?

**Ticket:** FRE-695 (Approved, Tier-1:Opus, "Memory Recall Quality"). Continuation of FRE-694/670,
same build session (context kept). **Backing:** ADR-0087 §D (recall measurement) · ADR-0100 (the floor
this gates). **Branch:** `fre-695-reranker-separation`. Eval + docs only — no deploy.

## The question (binary, decisive)

FRE-694 proved NO bi-encoder embedder — local 0.6B/4B/8B f16 or cloud Voyage — opens a clean floor on
the FRE-670 probe (best Youden's J only 0.42–0.59; positive/negative clouds overlap everywhere). The
hypothesised lever is the **cross-encoder reranker**, which reads (query, document) *together* rather
than comparing two pre-computed vectors. Does any reranker open a clean floor on the same hard FRE-670
distractors? If yes → the reranker is the lever (FRE-655 calibrates the floor on **reranker scores**,
then local-vs-cloud). If even rerankers overlap → the floor problem is deeper than the retrieval models.

## Acceptance criteria (definition of done)

- **AC1 — separation per reranker arm** on the FRE-670 probe, measured on relevance scores: per case,
  score the query against its true-match note(s) (positives, per-expected-entity) and against the
  non-expected notes (negatives, the strongest distractor per query + every control). Report best
  Youden's J, overlap counts, robust p5/p95, clean-floor verdict — **reusing `separation_report.py`**.
- **AC2 — instrument sanity per arm:** a trivial relevant-vs-irrelevant case scores relevant near 1,
  irrelevant near 0, asserted **before** any hard-case aggregate is trusted (the FRE-694 discipline).
  Engine + precision recorded per arm.
- **AC3 — verdict:** does any reranker open a clean floor the embedder (J ≤ 0.59) could not?
- **AC4 — recommendation** feeding FRE-655 floor calibration + the production reranker architecture
  (local laptop vs cloud Voyage; weigh raw-memory egress cost for any cloud adoption).
- **AC5 — curated research doc** (no PII); raw run JSON gitignored.

## Verified facts (probed 2026-06-29)

- **Rerank API** (`src/personal_agent/memory/reranker.py`): `POST {endpoint}/rerank` with
  `{model, query, documents, top_n}`; response `results[]` (llama.cpp) **or** `data[]` (Voyage), each
  item `{index, relevance_score}`. Scores re-aligned to input order by `index`. Scores are already
  in `[0,1]` — no `(cos+1)/2`, no dimension sweep.
- **Arms (all f16):**
  - `rr-0.6b-gpu` — `slm.frenchforet.com/v1`, id `Voodisss/Qwen3-Reranker-0.6B` (port 8508), CF headers.
  - `rr-4b-gpu` — `slm.frenchforet.com/v1`, id `Voodisss/Qwen3-Reranker-4B` (port 8506), CF headers.
    **The live production reranker** → doubles as the production-representative arm.
  - `rr-0.6b-cpu` — `localhost:8504/v1`, id `Qwen3-Reranker-0.6B.F16` — same-model VPS-CPU cross-check.
  - `voyage-rerank-2.5` and `voyage-rerank-2.5-lite` — `api.voyageai.com/v1`, bearer key from
    `pass show VOYAGEAI_API_KEY`. (8B reranker excluded this round — owner capped the local ladder at 4B.)
- Owner verified live: 4B-f16 0.967 (relevant) vs ~2e-6 (irrelevant); VPS 0.6B-f16 0.997 vs ~3e-5.
  Voyage rerank-2.5 0.81 vs 0.22 (confirmed this session). The instrument works on easy cases; the bench
  measures the HARD distractors.

## Design

### D1 — Extend `separation_benchmark.py` with reranker arms (per ticket)

Reuse `separation_report.py` (summarize_separation / overlap / sweep_floor / propose_floor) + the same
54-case `semantic_probe.yaml` + the same per-expected-entity-positive / top-non-match-negative metric
as FRE-694. The document corpus is the same 49 notes (`"{name}: {description}"`). The embedder path is
**left untouched** (it is parity-validated); the reranker path is a parallel branch sharing a new pure
helper `separation_from_scores(cases, note_names, score_rows)` (score_rows[i] = the arm's relevance
scores of query *i* against every note). No dim sweep, no `(cos+1)/2`.

### D2 — Rerank call (mirror reranker.py) — codex fixes folded in

`POST {endpoint}/rerank`, `{model, query, documents, top_n=len(documents)}` — **one request per query
with all 49 documents (never per-document or chunked)**, so the score reflects the same listwise
candidate-set context production uses. Parse `results[]` (llama.cpp) **or** `data[]` (Voyage —
documented as an *intentional* delta from reranker.py, which reads only `results[]`), re-align by
`index`, and **fail loud if the result count ≠ len(documents)** (a truncated response). CF-Access headers
come from **`cf_access_service_token_headers()` imported directly** (not a raw env rebuild — avoids
header-name/casing divergence; codex #4) for the slm host; bearer key for Voyage; none for VPS `:8504`
(lazy-import so the cloud-only arm stays `personal_agent`-free). **One shared `httpx.AsyncClient` per
arm** with a small retry/backoff on 429/5xx (216+ requests across arms vs Voyage rate limits; codex #4).

### D3 — Instrument-sanity gate — rank-order, not a fixed 0.5 (codex #2)

A reranker has no Neo4j cosine to parity-check against, so the validation is the ticket's sanity case —
but a fixed 0.5 threshold contradicts D4 (scores aren't comparable across arms). Instead the gate is
**scale-agnostic**: on a known easy pair the relevant doc must **rank #1 AND out-score the irrelevant
doc** (`relevant_rank == 1 and relevant_score > irrelevant_score`); the **score gap is recorded as the
arm's local calibration reference**. Aborts the arm on failure (broken endpoint / wrong model / auth).
Exposed standalone (`--sanity`) and run automatically before each arm's aggregates.

### D4 — Cross-arm comparability + best-J over *observed* scores (codex #3)

Relevance-score *scales* differ across cross-encoders, so **absolute thresholds are not comparable
across arms** and a proposed floor is **arm-specific, never transferable** between reranker endpoints.
Two consequences for the metric:
- Best Youden's J is computed by sweeping candidate thresholds at the **sorted unique observed scores**
  per arm (not the fixed 0.0–0.95/0.05 grid `calibration.sweep_floor` uses — a compressed score band
  would straddle that coarse grid and understate separation). New pure helper `best_separation_at_observed`.
- The verdict keeps **p5/p95 and min/max** alongside J (a compressed scale can make equal J hide a poorer
  score margin — `separation_report` already tracks these).
The cross-arm J comparison (does arm X separate better than Y) is valid as a *decision* metric; the
cross-arm *floor* is not — FRE-655 calibrates per chosen reranker on that reranker's own distribution.

## Plan (atomic steps, TDD)

1. **Pure helpers + tests** — `tests/evaluation/test_fre695_reranker.py` (RED): `parse_rerank_response`
   (results[]/data[] → scores aligned to input order; fail-loud on count mismatch), `separation_from_scores`
   (per-expected-entity positives, top-non-match negatives → summarize), and `best_separation_at_observed`
   (max Youden's J swept at sorted-unique observed scores). Pure parts in `separation_report.py`; no network.
2. **Reranker path in `separation_benchmark.py`** — add the reranker arms to `ARMS`; a `_rerank(arm,
   query, documents)` call (mirror reranker.py: one request, all docs, top_n=len, index re-align,
   results[]/data[], shared AsyncClient + backoff, `cf_access_service_token_headers()` for slm); a reranker
   run branch (rank-order sanity gate → score matrix → `separation_from_scores` → best-J-over-observed +
   summarize → gitignored JSON + table). `--sanity` standalone.
   - verify: `make test-k K=fre695` passes; `--arm rr-0.6b-gpu --sanity` shows relevant rank #1 + the gap.
3. **Run arms** (offline, no substrate): rr-4b-gpu (prod), rr-0.6b-gpu, rr-0.6b-cpu (cross-check),
   voyage-rerank-2.5, voyage-rerank-2.5-lite. Sanity each; capture separation. **Verdict** = clean floor?
4. **Research doc** — `docs/research/2026-06-29-fre-695-reranker-separation.md` (curated aggregates,
   no PII): separation table per arm, the embedder-vs-reranker comparison, verdict, recommendation.
5. **Quality gates** — `make test` · `make mypy` · ruff · pre-commit. PR.

## Risks / halt conditions

- Remote rerankers (slm GPU, Voyage) — read-only inference; only the paraphrased probe (no PII) is sent.
  Voyage key read from `pass` at run time, never persisted/logged.
- If a reranker DOES open a clean floor → big result (the lever is found); if all overlap → also a real
  result (rethink). Either way report honestly; don't tune toward a desired outcome.
- n = 54: extrema outlier-sensitive → report robust p5/p95 alongside (FRE-694 discipline).

## Out of scope

- The 8B reranker (owner-capped at 4B this round).
- FRE-655 floor calibration itself (this feeds it); any production reranker swap/deploy.
