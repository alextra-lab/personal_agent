# FRE-1545 — Proactive memory: gate admission on the reranker score

Ticket: FRE-1545 (Approved, owner decision 2026-10-03). Design intent: ADR-0148 D4. Follow-up to
FRE-1477 (embedder gate, inert: no admissible bound) and FRE-1479/FRE-1480 (reranker gates on
broad recall and entity match).

## Scope

- Give every proactive candidate a reranker score, computed upstream of the synchronous
  `build_proactive_suggestions`, in the async adapter.
- Gate admission on that score ahead of `_combine_scores`, for the candidates ADR-0148 D4's
  proactive clause binds (zero entity overlap and zero topic hits), so recency cannot compensate.
- No score (reranker disabled, raising, passthrough, omitted index, fallback model) → the
  candidate is dropped as `RECALL_RELEVANCE_UNAVAILABLE`, and the turn composes UNAVAILABLE.
- Calibrate the bound on the proactive population (probe set `semantic_probe.yaml`, the FRE-1477
  labelled set), commit the artifact, bind it with a `config_guard` check.
- Measure the added latency per proactive call, p50 and p95.

## Design decisions

### D1 — Where the score is computed: upstream, in the async adapter

`ProtocolAdapter.suggest_relevant` (`memory/protocol_adapter.py:330`) is already async and is the
only production caller of `build_proactive_suggestions`. It holds the query text and the raw rows
after `suggest_proactive_raw`. A new async function `score_proactive_relevance(raw_rows, query_text,
*, trace_id, session_id)` in `memory/proactive.py` does one `rerank()` call over the deduplicated
candidates and returns `Mapping[tuple[str, str], RelevanceValue]` keyed by the existing
kind-qualified identity (`_candidate_identity`). `build_proactive_suggestions` stays synchronous
and pure, with a new keyword `relevance: Mapping[...] | None = None`.

Rejected: an async `build_proactive_suggestions`. It mixes I/O into the selection logic and
changes about 40 synchronous test call sites for no gain.

Latency cost: one serial `rerank()` call per proactive turn, after the raw query (it needs the
rows). Expected about 250 ms (FRE-695 measured Voyage at that). AC-7 measures it. The call is
skipped when the gate is inert (bound None or `proactive_memory_relevance_gate_enabled` off), so
an unarmed deployment pays nothing. Worst case on a Voyage outage is the existing `rerank()`
chain: 10 s primary timeout, then the 30 s fallback. The fallback model's scores do not match the
calibrated model, so the gate reports UNAVAILABLE. This is the same exposure broad recall has
today. No new timeout is added (no second client).

`top_k=len(documents)` is passed explicitly. `rerank()` otherwise defaults to
`reranker_top_k=10` and would return 10 scores for up to 40 candidates.

### D2 — Reuse: `rerank()` plus one extracted provenance helper

The ticket names `rerank()` and the `_rerank_fused_items` plumbing. The proactive scorer calls
`rerank()` directly (the one client). The provenance filter in `_rerank_fused_items`
(`service.py:5627-5631`: index in range, `model_id is not None`) is extracted to
`memory/reranker.py::measured_scores(results, count) -> dict[int, RelevanceValue]` and used by
both. Behaviour of `_rerank_fused_items` does not change.

`_rerank_fused_items` itself is not called, for three reasons:
1. It needs entity `elementId`s. Proactive rows carry none.
2. It does a second Neo4j round trip for texts the rows already hold (latency).
3. It skips a set of one item (`len(items) <= 1`), so a one-candidate proactive turn would be
   UNAVAILABLE for no reason.

Documents are built in Python, identical to `_resolve_item_texts` (`service.py:5525-5546`):
- entity: `(name or '') + ' ' + (description or '')`
- episode: `summary` if not None, else `user_message` if not None, else `''` (Cypher `coalesce`).

The FRE-1479 calibration validated exactly these two document shapes.

### D3 — The gate, its setting and its artifact

- New setting `proactive_memory_rerank_relevance_bound: float | None` (reranker score space),
  default = the calibrated bound. Reuse the existing flag `proactive_memory_relevance_gate_enabled`
  as the path's relevance-gate switch (off restores pre-FRE-1477 behaviour, AC-2's control).
- New artifact `config/calibration/proactive_rerank_relevance_bound.json`
  (`RerankerRelevanceCalibration` schema), new constant `PROACTIVE_RERANK_RELEVANCE_BOUND_FILE`.
- New `config_guard.check_proactive_rerank_bound_calibration`, a wrapper over the existing
  `_check_reranker_bound_calibration` (the same five states), registered beside the others.
- The FRE-1477 embedding-term gate stays as it is: inert (`None`), its incompatibility committed.
  It runs after the new gate. Retiring it is not in scope.

Verdict: one predicate. `context._relevance_verdict`'s body moves to
`memory/relevance_gate.py::relevance_verdict(score, model, *, bound, gate_enabled,
calibrated_model) -> DropReason | None`. `context._relevance_verdict(item, ...)` stays as a thin
wrapper (same signature, same tests). `memory/` must not import `request_gateway/`, so the move is
required for the proactive path to share the rule.

Calibrated model: `context._calibrated_reranker_model` (cached artifact read) moves to
`config/calibration.py::calibrated_reranker_model(filename)`. The context name stays and
delegates, so the existing test patches still bind.

Proactive predicate, in `build_proactive_suggestions`, after the empty-description filter and
before the FRE-1477 gate and `_combine_scores`:

```
if gate armed and overlap == 0 and topic == 0:
    value = relevance.get(identity) if relevance else None
    verdict = relevance_verdict(value.score / value.model or None, bound, enabled, calibrated)
    if verdict: discard(relevance_score=None, drop_reason=verdict); continue
```

The overlap/topic condition is ADR-0148 D4's proactive clause verbatim ("a candidate with no
entity overlap, no topic hit, and [a relevance value] below the bound is not admitted"). A
candidate with overlap or a topic hit carries relevance evidence other than recency and is not
gated, as under FRE-1477.

### D4 — No score

A bound candidate with no `RelevanceValue`, or with a value from a model other than the calibrated
one, is dropped as `RECALL_RELEVANCE_UNAVAILABLE` (from `relevance_verdict`). `relevance=None`
(the default) means "not scored", so a caller that forgets the mapping fails safe.

`context._query_memory_for_intent`: after the proactive call, append `_unavailable_cause(discards)`
to `failure_causes`. Every later return (proactive success, no entity names, entity-match
fall-through) then composes UNAVAILABLE. Today a fully discarded proactive result falls through
and can compose NOTHING_RELEVANT.

The scorer catches a raising `rerank()` (it never raises today, but `_rerank_fused_items` guards
the same way), logs `proactive_rerank_failed`, and returns an empty mapping. It logs
`proactive_memory_reranked` (trace_id, session_id, candidate_count, scored_count, model, rerank_ms)
on success, for live latency observation.

### D5 — Calibration harness

`scripts/eval/fre1545_proactive_reranker_calibration/{__init__.py,calibrate.py,README.md}`. Test
substrate only (FRE-375 pins copied from FRE-1479). Reranker and managed embedder pinned from `pass`
as in FRE-1479.

Population — the proactive path's own candidates, through production code:
1. Seed the probe corpus (`seed_replay`), as FRE-1477/FRE-1479 do.
2. Per probe query: production `generate_embedding(mode="query")` → production
   `suggest_proactive_raw` with a fresh session id (every seeded turn is cross-session), with
   `AGENT_PROACTIVE_MEMORY_VECTOR_TOP_K=100` pinned so every labelled positive enters (FRE-1477
   widened top_k the same way; the corpus is 49 entities) → production
   `score_proactive_relevance`. Fail loud if any candidate is unscored or scored by a model other
   than the serving one (a passthrough or truncated response would describe nothing).
3. Labels: an entity candidate is positive if its name is an expected name. An episode candidate
   is positive if any entity its turn `DISCUSSES` is expected (FRE-1479's turn rule). Negative:
   per query, the strongest non-match over both kinds.
4. `choose_bound` from the FRE-1479 analysis module (the same D4 rule). Incompatible → write the
   artifact with no bound and stop (no number picked).

Checks (all must pass before a number is reported), reusing FRE-1479 helpers:
- P1: the proactive document builder plus `rerank()` over every corpus entity, vs
  `separation_benchmark`'s independent Voyage arm. This validates the instrument on the population
  both sides build. The proactive population is then drawn with the validated instrument.
- P2R: FRE-695 sanity (`_run_sanity`).
- P3: `_listwise_sensitivity` — score movement when the wide set shrinks to the input cap.
  The harness's wide set (top_k 100) is wider than production's (top_k 20).

Latency (AC-7): a second pass per query with production width — the dense rows cut to
`proactive_memory_vector_top_k` (20) in their returned order, plus the lexical rows — timing the
production `score_proactive_relevance` call. Report p50 and p95 over the 54 queries. Printed by the
harness and recorded in the research note (the artifact schema has no latency field and does not
need one).

Spend: about 54 + 54 + 54 rerank calls + sanity + P3, and about 200 embedding calls. Cents.

### D6 — Research note

`docs/research/2026-10-04-fre-1545-proactive-reranker-calibration.md`: the measured bound, the
admitted share (total, entity, episode), the negative median, the parity results, the latency
p50/p95, and the comparison with broad recall's 0.338891.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 | `test_proactive_rerank_bound_calibration.py`: the committed artifact's `positive_scores` admit ≥ 90% at the configured bound, and `negative_median_rerank < bound`. Also the harness output. |
| AC-2 | `test_proactive_rerank_gate.py`: candidate with reranker score = artifact `negative_median_rerank`, calibrated model, zero overlap, zero topic, timestamp now, deployed default weights, vector score at FRE-1477's measured non-match → dropped `RECALL_RELEVANCE_BOUND`. Same fixture with `proactive_memory_relevance_gate_enabled=False` → admitted. |
| AC-3 | Scorer test: mocked `rerank()` returns scores out of input order (sorted desc, as the real call does). Each candidate's `RelevanceValue.score` equals the score at its own `index`. A gate test: two candidates, scores either side of the bound, only the right one admitted. |
| AC-4 | (a) `reranker_enabled=False` (real `rerank()` passthrough, model None) and (b) `rerank` raising: zero-evidence candidates dropped `RECALL_RELEVANCE_UNAVAILABLE`, none admitted. (c) `_query_memory_for_intent` with those discards → stage report FAILED with `recall_relevance_unavailable` (UNAVAILABLE), not COMPLETED, on both the fall-through and the proactive-success return. |
| AC-5 | Committed artifact names `rerank-2.5`, date, probe set, bound == setting default. Guard test: artifact naming a non-serving model → `proactive_rerank_bound_calibration_stale` finding. Plus missing / mismatch / malformed / incompatible states. |
| AC-6 | Gate test: a reranker-bound rejection's discard carries `RECALL_RELEVANCE_BOUND`, never `RECALL_SCORE_THRESHOLD`. |
| AC-7 | Harness latency pass: p50/p95 of `score_proactive_relevance` at production width, on the test substrate with the serving reranker. Reported in the research note and the handoff. |

## Steps

1. `memory/reranker.py`: add `measured_scores`. Refactor `_rerank_fused_items` to use it.
   → `make test-file FILE=tests/personal_agent/memory/test_broad_recall_relevance.py` green.
2. `memory/relevance_gate.py` + `config/calibration.py::calibrated_reranker_model`; context
   wrappers delegate. → `make test-k K=relevance` green.
3. Settings field, calibration constant, guard wrapper + registration. Tests first
   (`tests/personal_agent/config/test_proactive_rerank_bound_calibration.py`).
4. `memory/proactive.py`: `_proactive_document`, `score_proactive_relevance`, the gate, the
   `relevance` keyword. Tests first (`tests/personal_agent/memory/test_proactive_rerank_gate.py`).
   Existing proactive tests that rely on the gate being absent get the new bound pinned to None
   in a module fixture (they test other gates).
5. `memory/protocol_adapter.py`: call the scorer, pass the mapping. `request_gateway/context.py`:
   the unavailable cause. Tests.
6. Harness + README. Run it on the test stack (the test stack is up). Commit the artifact. Set the
   setting default to the artifact bound.
7. Research note. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.

## Risk

Standard/Complex: `src/` memory logic, a paid call per proactive turn (production has
`AGENT_PROACTIVE_MEMORY_ENABLED=true`), and a change to which items reach the model. Codex
plan-review required.

## Codex plan review (2026-10-04, task-mutabli9-7qrdbz) — dispositions

1. OK. The scorer must reuse the build's split/dedupe loop → one shared `_deduped_candidates`.
2. OK. Build documents from the raw row, not the episode payload (the payload truncates a falsey
   summary into `user_message`, which is not Cypher-equivalent).
3. OK.
4. PROBLEM, accepted: append the unavailable cause only when it is not None (`",".join` on a
   `None` raises and collapses to `memory_query_failed`).
5. PROBLEM, accepted: a top-100 dense retrieval suppresses lexical hits that sit in dense ranks
   21–100, so cutting it to 20 does not recreate production. The latency pass runs a real top-20
   retrieval. P3 compares, per shared candidate, the wide-set score with the production-width
   score over every query, instead of the FRE-1479 helper (which caps at `reranker_input_cap`).
6. PROBLEM, accepted: add tests for an empty episode document, duplicate identities, lexical-only
   rows, and the adapter with the scorer mocked. Pin the new bound to None in every existing
   proactive test module that relies on the gate being absent.
