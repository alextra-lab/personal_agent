# FRE-488 — Memory-recall harness scaffold (`scripts/eval/fre435_memory_recall/`)

**Ticket:** FRE-488 (Approved → In Progress) · Project: Memory Recall Quality · Parent: FRE-435
**ADR:** ADR-0087 §D1/D3 (measurement-first program). **Analog:** `scripts/eval/fre433_cache_ab/`.
**Tier:** Sonnet. **Risk:** Standard/Complex (new code driving `src` write + retrieval paths) → codex plan-review required.

## Goal / AC (from ticket + ADR Verification #1,#3)

A **reusable** harness under `scripts/eval/fre435_memory_recall/` that:
1. loads a probe set (pluggable: bespoke YAML now; LongMemEval adapter = stub, FRE-490),
2. drives a case end-to-end against the **test substrate** (Neo4j :7688 / ES :9201 / Postgres :5433) — exercising the **write path** (`entity_extraction` → `promote` → `MemoryService`) and the **retrieval path** (`MemoryServiceAdapter.recall` / `query_memory` → `reranker.rerank`),
3. scores the **D1 metrics**, and
4. emits a structured per-case + aggregate report with a **hypothesis-attribution** breakdown.

**AC:** runs end-to-end on a tiny **seed** probe set against the test substrate and emits the report with all D1 metrics populated.

## Explicit scope boundary (what FRE-488 is NOT)

- **NOT** the real labeled gate set — that is **FRE-489** (this ships only a 2–3 case toy *seed* to prove the instrument runs).
- **NOT** the LongMemEval adapter body — **FRE-490** (ship the loader interface + a `NotImplementedError` stub).
- **NOT** the baseline numbers / hypothesis verdict / gate-cutoff calibration — **FRE-491**.
- **NOT** an LLM-judge for description-integrity — scaffold uses a deterministic proxy (non-empty + no exact cross-contamination), clearly labelled `proxy`; real judge is later.
- Phase 1 changes **no production behaviour** (ADR-0087 §scope). No `src/` runtime edits — harness only *calls* existing `src` APIs.

## Design decisions (flag these to codex + owner)

1. **In-process Python driver, not HTTP `/chat`.** The ticket names the exact write/retrieval functions to exercise; HTTP `/chat` only runs against prod substrate. So the harness imports and calls the APIs directly. **(codex Q1)** `settings` is a cached singleton bound at import (`config/settings.py` `_settings`; `config/__init__.py` `settings = get_settings()`) — so the test-substrate env (`AGENT_NEO4J_URI=bolt://localhost:7688`, `AGENT_ELASTICSEARCH_URL=:9201`, `AGENT_DATABASE_URL=:5433`, `APP_ENV=test`) MUST be `os.environ.setdefault`'d at the **very top of `harness.py`, before any `from personal_agent…` import** (exactly like `tests/conftest.py:16-26`). Metric/probe/report/attribution modules import no `personal_agent` code, so only `harness.py` carries this guard. `APP_ENV=test` arms the prod-URI guard.
2. **Two write modes.** `--write-mode replay` (default, offline — seed expected entities/relationships directly via `MemoryService.create_entity`/`create_relationship`, no LLM) and `--write-mode extract` (real — `extract_entities_and_relationships` → `run_promotion_pipeline`, needs SLM). `replay` is what makes the seed AC runnable without a live model on the write path. (ADR: "offline replay preferred.")
3. **Backend-aware truth-source (FRE-433 discipline).** Retrieval outcome is read from the **actual** `recall()`/`query_memory()` return value (facts/entities + empty/None = "denied"), never a proxy log field.
4. **The model-dependent live seed run is master's post-deploy step** (needs `make test-infra-up` + SLM). In the build session I TDD the deterministic core (metrics, attribution, report, probe loading) with mocked substrate/LLM and do **not** fire a live LLM (project rule: integration needs live LLM, not in agent session).
5. **Embedding-backend honesty (codex Q4).** `create_entity` calls `generate_embedding`, which without SLM returns a **zero vector** (`memory/embeddings.py:76-91`) → persisted (`service.py:723-725`) → `query_memory` skips vector search (`service.py:1531-1559`) and `rerank` passes through → offline `replay` recall degrades to **keyword-only**. This does NOT crash (good for the AC), but it is **not** the real vector pipeline. The harness therefore probes `generate_embedding` once at startup and stamps the run report `embedding_backend: real | zero-vector(keyword-only)` (backend-aware truth-source, FRE-433 discipline) so metrics are never silently misread. Meaningful vector-path measurement is `--write-mode extract` with SLM up (master / FRE-491). Zero-vector stub-injection is explicitly out of scope (note as a possible follow-up).

## File layout

```
scripts/eval/fre435_memory_recall/
  __init__.py        # make package importable as scripts.eval.fre435_memory_recall.*
  probes.py          # ProbeCase / ExpectedRecall / ProbeTurn dataclasses + load_probe_set(yaml) + load_longmemeval(stub)
  metrics.py         # pure scoring fns: recall@k, precision@k, MRR, nDCG, false-negative, k-sweep, write-completeness
  attribution.py     # attribute_failure(case_result) -> Hypothesis (D4 H1..H6 keyed on metric pattern)
  report.py          # CaseResult/RunReport dataclasses -> structured JSON + markdown
  harness.py         # CLI driver: load -> (seed write) -> retrieve -> score -> attribute -> emit
  seed_probe.yaml    # 2-3 toy cases incl. one false-negative-shaped + one pedagogical-shaped tag
  README.md          # run protocol (test-infra-up, write-modes, output location)
tests/evaluation/
  test_fre435_metrics.py        # TDD core: every metric fn on hand-computed fixtures
  test_fre435_attribution.py    # TDD: each hypothesis bucket from a synthetic CaseResult
  test_fre435_probes.py         # TDD: YAML round-trip + schema validation + longmemeval stub raises
  test_fre435_report.py         # TDD: JSON/markdown render on a fixed RunReport
```

Output (gitignored): `telemetry/evaluation/fre435-memory-recall/<run-id>.{json,md}` — add a `.gitignore` line mirroring the FRE-433 one (line 167).

## Metric definitions (the TDD heart — pure functions over labelled ids)

Retrieval (over `retrieved: Sequence[str]` ordered, `relevant: set[str]`). **IDs are namespaced (codex Q3): `MemoryRecallResult` carries `episodes` (keyed `turn_id`) and `entities` (keyed entity id/name) — every label and every retrieved id declares its namespace (`episode:` / `entity:` prefix) and is deduped before scoring, so cross-namespace ids never collide.**
- `recall_at_k = |relevant ∩ retrieved[:k]| / |relevant|`; **when `|relevant| == 0` → return `None` and EXCLUDE from the aggregate mean (never `1.0` — codex Q3, avoids silent inflation).**
- `precision_at_k = |relevant ∩ retrieved[:k]| / k`
- `reciprocal_rank = 1/rank of first relevant, else 0`  (MRR = mean over cases with `|relevant|>0`)
- `ndcg_at_k` = DCG/IDCG, binary gains; **IDCG uses `min(k, |relevant|)` ideal hits (codex Q3), not the full relevant set.**
- **Two distinct failure metrics (codex Q3 — do not conflate):**
  - `false_negative` (**headline, ADR §D1**) = `relevant` non-empty AND (`retrieved` empty OR system `denied`) — the "no prior discussions" symptom.
  - `retrieval_miss` = `relevant` non-empty AND `recall@k == 0` even with **non-empty** retrieval (returned the wrong context). Superset signal; reported alongside FN.
- `k_sweep(retrieved, relevant, ks)` → `{k: (recall, precision)}` (separates "not in index" from "ranked too low")

Write-completeness (over `WriteOutcome`):
- `extraction_fire_rate`, `landing_rate` (non-empty fact created / expected), `description_integrity` (proxy, labelled), `joinability` (optional `JoinabilityWalk`; `None` when not run).

## Steps (TDD; each ⇒ verify)

1. **Package skeleton + probes.py** → `touch __init__.py`; write `ProbeCase`/`ExpectedRecall`/`ProbeTurn` (frozen) + `load_probe_set` + `load_longmemeval` stub. Write `seed_probe.yaml` (2–3 cases, tags incl. `false-negative`, `pedagogical`). **Anti-vacuous-green guard (codex Q2): the seed MUST contain ≥1 case with `|relevant|>0`, ≥1 case with an expected write, and ≥1 case that is a non-empty-but-wrong miss — and `load_probe_set` asserts a probe set is non-degenerate (raises if every case has empty `relevant`), so a broken metric pipeline can't pass the AC on vacuous data.**
   - Test: `tests/evaluation/test_fre435_probes.py` — load `seed_probe.yaml`, assert schema + non-degenerate guard; `load_longmemeval` raises `NotImplementedError`.
   - Verify: `make test-file FILE=tests/evaluation/test_fre435_probes.py`
2. **metrics.py** (write tests first → fail → implement).
   - Test: `test_fre435_metrics.py` — hand-computed fixtures for each fn (e.g. retrieved=[c,a,b], relevant={a}, recall@1=0, recall@2=1, RR=1/2, nDCG@2=1/log2(3); false-negative truth table).
   - Verify: `make test-file FILE=tests/evaluation/test_fre435_metrics.py`
3. **attribution.py** → `attribute_failure(CaseResult) -> Hypothesis`: landed=0 → H1; landed but recall@maxk=0 → H3 (not retrievable); recall@maxk=1 & recall@prodk=0 → H3/H4 (ranked too low); denied while present → H4; description proxy fail → H2; fail under all → H5/H6.
   - Test: `test_fre435_attribution.py` — one synthetic `CaseResult` per bucket.
   - Verify: `make test-file FILE=tests/evaluation/test_fre435_attribution.py`
4. **report.py** → `RunReport`/`CaseResult` dataclasses + `render_json` + `render_markdown` (per-case table + aggregate rollup + hypothesis breakdown), analog of fre433 `render_markdown`. **Run meta stamps `embedding_backend: real|zero-vector` (codex Q4) and `write_mode`** so a degraded keyword-only run is never misread as the real vector pipeline.
   - Test: `test_fre435_report.py` — fixed `RunReport` → asserts JSON keys + markdown contains headline false-negative-rate line + hypothesis table.
   - Verify: `make test-file FILE=tests/evaluation/test_fre435_report.py`
5. **harness.py** → CLI (`--probe-set`, `--write-mode {replay,extract}`, `--run-id`, `--out`, `--k-sweep`). Orchestrates: in-process test-substrate env setup → `MemoryService().connect()` → per case: seed write (replay: `create_entity`/`create_relationship`; extract: `extract_entities_and_relationships`→`run_promotion_pipeline`) → retrieve via `MemoryServiceAdapter.recall` → score → attribute → emit. Thread `trace_id`/`session_id` on every write (ADR-0074). Health-probe substrate before driving (analog fre433 gateway-health gate); structlog with `trace_id`.
   - Unit-testable seam: factor the per-case scoring (`score_case(retrieved, denied, write_outcome, case) -> CaseResult`) as a pure fn so it's covered by steps 2–4; the I/O driver itself is exercised live by master.
6. **README.md** + `.gitignore` line + docstrings. Document: `make test-infra-up` first; `--write-mode replay` for offline seed; output dir; "no raw dumps in git".

## Quality gates (all before PR)

`make test-file FILE=tests/evaluation/test_fre435_metrics.py` (+ the other 3) → then module run `make test-k K=fre435` → then `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
(Full `make test` is 7+ min — run the fre435 subset for iteration; full suite once before PR.)

## Post-deploy runbook (for master — proves AC live)

```
make test-infra-up                     # Neo4j:7688 / ES:9201 / Postgres:5433
# SLM server up only if exercising --write-mode extract
uv run python scripts/eval/fre435_memory_recall/harness.py \
    --run-id seed-$(date +%Y%m%d) --probe-set scripts/eval/fre435_memory_recall/seed_probe.yaml \
    --write-mode replay
# expect: telemetry/evaluation/fre435-memory-recall/seed-*.{json,md} with all D1 metrics populated
make test-infra-down
```

## Follow-ups to file (Needs Approval, Memory Recall Quality project)

- (likely) description-integrity LLM-judge as a reusable scorer — if it grows beyond FRE-491's needs.
- Any seam discovered in `src` that needed a read-only accessor (none expected — flag if so).

## Next ticket disposition

FRE-489 (bespoke gate set) is next and **loads this harness's `ProbeCase` schema** → keep context; but 489 needs live-corpus mining + owner-set N + labeling, so it gets its own planning round.
