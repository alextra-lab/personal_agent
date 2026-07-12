# FRE-728 — ADR-0115 Dispatch: output_kind write-time routing

**Ticket:** FRE-728 (Approved, stream:build1, Tier-2:Sonnet)
**ADR:** ADR-0115 (Accepted) — D3 (dispatch: isolation by absence-of-write)
**Depends on:** FRE-863 (emission, merged — `output_kind`/`class` now on every extracted entity)
**Parallel to:** FRE-864 (persistence — `class` on `:Entity`, in progress on `build2`, not merged)
**Downstream (explicitly out of scope here):** FRE-729 (`owner_diagnostic` dedup-aware, ticket-linked
sysgraph Proposal), FRE-731 (System-domain observability monitor), FRE-732 (one-time KG cleanup)

## Scope

Add the dispatch step at the consolidator's output: route each extracted **entity** by its
(already-emitted, FRE-863) `output_kind`:

- `knowledge` (default, fail-open) → Core, unchanged existing path.
- `ephemeral` → **no Core write**. Already durably observed in Elasticsearch via the existing
  unconditional `write_capture()` → `schedule_es_index()` call at capture time (before
  consolidation ever runs) — no new code needed for the "observed in ES" half.
- `finding` → **no Core write**; routed to `sysgraph` as a durable, queryable append-only
  `sysgraph.stat` row (new `SysgraphRepository.record_finding()`), so it lands in a distinct home
  from both Core and the ephemeral/ES-only case (AC-3 requires exactly-one-home per kind).

Stances/claims are unconditionally `output_kind="knowledge"` at emission (FRE-863,
`entity_extraction.py:623,634`) — they never need a dispatch branch.

### Acceptance criteria carried (ADR-0115)

- **AC-2** — System-natured fixture → zero new `:Entity` nodes, raw item present in ES.
- **AC-3** — `output_kind` routes to exactly one home (per-fixture presence/absence check).
- **AC-5, dispatch half** — known System-natured fixtures produce zero `:Entity` nodes. (The
  persistence half — no entity has `class IS NULL` — depends on FRE-864 and is asserted jointly at
  master's integration gate per the ADR: "the assembled seam ... requires both (2) and (3)".)

### Why sysgraph.stat, not a Proposal (non-goal boundary vs FRE-729)

FRE-729 is explicitly chartered (Linear description, re-read before this plan) to: add the
`owner_diagnostic` value to the `sysgraph.proposal.source` CHECK constraint + `ProposalSource` enum,
capture a dedup-aware Proposal via `read_before_emit`, and write bidirectional ticket linkage. That
is real, separate scope (a schema/migration change + the ADR-0105 promotion machinery), not "does a
finding land in sysgraph at all." `sysgraph.stat` is a pre-existing, unconstrained, generic
append-only observation table (already used by `record_maintenance_run`) — using it here satisfies
AC-3's routing check without pre-building FRE-729's feature, and `sysgraph.derives_from`
(proposal_id → stat_id) already exists in the schema for FRE-729 to later cite this row as evidence
if it chooses.

## Codex round 1 — findings folded in

Codex plan-review (2026-07-12) confirmed the routing/scope design but caught one **critical** gap and
one real weakness, both folded into the design below:

1. **Critical — `create_conversation`'s `key_entities` MERGE leak (unfixed, this plan would have
   failed AC-2/AC-3).** `_process_capture` calls `memory_service.create_conversation(turn, ...)`
   *before* the entity-creation loop (`consolidator.py:668`, vs. the entity loop at `:682`).
   `TurnNode.key_entities` is built from `extraction_result.get("entity_names", [])`
   (`consolidator.py:655`) — the **unfiltered** list from `entity_extraction.py:899`, which includes
   ephemeral/finding names. `create_conversation` then unconditionally
   `MERGE (e:Entity {name: $name}) ... MERGE (t)-[:DISCUSSES]->(e)` for **every** name in
   `key_entities` (`memory/service.py:1052-1068`) — a bare Core node, regardless of `output_kind`.
   Gating only the later `create_entity` call (the original plan) would still leak a bare `:Entity`
   node into Core for every ephemeral/finding item via this earlier MERGE. **Fix:** partition
   `extraction_result["entities"]` by `output_kind` *before* constructing `turn`, and build
   `key_entities` from the `knowledge`-only partition.
2. **Failure mode — a swallowed `record_finding` failure silently violates AC-3.** If the sysgraph
   write fails after Core-write was already skipped, the item lands in neither home. Once
   `create_conversation` runs, `memory_service.turn_exists()` marks the capture consolidated
   (`consolidate_recent_captures`'s skip-gate) — this capture is **never reprocessed**, so a swallowed
   failure is a permanent, silent loss, not a transient one that self-heals next tick. **Fix:** count
   sysgraph-write failures in a *separate* counter from successful dispatch (never conflate "attempted"
   with "landed"), and log at WARNING with enough identity (entity name + trace_id) to reconstruct from
   ES. This does **not** add new retry/durability machinery — stance/claim/relationship writes in this
   same function already have this exact best-effort, non-retried risk profile (a failed
   `assert_stance`/`assert_claim`/`create_relationship` today is likewise not retried once the Turn
   exists); this makes the *new* finding write's failure mode match the *existing*, accepted risk
   profile of its siblings, not eliminate it — building real retry durability here would be new,
   out-of-scope machinery, not a fix to a regression this ticket introduces.
3. Confirmed no other call site needs dispatch-gating (single production consumer of
   `extraction_result["entities"]`/`entity_names"]` is `consolidator.py`).
4. Confirmed `create_relationship` is already safe (`MATCH`, not `MERGE`, on both endpoints) — no
   change needed there.
5. Confirmed AC-2's "raw item present in ES" is pre-existing, unmodified, best-effort behavior
   (`write_capture` → `schedule_es_index`, `captains_log/es_indexer.py:103-116` logs but never raises
   on a missing/failed indexer) — cited as evidence in the PR/ticket comment, not re-tested here (this
   diff doesn't touch that path).

## Files

1. **`src/personal_agent/sysgraph/repository.py`**
   - New query constant `_INSERT_FINDING_STAT` + method `record_finding(entity_name, entity_type,
     description, trace_id, session_id) -> None`, inserting into `sysgraph.stat` with
     `name='dispatch_finding_observed'`. No schema change (table already exists).

2. **`src/personal_agent/second_brain/consolidator.py`**
   - Import `SysgraphRepository, get_default_sysgraph_repo` from `personal_agent.sysgraph` (mirrors
     `captains_log/reflection.py`'s existing pattern — this is a producer/write path, not a
     recall/tutor path, so it doesn't violate `test_isolation.py`'s import-boundary check, which only
     scans `memory/orchestrator/tools`).
   - **Before** constructing `turn = TurnNode(...)`, partition `extraction_result.get("entities", [])`
     by `entity_data.get("output_kind", "knowledge")` into `knowledge_entities` / `ephemeral_entities`
     / `finding_entities` (anything not exactly `"ephemeral"` or `"finding"` is `knowledge` —
     fail-open, ADR-0115 D4). Build `key_entities` from `knowledge_entities` names only (mirrors
     `entity_extraction.py:899`'s own `if e.get("name")` filter). Leave `_entity_data` as the full,
     unfiltered list (harmless — `create_conversation` only looks up types for names actually present
     in `key_entities`).
   - Replace the entity-creation loop: iterate `knowledge_entities` for the existing (unchanged)
     `create_entity` Core-write path; iterate `ephemeral_entities` to count
     `entities_dispatched_ephemeral` (no write — already ES-observed via capture-time `write_capture`);
     iterate `finding_entities` to best-effort `sysgraph_repo.record_finding(...)` via
     `get_default_sysgraph_repo()` (the process-level singleton set at app startup, `service/app.py`),
     wrapped in try/except — **never raise** (a sysgraph hiccup must not abort this capture's
     knowledge/stance/claim writes) — incrementing `entities_dispatched_finding` only on confirmed
     success and a separate `entities_dispatch_finding_failed` on `None` repo or a write exception
     (logged at WARNING with entity name + trace_id).
   - Thread all four counters (`entities_dispatched_ephemeral`, `entities_dispatched_finding`,
     `entities_dispatch_finding_failed`, and keep `entities_created` for the knowledge path) through
     `_process_capture`'s return dict and `consolidate_recent_captures`'s aggregation + `summary` dict
     (mirrors the existing `stances_created`/`claims_created` pattern) — observability, not silent
     counting.

3. **Relationships** — no change. `create_relationship`'s Cypher uses `MATCH` (not `MERGE`) to
   locate both endpoints (`service.py:2451`); if a dispatched-away (ephemeral/finding) entity has no
   Core node, the `MATCH` finds nothing and the relationship is silently skipped — already safe,
   verified by reading the existing query, no new code needed.

## Tests

- **`tests/test_second_brain/test_consolidator_dispatch.py`** (new, unit, mocked `memory_service` +
  mocked sysgraph repo via `personal_agent.second_brain.consolidator.get_default_sysgraph_repo`,
  following `test_consolidator_claims_wiring.py`'s existing fixture/patch style):
  - ephemeral entity → `memory_service.create_entity` not called; `entities_dispatched_ephemeral == 1`.
  - finding entity → `create_entity` not called; mocked `record_finding` awaited once with the
    entity's name/type/description + capture's trace_id/session_id; `entities_dispatched_finding == 1`.
  - knowledge entity → unchanged regression (`create_entity` called, `entities_created == 1`).
  - **regression for the codex-caught leak:** a capture with an ephemeral and/or finding entity →
    inspect the `TurnNode` passed to `memory_service.create_conversation` and assert its
    `key_entities` contains **only** the knowledge entity's name (proves the MERGE-leak fix; this is
    the test that would have failed against the original, unrevised plan).
  - mixed turn (one of each kind in one capture's `entities[]`) → each routes to exactly one home in
    a single pass (mirrors the ADR's "Rafale + health check" example): knowledge in `key_entities` +
    `create_entity`, ephemeral in neither, finding in `record_finding` only.
  - missing/invalid `output_kind` on an entity dict → fails open to knowledge (defensive; extraction
    already normalizes this via FRE-863, but the consolidator's own default is asserted directly).
  - `get_default_sysgraph_repo()` returns `None` (sysgraph not wired, e.g. CLI/eval scripts) → finding
    entity is still skipped from Core, `entities_dispatch_finding_failed == 1`, a warning is logged,
    and `_process_capture` completes without raising.
  - `record_finding` raises → `entities_dispatch_finding_failed == 1` (not counted as
    `entities_dispatched_finding`), no exception propagates out of `_process_capture`.

- **`tests/personal_agent/sysgraph/test_repository.py`** (existing file, integration,
  `@pytest.mark.integration`, needs `make test-infra-up`): add
  `test_record_finding_inserts_a_queryable_stat_row`, mirroring
  `test_record_maintenance_run_inserts_a_queryable_stat_row` — asserts a `sysgraph.stat` row with
  `name='dispatch_finding_observed'` and the expected metadata JSON, cleaned up after.

## Verify

```
make test-file FILE=tests/test_second_brain/test_consolidator_dispatch.py
make test-file FILE=tests/test_second_brain/test_consolidator_claims_wiring.py   # regression
make test-infra-up && make test-file FILE=tests/personal_agent/sysgraph/test_repository.py
make test        # full fast suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Code-review round (Step 8, effort=high) — findings folded in

The `code-review` workflow (high effort) confirmed one real correctness bug and two cleanup
items, all fixed on-branch before the PR:

1. **CONFIRMED — relationships loop not filtered by dispatch.** The entity-creation loop now only
   MERGEs Core nodes for `knowledge` entities, but the relationships loop was left iterating *all*
   extracted relationships unfiltered. A relationship touching an ephemeral/finding endpoint would
   either silently no-op (`create_relationship`'s `MATCH` finds nothing) or — worse — splice an edge
   onto an unrelated pre-existing Core entity that happens to share the dispatched-away entity's name
   from a prior turn. **Fix:** build `dispatched_away_names` (ephemeral ∪ finding entity names from
   this turn) and skip + count (`relationships_dispatch_skipped`) any relationship whose source or
   target matches, with a WARNING log. Regression tests added: skip-on-dispatched-endpoint, and a
   guard test proving ordinary knowledge-to-knowledge relationships are unaffected.
2. **CONFIRMED — new test file used the legacy test path.** `tests/personal_agent/second_brain/`
   already exists and is where genuinely new second_brain test files land (e.g. `test_taxonomy.py`,
   2026-07-05) per CLAUDE.md §2/§4's `tests/personal_agent/<module>/` convention — the older
   `tests/test_second_brain/` siblings (`test_consolidator_claims_wiring.py` etc.) predate that
   convention. Moved the new file to `tests/personal_agent/second_brain/test_consolidator_dispatch.py`.
3. **PLAUSIBLE — `_entity_data` re-derived `all_entities` independently.** Fixed to reuse the
   already-computed `all_entities` local instead of a second `extraction_result.get("entities", [])`
   call, removing a maintenance trap where the two could silently diverge.

## Non-goals (confirmed against sibling tickets before writing this plan)

- `owner_diagnostic` source discriminator, dedup-aware Proposal, bidirectional ticket linkage → **FRE-729**.
- System-domain observability monitor (volume/grounding signals, ADR-0106 D6) → **FRE-731**.
- One-time cleanup/reclassification of already-accreted System entities → **FRE-732**.
- `class` persistence on `:Entity` (ADR-0115 D2) → **FRE-864** (parallel, separate ticket).
- Class-aware recall ranking (ADR-0115 D6) → unowned follow-up, not any current ticket.
