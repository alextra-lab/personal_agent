# FRE-1477 — Calibrate the proactive relevance bound, and gate ahead of the score combination

**Ticket:** FRE-1477 (Approved, `stream:build2`, `Tier-1:Opus`)
**Backing:** ADR-0148 D4 · FRE-1287 (the shipped half) · FRE-694 (`docs/research/2026-06-29-fre-694-embedder-separation.md`) · FRE-724 (the lexical augment) · FRE-1118 (umbrella)
**Diff class:** self-serve — no production write path, no destructive code, no schema change, no cost or governance code. The calibration writes to the **test** substrate only (FRE-375).
**Risk tier:** Complex. Codex plan-review completed 2026-09-10; revision 2 folds in all six findings plus one this session found independently (D-G).

---

## 0. Outcome — Branch B (§ D-F)

The calibration ran against the serving arm and **found no admissible bound**. On
`Qwen3-Embedding-8B` @ 1024 the lowest bound rejecting the median top-ranked non-match (0.6798)
admits 84.2% of the 57 labelled positives, below D4's 90%. Both parity checks passed first
(P1 max Δ 0.0107, P2 max Δ 0.0097), so the measurement is trustworthy and the verdict stands.

Everything below shipped. The bound did not: `proactive_memory_relevance_bound` is `None`, the gate
is inert, and **FRE-1477 is not dischargeable on this merge** — D4 hands the decision to the owner.
Full result and the three courses open: `docs/research/2026-09-10-fre-1477-proactive-relevance-calibration.md`.

One correction the run itself produced: the first attempt passed P1 while comparing 4096-dimension
offline vectors against 1024-dimension index vectors. It passed *inside* tolerance, and only the
consistent direction of the three deltas revealed it. Recorded in the research note, because it is
the failure mode a parity gate exists to catch.

---

## 1. Scope

1. A hard relevance gate ahead of `_combine_scores` in `memory/proactive.py`, with its own drop reason.
2. A calibration harness that measures the bound against the **serving** embedder arm and commits its output.
3. A `config_guard` check binding the configured bound to that committed artifact.
4. Repair of the FRE-1287 fixture that pinned `vector_score=0.5` (orthogonal) and never saw the case it was written for.
5. **Fold-in (D-G):** the FRE-724 lexical augment enters candidates carrying a *constant* in the `vector_score` field. The gate must not treat a constant as a relevance measurement.

Out of scope: broad recall (FRE-1479) and entity match (FRE-1480). This ticket blocks both.

---

## 2. Facts established before planning

| Fact | Evidence |
|---|---|
| Serving embedder arm is `Qwen3-Embedding-8B` at OVH, `embedding_dimensions=1024` | `/opt/seshat/.env:675-678`; `settings.embedding_dimensions` default 1024 |
| The arm is reachable and answers | One-text probe, HTTP 200, native width 4096; production requests `dimensions=1024` (`memory/embeddings.py:172-180`) |
| Its credentials are in `pass` | `seshat/AGENT_OVH_AI_BASE_URL`, `seshat/AGENT_MANAGED_EMBEDDING_TOKEN` — the pattern `_voyage_key()` already uses |
| No 0.6B embedder serves | No `:8503` container in `docker ps` |
| Test substrate is up | `build2-neo4j-test-1` :7688 · `build-elasticsearch-test-1` :9201 · `build-postgres-test-1` :5433 |
| The probe set is 54 cases / 49 entities / 57 labelled positives | `scripts/eval/fre435_memory_recall/semantic_probe.yaml` |
| A Neo4j `calibrate` path already exists | `ab_relevance_bounded.py:306` — co-seeds all cases, queries `_query_entity_vector_candidates` |
| An independently authored offline harness exists | `separation_benchmark.py` — its own `_entity_text`, `truncate_renormalize`, `_score`, `_score_dim` |
| The proactive `vector_score` is the same index score | `service.py:1000-1016` yields `score AS vector_score`, copied untransformed at `:1049-1054` |
| `_normalize_vector_score` is called once, and its local feeds the combination | `proactive.py:321`, consumed at `:334` |
| `ProactiveMemoryDiscard.relevance_score` is `float \| None`, documented as "None when the gate fired before scoring" | `proactive_types.py:60-65` |
| `config_guard` only reports; it never mutates settings | `run_all_checks` returns `list[Finding]` (`config_guard.py:1502`) |
| **The lexical augment is live in production and injects a constant** | `.env:682` `MULTIPATH_RECALL_ENABLED=true`, `.env:685` `RECALL_SIMILARITY_FLOOR=0.60`; `service.py:1124` `baseline = recall_similarity_floor`, written as `vector_score` at `:1167` |

---

## 3. Design decisions

### D-A — The bound lives in normalized embedding-term space

AC-3 requires the value the gate compares to be the output of `_normalize_vector_score`, which is
`max(0, 2x - 1)` over the Neo4j score. The configured bound therefore lives in that space.

The calibration reports **both** `bound_neo4j_space` and `bound_embedding_term`. `config_guard` compares
`bound_embedding_term` to the configured field. AC-1's fixture is stated in Neo4j score space, as the ticket
writes it, and the gate normalizes it. Confirmed sound in plan review.

### D-B — The parity gate: two assertions, not one

The ticket asks for "the same hard parity gate FRE-694 used against the Neo4j `calibrate` path".

Revision 1 proposed a single index-vs-client-side pairwise comparison. Plan review found that unsound, and
it was right: FRE-694's gate compared an **independently authored** offline harness against a **separately
run** Neo4j calibrate, on three aggregates. That structure catches shared corpus-construction,
text-formatting, embedding-mode and aggregation error. A same-run comparison of two views of one pipeline
cannot catch any of those.

A literal re-run is still impossible — `_FRE670_CALIBRATE_06B` (`separation_benchmark.py:285`) is a 0.6B
number and no 0.6B endpoint serves. What transfers is the **structure**, and the structure is available on
the serving arm because the independently authored harness already exists.

The gate is therefore two assertions, both at FRE-694's own tolerance of 0.02:

- **P1 — cross-implementation, FRE-694's structure.** Run `separation_benchmark.py`'s offline pipeline on
  the serving arm (its own corpus build, its own `"{name}: {description}"` formatting, its own query mode,
  its own `(cos+1)/2`, its own per-case aggregation) and compare its **positive median, negative median and
  negative maximum** against the same three from the Neo4j calibrate run. Two independent implementations,
  compared on aggregates — FRE-694's check with the reference measured now instead of in June.
- **P2 — index fidelity, FRE-694's stated gap.** Per candidate pair, the Neo4j index score against the
  client-side cosine over the same production embeddings. This closes FRE-694's own caveat: *"this is an
  embedding-geometry test, not a Neo4j-HNSW index-fidelity test."*

Both must pass before any number is trusted. The artifact records, for P1, the six aggregates, and for P2
the **independently recorded score pairs** — not the deltas — so AC-5's re-run recomputes them rather than
re-reading a stored conclusion.

### D-C — The positive set is per-expected-entity, taken whole

FRE-694's metric is *"per-expected-entity positives vs each query's strongest non-match negative"*. The
existing `calibrate` path takes a per-case **max** positive, a different denominator. AC-4 names the whole
labelled positive set, so the harness uses FRE-694's metric.

`top_k` covers the whole corpus (49 entities, `top_k = max(vector_top_k, 2 × cases) = 108`), so every
labelled positive is measurable. A labelled positive the index does not return contributes no score and
counts as **not admitted**, never as an invented score.

The artifact stores the **complete positive score population** (all 57 values) and the complete negative
population, so AC-4 reapplies the bound to the real list rather than to a stored share.

### D-D — Predicate, placement, and the discard record

The predicate:

```
gate fires when:  gate_enabled  and  bound is not None
                  and overlap <= 0.0  and  topic <= 0.0
                  and relevance_value < bound
```

`<` matches the ticket's stated convention, mirroring the dense arm's `>=` at `service.py:5020`.

**Loop order.** Plan review correctly noted that `_combine_scores` currently runs at `proactive.py:334`,
*before* the empty-description filter at `:339`. The loop is reordered to:

1. Compute the four subscore inputs (unchanged).
2. Empty-description filter (FRE-1114) — stays earliest, and **keeps its current record exactly**, including
   its computed `relevance_score`. It calls `_combine_scores` inside its own branch, which then `continue`s.
   That candidate is already rejected by an unrelated filter; the combination there only labels the record.
3. **The relevance gate.** `_combine_scores` is never called for a gated candidate, so recency cannot
   compensate — which is D4's actual requirement.
4. `final = _combine_scores(...)`, then the `min_score` threshold, unchanged.

**The discard record.** One `ProactiveMemoryDiscard` per gated candidate, `drop_reason=RECALL_RELEVANCE_BOUND`
and `relevance_score=None` — the field is `float | None` and its docstring already reserves `None` for
"the gate fired before scoring" (`proactive_types.py:60-65`). A conservation test asserts
`len(emitted) + len(discarded) == deduped_candidate_count` across the new boundary.

### D-E — Two new settings, and where the measured value lives

- `proactive_memory_relevance_bound: float | None` — **its default is the calibrated value**, with the
  artifact path named in the field description. Plan review found revision 1 had no committed source for the
  runtime value; this is it. A hand-written constant is what AC-5 fails *only when no artifact stands behind
  it*; here one does, and `config_guard` asserts they agree.
  In Branch B (§ D-F) the default stays `None`.
- `proactive_memory_relevance_gate_enabled: bool = True` — AC-2's companion needs the same fixture admitted
  with the gate off.

An operator can still override the bound through `AGENT_PROACTIVE_MEMORY_RELEVANCE_BOUND`. That is precisely
the disagreement AC-5's finding exists to catch, so the two must remain independently settable.

### D-F — When the calibration finds no admissible bound

D4 reserves this case and AC-4 fails a bound configured in it. The harness reports the incompatibility and
stops. The artifact records `bound: null`, `incompatible: true`, and the reason. The configured value stays
`None` and the gate stays inert.

Plan review found revision 1 self-contradictory here, and it was: step 8 demanded a clean `config_guard` run
while D-F demanded a finding. Resolved as follows.

- `config_guard` raises `relevance_bound_calibration_incompatible` in Branch B. Step 8's verification is
  **"the guard's findings are exactly the expected set"** — empty in Branch A, exactly that one finding in
  Branch B — not "empty".
- **Branch B does not discharge the ticket.** AC-1 asserts rejection at a calibrated bound; with no bound
  there is nothing to assert. The deliverable is then the harness, the artifact recording the
  incompatibility, the inert gate, the guard finding and the repaired fixture, and the handoff states
  plainly that FRE-1477 needs an owner decision — a reranker-side bound or a different arm — exactly as D4
  requires. It is not marked complete.

FRE-694's verdict ("no embedder opens a clean floor") makes Branch B a live possibility. Its 8B arm measured
pos median 0.738, neg median 0.662, pos p5 0.649. **Step 6 runs before the gate is written**, so the measured
outcome drives the rest of the plan rather than the reverse.

### D-G — A constant in `vector_score` is not a relevance measurement (fold-in)

Found this session, outside the plan review.

`_augment_proactive_with_lexical` (`service.py:1080-1178`, FRE-724) appends lexical-arm entity hits to the
proactive rows with `vector_score = recall_similarity_floor` — a configuration constant, not a measurement
(`:1124`, written at `:1167`). Production runs this path: `MULTIPATH_RECALL_ENABLED=true` and
`RECALL_SIMILARITY_FLOOR=0.60`.

Two consequences.

1. **Today, unrelated to this ticket.** Such a candidate already carries an embedding term of
   `max(0, 2×0.60 − 1) = 0.20` on no embedding evidence. That is a surviving floor of exactly the kind
   FRE-1287 removed; FRE-1287 removed the *subscore* floors, and this one enters upstream in the row, so it
   was untouched.
2. **For this gate.** Whether a lexical-only candidate passes would depend on where the calibrated bound
   falls relative to an unrelated constant. The gate would be deciding on a number that is not a relevance
   measurement — the pathology D4 exists to stop.

**Treatment.** The row carries its provenance: `suggest_proactive_raw` sets `vector_score_measured: True`
on dense-arm rows and `False` on lexical-augment rows. The gate treats an unmeasured score as no relevance
evidence, so a lexical-only candidate with zero overlap and zero topic is rejected with
`RECALL_RELEVANCE_BOUND`. This is D4's own rule applied where it lands, not a new policy.

Scope: this is the proactive path's admission boundary, which is the ticket's subject, and it is required for
the gate to be correct. Folded in per the lifecycle rule, and named in the handoff. It does not change the
lexical arm's own behaviour, the combination, or any other path.

---

## 4. Files

| File | Change |
|---|---|
| `src/personal_agent/captains_log/turn_evidence.py` | New `DropReason.RECALL_RELEVANCE_BOUND` |
| `src/personal_agent/config/settings.py` | Two new fields (D-E) |
| `src/personal_agent/memory/proactive.py` | The gate + loop reorder (D-D), provenance read (D-G) |
| `src/personal_agent/memory/service.py` | `vector_score_measured` on both proactive row builders (D-G) |
| `src/personal_agent/config/calibration.py` | **New** — frozen model + loader for the committed artifact |
| `config/calibration/proactive_relevance_bound.json` | **New** — the committed artifact |
| `src/personal_agent/config/config_guard.py` | **New** `check_relevance_bound_calibration`, registered in `run_all_checks` |
| `scripts/eval/fre1477_relevance_calibration/{__init__,calibrate,analysis}.py`, `README.md` | **New** — the harness; `analysis.py` holds the pure functions |
| `docs/research/2026-09-10-fre-1477-proactive-relevance-calibration.md` | **New** — the narrative record |
| `tests/personal_agent/memory/test_proactive_scoring_floors.py` | Fixture repair |
| `tests/personal_agent/memory/test_proactive_relevance_gate.py` | **New** — AC-1, AC-2, AC-3, AC-6, AC-7, conservation, D-G |
| `tests/personal_agent/config/test_relevance_bound_calibration.py` | **New** — AC-4, AC-5 |
| `tests/scripts/eval/test_fre1477_calibration_analysis.py` | **New** — the harness's pure functions |

---

## 5. Steps

Steps 1–5 are pure and run offline. Step 6 is the live measurement and gates everything after it.

1. **`DropReason.RECALL_RELEVANCE_BOUND`.** Add the member and its docstring entry.
   *Verify:* `uv run pytest tests/personal_agent/captains_log -q` passes.

2. **The two settings** (D-E), both with descriptions (`check_field_descriptions` requires one).
   *Verify:* `uv run python -c "from personal_agent.config import settings; print(settings.proactive_memory_relevance_bound)"`.

3. **`config/calibration.py`.** Frozen Pydantic model over the artifact schema:
   `component {role, model, dimensions}` · `measured_on` · `probe_set` · `bound_neo4j_space | None` ·
   `bound_embedding_term | None` · `incompatible: bool` · `incompatible_reason | None` ·
   `positive_scores: list[float]` · `negative_scores: list[float]` · `positive_admitted_share | None` ·
   `negative_median_neo4j` · `parity {tolerance, p1_aggregates: {...}, p2_pairs: [[index, offline], ...]}`.
   Loader resolves from `repo_root() / "config/calibration"`.
   *Verify:* a unit test round-trips the committed artifact.

4. **The harness's pure core** (`analysis.py`):
   - `choose_bound(positives, negatives, *, min_positive_share, step)` — the lowest bound strictly above
     `median(negatives)` admitting `>= min_positive_share` of `positives`, or the incompatibility.
   - `parity_aggregates(positives, negatives)` — the three FRE-694 statistics.
   - `parity_deltas(pairs)` — recomputes P2 deltas from recorded pairs.
   `_normalize_vector_score` is **imported** from `memory.proactive`, never re-implemented.
   *Verify:* `uv run pytest tests/scripts/eval/test_fre1477_calibration_analysis.py -q`, including a seeded
   incompatible case that must return the incompatibility rather than a number.

5. **The harness driver** (`calibrate.py`). Pins the test substrate at module top (mirrors
   `ab_relevance_bounded.py:110-123` exactly), reads endpoint and token from `pass`, seeds via `seed_replay`,
   queries `_query_entity_vector_candidates`, computes the client-side side, runs P1 and P2, calls
   `choose_bound`, writes the artifact and a markdown summary.
   *Verify:* `uv run ruff check scripts/eval/fre1477_relevance_calibration/` clean; `--dry-run` lists the
   corpus without embedding.

6. **Run the calibration against the serving arm.** ~103 embedding calls plus the P1 offline pass.
   *Verify:* P1 and P2 both within 0.02, printed and recorded; artifact written; distributions reported.
   → **Branch A** (a bound exists): configure it as the field default, continue at step 7.
   → **Branch B** (incompatible): artifact records it, bound stays `None`, continue at step 7 with the gate
   inert, and § D-F governs the handoff.

7. **The gate + loop reorder** (D-D) and the provenance flag (D-G), TDD: the AC-1 test first, confirmed
   failing.
   *Verify:* `uv run pytest tests/personal_agent/memory/test_proactive_relevance_gate.py -q`.

8. **`config_guard.check_relevance_bound_calibration`.** Findings for: configured bound with no artifact ·
   artifact component ≠ serving arm · artifact bound ≠ configured value · artifact reports incompatibility.
   Registered in `run_all_checks`.
   *Verify:* `uv run pytest tests/personal_agent/config/test_relevance_bound_calibration.py -q`; then
   `uv run python -m personal_agent.config.config_guard` produces **exactly the expected finding set**
   (empty in Branch A, one incompatibility finding in Branch B).

9. **Repair the FRE-1287 fixture.** `test_proactive_scoring_floors.py:141` moves from
   `vector_score=0.5,  # orthogonal` to the calibrated median top-ranked non-match, and its hardcoded
   `2026-08-25` timestamp becomes a same-instant timestamp so recency does not decay with the calendar.
   *Verify:* the repaired test fails against `main`'s `proactive.py` and passes with the gate.

10. **Docs.** The research note; FRE-1477's rows in ADR-0148's affected-files list if absent.

11. **Gates.** `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

---

## 6. Acceptance criteria → evidence

Every test below reads the bound and the distributions **from the committed artifact**, so no test can drift
from the calibration by restating its number.

| AC | How it is discharged |
|---|---|
| **AC-1** — gate rejects at the measured non-match | `test_median_top_ranked_non_match_is_rejected` — `vector_score` = the artifact's `negative_median_neo4j`, zero overlap, zero topic, same-instant timestamp, `deployed_scoring` weights. |
| **AC-2** — that test can fail | `test_same_fixture_is_admitted_with_the_gate_disabled` — identical row, `proactive_memory_relevance_gate_enabled=False`; asserts admission. |
| **AC-3** — gated value is the path's own scorer output | **Sentinel, not a recording wrapper** (plan-review finding: a wrapper passes even if the gate reads the raw row, because the combination already calls the normalizer). `test_gate_reads_the_normalizer_not_the_raw_row` monkeypatches `_normalize_vector_score` to return a value that **flips the decision** — raw row below the bound, normalizer's return above it. The candidate must be admitted. A gate comparing the raw value rejects it and the test fails. |
| **AC-4** — bound does not suppress genuine relevance | `test_configured_bound_admits_the_whole_positive_set` applies the bound to the artifact's **complete `positive_scores` list** (D-C) and asserts `share >= 0.90`; `test_configured_bound_rejects_the_median_non_match` asserts the other half. Both must hold. Branch B: `test_no_bound_is_configured_when_the_calibration_reports_incompatibility`. |
| **AC-5** — configured value traces to a committed artifact | `test_artifact_names_the_serving_arm` · `test_artifact_bound_equals_the_configured_value` · `test_recorded_parity_assertion_holds` — **recomputes** P1 aggregates and P2 deltas from the recorded populations and score pairs, then asserts against the tolerance (plan-review finding: asserting stored deltas against a stored tolerance is tautological) · `test_induced_arm_mismatch_raises_a_finding_and_leaves_the_bound_in_force` mutates a copy's component, asserts a `Finding` **and** that `settings.proactive_memory_relevance_bound` is unchanged. |
| **AC-6** — relevance rejection distinguishable | `test_gate_rejection_carries_its_own_drop_reason` — `drop_reason is DropReason.RECALL_RELEVANCE_BOUND`, explicitly `is not DropReason.RECALL_SCORE_THRESHOLD`, and `relevance_score is None`. |
| **AC-7** — genuinely relevant candidates still admitted | **Per-probe, not aggregate** (plan-review finding; ADR-0148 AC-6 rejects an aggregate that holds while individual probes swap). `test_no_relevant_probe_loses_admission` drives each of the artifact's labelled positives as its own single-row turn through `build_proactive_suggestions`, gate off then gate on, and asserts **no probe identifier** admitted with the gate off is rejected with it on. One row per turn also removes the candidate/item caps as a confound. |
| **Conservation** (D-D) | `test_gated_candidates_are_conserved_in_the_discard_record` — emitted + discarded equals the deduplicated candidate count across the new boundary. |
| **D-G** | `test_a_lexical_baseline_score_is_not_relevance_evidence` — a row with `vector_score_measured=False` and zero overlap / zero topic is rejected, whatever the constant's value. |

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| No bound separates the populations on the serving arm | D-F. Step 6 runs before the gate is written. The incompatibility is reported to the owner and the ticket is not marked complete. |
| The parity gate is weaker than FRE-694's | D-B carries both assertions: P1 reproduces FRE-694's cross-implementation structure, P2 adds the index fidelity FRE-694 explicitly did not test. |
| The calibration writes to production substrate | Test substrate pinned at module top before any `personal_agent` import; `wipe_substrate` refuses outside `Environment.TEST`. |
| A deployment identifier leaks into the public repo | Endpoint and token come from `pass` at run time. Neither harness nor artifact records a hostname; the artifact names the **model and dimension**, which is what AC-5 asks for. |
| The gate suppresses a candidate that overlap or topic would have carried | The predicate fires only at `overlap == 0` **and** `topic == 0`. Any overlap or topic evidence leaves the candidate untouched. |
| The loop reorder changes an existing telemetry record | The empty-description branch keeps its exact current construction, including its computed `relevance_score` (D-D step 2). Only the new gate's record is new. |
| D-G changes recall behaviour beyond the ticket | It changes admission only for a lexical-only candidate that also has zero overlap and zero topic — the population D4 forbids admitting. Named in the handoff for master. |
