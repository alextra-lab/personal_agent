# FRE-630 — KG extraction quality → SOTA (Phase 1: the measurement instrument)

**Ticket:** [FRE-630](https://linear.app/frenchforest/issue/FRE-630) (Memory Recall Quality, parent FRE-435) · **Approved** · Tier-1:Opus
**Backing methodology:** ADR-0087 (measurement-first) · precedent: FRE-636 taxonomy spike, FRE-488/489/670 recall harness
**Method posture:** measure-don't-assert (FRE-433/434). No production behaviour change in this PR.

---

## Why this is Phase 1 (scope decision)

FRE-630 is a **program**, not a single deliverable — its own body names three surfaces:
(1) a benchmark, (2) a SOTA survey, (3) improve+A/B across code/prompts/gates. "One phase = one PR"
(a build halt condition) forbids bundling them. The three improve+A/B changes are each their own
flag→verify→rollout experiment and become follow-up tickets. **Phase 1 = the measurement foundation
the whole program needs and which does not exist today.**

**Two findings from the read-only code map that shape the plan:**

1. **The landscape shifted after the ticket was filed (2026-06-27).** The current extractor already
   has: the 3-class knowledge taxonomy World/Personal/System (FRE-637), structured `stances` + `claims`
   emission instead of description-flattening (FRE-638/711/725), a 6-type controlled relationship vocab
   (PART_OF/USES/RELATED_TO/SIMILAR_TO/CREATED_BY/LOCATED_IN), embedding-based dedup + a confidence-gated
   description-correction path (FRE-711/725), and runs on **gpt-5.4-mini** (cloud) / gpt-5.4-nano (local)
   — **not** the qwen3-8b the ticket's failure catalog came from. So the ticket's specific bugs
   (visit→LIVES_IN, `DISCUSES` garbage entity, flattened Stance) may be **partly stale**. We must measure
   the *current* extractor before assuming any of them persist.
2. **There is no extraction-quality benchmark.** The FRE-435/489/670 harness measures *retrieval*
   (recall@k) and assumes extraction is correct. Nothing scores whether the *right* entities/edges/classes
   were extracted in the first place. That instrument is the gap.

---

## Deliverables (this PR)

### A. Pre-write extraction-quality benchmark package — `scripts/eval/fre630_extraction_quality/`

**Altitude (codex P0.1):** Phase 1 measures **pre-write extraction quality** — it scores the *output* of
`extract_entities_and_relationships(user_message, assistant_response, …)` (a `dict` with
`entities`/`relationships`/`stances`/`claims`) against a gold label set. **No Neo4j write is needed.** It
therefore does **not** observe embedding-based dedup, the description-correction gate, or write-time
validation — those are graph-write behaviours. A **post-write graph-state benchmark is explicitly deferred
to Phase 2** and filed as a follow-up *iff* baseline failures implicate persistence/dedup/write-gate.

Mirrors the FRE-435 layout (pure core unit-tested; I/O driver run by the integrator, not CI).

| File | Role |
|------|------|
| `gold.py` | `GoldCase` schema (versioned) + `load_gold_set` (YAML) + degeneracy/discipline guards |
| `matching.py` | **Pure** tiered entity matcher (the codex-P0.2 core): normalize → alias → narrow fuzzy |
| `metrics.py` | **Pure** scoring functions (no I/O, no LLM) — the unit-tested core |
| `scoring.py` | `score_case(gold, extracted)` — pure; matches gold vs the extractor dict, emits per-case diffs |
| `report.py` | `RunReport`/`CaseResult` + `aggregate` (+ per-tag breakdown) + JSON/markdown; rich run stamping |
| `harness.py` | I/O driver (CLI): for each case call the real extractor `--samples N` times, score, emit report |
| `gold_extraction.yaml` | The curated gold set (~24 cases), grounded/paraphrased from the live corpus, no PII |
| `README.md` | run protocol, cost note, backend-aware-truth note, **"calibration/regression set, not statistically powered"**, curation discipline |
| `__init__.py` | package marker |

**`GoldCase` schema** (frozen dataclass; `GOLD_SCHEMA_VERSION` constant, stamped in the report):
```
case_id: str
tags: list[str]                       # failure-mode + domain tags (per-tag metrics keyed off these)
source: {user: str, assistant: str}   # the turn text fed to the extractor
expect_entities: list[{name, aliases?, type, class}]  # gold entity; `aliases` = accepted surface forms (codex P0.2)
expect_relationships: list[{source, type, target}]    # typed-edge triples; source/target refer to gold entity canonical names
expect_stances: list[{target, affect?}]               # gold structured stances (may be empty)
expect_claims: list[{facet, content_gist}]            # gold structured claims (may be empty)
forbid_entities: list[str]            # hallucination traps — MUST NOT be extracted (role labels, tool names, misspelled-rel-as-entity)
forbid_rel_types: list[str]           # off-vocabulary edge types that MUST NOT appear (e.g. LIVES_IN for a visit)
dedup_variants: list[[str, str]]      # case/spelling variants that MUST collapse to one canonical
```

**Matcher (`matching.py`, pure — codex P0.2, the biggest strengthening).** Entity matching is tiered so
the benchmark does not punish correct-but-differently-worded extractions, and so one name mismatch does
not cascade into false relationship misses:
1. **Deterministic normalization** — case-fold, whitespace-collapse, strip surrounding punctuation,
   Unicode accent-fold (NFKD). `Neo4j`==`neo4j`, `Météo France`==`meteo france`.
2. **Alias table** — a gold entity matches any of its `aliases` (hand-authored accepted surface forms:
   abbreviations, reorderings, known synonyms).
3. **Narrow fuzzy adjudication** — only for still-unmatched candidates: token-set ratio ≥ a conservative
   threshold (stdlib `difflib`, no new dep), logged as a *fuzzy* match so it's auditable, never silent.
   Each extracted entity resolves to **at most one** gold entity (greedy best-match, stable order).
   `matcher_version` is stamped in the report.
   **Relationships are scored over the *resolved gold entity ids*, not raw strings** — an edge counts iff
   both endpoints resolved to the gold entities named in the triple and the type matches.

**Metrics (`metrics.py`, all pure)** — the D1-analog for the *write* side, all computed off the resolved matches:
- **Entity precision / recall / F1** — via the tiered matcher above (exact/alias/fuzzy match tier recorded).
- **Entity-type accuracy** — over matched entities (right one of the 7 types).
- **Knowledge-class accuracy** — over matched entities (World/Personal/System — the FRE-636 gap).
- **Relationship precision / recall** — typed-edge match over resolved endpoints.
- **Relationship-type correctness** — over matched (source,target) pairs: was the *edge type* right?
  (the residence-vs-visit headline — also encoded via `forbid_rel_types` traps.)
- **Hallucination rate** — extracted entities matching a `forbid_entities` trap ÷ total extracted; plus
  off-vocab-edge-type rate (`forbid_rel_types`).
- **Extraction-failure / empty rate (codex P2)** — cases where the extractor returned the empty
  `_default_extraction_result` fallback (timeout / parse-fail / empty response) *while gold has positives*.
  Counted and surfaced separately so a fallback never masquerades as a mere precision/recall miss.
- **Dedup/normalization rate** — for each `dedup_variants` pair, did the extractor's *single returned dict*
  converge on one canonical name? (Scope note per codex P1.5: this measures the extractor's own
  normalization, **not** embedding/graph-write dedup — that's Phase 2.)
- **Description integrity** — a **labeled proxy** (non-empty, single-sentence, no stance-flatten markers),
  reported but never a headline score; LLM-judge deferred to a follow-up.
- **Stance / claim emission rate** — did the extractor emit expected structured `stances`/`claims`
  instead of flattening them into a World description? (directly tests the FRE-636 finding.)

**Report** — first-class extraction-specific artifacts (codex P1.4/P1.5): stamps `extractor_model`,
`entity_extraction_role`, `provider` (cloud/local), `model_config_path`, `git_commit`, `prompt_hash`
(hash of `_EXTRACTION_PROMPT_TEMPLATE`+`_EXTRACTION_SYSTEM_PROMPT`), decoding params when the client
exposes them, `matcher_version`, `gold_schema_version`, and `samples` (N). Emits **per-tag breakdowns**
and **per-case diffs** (missed / spurious / wrong-type / wrong-class / hallucinated). With `--samples N>1`
it reports per-metric mean/std across samples (measurement-stability band, codex P1.4). Raw runs land in
`telemetry/evaluation/fre630-extraction-quality/<run-id>.{json,md}` (**gitignored** — curated summaries only).

### B. Discipline + unit tests (the TDD proof) — `tests/evaluation/`
- `test_fre630_metrics.py` — the pure matcher/metric/scoring/report core, fully deterministic, **no LLM**.
  Hand-built `(gold, extracted)` fixtures exercise every metric incl. the trap paths, **the three matcher
  tiers** (exact/alias/fuzzy) and the fuzzy-threshold boundary, edge-scoring-over-resolved-ids, and the
  empty-fallback path. **Written first.**
- `test_fre630_gold_set.py` — loads `gold_extraction.yaml` and asserts the disciplines:
  PII denylist over every authored string (reuse the FRE-489/670 denylist approach), schema validity,
  non-degeneracy (≥1 positive label per case), and that the ticket's named failure modes are represented
  (≥1 residence-vs-visit trap, ≥1 hallucination trap, ≥1 dedup-variant pair, ≥1 stance/claim case).

### C. SOTA survey — `docs/research/2026-07-03-fre-630-extraction-quality-sota.md`
GraphRAG, HippoRAG, iText2KG, modern typed relation extraction, entity resolution/canonicalization,
and hallucination control for LLM KG extraction — **with references**. Each technique mapped to a
concrete candidate change in *our* pipeline (prompt/schema, relationship-vocab, dedup, write-gate).
Opens with the "landscape shifted since the ticket" finding + a compact current-architecture summary
(so the reader knows what already exists). Privacy-generalized (public repo).

### D. Baseline measurement (owner-gated — LLM spend)
Run the harness against the gold set with the **real extractor** → a curated baseline table appended to
the research doc (never raw dumps). To make baseline numbers trustworthy given a stochastic LLM (codex P1.4),
run **`--samples 3`** (≈72 small extraction calls) and report per-metric mean/std, and stamp the full
run metadata (git commit, prompt hash, model id, provider, config path, matcher version, decoding params
when exposed). Determinism is recorded, not assumed. **Two options, owner's call:**
- **(rec) cloud gpt-5.4-mini** — the actual prod extractor; the baseline that matters; ~$ pennies but real spend.
- **local SLM** — free, but measures a different model than prod.
The instrument + tests + survey are fully provable **without** this run; the baseline is the run's output
and needs explicit owner OK (eval-spend discipline). If the owner prefers, master runs it post-merge.

### E. Follow-up tickets (Step 5, Needs Approval, under Memory Recall Quality)
One per improvement surfaced by baseline + SOTA — each its own flag→verify→A/B against this benchmark, e.g.:
- Relationship-type **validation gate** (reject/normalize off-vocab edge types at the write path — today none exists).
- Relationship-vocab refinement (residence vs visit) *iff* the baseline shows the failure persists.
- Canonicalization/dedup tuning per the measured dedup-rate.
- Prompt/schema refinements per measured hallucination + type-accuracy.
- Description-integrity **LLM-judge** (replace the deterministic proxy).

---

## Acceptance criteria (Phase 1 — the provable definition of done)

FRE-630 cites ADR-0087 methodology, not an ADR with an AC table, so these are the instrument-phase invariants:

| # | Criterion | Proof |
|---|-----------|-------|
| AC-1 | Pure matcher (3 tiers) + metric/scoring/report core exists, green, deterministic, no LLM; edges scored over resolved ids; empty-fallback counted | `make test-k K=fre630_metrics` |
| AC-2 | Curated gold set (≥20 cases) loads + passes PII-denylist + schema + non-degeneracy + failure-mode-coverage | `make test-k K=fre630_gold_set` |
| AC-3 | Harness runs end-to-end against the real extractor (≥1 case smoke) and emits a schema-valid report stamped with model id + prompt hash + git commit + matcher/schema version | bounded run output (owner-OK'd) |
| AC-4 | SOTA survey doc committed with references + technique→change mapping | doc in `docs/research/` |
| AC-5 | Baseline table (`--samples 3`, mean/std) for the current extractor (curated) | run output — **gated on AC-3 owner OK**; else master post-merge |
| AC-6 | Improvement follow-up tickets filed (Needs Approval), incl. the deferred Phase-2 post-write graph benchmark | Linear ids in the ticket comment |

---

## Build order (TDD)

1. `test_fre630_metrics.py` fixtures + failing assertions → `matching.py` (tiered matcher) + `metrics.py` + `scoring.py` → green. (pure, fast)
2. `gold.py` schema + loader → `gold_extraction.yaml` (~24 cases, incl. `aliases`) → `test_fre630_gold_set.py` green.
3. `report.py` (stamping + per-tag + per-case diffs) + `harness.py` (`--samples N` I/O driver) → smoke against the real extractor (1 case) → schema-valid report.
4. SOTA survey doc (C).
5. Baseline run (D) — **only after owner OK** — curate table into the doc.
6. File follow-up tickets (E).
7. Quality gates: `make test` (module then full) · `make mypy` · `make ruff-check`/`format` · `pre-commit`.

## Standards / notes
- `.gitignore` must cover `telemetry/evaluation/fre630-extraction-quality/` (mirror the fre435 entry).
- No ADR-0074 identity-threading new surfaces here (eval harness, no new prod `log.*`/`bus.publish`/`MERGE`).
- **De-scoped (codex P1.6):** the root `CLAUDE.md` "qwen3-8b extraction" stale-note correction is **not**
  bundled here — flagged to master as a separate one-line doc fix so it doesn't ride this eval PR.
- Gold-set curation discipline (public repo): grounded-but-paraphrased, no verbatim transcripts, no PII/names/paths.
