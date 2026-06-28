# FRE-654 — ADR-0100 Relevance-Bounded Recall: `query_memory_broad` seam

**Ticket:** FRE-654 (Approved, Tier-2) · **ADR:** ADR-0100 · **Parent:** FRE-494 · **Depends on:** FRE-653 (merged)
**Branch:** `fre-654-query-memory-broad-seam`
**Scope boundary:** the broad-recall path (`MEMORY_RECALL` intent) ONLY. Reuses the FRE-653 flag, floor, and `_filter_entities_by_floor` helper. A/B + calibration + rollout = FRE-655 (seam).

## Problem (from ADR-0100 §"Two paths, one bounded seam")
`query_memory_broad` (the `MEMORY_RECALL` intent path) has **no `query_text`/embedding param and no vector step** — its entity candidate generation is recency-only Cypher (`t.timestamp >= $cutoff`, 90-day window). So an entity discussed only >90 days ago can never surface on the broad path, even with the FRE-653 flag on. This ticket threads `query_text` in and brings the broad entity query onto the relevance-keyed path.

## Acceptance criterion carried by THIS ticket (ADR-0100)
- **AC-1b** — with the flag on and `query_text` threaded in, a `MEMORY_RECALL`-style probe whose relevant turn is **>90 days old** appears in `recall_broad()` results, asserted as a matching **entity name** in the result's `entities` field. *Fails if* the broad path still returns only within-window entities (proves the broad seam landed, not just the `query_memory` half).

## Design

### `query_memory_broad` (service.py) — add `query_text: str | None = None`
When `relevance_bounded_recall_enabled` **and** `query_text`:
1. Embed `query_text`; run the entity_embedding vector top-k via a **new helper** `_query_entity_vector_candidates(session, query_embedding, top_k) -> list[{name, score}]`; floor-filter with the existing `_filter_entities_by_floor` → `relevant_entity_names`, `entity_scores`.
2. De-gated entity query (the 90-day cutoff demoted to a ranking signal — relevant entities bypass it):
   ```cypher
   MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)
   WHERE <turn_vis_frag>
     [AND e.entity_type IN $entity_types]
     AND (t.timestamp >= $cutoff OR e.name IN $relevant_entity_names)
   WITH e, count(t) AS mentions, coalesce($entity_scores[e.name], 0.0) AS escore
   RETURN e.name AS name, e.entity_type AS type, e.description AS description, mentions
   ORDER BY escore DESC, mentions DESC
   LIMIT $limit
   ```
   - Recency-window entities (existing) ∪ vector-relevant entities **across all time** (via `OR e.name IN $relevant_entity_names`). Relevant old (>90d) entities bypass the cutoff and rank first by `escore` → they appear in results (AC-1b).
   - Gracefully reduces to legacy when there are no vector hits: empty `$relevant_entity_names` → the `OR` is false → recency-only; empty `$entity_scores` → `escore` all 0.0 → `ORDER BY mentions DESC` = legacy ordering.
   - Dynamic map-param subscript `$entity_scores[e.name]` (verified on Neo4j 5 in FRE-653).
3. **Sessions and `turns_summary` stay recency-based** — they are intrinsically "recent activity" surfaces and AC-1b asserts only on `entities`. Scoping documented in the docstring.

Flag off **or** no `query_text` → the legacy entity query runs unchanged (byte-for-byte legacy). The `query_memory_broad` failure/empty paths are untouched.

### Threading `query_text` through the call chain
- `memory/protocol.py` — add `query_text: str | None = None` to the `recall_broad` Protocol signature + docstring.
- `memory/protocol_adapter.py::recall_broad` — add `query_text: str | None = None`, pass to `query_memory_broad`.
- `request_gateway/context.py:169` — pass `query_text=user_message` to `recall_broad`.
- `orchestrator/executor.py:2000` — pass `query_text=ctx.user_message` (and `trace_id=ctx.trace_id`) to the direct `query_memory_broad` call (the broad path's vector branch logs warnings with `trace_id`; ADR-0074 hygiene).

### Caller dispositions (codex Q3 — the param is optional/default None, so non-threaded callers stay byte-for-byte legacy)
- **Thread (the automatic `MEMORY_RECALL` paths — AC-1b's target):** `context.py` (via `recall_broad`) and `executor.py` (direct).
- **Intentionally legacy (documented, not silent):** `tools/memory_search.py:178` and `ui/memory_cli.py:152/197` already pass `recency_days → 3650` (a ~10-year window) on the broad branch, so they are **not** the recency-gate victim AC-1b targets; threading vector relevance there is a clean follow-up, not this ticket. Left `query_text=None` (legacy). Noted in the ticket handoff.

### Test conformance (codex Q3/risk 2)
- `tests/personal_agent/memory/test_protocol.py:214` — the `FakeMemory.recall_broad` gains `query_text: str | None = None` (Protocol conformance). The existing delegation test (`:279`) still passes unchanged (param optional). Add a forwarding assertion: `adapter.recall_broad(query_text="x")` calls `query_memory_broad(query_text="x", ...)`.

### Semantic notes (codex risk 3/4 — documented, accepted for this ticket)
- **`mentions` for a vector-relevant old entity counts all-time turns** (the `OR e.name IN $relevant_entity_names` admits all its DISCUSSES pairs), vs recency-window for non-vector entities. `mentions` is an ordering/display hint, not a gate; the shift is acceptable and documented in the docstring.
- **Access-event relationship collection stays recency-scoped** (`t.timestamp >= $cutoff`), so an old vector-surfaced entity contributes its entity-id but no relationship-ids — the event handles an empty relationship list fine (no crash). Not de-gating the rel query keeps the diff surgical; noted as a possible ADR-0042 follow-up.

## Files
1. `src/personal_agent/memory/service.py` — `query_memory_broad` param + flag-on branch + `_query_entity_vector_candidates` helper.
2. `src/personal_agent/memory/protocol.py` — `recall_broad` signature.
3. `src/personal_agent/memory/protocol_adapter.py` — `recall_broad` threading.
4. `src/personal_agent/request_gateway/context.py` — call site.
5. `src/personal_agent/orchestrator/executor.py` — call site.
6. Tests (below).

## Tests (TDD — failing first)
`tests/test_memory/test_relevance_bounded_recall.py` (unit, no Neo4j):
- `test_query_entity_vector_candidates_shape` — the helper returns `{name, score}` rows (with a fake session whose `run` returns a stub result). *(If the helper is a thin Cypher wrapper, cover it via the integration test instead and note that.)*

`tests/test_memory/test_memory_service.py::TestMemoryQueries` (integration, skips w/o Neo4j):
- `test_query_memory_broad_flag_off_excludes_old_entity` — AC-1b control: seed entity E discussed only ~120 days ago; flag off; `query_memory_broad(query_text=..., recency_days=90)` → E **absent** from `entities`.
- `test_query_memory_broad_flag_on_surfaces_old_entity` — AC-1b: flag on; mock `generate_embedding` (non-zero) + patch `_query_entity_vector_candidates` → `[{"name": E, "score": 0.9}]`; `query_memory_broad(query_text=...)` → E **present** in `entities` (asserted by name). The contrast with the control proves the seam landed.

`tests/test_request_gateway/` or adapter test — assert `query_text` is forwarded: a unit test that `protocol_adapter.recall_broad(query_text=...)` calls `query_memory_broad` with that `query_text` (mock the service).

## Quality gates
`make test-k K="broad or relevance_bounded"` → module → `make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Out of scope
The `query_memory` path (FRE-653, merged), floor calibration / A/B / rollout (FRE-655), broad-path `memory_recall` telemetry event (not required by AC-1b; possible follow-up). Sessions/turns_summary de-gating (intrinsically recent surfaces).
