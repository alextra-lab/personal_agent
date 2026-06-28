# FRE-653 — ADR-0100 Relevance-Bounded Recall (`query_memory` path)

**Ticket:** FRE-653 (Approved, Tier-2) · **ADR:** ADR-0100 · **Parent:** FRE-494 / ADR-0087 Phase 2
**Branch:** `fre-653-relevance-bounded-recall`
**Scope boundary:** `query_memory` ONLY. `query_memory_broad` seam = FRE-654. A/B + floor calibration + rollout = FRE-655 (seam). Embedder = FRE-656.

## Problem (from ADR-0100)
Three compounding defects on the automatic recall path:
1. **Recency candidacy gate** — `query_memory` appends `AND c.timestamp >= $cutoff_date` (`service.py:1475`); old turns are never candidates.
2. **Recency-ordered LIMIT** — `RETURN DISTINCT c ORDER BY c.timestamp DESC LIMIT $limit` (`:1478`); recent chatter crowds out relevant-old.
3. **Unused relevance ordering** — vector + reranker scores are computed (`_calculate_relevance_scores`, `:1595`) but the returned `conversations` keep Cypher (timestamp) order; nothing sorts by relevance.

Fix: relevance-keyed candidate generation (vector top-k over `entity_embedding` ∪ entity-name match, no cutoff), sort by combined relevance score, LIMIT after ranking, calibrated similarity floor. **Behind `relevance_bounded_recall_enabled` (default off); off == legacy exactly.** Converges on the existing `suggest_proactive_raw` pattern (`service.py:237`).

## Acceptance criteria carried by THIS ticket (ADR-0100)
- **AC-1a** — recall invariant to `recency_days` (1/30/365 all surface a >30-day positive) with flag on.
- **AC-2** — returned order is relevance-ranked: old-relevant ranks ahead of recent-irrelevant.
- **AC-3** — recent distractors don't evict the old positive (recall@k holds as distractor count grows).
- **AC-4** — query with no relevant memory returns nothing above `recall_similarity_floor`.
- **AC-6** — `memory_recall` event `empty_result` flag agrees with the actual payload (empty + non-empty fixtures).
- **AC-7** — flag off reproduces the FRE-491 baseline (the >30-day positive absent at the default 30-day cutoff).

(AC-1b broad-path and AC-5 scale-invariance belong to FRE-654/FRE-655; `candidate_set_size ≤ top_k`-style telemetry is emitted here so FRE-655 can assert it.)

## Design

### Candidate generation (flag on)
1. Vector query (already at `:1537`) → entities + cosine scores. **Floor-filter**: keep entities with `score >= recall_similarity_floor` → `relevant_entity_names`.
2. Single candidate Cypher, **cutoff clause removed**, with **per-entity turn capping** (NOT a global timestamp LIMIT — see codex Q1/Q5):
   ```cypher
   MATCH (c:Turn)-[:DISCUSSES]->(e:Entity)
   WHERE <vis_frag> AND (e.name IN $entity_names OR e.name IN $relevant_entity_names)
   WITH e, c
   ORDER BY c.timestamp DESC
   WITH e, collect(DISTINCT c)[0..$per_entity_cap] AS turns
   UNWIND turns AS c
   RETURN DISTINCT c
   LIMIT $candidate_cap
   ```
   - **Why per-entity, not global:** a global `ORDER BY timestamp DESC LIMIT` re-introduces Gate-B — a noisy entity with >cap *recent* turns would fill the candidate set and evict the old positive (AC-3 fails). Bounding turns *per entity* means recent distractors under **other** (noisy) entities cannot crowd out the positive's own (relevant) entity. This mirrors `suggest_proactive_raw`'s per-entity `collect` but keeps the top-K recent turns per entity (turn-level relevance) instead of just one.
   - `per_entity_cap = max(query.limit, 10)`. Total candidates ≤ `(|entity_names| + entities_above_floor) * per_entity_cap` — bounded by the entity count (≤ `entity_names` + `top_k`), so **scale-invariant** to KG growth. `candidate_cap` (e.g. 500) is a bare runaway backstop only — with per-entity capping the natural set is already small, so it never bites for the ACs.
   - The old positive survives as a candidate as long as its **own** relevant entity does not have >`per_entity_cap` more-recent turns (probe-controlled; holds for AC-1a/AC-3).
   - Entity-name matches (`$entity_names`) bypass the floor — they're explicit query entities (the ADR unions the entity-name match unconditionally). The floor guards the *vector-expanded* branch (AC-4). Trade-off (a weak-but-explicit entity-name hit can surface) is documented for FRE-655 floor calibration; AC-4's probe case has no entity match + sub-floor vectors → empty.
   - When there are no `entity_names` and `query_text` is None: legacy `MATCH (c:Turn)` fallback path is preserved (no behavioural change there; relevance-bounded only applies when there is a relevance signal). [codex Q5 risk 3: safe as-is.]
3. Score with existing `_calculate_relevance_scores` (recency is **already** an additive weight there — no change needed; the ADR's "recency as a weight, not a gate" is satisfied by removing the Cypher cutoff).
4. **Sort** `conversations` by `relevance_scores` desc (stable) — fixes defect 3.
5. **LIMIT after ranking**: `conversations[:query.limit]`.
6. Emit `memory_recall` event.

### Flag off
Existing code path untouched: cutoff + `ORDER BY timestamp DESC LIMIT $limit`, no sort, no floor. The `memory_recall` event is emitted in **both** modes (pure telemetry, not a recall-result behaviour change) so the empty-result prod watch and AC-6 work regardless of flag. The emit is wrapped in `try/except` so a telemetry failure can never alter recall. [codex Q4: AC-7 is verified on **result/payload parity**, not log-line parity.]

### New pure helpers (unit-testable without Neo4j)
- `_rank_conversations_by_relevance(conversations, relevance_scores) -> list[TurnNode]` — stable sort desc, missing score → 0.0.
- `_filter_entities_by_floor(vector_results, floor) -> tuple[list[str], dict[str,float]]` — names + scores with `score >= floor`.
- `_build_memory_recall_event(...) -> dict[str, Any]` — assembles event fields; `empty_result = len(returned) == 0`.

### memory_recall event fields (→ agent-logs-*)
| field | type | source |
|-------|------|--------|
| `candidate_set_size` | integer | candidate **turn** count (honest — turns, not entities; AC-5's `≤ top_k` framing is a turn/entity mismatch and belongs to FRE-655, flagged to master) |
| `vector_entity_count` | integer | entities above floor (the top-k-bounded entity set) |
| `result_count` | integer | returned turns after limit |
| `empty_result` | boolean | `result_count == 0` |
| `top_vector_score` | float | max of vector_scores (0.0 if none) |
| `median_vector_score` | float | median of vector_scores (0.0 if none) |
| `recency_span_seconds` | float | newest_hit − oldest_hit over returned turns |
| `recall_latency_ms` | float | wall-clock of the recall |
| `recalled_token_count` | integer | `estimate_tokens` over returned turn text |
| `similarity_floor` | float | `recall_similarity_floor` in effect |
| `relevance_bounded_enabled` | boolean | flag state |

Field names chosen to land correctly under the existing `agent-logs-*` dynamic_templates (`*_score`/`*_ms`/`*_seconds` → float; `*_count`/`*_size` → long). Explicit mappings added anyway (defense-in-depth, per the ES-mappings-first-pass lesson).

## Files
1. `src/personal_agent/config/settings.py` — add `relevance_bounded_recall_enabled: bool = False` and `recall_similarity_floor: float = 0.0` (ge=0, le=1) near the reranker block. Default floor 0.0 = no floor (calibration is FRE-655); config-driven, never hardcoded.
2. `src/personal_agent/memory/service.py` — `query_memory`: flag-gated candidate gen + sort + limit-after-rank + event; add the 3 helpers. `import time` for latency; reuse `estimate_tokens`.
3. `docker/elasticsearch/index-template.json` — add explicit mappings for the 10 fields above.
4. Tests (below).

## Tests (TDD — write failing first)
`tests/test_memory/test_relevance_bounded_recall.py` (new, unit — no Neo4j):
- `test_rank_conversations_by_relevance_orders_desc` — AC-2 mechanism: out-of-order scores → sorted desc; stable on ties.
- `test_rank_missing_score_treated_zero`.
- `test_filter_entities_by_floor_drops_below` — AC-4 mechanism.
- `test_build_event_empty_result_true_on_empty` / `_false_on_nonempty` — AC-6 (pure).
- `test_build_event_token_count_and_span` — span/token fields populated.
- `test_settings_defaults_off_and_zero_floor` — AC-7 default posture.

`tests/test_memory/test_memory_service.py` (extend, integration — skips w/o Neo4j):
- `test_query_memory_relevance_bounded_invariant_to_recency` — AC-1a: seed a >30-day turn for a query entity; flag on; assert present at recency_days 1/30/365.
- `test_query_memory_flag_off_reproduces_cutoff` — AC-7: flag off; >30-day turn absent at default cutoff.
- `test_query_memory_distractors_do_not_evict` — AC-3: add recent distractors **under a different (noisy) entity**; old positive (under its own relevant entity) stays in results — proves per-entity capping, not a global timestamp cap.
- `test_query_memory_relevance_ordering` — AC-2: old turn with high vector/reranker vs recent turn with low everything → old ranks first (the scenario the FRE-489 probe encodes).

ES mapping audit: `python3 -c` walk of every new field through `index-template.json` dynamic_templates, asserting float-vs-long.

## Quality gates
`make test-k K=relevance_bounded` → module → `make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Out of scope (do NOT touch)
`query_memory_broad`, `context.py`/`protocol_adapter.py` threading, weight re-tuning, floor calibration, A/B run, rollout. Default off; no deploy.

## Master review corrections (PR #268 round 1 → round 2)

A high-effort workflow review (master) surfaced 6 confirmed flag-on correctness defects. All fixed:

1. **recency_days contract (silent drop).** Documented the contract explicitly in `query_memory`: under the flag, on the entity-recall path, `recency_days` is demoted to a ranking weight (no candidacy gate) per ADR-0100 §2/§3 + AC-1a. The automatic callers pass it as a default; the explicit-window tool case is filed as **FRE-658** for the rollout ticket.
2. **candidate_cap had no top-level ORDER BY → arbitrary slice before ranking.** The candidate Cypher now computes a per-turn relevance key (`escore`: explicit name-match = 1.0, else floored vector score) and `ORDER BY turn_rel DESC, c.timestamp DESC` **before** `LIMIT $candidate_cap`, so the cap keeps the most-relevant turns. (Dynamic map-param subscript `$entity_scores[e.name]` verified on Neo4j 5.)
3. **AC tests seeded only one turn.** Added `test_query_memory_old_relevant_turn_survives_same_entity_crowding`: 8 recent + 1 old turn under the **same** entity, limit 3, mocked embedder/reranker give the old turn the content signal → it survives `[:limit]`. The proof the ticket lacked.
4. **Floor not applied to ranking scores.** `vector_scores_for_ranking = floored_vector_scores when flag on` — a below-floor entity no longer boosts ranking (AC-4 consistency).
5. **Quality metrics on pre-slice set.** After ranking+slice, `relevance_scores` is restricted to the returned set, so `_log_query_quality_metrics`, `MemoryQueryResult`, and `memory_query_completed` all report the post-slice count (asserted in the new test: `len(relevance_scores) == len(conversations) <= limit`).
6. **Reorder gated only on the flag.** Now gated on `relevance_bounded and entity_recall`, so flag-on id-lookups/bare-fallback keep legacy behaviour (`test_query_memory_flag_on_id_lookup_not_reordered`).
7. **(rec) Magic numbers → settings.** `recall_per_entity_turn_cap` (10) and `recall_candidate_cap` (500) are now config Fields alongside `recall_similarity_floor`, so FRE-655 can calibrate without a code change.
