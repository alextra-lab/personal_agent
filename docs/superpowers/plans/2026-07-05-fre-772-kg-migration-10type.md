# FRE-772 — KG migration: re-type existing entity nodes to the ADR-0109 V2 10-type taxonomy

**Ticket:** FRE-772 (Approved → In Progress) · **Backing:** [ADR-0109](../../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md)
(Accepted) + Amendment 1, **Implementation Notes step 5** · **Gate:** AC-4.
**Depends on:** FRE-771 (V2 extractor prompt, merged #368 — *not yet deployed to prod*),
FRE-769 (downstream-impact finding, `docs/research/2026-07-04-fre-769-recall-type-downstream-impact.md`).
**Owner decisions (2026-07-05):** Concept re-classifies into the **full conceptual family (5)**;
the recall-consumer remap is a **separate ticket** (not folded here).

---

## Acceptance criterion this ticket owns

> **AC-4** — the KG migration re-types every existing entity node (no `Concept`/`Technology`/`Topic`
> remnants; 0 orphans). Proof: migration report + joinability probe (ADR-0074).

Definition of done = AC-4 proven. This session proves the **mechanism** (unit + integration tests on the
test substrate with a mocked classifier); the **live** AC-4 proof (report + joinability on the real graph)
is master's post-deploy step, since running a Neo4j data migration against prod is an always-ask deploy.

---

## Scope (this PR)

**New:**
- `src/personal_agent/second_brain/taxonomy.py` — single source of truth for the type enums:
  `V2_ENTITY_TYPES` (10), `V1_ENTITY_TYPES` (7), `V1_TO_V2_DETERMINISTIC` (dict), `V1_CONCEPT_TARGET_TYPES`
  (the 5 conceptual). The migration imports these; the separate consumer-remap ticket will import them too.
- `scripts/migrate_fre772_entity_type_v2.py` — the idempotent migration.
- `tests/personal_agent/second_brain/test_taxonomy.py` — enum/map invariants + extractor drift guard.
- `tests/scripts/test_migrate_fre772.py` — unit tests (mocked driver + mocked classifier).
- `tests/scripts/test_migrate_fre772_integration.py` — real test Neo4j (:7688), `integration` marker
  (skipped by `make test`).

**NOT in this PR (owner: keep separate):** the recall-consumer remap
(`orchestrator/executor.py _ENTITY_TYPE_KEYWORDS`, `tools/memory_search.py` schema). Filed as a follow-up
ticket that gates the coordinated V2 prod cutover. `memory/dedup.py` needs no code change — it is a
type-agnostic mechanism and the data migration is its fix.

---

## Taxonomy maps (from ADR-0109 § Decision + Amendment 1)

Deterministic (no model needed):

| V1 | V2 |
|----|----|
| `Technology` | `TechnicalArtifact` |
| `Topic` | `DomainOrTopic` |
| `Person` / `Organization` / `Location` / `Event` | unchanged (valid in both V1 and V2) |

Model re-classification — `Concept` → exactly one of the **conceptual family (5)**:
`MethodOrConcept` · `DomainOrTopic` · `Phenomenon` · `QuantityMeasure` · `KnowledgeArtifact`.
(Owner decision: the 5, not the ADR-text 3, because Amendment 1 added `QuantityMeasure`/`KnowledgeArtifact`
specifically to home entities — e.g. `wavelength`, an authored paper — many of which are stored today as
`Concept`; this is a near-one-way door, so re-file them correctly now.)

Out-of-vocabulary stored values (`Unknown` / `''` / `NULL`, which the read path tolerates today) are
**left untouched** and counted in the report — they are not V1 taxonomy remnants and coarsening does not
concern them.

---

## Migration algorithm (idempotent, re-runnable, fail-closed)

Driver built directly from `settings.neo4j_uri/neo4j_user/neo4j_password` (precedent:
`scripts/migrate_fre229_visibility_backfill.py`, `scripts/backfill_participated_in.py`). House gate:
`--confirm-prod` + refuse when `settings.environment != TEST` without it.

1. **Snapshot** — write a reversible JSON of every `:Entity {name, entity_type}` to `--snapshot-path`
   (default under `telemetry/`, gitignored). The runbook additionally instructs master to take a full
   Neo4j dump before running (the ADR's "snapshot Neo4j first").
2. **Deterministic remap** — one Cypher per mapping, idempotent by predicate:
   ```
   MATCH (e:Entity) WHERE e.entity_type = $v1
   SET e.entity_type = $v2,
       e.entity_type_migration = 'fre772',
       e.entity_type_migrated_at = $now
   RETURN count(e) AS n
   ```
   Person/Org/Location/Event are valid in both taxonomies → untouched, unstamped.
3. **Concept re-classification (LLM)** —
   `MATCH (e:Entity {entity_type:'Concept'}) RETURN e.name, e.description, elementId(e)`.
   For each, a bespoke focused classifier (name + description + the 5 conceptual GoLLIE definitions) →
   exactly one of the 5. **Fail-closed:** any out-of-set / empty / error result leaves the node as
   `Concept` and records it in the report as `unclassified` (a re-run retries it — never guess a type).
   Bounded concurrency (`asyncio.Semaphore`); `LiteLLMClient(budget_role="entity_extraction")` so cost
   lands in the right lane; per-node + total cost reported. On success:
   `SET e.entity_type=$v2, e.entity_type_migration='fre772', e.entity_type_migrated_at=$now`.
4. **Report** — before/after type histogram, per old→new counts, `unclassified` list, cost, elapsed
   (JSON to `--report-path` + printed summary).
5. **Joinability** — the runbook and the integration test run `scripts/monitors/joinability_probe.py`
   to confirm 0 orphans (re-typing a scalar property cannot create orphans; the probe confirms it).

**ADR-0074 identity threading:** these are `SET`s on **existing** nodes (not `MERGE`/create), so
`originating_trace_id`/`originating_session_id` are left intact. Migration provenance is the update-marker
(`entity_type_migration='fre772'`, `entity_type_migrated_at`), the update-time analog of origination.
The idempotency guard is the `WHERE entity_type = <V1>` predicate (already-migrated nodes are skipped),
so a re-run is a no-op except for stragglers/`unclassified`.

**Classifier is mock-injectable** — the migration takes a classifier callable (default = the real
LiteLLM-backed one) so unit/integration tests pass a deterministic fake and `make test` makes **zero**
LLM calls / spends nothing.

---

## Tests → AC-4 proof

**Unit (CI-gating; no LLM, no real DB):**
- `test_taxonomy.py` — `len(V2_ENTITY_TYPES)==10`; `V1_TO_V2_DETERMINISTIC` covers every V1 type except
  `Concept`; `V1_CONCEPT_TARGET_TYPES ⊆ V2_ENTITY_TYPES` and has 5; **drift guard**: every V2 name appears
  in `entity_extraction._EXTRACTION_PROMPT_TEMPLATE` (migration and live extractor can't diverge silently).
- `test_migrate_fre772.py` — with a fake async session recording Cypher/params + a fake classifier:
  deterministic maps produce the right V2 value + marker; unchanged types untouched; `Concept` happy-path
  gets the classifier's type; fail-closed on out-of-set/error → stays `Concept` + `unclassified`;
  idempotent re-run = no-op; report counts correct.

**Integration (`integration` marker, real test Neo4j :7688, skipped by `make test`, run locally/by master):**
- Seed V1-typed nodes (+ a couple relationships, an `Unknown`, a `Concept`) → run migration with a mocked
  classifier → assert: no `Concept`/`Technology`/`Topic` remnants, deterministic maps correct, `class`
  property untouched, joinability probe 0 orphans, re-run is a no-op.

**Live AC-4** (master, post-deploy): the migration report + joinability probe on the real graph.

---

## Runbook (for master — post-merge, coordinated V2 cutover; **never piecemeal**)

The V2 prompt (FRE-771) is merged but **not yet deployed to prod**, so prod is currently V1-consistent
(extractor emits V1, consumers filter V1). The cutover is one coordinated deploy:

1. **Merge the consumer-remap follow-up ticket first** (recall consumers must speak V2 before the graph
   does).
2. **Deploy** the V2 extractor prompt + consumer remap: `ENV=cloud make rebuild SERVICE=seshat-gateway`.
3. **Snapshot** the graph (full Neo4j dump) — always-ask deploy class; confirm with owner.
4. **Dry-run** the migration: `uv run python scripts/migrate_fre772_entity_type_v2.py --dry-run --confirm-prod`
   → review the report's histogram + projected Concept classifications + cost estimate.
5. **Run**: `uv run python scripts/migrate_fre772_entity_type_v2.py --confirm-prod`.
6. **Verify AC-4**: report shows 0 `Concept`/`Technology`/`Topic` remnants; run the joinability probe
   (0 orphans); recall spot-check ("what tools have I used" now filters `TechnicalArtifact` and returns
   rows).

Running the migration **before** the V2 prompt deploy would let the still-V1 extractor write fresh V1
nodes → the migration would need re-running; hence it runs **with/after** the prompt deploy.

---

## Codex plan-review dispositions (2026-07-05) — all folded

A near-one-way-door prod graph rewrite, so all eight findings are incorporated:

1. **Batching.** Deterministic remap runs `CALL { MATCH (e:Entity) WHERE e.entity_type=$v1 SET ... } IN
   TRANSACTIONS OF $batch ROWS` (auto-commit, bounded locks). The Concept path pages by an `elementId`
   **cursor** (`WHERE e.entity_type='Concept' AND elementId(e) > $cursor ORDER BY elementId(e) LIMIT
   $batch`), which also fixes the fail-closed re-fetch trap: a node left `Concept` this run is cursored
   past, not re-fetched in the same run (a fresh re-run restarts the cursor and retries it).
2. **Postflight recount.** After migration the report re-counts V1 types; a non-zero `Technology`/`Topic`
   count (or any remaining `Concept`) fails the run's success assertion. Post-V2-deploy the extractor emits
   no `Concept`/V1, so no new stragglers appear during the window (documented in the runbook).
3. **Fail-closed auditability.** The report records the classifier `model` + a `PROMPT_VERSION` constant;
   each fail-closed node also gets a non-type annotation `entity_type_migration_error` (does not change
   `entity_type`). **AC-4 is not declared while any `Concept` remains** — unclassified nodes block Done and
   go to human review.
4. **Rollback.** `--rollback --snapshot-path <file>` restores `entity_type` from the snapshot keyed by
   `name` (the MERGE key, unique), leaving nodes created after the snapshot untouched (warned). Tested.
5. **Dry-run contract.** `--dry-run` issues **zero** writes (asserted by a unit test that fails on any
   recorded `SET`), while still calling the classifier to preview Concept classifications + real cost so
   master can review before committing.
6. **Run identity (ADR-0074).** A `entity_type_migration_run_id` (uuid) is stamped with the marker, and
   classifier calls carry `SystemTraceContext.new("entity_type_migration")` so their cost/log rows join.
7. **Test strength.** The integration test is run this session against test Neo4j (:7688) and its before/
   after histogram + joinability output captured as AC-4 mechanism evidence in the handoff — not left as a
   skipped afterthought. The prod dry-run report + joinability remain master's deploy-gate artifacts.
8. **FRE-793 as a hard gate.** A **preflight** imports the in-process recall keyword map and refuses to run
   unless it already emits only V2 strings (proof the FRE-793 remap is in the deployed code), with a
   `--skip-consumer-check` override for the isolated test substrate. Turns the load-bearing sequencing
   constraint into a script-enforced blocker, not prose.

## Out of scope / follow-ups
- **Consumer remap** (separate ticket, Needs Approval) — `_ENTITY_TYPE_KEYWORDS` + `search_memory` schema
  V1→V2; gates the cutover above.
- Keyword-vocabulary expansion (new keywords for `Phenomenon`/`QuantityMeasure`/`KnowledgeArtifact`) — a
  recall-quality nicety, belongs to the consumer-remap ticket, not here.
- The ADR-0100/0104 flag-gated recall arms carry the same latent V1 assumption (FRE-769 findings #6–8);
  remap before those flags flip on — tracked by the consumer-remap ticket's note, flags are off today.
