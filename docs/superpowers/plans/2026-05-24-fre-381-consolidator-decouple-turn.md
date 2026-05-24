# FRE-381 — Consolidator: decouple Turn creation from entity extraction (Stage 2)

**Linear:** FRE-381 (Needs Approval) · **ADR:** ADR-0074 §I5 amendment (drafted below)
**Predecessor:** FRE-380 (Stage 1, shipped 2026-05-24, PR #78 `9d44610`)
**Date:** 2026-05-24 · **Target:** post-2026-05-30 joinability gate + post-FRE-380 soak
**Tier:** Sonnet-implementable from this plan; ~1-2 days

---

## Context

FRE-376 Phase 5's joinability probe surfaced a long-standing gap: the consolidator's `_process_capture` writes the `(:Turn)` node only *after* successful entity extraction (`consolidator.py:464-496`). When extraction is broken for an extended period (the 2026-05-23 17h `trace_ctx` regression accumulated dozens of captures-without-Turns), the probe flags `three_way_mismatch` orphans and the gate stays red.

**Stage 1 (FRE-380, shipped)** caps extraction retries and writes a *stub Turn* after N failures. The capture is joinable, the LLM-derived semantic enrichment is accepted as lost.

**Stage 2 (this plan)** inverts the dependency: Turn creation becomes deterministic and idempotent; entity attachment becomes a separate, independently-retryable step. Stage 1's cap remains as a final safety net.

The Stage 2 plan was reviewed by codex (REQUEST CHANGES) and the design below incorporates all six adjustments codex flagged.

---

## Goals & non-goals

**Ship:**
- New `turn_lacks_entities()` predicate replacing `turn_exists()` as the consolidator's dedup gate.
- New `MemoryService.ensure_turn()` — idempotent MERGE-based Turn creation independent of extraction.
- New `MemoryService.update_turn_extractor_model()` — sets `extractor_model` on an existing Turn after extraction succeeds.
- `TurnNode.extractor_model: str | None` field (Pydantic + Cypher MERGE in `create_conversation`).
- ADR-0074 §I5 amendment clarifying staged `extractor_model` semantics (Stage 1 stub Turns *and* Stage 2 freshly-created-pre-extraction Turns both permitted to carry `extractor_model=None` transiently).
- Consolidator `_process_capture` reorganized: Step A (ensure Turn) before Step B (extract + attach).
- Unit tests + integration test verifying idempotent re-run, partial Entity attach, MERGE on repeat entities.

**Explicitly defer:**
- Migrating Phase 1 (capture write) into a single-phase write — too invasive, would touch the gateway hot path.
- Replacing `captains_log_captures` as the source of truth.
- Backfill script for *historical* orphans pre-Stage-1 — only ship if soak data shows the consolidator's window doesn't catch them naturally.

---

## ADR-0074 §I5 amendment (draft)

Add the following paragraph to `docs/architecture_decisions/ADR-0074-end-to-end-traceability.md` immediately after the §I5 enumeration of `(:Turn)` / `(:Entity)` / `(:Relationship)` properties:

> **§I5 clarification — staged `extractor_model` (added 2026-XX-XX, FRE-381):**
> The `(:Turn).extractor_model` property is *populated when entity extraction succeeds* and *permitted to be NULL during three transient or terminal states*:
>
> 1. **Stage 2 in-flight Turns** — written by `MemoryService.ensure_turn()` before entity extraction has been attempted in the current consolidator tick. The property is set when the matching extraction completes successfully via `MemoryService.update_turn_extractor_model()` on the same `turn_id`.
> 2. **Stage 1 capped stub Turns** (per FRE-380) — written after `settings.consolidator_max_extraction_attempts` extraction attempts have all failed. `extractor_model` remains NULL permanently because no extractor ever produced output for this Turn. `Turn.properties.extraction_outcome="capped_after_retries"` is the durable marker.
> 3. **Pre-FRE-381 historical Turns** — Turns created before this amendment was applied. The migration policy is observe-only; no backfill is required.
>
> The `(:Turn).originating_trace_id` and `(:Turn).originating_session_id` properties remain *required at all times* (write or update). The AST lint at `scripts/check_identity_threaded.py` continues to enforce both. `extractor_model` is *not* lint-enforced because its absence is a valid state.

Same commit as Stage 2 implementation. Bump ADR `Status:` only if FRE-376 has already flipped it to `Accepted` (post-May-30 gate); otherwise leave as-is.

---

## Architecture

### Algorithm (revised per codex review)

```
For each capture in read_captures(window):

    if turn_lacks_entities(capture.trace_id):
        # ── Step A — deterministic, idempotent (MERGE) ─────────────────
        await ensure_turn(
            turn_id=capture.trace_id,
            trace_id=capture.trace_id,
            session_id=capture.session_id,
            originating_trace_id=capture.trace_id,
            originating_session_id=capture.session_id,
            extractor_model=None,                    # filled by Step B on success
            user_message=capture.user_message,
            assistant_response=capture.assistant_response,
            summary=(capture.user_message or "").strip()[:200],  # provisional
            key_entities=[],
            visibility="group",                       # FRE-343 / FRE-229
        )

        # ── Step B — LLM-gated; idempotent on retry ────────────────────
        attempt_number = previous_attempt_count(capture.trace_id, "entity_extraction") + 1
        max_attempts = settings.consolidator_max_extraction_attempts

        try:
            extraction_result = await extract_entities_and_relationships(
                capture.user_message,
                strip_think(capture.assistant_response),
                trace_id=capture.trace_id,
                session_id=capture.session_id,
                attempt_number=attempt_number,
            )
            if is_fallback(extraction_result, capture):
                if attempt_number >= max_attempts:
                    # Stage 1 cap path — write properties marker on existing Turn
                    await mark_turn_extraction_capped(capture.trace_id, attempt_number)
                    record_consolidation_attempt(outcome="extraction_capped")
                    return {"turn_created": False, "entities_created": 0, "capped": True}
                # Below cap — Turn stays; retry next tick
                record_consolidation_attempt(outcome="extraction_returned_fallback")
                return {"turn_created": False, "entities_created": 0}

            # Successful extraction — attach entities
            await attach_entities_to_turn(
                turn_id=capture.trace_id,
                entities=extraction_result["entities"],
                relationships=extraction_result["relationships"],
                extractor_model=resolve_extractor_model_id(),
            )
            await update_turn_summary(capture.trace_id, extraction_result["summary"])
            await update_turn_extractor_model(capture.trace_id, resolve_extractor_model_id())
            record_consolidation_attempt(outcome="success")
            return {...}

        except BudgetDenied as exc:
            record_consolidation_attempt(outcome="budget_denied", denial_reason=exc.denial_reason)
            return {...}
```

Note: the cap stays as a final safety net even with Stage 2 — the `extraction_capped` path is now a metadata update on an existing Turn rather than a separate `create_conversation` call, but the user-visible semantics are identical.

### `turn_lacks_entities()` predicate (verbatim Cypher)

```cypher
MATCH (t:Turn {turn_id: $turn_id})
RETURN NOT EXISTS { MATCH (t)-[:DISCUSSES]->(:Entity) } AS turn_lacks_entities
```

Per codex's audit: only `:DISCUSSES` counts as "has entities". `:REFERENCES` / `:ENTAILS` do not exist in the codebase. `:NEXT`, `:CONTAINS`, `:PARTICIPATED_IN` are structural/provenance edges and must not be confused with entity attachment.

Important nuance: a Turn that exists but has no `:DISCUSSES` edges *and* whose `properties.extraction_outcome == "capped_after_retries"` is **final** — Stage 2 must not re-extract it. The consolidator's outer loop checks `previous_attempt_count >= max_attempts` *before* taking the Step A branch:

```python
prior_attempts = await previous_attempt_count(capture.trace_id, "entity_extraction")
if prior_attempts >= settings.consolidator_max_extraction_attempts:
    # Stage 1 capped — already terminal, skip
    continue
if await turn_lacks_entities(capture.trace_id):
    # Run Steps A+B
```

This satisfies AC-4 from FRE-381 (Stage 1's cap remains active).

---

## Files to create / modify

### Create

- `tests/test_second_brain/test_consolidator_decouple.py` — unit tests for Steps A+B, MERGE idempotency, partial-attach retry.
- `tests/test_memory/test_turn_lacks_entities.py` — Cypher predicate unit test (with mocked driver) + integration test.

### Modify

- `docs/architecture_decisions/ADR-0074-end-to-end-traceability.md` — add the §I5 clarification paragraph (drafted above). Same commit as code change.
- `src/personal_agent/memory/models.py` — `TurnNode.extractor_model: str | None = None` (after `key_entities`, before `properties`).
- `src/personal_agent/memory/service.py`:
  - `create_conversation()` Cypher MERGE in `_create_turn_node` (line ~382-460) — write `extractor_model` (may be NULL).
  - New `ensure_turn(...)` — MERGE-only, no entity attachment. Reuses the same Cypher core as `create_conversation` but skips the `_DISCUSSES` edge writes.
  - New `turn_lacks_entities(turn_id) -> bool` — the predicate above.
  - New `update_turn_extractor_model(turn_id, extractor_model)` — `MATCH (t:Turn {turn_id: $tid}) SET t.extractor_model = $extractor_model`.
  - New `update_turn_summary(turn_id, summary)` — same pattern.
  - New `attach_entities_to_turn(turn_id, entities, relationships, extractor_model)` — extracts the Entity / `:DISCUSSES` writes currently inline in `_process_capture`. MERGE-based so repeat attaches are no-ops; new entities create new edges.
  - New `mark_turn_extraction_capped(turn_id, attempts)` — `MATCH (t:Turn) SET t.properties.extraction_outcome="capped_after_retries"` (or whichever property-set strategy fits Cypher map semantics).
- `src/personal_agent/second_brain/consolidator.py`:
  - Replace the existing `turn_exists()` gate (`consolidator.py:148-157`) with the new flow: `prior_attempts` check → `turn_lacks_entities()` → Step A → Step B.
  - Remove the existing inline `create_conversation` + entity-creation block (lines 502-566); replaced by `ensure_turn` + `attach_entities_to_turn`.
  - Stage 1's stub-Turn write path becomes `mark_turn_extraction_capped()` (Turn already exists from Step A; only the marker needs updating).

### No changes needed

- `scripts/check_identity_threaded.py` — already exempt for `extractor_model` (codex confirmed lint enforces only origination fields).
- ES index templates — Turn shape isn't in ES.
- Joinability probe walk — already treats `(:Entity)` count as `absent_ok`; will see Stage 2 Turns as green.

---

## Build order

1. **Branch + ADR amendment**: open branch `fre-381-consolidator-decouple-turn`. Edit ADR-0074 §I5 inline. Commit alone for review surface.
2. **Schema change**: `TurnNode.extractor_model` field. `create_conversation` MERGE writes it. Existing tests should pass without modification (default value).
3. **New MemoryService methods**: `ensure_turn`, `turn_lacks_entities`, `update_turn_extractor_model`, `update_turn_summary`, `attach_entities_to_turn`, `mark_turn_extraction_capped`. Each with a unit test using mocked async driver.
4. **Integration test for `turn_lacks_entities`**: stand up `make test-infra-up`, write a Turn with and without entities, verify the predicate.
5. **Consolidator reorg**: replace `_process_capture` body with the Step A + Step B flow. Update existing unit tests.
6. **Stage 1 cap stays**: verify `consolidator_max_extraction_attempts` is still honored and now records via `mark_turn_extraction_capped` instead of a fresh `create_conversation`.
7. **Probe verification**: run `python -m scripts.monitors.joinability_probe --session-id <test-sid>` against a freshly-Step-A'd Turn; outcome should be green (Turn exists with origination; Entity check `absent_ok`).
8. **PR**, quality gates, merge, deploy, run for one consolidator tick, verify Turn appears in Neo4j with `extractor_model=None`, then entities attach within the next interval.

---

## Acceptance criteria

| AC | Phase | Description |
|---|---|---|
| **AC-1** | pre-merge | ADR-0074 §I5 amendment committed in same branch; lint exception explicitly documented |
| **AC-2** | pre-merge | `TurnNode.extractor_model` field added; existing tests pass unchanged |
| **AC-3** | pre-merge | `turn_lacks_entities()` Cypher query passes unit test + `make test-infra-up` integration test |
| **AC-4** | pre-merge | Stage 1's `consolidator_max_extraction_attempts` cap remains active; capped Turns are not re-extracted in Step B |
| **AC-5** | pre-merge | Idempotent re-run: process same capture twice → exactly one Turn, exactly one set of entities (MERGE not double-create) |
| **AC-6** | pre-merge | Partial Entity attach: process capture once with extraction returning 3 entities, then again with 5 — Turn has 5 entities (3 original + 2 new) |
| **AC-7** | pre-merge | `make mypy && make ruff-check && make test` all clean |
| **AC-8** | post-deploy, same session | Drive one fresh chat turn. Within ~10s of session end (or next consolidator tick), `MATCH (t:Turn {turn_id: $trace_id})` returns a Turn with `extractor_model=NULL`. Within the subsequent consolidator interval, `extractor_model` updates to the resolved extractor id and `(:DISCUSSES)` edges appear. |
| **AC-9** | post-deploy, same session | Joinability probe run on the test session returns `outcome=green` |
| **AC-10** | 1-week soak | Capped-stub ratio drops to near-zero (compared to FRE-380 baseline). Persistent stubs only when extraction is truly down. |

---

## Soak data collection plan (for FRE-380, feeds Stage 2 design)

Run `scripts/soak/fre-380-stage1-soak.sh` once daily between now and 2026-05-30 (or on-demand via the gate-check routine). Outputs feed into the Stage 2 PR description.

### Metrics to track

1. **Stub vs entity-attached Turn ratio**
   - ES query: `consolidation_extraction_capped` event count per day
   - PG query: `SELECT outcome, COUNT(*) FROM consolidation_attempts WHERE started_at > now() - interval '1 day' GROUP BY outcome`
   - Decision: if stub-Turn writes dominate `>10%` of consolidations, extraction is structurally broken → root-cause before Stage 2 ships; otherwise Stage 2 ships as planned.

2. **Attempt distribution at the point of capping**
   - PG query (joining `consolidation_attempts` on `trace_id`): for traces that hit `extraction_capped`, what was the distribution of attempt outcomes (which fail mode produced the cap — fallback? budget_denied? model_error?)
   - Decision: informs whether Stage 2's `is_fallback` detection needs refinement.

3. **Consolidator window coverage** (codex open question #6)
   - PG query: `SELECT MAX(timestamp) - MIN(timestamp) FROM captains_log_captures WHERE trace_id NOT IN (SELECT trace_id FROM ...neo4j-projected-trace-ids...)`
   - Neo4j query: `MATCH (t:Turn) RETURN min(t.timestamp), max(t.timestamp)` — to verify the consolidator window goes far enough back.
   - Decision: if pre-Stage-1 orphans exist *outside* the consolidator's `read_captures(start_date, end_date, limit)` window, file a one-shot backfill script as a separate ticket; otherwise rely on implicit catch-up.

4. **Stub Turn UX impact**
   - Manual: open PWA, query `memory_search` for a known stub Turn topic. Does it surface? Is the empty `key_entities` confusing?
   - Decision: surface as-is, surface with a flag, or filter from `recent_turns` results (per codex finding #5).

5. **Joinability probe outcome trend**
   - ES daily histogram on `agent-monitors-joinability-*.outcome`
   - Decision: green for 7 consecutive days → flip ADR-0074 + FRE-376 (gate satisfied) and Stage 2 ships into a verified-clean substrate.

---

## Risks & open questions

1. **`turn_lacks_entities()` race**: two consolidator passes on the same capture could both pass the predicate, both call `ensure_turn` (idempotent — fine), both run extraction (wasted compute — annoying). Mitigation: the existing `previous_attempt_count` check above plus optimistic locking via a Cypher conditional update. Open: do we add a per-trace lock, or accept the rare double-extraction? Lean toward accept — it's a transient inefficiency, not a correctness bug.

2. **Property-map updates in Cypher**: `mark_turn_extraction_capped` modifies `t.properties` (a map). Neo4j has limited support for map-property updates compared to top-level properties. Open: should `extraction_outcome` and `extraction_attempts` be top-level properties on Turn instead of inside `properties`? Probably yes — simpler Cypher, easier to index. Stage 2 should promote them.

3. **`update_turn_summary` overwrite semantics**: Step A writes provisional `summary = user_message[:200]`; Step B overwrites with extraction's summary. Open: if extraction is re-run on a Turn whose summary was already extracted (rare), should the second extraction's summary win, or keep the first? Lean toward "last write wins" — extraction is stateless and the latest pass has the most evidence.

4. **Session topic rollups stay incomplete while Turns are entity-less** (codex finding #4): `link_session_turns()` and `_update_session_dominant_entities()` only see Turn→Entity edges. Acceptable transient state. Open: do we add a "refresh on extraction-attach" hook? Probably yes — file as small follow-up if soak shows it matters.

5. **Backfill question depends on soak data** (codex open question #6): defer until 2026-05-30.

---

## Verification (end-to-end)

```bash
# 1. Pre-merge
make mypy && make ruff-check
make test                                                # unit tests
make test-infra-up && make test-integration              # turn_lacks_entities integration
make test-infra-down

# 2. Deploy
ENV=cloud make build SERVICE=seshat-gateway

# 3. Drive one fresh turn from the PWA. Then:

# 3a. Verify Turn appears with extractor_model=NULL within ~10s
docker exec cloud-sim-neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD \
  "MATCH (t:Turn {turn_id: '<recent-trace-id>'}) RETURN t.extractor_model, t.summary, size((t)-[:DISCUSSES]->()) AS entity_count"

# 3b. Wait one consolidator interval. Re-query — extractor_model should be populated,
#     entity_count > 0.

# 3c. Run joinability probe on that session
python -m scripts.monitors.joinability_probe \
    --session-id <recent-session-id> --dry-run
# expect: outcome=green

# 4. (Day 7 post-deploy) Re-run soak script
bash scripts/soak/fre-380-stage1-soak.sh > soak-stage2-post.txt
# Compare stub ratio against baseline; expect <2% capped, dominated by genuinely
# uncatchable extraction failures.
```

---

## Cross-references

- **FRE-376 Phase 5** (joinability probe) — surfaced the orphan accumulation; verifies Stage 2 success post-merge.
- **FRE-380 Stage 1** (PR #78, `9d44610`) — the containment fix that Stage 2 supersedes; cap mechanism survives.
- **ADR-0074 §I5** — the invariant being amended.
- **FRE-374 replay** — should land in the same week as Stage 2 (both touch consolidator + memory/service).
- **Codex review session 2026-05-24** — REQUEST CHANGES findings #1-#6 all addressed in this plan.
