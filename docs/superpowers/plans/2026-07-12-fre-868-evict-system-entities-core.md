# FRE-868: Evict existing System-natured entities from Core

**Ticket:** FRE-868 (Approved, Tier-2:Sonnet, stream:build2)
**Backing ADR:** ADR-0115 D3 (isolation by absence-of-write) + Implementation Notes step 5 +
Risks table row "Existing ~7,992 `class=None` entities stay unclassified"
**Depends on (merged):** FRE-865 (backfill — sets `class_backfill_output_kind` on System-natured
`:Entity` nodes, does not remove them), FRE-728 (write-time dispatch for **new** extractions only —
confirmed by reading its diff to have no sweep over existing nodes)
**Related:** FRE-729 (owner_diagnostic Proposal + ticket-linkage pipeline over sysgraph findings —
a richer downstream consumer of `sysgraph.stat` rows, explicitly out of scope here, same as it was
for FRE-728)

## Scope (from ticket + ADR)

- Build a one-time sweep that consumes the `class_backfill_output_kind` marker FRE-865 wrote on
  existing `:Entity` nodes (`ephemeral` or `finding` — `knowledge`-natured entities were classed
  directly by FRE-865 and never carry this marker) and **removes those entities from Core**,
  completing ADR-0115 D3's "absent from Core" invariant for the pre-existing corpus (FRE-728 only
  gates new writes).
- `finding` → route to `sysgraph` via `SysgraphRepository.record_finding()` (the same sink FRE-728
  uses for new `finding` items), then delete the node from Core.
- `ephemeral` → delete the node from Core directly. No sysgraph write — `ephemeral` never gets one
  in FRE-728 either (D3: "ephemeral → ES only"; the original capture was already durably observed
  in Elasticsearch at extraction time via `write_capture`, independent of this sweep).
- Idempotent: a deleted node cannot match the candidate predicate again, so re-running converges to
  zero new deletions.
- Run-id rollback, proven on the **test substrate only** — this ticket does not run against prod.
  The prod run is a separate, later, master-gated ops action behind `--confirm-prod` (same posture
  as FRE-865/FRE-772).
- Report: counts evicted by kind (`ephemeral`/`finding`), before/after System-in-Core count (nodes
  still carrying `class_backfill_output_kind`).

## Design decisions

**Revised after a codex plan-review pass (2026-07-12) — see "Codex findings addressed" below for
what changed and why.**

1. **Deletion is destructive — rollback needs a real snapshot, not FRE-865's in-place property
   restore.** FRE-865's rollback works because it never removes the node, only sets/unsets
   properties on a node that still exists. This ticket **deletes** the node, so a naive `run_id`
   marker on the node is gone with it. Rollback must reconstruct the node from a **snapshot written
   before deletion** — mirrors `migrate_fre772_entity_type_v2.py`'s snapshot-file pattern, but a
   flat `{name, entity_type}` snapshot is insufficient here because we're recreating a whole node
   (not just resetting one property) plus its edges.
   - Before deleting a candidate, capture a `NodeSnapshot`: `old_element_id`, `labels`, full
     `properties(e)` (type-tagged — see decision 7), and every relationship touching it — `type`,
     direction (`outgoing: bool`), `old_rel_element_id` (needed for dedup, decision 8), the **other
     endpoint's** `old_element_id`/`labels`/`stable_key`/`properties`, and the relationship's own
     (type-tagged) properties. One Cypher call:
     `MATCH (e)-[r]-(other) WHERE elementId(e) = $eid RETURN elementId(r) AS rel_eid, type(r),
     elementId(startNode(r)) = $eid AS outgoing, properties(r) AS rel_props,
     elementId(other) AS other_eid, labels(other) AS other_labels, properties(other) AS other_props`.
   - **Snapshot is written durably BEFORE any mutation for that candidate — not accumulated in
     memory and flushed once at the end.** Per candidate: (1) capture `NodeSnapshot` (read-only,
     zero mutation), (2) append it as one line to a JSONL file at `--snapshot-path` and flush+fsync,
     (3) only then proceed to the sysgraph write (`finding`) and/or `DETACH DELETE`. If the process
     crashes between (2) and (3), the node still exists (nothing was mutated yet) — it is simply a
     candidate again next run, no rollback is needed, and no data was lost. This closes the gap
     codex flagged (finding 1): writing the snapshot only after `run_sweep` returns meant a crash
     right after `DETACH DELETE` but before the file write would have destroyed the only undo
     record.
   - `--snapshot-path` is **required** for any applying (non-dry-run, non-rollback) invocation — the
     CLI refuses to run with nowhere to durably write the undo record before it deletes anything.
   - **Rollback** (`--rollback --run-id <id> --snapshot-path <path>`), two passes:
     - **Pass 1 — recreate nodes, idempotently.** For each snapshot in `run_id`: `MERGE (n {label,
       fre868_restored_from_element_id: old_element_id})` (not `CREATE`) so a rollback re-run after a
       partial crash matches the already-recreated node instead of duplicating it (closes codex
       finding 2), `ON CREATE SET` the rest of the (type-tagged, decoded) properties. Build an
       `old_eid → new_eid` map from the result.
     - **Pass 2 — reconnect relationships, idempotently and without double-restore.** For each
       snapshot's relationships: resolve the other endpoint via the `old_eid → new_eid` map first
       (covers Entity↔Entity pairs where **both** ends were evicted in the same run); if not in the
       map, resolve via the neighbor's **stable key** (decision 9), never raw `elementId` (closes
       codex finding 4 — elementId is not treated as a durable cross-time identity for
       rollback-file purposes). `MERGE` the relationship keyed on a stamped
       `fre868_restored_rel_id = old_rel_element_id` property (closes codex finding 3): since each
       Entity↔Entity edge is captured once in **each** endpoint's snapshot, restoring it from the
       second snapshot would otherwise create a duplicate edge — the `MERGE` on the old relationship
       id makes the second attempt a no-op. If the neighbor cannot be resolved by either the map or a
       stable key (independently deleted since, or an unsupported neighbor label — decision 9),
       skip that one relationship and report it (type + old neighbor id) rather than silently
       dropping it.
   - This is a deliberately test-substrate-scoped rollback design (same posture as FRE-865 decision
     7 and FRE-772): a prod rollback story, if ever needed, is designed when a prod run is actually
     planned — flagged explicitly, not silently assumed.
   - **Rollback does not touch sysgraph.** `sysgraph.stat` is an append-only observation log (ADR-
     0105) — a `finding` row this sweep wrote stays after a Core-side rollback of the entity it
     described. Stated explicitly (codex raised this as an open question) rather than left implicit.

2. **Dispatch order: snapshot durably persisted, then sysgraph write, then delete — and delete only
   on sysgraph success.** For a `finding` candidate, the full per-candidate order is: (1) snapshot +
   durable write (decision 1), (2) `record_finding()`, (3) `DETACH DELETE` only if (2) succeeded. If
   the sysgraph write raises, log a warning, leave the node untouched (marker stays, retried next
   run), and count it as `dispatch_finding_failed` — mirrors FRE-728's "best-effort against the
   process-level singleton, never silently conflated with landed" posture. **Accepted, documented
   gap (narrowed, not eliminated, by decision 1's reordering):** if the sysgraph write succeeds but
   the subsequent `DETACH DELETE` fails (process crash strictly between (2) and (3), a narrow
   window), the node still carries the marker and is re-swept next run, producing a **duplicate**
   `sysgraph.stat` row. `sysgraph.stat` is an append-only observation sink (not a ledger), so a
   duplicate observation is not a correctness bug the same way a duplicate financial transaction
   would be. A true fix needs an idempotency key on `record_finding`/the `sysgraph.stat` schema — a
   change to a shared component out of this ticket's scope (codex's own conclusion on this point) —
   so this is accepted and documented, matching the precedent FRE-865 set for its own accepted
   mid-run-crash gap.
3. **Script pattern: new `GraphProtocol` (Neo4j) + new minimal `SysgraphProtocol` seam**, both
   fakeable for unit tests, mirroring `migrate_fre865_entity_class_backfill.py`'s
   `GraphProtocol`-behind-a-Protocol structure. No `CostGate`/LLM involved at all — this sweep is
   purely mechanical (it only reads a marker FRE-865 already computed), which is materially simpler
   than FRE-865/FRE-772.
4. **Candidate predicate:** `MATCH (e:Entity) WHERE e.class_backfill_output_kind IS NOT NULL`. Only
   values FRE-865 ever writes are `"ephemeral"`/`"finding"` (verified by reading
   `migrate_fre865_entity_class_backfill.py`'s `mark_for_dispatch` call site — only reached in the
   `else` branch when `output_kind != "knowledge"`), so no third branch is needed, but the script
   defensively logs and skips (never deletes) any unrecognized value rather than assuming.
5. **Report shape** (`SweepReport`): `run_id`, `dry_run`, `started_at`, `finished_at`,
   `before_marked_count` (nodes with the marker, i.e. today's System-in-Core count),
   `after_marked_count` (should trend to 0 across full runs), `before_total_entities`,
   `after_total_entities`, `evicted_ephemeral`, `evicted_finding`, `dispatch_finding_failed`,
   `total_candidates_this_run`, `success`. `_print_summary` prints all of these plus a warning line
   if `dispatch_finding_failed > 0`.
6. **CLI:** `--confirm-prod`, `--dry-run` (preview only, zero writes — including zero sysgraph
   writes and zero snapshot-file writes), `--snapshot-path <path>` (required when applying),
   `--rollback --run-id <id> --snapshot-path <path>` (restore from the file), `--batch-size`,
   `--report-path`. Same prod-write guard as FRE-865/FRE-772
   (`AGENT_ENVIRONMENT != test` requires `--confirm-prod`).
7. **Property type-tagging for JSON round-trip.** Neo4j returns native Python objects for temporal
   properties (`neo4j.time.DateTime`, etc.) that are not JSON-serializable and, per FRE-865's own
   documented `last_seen` heterogeneity (`migrate_fre865_entity_class_backfill.py:285`), matter for
   correctness, not just serialization. At capture, any property value exposing `.iso_format()` (or
   `isinstance(..., (datetime, ...))` for driver-native temporal types) is stored as
   `{"__fre868_type__": "datetime", "value": <iso string>}`; every other JSON-compatible value
   (str/int/float/bool/list/dict — covers `Entity.properties`' freeform dict and the `embedding`
   float list) is stored as-is. At restore, tagged values are rehydrated via Cypher `datetime($v)`;
   everything else is set as a literal. This closes codex finding 6 with a bounded mechanism (same
   spirit as FRE-865's `toString()` coercion, not a generic Neo4j-type serializer) rather than
   silently downgrading the rollback promise to lossy.
8. **Relationship de-duplication via `elementId(r)`.** Every relationship is captured once from
   *each* endpoint's snapshot (an Entity↔Entity edge where both ends are evicted appears in both
   `NodeSnapshot`s). `RelSnapshot.old_rel_element_id` (Neo4j's own `elementId(r)`, stable for the
   life of the relationship) is the dedup key both at rollback time (decision 1, pass 2's `MERGE`)
   and is asserted-unique in tests — closes codex finding 3.
9. **Neighbor resolution uses a per-label stable key, never raw `elementId`, for anything not
   resolved via the same-run old→new map.** `elementId` is Core's internal identity — safe to
   compare *within* a single sweep run (decision 8), but codex correctly flagged it as a brittle
   *durable* identity for a rollback file read back later. Supported stable keys, matching this
   corpus's actual neighbor shape (`consolidator.py`'s `create_conversation`/`create_entity`/
   `create_relationship` paths — `Entity`↔`Entity` and `Turn`-`DISCUSSES`→`Entity`): `Entity` →
   `name` (the same property `create_entity`'s own `MERGE` key uses); `Turn` → `turn_id`. Any other
   neighbor label is captured (for the report) but flagged `restorable: false` at snapshot time and
   reported, not attempted, at rollback — an explicit, accepted scope limit rather than a silent
   elementId-based reconnect that could attach to the wrong node after a long time gap.

## Codex findings addressed (plan-review, 2026-07-12)

A codex plan-review pass on the first draft of this plan (before any code was written) confirmed 6
issues. Disposition:

1. **Snapshot durability is after-the-fact — a crash after delete but before the file write loses
   the only undo record (critical).** → decision 1: snapshot is written durably (flush+fsync) per
   candidate *before* any mutation for that candidate, not accumulated and flushed once at the end
   of `run_sweep`.
2. **Rollback is not idempotent after a partial-rollback crash — rerunning duplicates recreated
   nodes (high).** → decision 1 pass 1: node recreation uses `MERGE` keyed on a stamped
   `fre868_restored_from_element_id`, not unconditional `CREATE`.
3. **Entity↔Entity relationships between two evicted nodes are captured from both endpoints and
   would be restored twice (high).** → decisions 1 (pass 2) and 8: `RelSnapshot` now carries the
   original `elementId(r)`; rollback `MERGE`s on a stamped `fre868_restored_rel_id`, making the
   second endpoint's restore attempt a no-op.
4. **Reconnecting non-evicted neighbors by raw `elementId` is a brittle durable identity for a
   rollback file (medium).** → decision 9: neighbor resolution uses a per-label stable key
   (`Entity.name`, `Turn.turn_id`); any other neighbor label is flagged non-restorable and reported,
   never silently attached via elementId.
5. **Sysgraph repository lifecycle (`connect()`/`disconnect()`) was underspecified (medium).** →
   Atomic step 2 below: CLI explicitly connects the `SysgraphRepository` before use and disconnects
   in a `finally`, mirroring the Neo4j driver lifecycle.
6. **Snapshot JSON serialization is lossy for Neo4j native temporal property types (medium).** →
   decision 7: type-tagged capture/restore for datetime-shaped values, bounded to the mechanism
   FRE-865 already established for the same class of problem (`toString()`/coercion), not a generic
   serializer.

Two open questions codex raised are answered explicitly in decision 1/2 rather than left implicit:
rollback does **not** delete the `sysgraph.stat` row a `finding` eviction wrote (append-only log,
ADR-0105); the sysgraph-write-then-delete crash window is narrowed (by fix 1) but not eliminated,
and is accepted/documented rather than solved with cross-substrate two-phase commit.

## Files

- **New:** `scripts/sweep_fre868_evict_system_entities.py` — `GraphProtocol` + `SysgraphProtocol`
  seams, `_Neo4jGraph`/`_SysgraphSink` real impls, `run_sweep`/`run_rollback` orchestration, CLI.
- **New:** `tests/scripts/test_sweep_fre868_evict_system_entities.py` — unit tests, in-memory
  `FakeGraph` + `FakeSysgraphSink`, no Neo4j/Postgres (CI-gating).
- **New:** `tests/scripts/test_sweep_fre868_evict_system_entities_integration.py` — integration test
  against real test-substrate Neo4j (`:7688`) + test-substrate sysgraph Postgres (`:5433`),
  `pytest.mark.integration`, seeded fixture corpus (an `ephemeral`-marked entity, a `finding`-marked
  entity, an unmarked/knowledge entity that must be left alone, and an Entity↔Entity relationship
  between two marked entities to exercise the both-ends-evicted rollback path).

## Atomic steps

1. Write failing unit tests in `tests/scripts/test_sweep_fre868_evict_system_entities.py` against
   the not-yet-existing module:
   - `ephemeral` candidate → deleted, no sysgraph call, `evicted_ephemeral` incremented.
   - `finding` candidate → `record_finding()` called with the entity's name/type/description, then
     deleted, `evicted_finding` incremented.
   - `finding` candidate whose `record_finding()` raises → node NOT deleted, `class_backfill_*`
     markers untouched, `dispatch_finding_failed` incremented.
   - unmarked / `knowledge`-classed entities are never fetched as candidates (predicate excludes
     them).
   - idempotent re-run: after eviction, a second `run_sweep` call fetches zero candidates (verifies
     against a `FakeGraph` whose deleted nodes are actually removed from its in-memory store).
   - dry-run: zero graph writes, zero sysgraph calls, but counts still populate the preview report.
   - unrecognized `class_backfill_output_kind` value → skipped (not deleted), logged, counted
     separately, never crashes the run.
   - snapshot capture: a candidate with one Entity↔Entity relationship and one Turn-typed neighbor
     produces a `NodeSnapshot` with both relationships recorded correctly (type, direction,
     `old_rel_element_id`, other endpoint identity/labels/stable-key/properties).
   - snapshot capture: a datetime-shaped property round-trips through the type-tagged
     capture/restore helper unchanged (decision 7).
   - snapshot is written (flush call observed on the fake file handle) *before* the fake graph's
     delete/sysgraph-write call happens for that candidate — asserts the ordering in decision 1/2,
     not just the end state.
   - `--snapshot-path` omitted on an applying (non-dry-run) call → refuses to run (raises/returns a
     clear error) rather than deleting with no undo record.
   - rollback: given snapshots for a `run_id`, recreates each node (`MERGE` on
     `fre868_restored_from_element_id`) with original (decoded) properties, reconnects relationships
     whose other endpoint still exists (resolved by stable key, not raw `element_id`) and those
     whose other endpoint was *also* evicted this run (via the old→new map built during the same
     rollback call).
   - rollback idempotency: running `run_rollback` twice for the same `run_id`/snapshots produces the
     same restored-node and restored-relationship count both times (no duplicates) — proves the
     `MERGE`-on-`fre868_restored_from_element_id` / `fre868_restored_rel_id` fix.
   - rollback dedup: an Entity↔Entity relationship captured in *both* endpoints' snapshots (both
     ends evicted) is restored exactly once, not twice — proves the `elementId(r)` dedup fix.
   - rollback: a relationship whose recorded other-endpoint no longer resolves (neither in the map
     nor via stable key) is skipped and reported by name — not silently dropped.
   - rollback: a neighbor with an unsupported label (neither `Entity` nor `Turn`) is flagged
     `restorable: false` at snapshot time and reported (not attempted) at rollback.
   → verify: `uv run pytest tests/scripts/test_sweep_fre868_evict_system_entities.py -x` fails with
   `ModuleNotFoundError`.
2. Implement `scripts/sweep_fre868_evict_system_entities.py`:
   - `EvictionCandidate` (element_id, name, entity_type, description, output_kind) dataclass.
   - `_encode_value`/`_decode_value` helpers implementing decision 7's type-tagging
     (`{"__fre868_type__": "datetime", "value": <iso>}` round-trip; everything else passed through).
   - `RelSnapshot` (rel_type, outgoing, old_rel_element_id, other_element_id, other_labels,
     other_stable_key: `tuple[str, str] | None` (label, key value) or `None` when unsupported,
     restorable: bool, other_properties, rel_properties) + `NodeSnapshot` (element_id, labels,
     properties, relationships: `list[RelSnapshot]`) dataclasses — encoded via `_encode_value` before
     JSON serialization (`orjson`), decoded via `_decode_value` on load.
   - `GraphProtocol` (`count_marked`, `count_total_entities`, `fetch_candidates`, `snapshot_node`,
     `delete_node`, `restore_node` (idempotent `MERGE`), `restore_relationship` (idempotent
     `MERGE`)) + real `_Neo4jGraph` Cypher impl.
   - `SysgraphProtocol` (`record_finding(name, entity_type, description) -> None`, minimal — no
     `trace_id`/`session_id` available for a backfilled node, pass `None` for both, matching
     `record_finding`'s existing `str | None` signature) + real `_SysgraphSink` wrapping a
     `SysgraphRepository` constructed from `settings.sysgraph_database_url`, with explicit
     `connect()`/`disconnect()` lifecycle methods the CLI calls around the sweep (mirrors the Neo4j
     driver lifecycle — this script is not the running app, so no process-level
     `get_default_sysgraph_repo()` singleton is set; closes codex finding 5).
   - `SnapshotWriter` (thin wrapper around an open file handle: `write(snapshot)` appends one JSON
     line + `flush()` + `os.fsync()`) — constructed once per run, passed into `run_sweep`.
   - `run_sweep(graph, sysgraph, snapshot_writer, *, run_id, now, dry_run, batch_size) ->
     SweepReport`: pages `fetch_candidates`, per candidate: unrecognized `output_kind` →
     skip+count+log (no snapshot, nothing to undo); otherwise `snapshot_node` → (`dry_run`: stop
     here, count only) → `snapshot_writer.write()` (durable) → `finding`: `record_finding`
     (catch+count+log on failure, skip delete) then `delete_node` on success; `ephemeral`:
     `delete_node` directly.
   - `run_rollback(graph, snapshot_path, run_id) -> (restored_node_count, restored_rel_count,
     skipped: list[str])`: load every line from `snapshot_path` matching `run_id`, pass 1 restores
     nodes (`old_eid → new_eid` map), pass 2 restores relationships per decision 1's resolution
     order, deduped via `old_rel_element_id`.
   - CLI: `--confirm-prod`, `--dry-run`, `--snapshot-path`, `--rollback --run-id`, `--batch-size`,
     `--report-path`; refuse to apply (non-dry-run, non-rollback) without `--snapshot-path`; refuse
     `--rollback` without both `--run-id` and `--snapshot-path`.
   → verify: unit tests from step 1 pass:
   `uv run pytest tests/scripts/test_sweep_fre868_evict_system_entities.py -v`.
3. Write the integration test (`make test-infra-up` first), seeding: an `ephemeral`-marked entity,
   a `finding`-marked entity, a `knowledge`-classed entity (must survive untouched), an
   Entity-RELATED_TO-Entity edge between the two marked entities (exercises the both-ends-evicted
   rollback dedup path) plus one edge from a `Turn` fixture to a marked entity (exercises the
   stable-key rollback path), and a datetime-valued property on one marked entity (exercises
   type-tagged round-trip). Assert: marked entities and their edges are gone from Core after the
   sweep; the `finding` entity produced a queryable `sysgraph.stat` row (query `sysgraph.stat WHERE
   name = 'dispatch_finding_observed'`); the `knowledge` entity and its edges are untouched; a
   second sweep run makes zero further deletions; rollback from the written snapshot file recreates
   both evicted entities with their original (including datetime) properties, reconnects both the
   Entity↔Entity edge (exactly once, not twice) and the Turn↔Entity edge, and running that same
   rollback call again produces the same restored counts (idempotent). Clean up seeded/restored
   nodes and rows in a `finally` block.
   → verify: `uv run pytest -m integration
   tests/scripts/test_sweep_fre868_evict_system_entities_integration.py -v` (test substrate already
   redirects to :7688/:5433 per `tests/conftest.py`).
4. Quality gates: `make test-file
   FILE=tests/scripts/test_sweep_fre868_evict_system_entities.py`, then full `make test` ·
   `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
5. Self-review: `code-review` skill at `medium`-to-`high` effort (new script performing destructive
   prod-eligible deletes, touches two substrates); `security-review` (Cypher params, sysgraph writes,
   file-based snapshot I/O — check path handling and that no secrets land in the JSON report).
6. PR + Linear comment per skill Step 9 — must state plainly that this ticket **proves the sweep on
   the test substrate only**; it does not run against prod. The prod run is a separate, later,
   master-gated ops action (same posture as FRE-865), and only after it runs does ADR-0115 AC-2/AC-3
   actually hold for the **pre-existing** corpus (new-write dispatch, FRE-728, already holds for new
   traffic).

## Post-implementation code review (workflow-backed, high effort) — 5 confirmed findings, all fixed

A background code-review workflow (`code-review` skill, high effort) found 5 confirmed correctness
defects after implementation, all fixed on this branch before PR:

1. **`--rollback --dry-run` silently performed real writes.** `run_rollback` accepted no `dry_run`
   parameter and the CLI's rollback branch never checked `args.dry_run` — a preview request
   recreated nodes/relationships for real via `apoc.merge.node`/`apoc.merge.relationship`. **Fixed**
   by threading `dry_run` through `run_rollback`: node "recreation" is simulated with an identity
   old→new mapping (sufficient to detect same-run both-evicted pairs) and the relationship loop
   short-circuits before the mutating `restore_relationship` call, while the read-only
   `find_element_id_by_stable_key` lookup still runs so preview counts stay accurate. Locked in with
   `test_rollback_dry_run_issues_zero_graph_writes` (asserts `restore_node_calls`/
   `restore_relationship_calls == 0`) and `test_rollback_dry_run_still_reports_unresolvable_relationships`.
2. **A failed relationship restore vanished silently.** When `restore_relationship()` returned
   `False` (the MATCH inside it found no start/end node), the code still marked the relationship
   `seen` and moved on — never counted in `restored_rel_count`, never added to `skipped`. The
   printed summary undercounted with no trace of the failure. **Fixed**: a `False` return now
   appends a `skipped` entry. Locked in with `test_rollback_counts_and_reports_a_failed_relationship_restore`.
3. **A Neo4j `Date` property was upcast to `DateTime` on restore.** The type-tagging (decision 7)
   duck-typed on `iso_format()` alone, so `Date`, `DateTime`, and `Time` were indistinguishable at
   decode time and all reconstructed via `datetime.fromisoformat()`. **Fixed**: tagging now
   dispatches on the exact `neo4j.time` type name (`Date`/`DateTime`/`Time`) and reconstructs via
   that type's own `from_iso_format`, so a bare `Date` restores as a `Date`. Locked in with
   `test_date_property_round_trips_as_date_not_datetime`.
4. **`SweepReport.success` was dead code.** Initialized `True` and never set `False` anywhere in
   `run_sweep`, so the documented `0 if report.success else 4` CLI exit-code contract never fired —
   a run with failed sysgraph dispatches or unrecognized marker values (both already WARNING-logged)
   still exited 0. **Fixed**: `success` is now set from the two failure signals the run already
   tracks (`dispatch_finding_failed == 0 and unrecognized_marker_count == 0`). Locked in with
   `test_success_is_false_when_a_finding_dispatch_fails`,
   `test_success_is_false_when_an_unrecognized_marker_is_seen`, `test_success_is_true_for_a_clean_run`.
5. **A non-restorable both-ends-evicted relationship was reported in `skipped` twice.**
   `seen_rel_ids` was only populated on the restorable path, so a relationship captured (with the
   same `old_rel_element_id`) in both evicted endpoints' snapshots — but flagged non-restorable —
   slipped past the dedup guard on its second sighting. **Fixed**: the dedup check/mark now runs
   before the restorable branch, covering both paths uniformly. Locked in with
   `test_rollback_dedupes_non_restorable_relationship_from_both_evicted_ends` (the missing coverage
   the review itself flagged).

A parallel `security-review` (general-purpose subagent, staged-diff scope) found **no HIGH/MEDIUM
findings** — every Cypher parameter is bound (`$name`), the one f-string label interpolation in
`find_element_id_by_stable_key` is allowlisted to two hardcoded literals (`Entity`/`Turn`) before
any query executes, and the sysgraph write goes through `SysgraphRepository`'s standard
role-checked `connect()` with no boundary bypass.

## Explicitly out of scope for this ticket

- Running against prod Neo4j/sysgraph Postgres (cloud-sim-*) — a separate, later, master-gated ops
  action, same posture as FRE-865/FRE-772.
- FRE-729's richer `owner_diagnostic` Proposal + ticket-linkage pipeline — this ticket only writes
  the same flat `sysgraph.stat` row FRE-728 already writes for new `finding` items; a dedup-aware
  pipeline consuming those rows remains FRE-729's separate scope.
- Any change to `entity_extraction.py`, `consolidator.py`, or FRE-865's backfill script — this
  ticket only reads the `class_backfill_output_kind` marker FRE-865 already writes and consumes it;
  it does not change how that marker is computed.
- A prod-grade, concurrency-safe rollback story (e.g. guarding against a same-named node recreated
  by live traffic between sweep and rollback) — deferred to when a prod run is actually planned, per
  decision 1.
