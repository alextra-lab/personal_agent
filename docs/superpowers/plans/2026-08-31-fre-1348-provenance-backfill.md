# FRE-1348 — provenance_state backfill: reconstruct from captures before marking none

Implements ADR-0098 Amendment A · A5. Backing PR (T2/T3): #1007 (`fre-1346-source-provenance`).

## Scope

Legacy `:Entity`/`:Claim` nodes and extracted relationships predate ADR-0098 Amendment A and
carry `provenance_state IS NULL` (the silent third state A5 forbids). This ticket replays T2's
containment rule (`memory/provenance.py`, already shipped) offline against each item's minting
capture, reconstructing real provenance where the address is recoverable, and marking `none`
only where it genuinely is not.

**Entities and Claims are reconstructable** — both carry a durable back-pointer to their minting
capture (`Entity.originating_trace_id`, `Claim.trace_id`), set ON CREATE by every write path.
**Relationships are not** — `create_relationship` never persists a trace/session key (A4b), so
they get a flat `none` stamp, reported separately, never a reconstruction attempt.

**Deliberate widening beyond a literal `IS NULL` filter for entities (not claims):**
`memory/service.py`'s `create_conversation` inline bare `:Entity` MERGE (the FRE-1346 self-review
fold-in, `service.py:1320`) stamps `provenance_state = COALESCE(e.provenance_state, 'none')` on
**every** mention of **every** entity, including a legacy one that has never been through
reconstruction. Since FRE-1346 already shipped and deployed, by the time this migration runs some
legacy entities will already read `'none'` from that incidental touch, not from a genuine
reconstruction failure. The entity candidate query must therefore select
`provenance_state IS NULL OR provenance_state = 'none'`, not `IS NULL` alone, or a real subset of
recoverable entities would be silently skipped — this is exactly the failure AC-2 exists to catch.
Claims have no equivalent bare-merge path (`assert_claim` is the only claim writer and always
stamps state explicitly at CREATE), so the claim query stays `IS NULL` only — re-scanning claims
already correctly marked `none` at write time would just waste reads for no benefit.

The transition is one-way (A5): setting `provenanced` is safe from *any* prior state (NULL, an
out-of-enum value, or `'none'` — 'provenanced' is the lattice's terminal state, so this write is
never guarded, just unconditional `SET`), while a `'none'` write is guarded by a `CASE` that
preserves an already-`'provenanced'` value untouched. The migration is idempotent and safely
re-runnable, but **not** "touches nothing on a second run" — every candidate whose predicate still
matches (see the widened predicate below) is re-examined every run, which is deliberate: a
candidate correctly left `'none'` today may become reconstructable later (a purged capture
restored, an ES-only capture indexed). Cost is extra reads, never incorrect writes.

**Codex plan-review round (2026-08-31) found 7 issues in the first draft, folded in below:**
finding 1 confirmed the widened filter is necessary (and found a *second* incidental-`'none'`
source beyond the one already named — `create_entity`'s own sourceless-write path,
`service.py:2211`, not just `create_conversation`'s bare MERGE); finding 6 additionally showed the
predicate must catch **any** out-of-enum value, not just `'none'`, to make AC-1 hold by
construction — so every candidate query (entities, claims, and relationships-with-weight) now
selects `provenance_state IS NULL OR NOT provenance_state IN ['provenanced','none']`, a strict
superset of the original `IS NULL OR = 'none'`. Finding 2 showed `r.weight IS NOT NULL` matches
every *current* production write site but is not an enforced invariant of `create_relationship`'s
API — the bulk relationship query now additionally excludes a frozen set of the known fixed-label
structural relationship types (`DISCUSSES`, `PARTICIPATED_IN`, `NEXT`, `CONTAINS`, `SOURCED_FROM`,
`HAD_DESCRIPTION`, `HAS_FACT`, `HAS_STANCE`, `OPERATED_BY`, `CURRENTLY_AT`) as a defensive second
discriminator. Finding 3 showed a disk-only capture lookup can report "missing" for a capture that
is only in Elasticsearch (`write_capture` writes disk synchronously but schedules the ES index as
a best-effort fire-and-forget task, and the reverse gap exists too, per `load_session_captures`'s
own union-not-replica documentation, `capture.py:627`) — capture resolution now takes an optional
`es_client` and falls back to a **batched** `ids` query across `{CAPTURES_INDEX_PREFIX}-*` for
whatever a page's disk lookups missed, mirroring `_read_session_captures_from_es`'s existing index
pattern and query shape; the CLI wires a real client, unit tests pass `es_client=None` (disk-only,
deterministic). Finding 4 was a plan-wording ambiguity, not a design bug — `_reconstruct` maps
`associate()`'s matched ids back to their `SourceRecord` objects, never returns ids directly.
Finding 5 added `entities_errors`/`claims_errors` fields so each kind's buckets reconcile exactly:
`reconstructed + none_no_match + none_missing_capture + errors == total`. Finding 7's race is
closed by the `CASE`-guarded `'none'` write described above, replacing the original separate
`WHERE ... SET` (whose guard could theoretically read stale relative to a concurrent live write —
the `CASE` reads and writes atomically within one `SET`, mirroring `create_entity`'s own existing
`CASE WHEN size($source_records) > 0 THEN 'provenanced' ELSE coalesce(...)  END` pattern,
`service.py:2217`).

## Files

1. **`src/personal_agent/captains_log/capture.py`** — add:
   - `build_capture_index() -> dict[str, pathlib.Path]`: one-time directory listing (filenames
     only, nothing parsed) mapping `trace_id -> file path`, across every date directory. Avoids an
     O(candidates × captures) full-directory scan when resolving many trace_ids.
   - `async def read_captures_by_trace_ids(trace_ids, *, disk_index, es_client=None) -> dict[str, TaskCapture]`:
     resolves a **batch** of trace_ids at once. Each is first looked up in `disk_index` and parsed
     (mirrors `_scan_captures`'s parse conventions — nil-UUID injection for pre-FRE-343 files,
     `_safe_error_summary` logging on a corrupt file, never raises). Whatever misses disk is
     resolved with **one** ES `search` (`ids` query) across
     `f"{CAPTURES_INDEX_PREFIX}-*,-{SUBAGENT_CAPTURES_INDEX_PREFIX}-*"` when `es_client` is given
     — mirrors `_read_session_captures_from_es`'s existing index pattern/`ignore_unavailable`/
     `allow_no_indices` shape (`capture.py:565-590`). `es_client=None` skips ES entirely (disk-only
     — the unit-test path). Returns only what was actually found; a trace_id absent from the
     result is genuinely unresolvable in either store right now.

2. **`scripts/migrate_fre1348_provenance_backfill.py`** — new one-time backfill script, following
   `scripts/migrate_fre865_entity_class_backfill.py`'s established shape (`GraphProtocol` seam so
   the orchestration is unit-testable against a fake graph; `--dry-run`; `--confirm-prod` guard;
   a `BackfillReport` dataclass printed + optionally written to `--report-path`). No rollback —
   unlike FRE-865 this migration only ever moves `none → provenanced` (A5), which is not something
   there is a legitimate reason to undo, so `--rollback` is out of scope here.

   - `EntityCandidate(element_id, name, originating_trace_id)`,
     `ClaimCandidate(element_id, claim_id, content, trace_id)`.
   - `_STRUCTURAL_RELATIONSHIP_TYPES`: frozenset of the ten known fixed-label types (`DISCUSSES`,
     `PARTICIPATED_IN`, `NEXT`, `CONTAINS`, `SOURCED_FROM`, `HAD_DESCRIPTION`, `HAS_FACT`,
     `HAS_STANCE`, `OPERATED_BY`, `CURRENTLY_AT`) — the defensive second discriminator alongside
     `r.weight IS NOT NULL` (codex finding 2: `weight` matches every *current* write site but is
     not an API-enforced invariant of `create_relationship`).
   - `ReconstructOutcome(matched_sources: list[SourceRecord], missing_capture: bool)` +
     `_reconstruct(capture, tool_registry, *, attribution) -> ReconstructOutcome`: pure function
     over an already-resolved `TaskCapture | None` (resolution itself now happens per-page, batched,
     before this is called) — `capture is None` → `missing_capture=True`; else runs
     `sources_from_tool_results` + `associate` (both already shipped, `memory/provenance.py`,
     A4/A4b), maps the ids `associate()` returns back to their `SourceRecord` objects (never
     returns bare ids), and returns whatever matched (possibly empty list, `missing_capture=False`).
   - `GraphProtocol`: `fetch_entity_candidates`/`fetch_claim_candidates` (cursor-paginated,
     `ORDER BY elementId(n)`, predicate `provenance_state IS NULL OR NOT provenance_state IN
     ['provenanced','none']` — catches both the incidental-`'none'` population and any historical
     out-of-enum value, making AC-1 hold by construction), `write_entity_provenanced`/
     `write_entity_none`, `write_claim_provenanced`/`write_claim_none`,
     `mark_relationships_none() -> int` (one bulk statement,
     `WHERE r.weight IS NOT NULL AND NOT type(r) IN $structural_types AND (r.provenance_state IS
     NULL OR NOT r.provenance_state IN ['provenanced','none'])`).
   - `_Neo4jGraph(driver)`: the real `GraphProtocol`.
     - `write_*_provenanced`: **unconditional** `SET provenance_state = 'provenanced'` — safe from
       *any* prior state (NULL, garbage, or already `'provenanced'`; that value is the lattice's
       terminal state, A5) — then reuses `MemoryService._source_merge_clause(alias)` (imported,
       called as a staticmethod — the exact Cypher fragment the live write path uses, not a
       re-derived copy) to mint `:Source`/`SOURCED_FROM`, **unconditionally too**: even an
       already-`'provenanced'` item legitimately gains a newly-discovered corroborating edge
       (A4b — multiple sources are recorded, not treated as ambiguity).
     - `write_*_none`: `SET provenance_state = CASE WHEN provenance_state = 'provenanced' THEN
       provenance_state ELSE 'none' END` — closes codex finding 7 (a plain `WHERE ... SET 'none'`
       has a race window against a concurrent live write moving the item to `'provenanced'`
       between this statement's read and write phases; the `CASE` reads and writes atomically
       within the one `SET`, mirroring `create_entity`'s own existing pattern at `service.py:2217`).
   - `run_backfill(graph, capture_index, tool_registry, es_client, *, dry_run, batch_size) -> BackfillReport`:
     pages entities then claims through `GraphProtocol`; per page, batch-resolves that page's
     distinct `originating_trace_id`/`trace_id` values via `read_captures_by_trace_ids` **once**,
     then calls `_reconstruct` per candidate against the resolved dict, writes the outcome (skipped
     under `--dry-run`), then one bulk relationship pass. A per-candidate exception is caught,
     logged, and counted in that kind's `_errors` bucket — never aborts the run (the candidate
     stays unresolved and is retried on the next invocation).
   - `BackfillReport`: `entities_total`, `entities_reconstructed`, `entities_none_no_match`,
     `entities_none_missing_capture`, `entities_errors` (five fields, sum-reconciled), the same
     five for claims, `relationships_marked_none`, `success` (`entities_errors == 0 and
     claims_errors == 0`). AC-4: `reconstructed + none_no_match + none_missing_capture + errors ==
     total`, per kind.
   - CLI: `_amain`/`main`, mirroring FRE-865's Neo4j-connect + `--confirm-prod` guard structure
     (no cost gate — this script makes no LLM calls).

## Tests (TDD — written failing first)

3. **`tests/personal_agent/captains_log/test_capture_by_trace_id.py`** — unit, `make test`:
   - `build_capture_index` returns `{trace_id: path}` across multiple date directories.
   - `read_captures_by_trace_ids([...], disk_index=idx, es_client=None)` returns the parsed
     captures for known trace_ids found on disk; a trace_id absent from the index is simply absent
     from the result (no exception).
   - a corrupt/non-JSON disk file: excluded from the result (logs, does not raise), and other
     trace_ids in the same batch still resolve.
   - pre-FRE-343 file (`user_id` absent/null) parses via the nil-UUID injection.
   - `es_client` given, a trace_id missing from disk: one `es_client.search` call is made (fake
     async client) scoped to the disk-misses only, `ids` query, and its hit is merged into the
     result; a trace_id found on disk is never looked up in ES (no wasted query).
   - `es_client=None`: no ES call attempted at all; disk-misses are just absent from the result.

4. **`tests/scripts/test_migrate_fre1348_provenance_backfill.py`** — unit, `make test`, fake
   `GraphProtocol` + real `get_default_registry()` (no I/O), `es_client=None`:
   - `_reconstruct`: `capture=None` → `missing_capture=True`; capture given, `fetch_url` output
     contains the attribution string → one matched `SourceRecord` (not a bare id); capture given,
     output does not contain it → `missing_capture=False`, empty matches; capture given but only a
     `web_search` call (no `referent_parameter`) → empty matches (A2 scope boundary — no referent
     to associate against).
   - `run_backfill` against a fake graph (`fetch_*_candidates` returning fixtures, capture
     resolution stubbed): reconstructs a mix of entities/claims correctly bucketed into each kind's
     five counters; `--dry-run` calls no `write_*` method; a candidate whose `_reconstruct` raises
     is counted in that kind's `_errors` bucket and does not abort the remaining candidates; each
     kind's five buckets sum to that kind's total (AC-4, at the orchestration level).
   - `mark_relationships_none` is called exactly once per run (bulk, not per-candidate) and its
     returned count lands in `report.relationships_marked_none` unchanged.

5. **`tests/scripts/test_migrate_fre1348_provenance_backfill_integration.py`** — `pytest.mark.integration`,
   live test Neo4j (:7688, FRE-375), real `run_backfill` end to end:
   - **AC-1**: after the migration, `MATCH (n) WHERE (n:Entity OR n:Claim) AND (n.provenance_state
     IS NULL OR NOT n.provenance_state IN ['provenanced','none']) RETURN count(n)` == 0, and the
     equivalent `MATCH ()-[r]->() WHERE r.weight IS NOT NULL AND NOT type(r) IN
     $structural_relationship_types AND (r.provenance_state IS NULL OR NOT r.provenance_state IN
     ['provenanced','none']) RETURN count(r)` == 0.
   - **AC-2**: seed ≥10 legacy `:Entity` nodes with `originating_trace_id` pointing at fixture
     captures (written to a `tmp_path`-patched captures dir) whose `fetch_url` tool-result output
     demonstrably contains each entity's name — **at least 2 of the 10 pre-stamped
     `provenance_state = 'none'`** (simulating the incidental `create_entity`/`create_conversation`
     touch codex finding 1 identified, `service.py:2211`/`:1320`), the rest left with the property
     unset. Run the migration. Assert all ≥10 are `provenanced` with a `:Source` node reachable via
     `SOURCED_FROM` carrying the fixture's `referent` — proving the widened predicate, not just the
     `IS NULL` case.
   - **AC-3**: for a sample of the AC-2 fixtures, independently re-run
     `grounding.containment.check_containment(name, fixture_capture_content)` (content read from
     the same fixture, not from `:Source` — `to_cypher_map()` deliberately never stores content,
     D3) and assert `CONTAINED` for every one.
   - **AC-4**: `BackfillReport`'s counts are non-trivial (`entities_reconstructed > 0` and
     `entities_none_missing_capture > 0` in a mixed fixture) and the per-kind buckets reconcile
     with `entities_total`/`claims_total`.
   - A legacy entity with `originating_trace_id` pointing at a capture that genuinely does not
     mention it → ends `'none'`, not `'provenanced'` (no false positive).
   - A legacy relationship (`r.weight` set, `provenance_state` absent) → ends `'none'`, never
     attempted for reconstruction.

## Verification

```bash
make test-infra-up   # once, if not already running
make test-file FILE=tests/personal_agent/captains_log/test_capture_by_trace_id.py
make test-file FILE=tests/scripts/test_migrate_fre1348_provenance_backfill.py
make test-file FILE=tests/scripts/test_migrate_fre1348_provenance_backfill_integration.py
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Fold-ins / follow-ups

None anticipated. `--rollback` and prod-run cost/scale tuning are explicitly out of scope (see
Files §2) — a prod backfill run itself is a separate, later, master-gated ops action (mirroring
FRE-865's own precedent), not part of this ticket.
