# FRE-638 — Claims storage model + retire first-write-wins

**Ticket:** FRE-638 (Approved) · **Backing ADR:** ADR-0098 D1/D2/D3 (PR #263) · **Tier:** 2 (Sonnet), built by Opus
**Depends on:** FRE-637 (landed dc7583b — extractor now emits `stances[]` / `claims[]` with provenance + `class`)

## Acceptance criteria carried (the definition of done)

- **AC-1 (correction):** a thin/wrong Claim, then a correct re-assertion (higher confidence / contradiction) →
  current query returns the **corrected** value; original **retained as superseded** (not gone / not still-current).
- **AC-2 (evolution, bitemporal):** change a Personal fact that *was* true → prior Claim has `valid_to`/`invalid_at`
  set and is **still present**; current-valid query returns **only the new**; the two validity intervals **do not overlap**.
- **AC-5 (native Stance traversal, Core unified):** a single Cypher
  `owner -[:HAS_STANCE]-> WorldConcept -[:RELATED_TO]-> WorldConcept` returns in **one** query, no cross-store hop.

## Design (minimal, AC-driven)

### Storage shapes (all in the one Neo4j "Core" graph)

**Stance** = native typed edge (supersession key = the `(owner, target)` pair — deterministic):
```
(o:Person {is_owner:true})-[:HAS_STANCE {
    affect, mastery, review_due,            // review_due = null (spaced-rep scheduler is out of scope / D4)
    class:'Stance',
    valid_from, valid_to, invalid_at,       // current ⇔ valid_to IS NULL AND invalid_at IS NULL
    trace_id, session_id, source_type, observed_at, extracted_at
}]->(c:Entity)                              // the World concept, already created from entities[]
```

**Personal Claim** = node hung off the owner (supersession key = content-embedding similarity):
```
(o:Person {is_owner:true})-[:HAS_FACT]->(cl:Claim {
    claim_id,                               // uuid4 minted at write
    content, class:'Personal', confidence,  // confidence from KnowledgeWeight.from_source(source_type)
    valid_from, valid_to, invalid_at, superseded_by, supersession_reason,
    embedding,                              // generate_embedding(content) — for future recall + matching
    trace_id, session_id, source_type, observed_at, extracted_at
})
```
`valid_from = observed_at` (turn time, from FRE-637 provenance). Current ⇔ `valid_to IS NULL AND invalid_at IS NULL`.

### Owner-sentinel resolution
`subject == "owner"` (FRE-637 sentinel) resolves to `MATCH (o:Person {is_owner:true})` (ADR-0052). If **no** owner
node exists, log `stance_write_skipped_no_owner` / `claim_write_skipped_no_owner` and skip — never fabricate an owner
from a sentinel (would dangle). Consolidator passes `user_id` for log correlation only.

### Supersession adjudication (pure, in `memory/supersession.py`)
Given the incoming claim and the **best-matching current candidate** (max cosine over owner's current Claims,
`None` if none ≥ `CLAIM_MATCH_THRESHOLD = 0.83`):

| Case | Condition | Action |
|------|-----------|--------|
| `FRESH` | no candidate ≥ threshold | insert new as current |
| `SUPERSEDE` | candidate exists AND `new.observed_at >= cand.observed_at` AND `new.confidence >= cand.confidence` | invalidate candidate (`valid_to=new.valid_from`, `invalid_at=now`, `superseded_by=new.claim_id`, `supersession_reason`), insert new current |
| `REJECT` | candidate exists AND `new.confidence < cand.confidence` | insert new as **non-current** (`invalid_at=now`, `valid_to=valid_from`); candidate stays current — retained-for-audit, *not naive last-write-wins* (ADR-0098 D2) |

`supersession_reason` maps to the ADR's own two triggers (D2): **`'correction'`** when `new.confidence > cand.confidence`
(the confidence/provenance-weighted override), **`'evolution'`** when confidence is equal and `observed_at` advanced (time moved).
This is a **heuristic label for the audit trail, not a robust classifier** — the storage layer cannot reliably tell "I
misremembered" from "it changed" from two content strings; a real correction-vs-evolution signal needs an extractor-emitted
contradiction flag (follow-up). Both modes produce the **same graph outcome** (old retained + superseded, new current), which
is all AC-1/AC-2 require; the label only annotates *why*. Threshold + adjudication are pure and unit-tested; the embedder is
injectable so tests are deterministic.

**Matching-fuzziness tradeoff (Codex #2):** embedding similarity is the identity mechanism because FRE-637's merged contract
emits only free-text `content` (no predicate/facet to key on) — this is the honest generalization of ADR-0073's similarity
slice. `CLAIM_MATCH_THRESHOLD` is set conservatively (0.83, tunable) so unrelated facts don't collide; a false supersession is
**recoverable** because the superseded original is always retained (`superseded_by` back-pointer) — ADR-0098 D2 mandates exactly
this recoverability. A structured claim-facet key is a follow-up, gated on the extractor emitting a predicate.

### Non-overlap invariant (AC-2)
Superseding sets `old.valid_to = new.valid_from`, so intervals are half-open `[from, to)` and adjacent, never overlapping.

### Atomicity + single-current invariant (Codex #5)
The consolidator is a **single-writer** (one run, captures processed sequentially — no concurrent writers to an owner's Claims).
Defensively, each `assert_claim` does candidate-select → invalidate → insert in **one `session.run` (one transaction)**, and on
supersede it invalidates **every** current Claim whose similarity ≥ threshold (not just the top one), so the "≤1 current per
fact-slot" invariant **self-heals** even if a prior bug left duplicates. Neo4j cannot express a partial-unique index over an
embedding-derived slot, so this atomic invalidate-all-matched is the guard (no DB constraint). Candidate ranking uses
`ORDER BY similarity DESC` computed in Python before the write.

## Out of scope (explicit — not silent narrowing) — **OWNER DECISION, Codex #4**
- **Entity-description first-write-wins MERGE** (`service.py:862-866`) is **left intact** — it guards FRE-375 test
  overwrites and lives on the hot dedup path. This ticket **retires first-write-wins by introducing the living-Claim
  substrate + supersession mechanism** (ADR D-seq step 2: "Claims model + first-write-wins retirement") and **proves it on
  Personal Claims** — the exact case ADR AC-1 describes ("a World/**Personal** Claim"). **What it does NOT do:** migrate
  World-fact-as-entity-description onto that substrate. That migration is a hot-path change (re-extraction correcting a
  non-empty World description needs the same contradiction/confidence machinery = World Claims) and isn't fed by the current
  extractor (still emits World as entities). **Decision for owner/master:** accept proving the living-knowledge mechanism on
  Personal now with World-entity migration as a sequenced follow-up, **or** expand FRE-638 to also cut World facts over
  (larger, riskier, touches recall/dedup). Recommendation: **keep scoped to Personal + Stance**; file the World-Claim
  migration as the next ticket. Master holds the AC gate against whichever the owner picks.
- Class-aware lifecycle / eviction / System gate (D4, D1 gate) → FRE-639. Retention offload / Docs isolation (D6/D3) → later.
- Vector index on `:Claim(embedding)` — a scaling follow-up (owner's current Claims are bounded; Python cosine is fine now).

## Files

| File | Change |
|------|--------|
| `src/personal_agent/memory/models.py` | +`Claim`, `Stance` Pydantic models (frozen) |
| `src/personal_agent/memory/supersession.py` | **new** — `SupersessionAction` enum, `adjudicate(new, candidate)`, `best_candidate(new_embedding, candidates, threshold)` (pure) |
| `src/personal_agent/memory/service.py` | +`assert_stance(...)`, +`assert_claim(...)` (Neo4j I/O; owner resolution; bitemporal Cypher) |
| `src/personal_agent/second_brain/consolidator.py` | after entity/relationship writes, loop `stances[]`→`assert_stance`, `claims[]`→`assert_claim`; thread trace_id/session_id/user_id |

## Steps (TDD — failing test first each)

1. **Models** — add `Claim`/`Stance` to `models.py`. Verify: `uv run python -c "from personal_agent.memory.models import Claim, Stance"`.
2. **Adjudication (pure)** — `tests/personal_agent/memory/test_supersession.py`: FRESH / SUPERSEDE (evolution: newer time, equal conf) / SUPERSEDE (correction: higher conf) / REJECT (lower conf) / non-overlap boundary / reason tagging. Then implement `supersession.py`. Run: `make test-file FILE=tests/personal_agent/memory/test_supersession.py`.
3. **`assert_stance` + `assert_claim`** — mocked-driver unit test `tests/personal_agent/memory/test_claims_stance_cypher.py` (project pattern: capture emitted Cypher) asserting: owner-sentinel `is_owner:true` match, HAS_STANCE / HAS_FACT+Claim creation, bitemporal props present, supersede path invalidates prior. Then implement the two service methods. Run that file.
4. **Consolidator wiring** — `tests/test_second_brain/test_consolidator_claims_wiring.py` (mock MemoryService): a capture whose extraction returns 1 stance + 1 claim → `assert_stance`/`assert_claim` each called once with resolved args. Then wire `consolidator._process_capture`. Run that file.
5. **Behavioral AC proof (integration, `@pytest.mark.integration`, live :7688)** — `tests/personal_agent/memory/test_claims_stance_storage.py`, `generate_embedding` patched for deterministic similarity, unique owner user_id per test:
   - **AC-1**: assert_claim("thin/wrong", conf=0.5) → assert_claim("correct", conf=0.8, similar) → current-valid query returns corrected; original present with `superseded_by` set, `supersession_reason='correction'`.
   - **AC-2**: assert_claim(lease=March, t0) → assert_claim(lease=June, t1>t0, similar, equal conf) → prior has `valid_to`/`invalid_at`, still present; current query returns only June; `old.valid_to == new.valid_from` (non-overlap); reason `'evolution'`.
   - **AC-5**: seed owner + two World `:Entity` + `RELATED_TO`; assert_stance(owner→concept1); single Cypher `MATCH (o:Person{is_owner:true})-[:HAS_STANCE]->(w1)-[:RELATED_TO]->(w2) RETURN ...` returns 1 row.
   - **REJECT**: lower-confidence contradiction does not clobber (candidate stays current).
   Run: `AGENT_NEO4J_USER=neo4j AGENT_NEO4J_PASSWORD=… uv run pytest tests/personal_agent/memory/test_claims_stance_storage.py -m integration` (in-session; report results to master).

## Quality gates
`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.
ADR-0074 identity threading on every new `MERGE|CREATE` (trace_id/session_id on Claim + Stance) and every new `log.*`.

## Follow-ups to file (Needs Approval, project Memory Recall Quality)
- **World-fact-as-entity-description → World Claims migration** (fully retire the Entity-description first-write-wins; the
  owner-decision above). The primary sequenced next step.
- **Structured claim-facet key** for supersession matching (gated on the extractor emitting a predicate/facet), + an
  **extractor contradiction flag** so correction-vs-evolution becomes a robust classification not a heuristic label (Codex #2/#3).
- Vector index on `:Claim(embedding)` when owner Claim count grows past Python-cosine scale.
- FRE-637 flagged: consolidator `is_fallback` gate ignores stances/claims (claims-only turn mis-detected as fallback).
