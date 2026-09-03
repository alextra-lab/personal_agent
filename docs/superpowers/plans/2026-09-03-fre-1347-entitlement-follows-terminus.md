# FRE-1347 — Entitlement follows the terminus; close the FRE-1338 leak; amend ADR-0138 D2

Backing ADR: ADR-0098 Amendment A §A6 (`docs/architecture_decisions/ADR-0098-memory-substrate-and-lifecycle-architecture.md:423-454`).
ADR-0138 D2 already carries the amendment-note text referencing this ticket
(`docs/architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md:222-255`) — the
narrowing is *written*, not yet *implemented*. This ticket implements it.

## Current state (verified by reading, not assumed)

- Entity write-side provenance is fully shipped and tested (`memory/service.py`, `memory/provenance.py`,
  `tests/personal_agent/memory/test_provenance_cypher.py`): every `:Entity` node carries a flat
  `provenance_state` property (`'provenanced'` | `'none'`) and, when provenanced, a
  `-[:SOURCED_FROM]->(:Source {referent, ...})` edge.
- The **read side never surfaces either.** `EntityNode` (`memory/models.py:244`) has no
  `provenance_state` field. `_search_memory_entitlement` (`grounding/source_registry.py:496`) only
  ever inspects `claims`/`claims_history`; its own docstring says entities are "this fix's sibling
  work... not this one's job" — that sentence is what this ticket makes stale.
- The **live default path is the leak itself.** `search_memory`'s entity-match branch
  (`tools/memory_search.py:199-213`, driven by `MemoryService.query_memory`,
  `memory/service.py:4127`) returns `matched_turns` built from `Turn.key_entities` — a bare
  `list[str]` property copied onto the Turn at write time, with **no link at all** to the `:Entity`
  node's provenance. `result.entities` is never populated on this path (confirmed: the function's
  only `MemoryQueryResult(...)` construction on success, `service.py:4511`, omits `entities=`), so
  `entities_found` in the tool output is dead — always `0`. This is exactly the FRE-1338 shape: Model
  B got bare names, never a checkable address.
- `query_memory_broad` (broad-recall path) *does* build entity dicts
  (`memory/service.py:5779-5834`, 3 Cypher blocks) but none return `provenance_state` or a referent
  either.
- `multipath_recall_enabled` defaults `False` (`config/settings.py:685`) and is unset in this
  worktree's `.env` — the multipath entity paths (`_multipath_query_memory`,
  `_resolve_fused_turns`, `_multipath_broad_entities`) are **not live**. Scoped out below,
  deliberately, with a follow-up note (they fail closed in the meantime: no `provenance_state` field
  read defaults to `Entitlement.AGENT_DERIVED`, never a false `EXTERNAL`).
- `verify_turn` already rejects `Entitlement.AGENT_DERIVED` only (`grounding/verification.py:374`) —
  AC-2 needs no verification-layer change, only correct entitlement classification upstream.

## Codex plan review — findings and dispositions

Codex reviewed this plan before implementation (required, Standard/Complex tier). Confirmed
correct: the `count(DISTINCT t)` / `collect(DISTINCT src.referent)` Cypher pattern, and the
most-restrictive fold ordering (any `AGENT_DERIVED` → `AGENT_DERIVED`; else any `USER_STATED` →
`USER_STATED`; else `EXTERNAL`). Four findings changed the design:

1. **Blocking, fixed in this plan** — `_entity_entitlement_of` collapsed every non-provenanced
   entity to `AGENT_DERIVED`, missing A6's "a statement the owner made" → `USER_STATED` row for
   entities written via the gateway's `store_fact` (`gateway/app.py:174`), which creates entities
   with no `SOURCED_FROM` edge (`provenance_state='none'`) but is explicitly user-provided, not
   extraction. Resolution: `create_entity` already threads `extractor_model: str | None`
   (`memory/service.py:2084-2117`) — `None` for the `store_fact` path, set for LLM extraction. This
   is an existing flat node property requiring **no new schema or write-path change**. Revised rule:
   `provenance_state == "provenanced"` → `EXTERNAL`; else `extractor_model` key present and `None`
   → `USER_STATED`; else → `AGENT_DERIVED` (covers both "none"-with-extraction and the
   malformed/missing-key case, fail-closed).
2. **Not blocking, scoped out with reasoning** — `matched_turns[].summary`/`key_entities` are
   agent-authored content the classifier doesn't inspect, so a call with one well-provenanced entity
   nominally admits the whole registered blob (including that summary) at `EXTERNAL`. Verified this
   is **not a regression**: pre-fix, every entity-bearing call with no claims already defaulted to
   `EXTERNAL` unconditionally, so this exact over-admission already existed for 100% of such calls;
   post-fix it only shrinks (calls with a `none`-terminus entity now correctly downgrade). Full
   correctness here needs per-item entitlement, which ADR-0098 Amendment A6 itself defers
   explicitly ("FRE-1302's deferred architecture"). Left as a documented follow-up, not fixed here.
3. **Not blocking, corrected the claim** — "multipath paths fail closed" was imprecise. Because
   `_entity_node_from_record` is shared, giving it `provenance_state`/`extractor_model` reads makes
   multipath entities (`_resolve_fused_turns`) entitlement-*correct* automatically (better than
   today, where `entities` isn't inspected by the classifier at all on any path). What multipath does
   **not** get from this plan is referent enrichment (`source_referents` stays empty there) — so an
   `EXTERNAL`-entitled multipath citation would still be a bare name, not a real address. Documented
   as a follow-up; not blocking since `multipath_recall_enabled` is off by default and unset here.
4. **Test strategy widened**: add a `store_fact`-shaped entity case (`provenance_state='none'`,
   `extractor_model=None`) to the AC-1 suite; add one test that drives the full path through
   `search_memory_executor` rather than only hand-built JSON; before editing `query_memory`, grep
   the full test tree for `query_memory`-mocking fixtures (not just the 4 files found by name
   search) and run the complete affected set, not a hand-picked subset.

## Scope

1. `memory/models.py` — `EntityNode` gains `provenance_state: str = "none"` and
   `source_referents: list[str] = Field(default_factory=list)`.
2. `memory/service.py`:
   - `_entity_node_from_record` reads `provenance_state` off the node (already a flat property —
     zero Cypher change needed at its 3 existing call sites) and accepts an optional
     `source_referents: list[str] | None` param.
   - `query_memory` (legacy entity-match path): when `entity_recall` is true, run one additional
     `session.run` (same session, same visibility-filter helper already used elsewhere for entities)
     resolving the matched `:Entity` nodes with `OPTIONAL MATCH (e)-[:SOURCED_FROM]->(src:Source)`,
     collecting `provenance_state` + distinct referents, and populate `MemoryQueryResult.entities`.
     This is the actual leak closure.
   - `query_memory_broad`'s 3 legacy Cypher blocks: add the same `OPTIONAL MATCH` +
     `e.provenance_state` + `collect(DISTINCT src.referent)`, using `count(DISTINCT t)` /
     `collect(DISTINCT src.referent)` side by side so the added optional pattern's cross product
     doesn't inflate the existing mention count.
   - Multipath paths: **not touched**, noted as follow-up (flag is off; fails closed).
3. `tools/memory_search.py` — entity-match branch gains an `"entities"` key built from
   `result.entities` (same dict shape as the broad-recall branch already uses, now consistently
   carrying `provenance_state`/`source_referents`). Broad-recall branch needs no code change — its
   dicts pass through whatever `query_memory_broad` now returns.
4. `grounding/source_registry.py` — `_search_memory_entitlement` folds `entities` into the same
   most-restrictive aggregation already applied to `claims`/`claims_history`, via a new
   `_entity_entitlement_of` helper (`EXTERNAL` iff `provenance_state == "provenanced"`, else
   `AGENT_DERIVED` — fails closed on `"none"`, a missing key, or a malformed value, same direction
   `_entitlement_of` already documents for Claims). Ordering: any `AGENT_DERIVED` present → whole
   call `AGENT_DERIVED`; else any `USER_STATED` → `USER_STATED`; else `EXTERNAL`. Empty-of-both
   (no claims, no entities — e.g. a turn-summary-only recall) keeps the existing `EXTERNAL` fallback,
   unchanged — that residual gap is turns-only and stays explicitly out of scope, same as today.
   Docstring updated: the "entities... not this one's job" sentence is now wrong and is corrected.
5. `docs/architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md` — the
   amendment note (lines 222-255) currently says the A6 narrowing "is being implemented under
   FRE-1347" (present tense, in-flight). Update to record it as shipped once the code lands.
6. `docs/architecture_decisions/README.md` — ADR-0138's index row (line 228) status column says only
   "amended 2026-08-25 — D2 `curl` illustration corrected"; it does not mention the 2026-09-01 A6
   narrowing amendment note at all. Add it, so a reader of the index doesn't read a stale summary.

## Tests (TDD — written first, confirmed failing, then made to pass)

- `tests/personal_agent/grounding/test_search_memory_entitlement_e2e.py` — extend with the ticket's
  AC-1 four scenarios, run through the *exact* existing chain (search_memory JSON →
  `register_tool_result` → `verify_turn`), mirroring the file's own Claims-based tests:
  - entity with `provenance_state="provenanced"`, no claims → `EXTERNAL`, `PASSED`.
  - claim `asserted_by="user"`, no entities → `USER_STATED`, `PASSED` (already covered; confirms no
    regression once entities are folded in).
  - entity with `provenance_state="none"`, no claims → `AGENT_DERIVED`, `SOURCE_NOT_ENTITLED` (AC-2).
  - mixed: one provenanced entity + one `"none"` entity in the same call → `AGENT_DERIVED` (least-
    entitled-item rule, AC-1's explicit mixed case).
- `tests/personal_agent/memory/test_hybrid_search.py` and any other `query_memory`-mocking test whose
  fixture's `session.run` side_effect is position/count-based: update the shared fixture helper(s)
  to account for the one new `session.run` call on the entity-recall path (all these tests already
  pass `entity_names=[...]`, so the new call is always reached). Verified empirically via
  `make test-file` after the implementation, not hand-traced — mock call ordering across
  vector-search/embedding-patch interactions is not safe to infer statically.
- `tests/test_tools/test_memory_search.py` — extend
  `test_search_memory_executor_entity_path_returns_matched_turns`-style coverage with a case where
  `MemoryQueryResult.entities` is non-empty and assert the tool output's `entities` key carries
  `provenance_state`/`source_referents` through.
- New or extended `memory/service.py` test proving the entity-match Cypher resolves
  `provenance_state` + `source.referent` correctly (mocked driver, following the
  `_run_side_effect`/cypher-text-dispatch pattern already used in this test suite) — this is the
  AC-3/leak-closure proof at the service layer; the full FRE-1338 replay (two sequential sessions
  against a shared graph) is the `integration`-marked live check, following
  `test_executor_recall_visibility.py`'s pattern if a mocked version can't stand in adequately for
  the cross-session provenance-persists-in-Neo4j behaviour.

## Acceptance criteria mapping (ticket's own ACs)

- AC-1 (entitlement follows terminus, mixed case) → `_entity_entitlement_of` +
  `_search_memory_entitlement` aggregation, tested in the e2e file above.
- AC-2 (agent-terminus not citable) → automatic once entitlement is correct;
  `verify_turn`'s existing `AGENT_DERIVED` rejection covers it; same e2e test proves it.
- AC-3 (FRE-1338 leak closed, without severing recall) → `query_memory`'s new entity resolution
  query returns the real `:Source.referent`, not a bare name; USER_STATED claims path is untouched
  (regression-proof via the existing passing e2e test).
- AC-4 (ADR-0138 records its own narrowing) → the amendment-note text already exists (added
  2026-09-01) and names this ticket; this PR updates it from in-flight to shipped, and fixes the
  README index row that never mentioned it.

## Self-review finding, fixed on-branch (post-implementation)

`feature-dev:code-reviewer` (scoped to `git diff origin/main...HEAD`) found the implemented
`_entity_entitlement_of` couldn't actually distinguish "user-stated via `store_fact`"
(`extractor_model is None`) from two other, unrelated ways an entity ends up with no
`extractor_model`: `create_conversation`'s bare-`MERGE` fallback (never sets the property) and
any pre-this-ticket legacy `:Entity` node. Neo4j has no persisted null, so "never set" and
"explicitly written as `None`" are structurally identical on read — the design as planned would
have misclassified both populations as `USER_STATED`, an admitted citable tier, for entities that
are actually agent/system-derived. Confirmed by reading `memory/service.py:1313-1345` (the
fallback) and `create_entity`'s `ON CREATE` clause (`extractor_model` only ever set `ON CREATE`,
conditionally).

Fixed by replacing the absence check with a positive sentinel: `USER_STATED_EXTRACTOR_SENTINEL`
(`grounding/source_registry.py`), written by `create_entity` only when its own `extractor_model`
argument is `None`, and by the bare-`MERGE` fallback as a *different*, explicit non-sentinel value
(`"key_entity_extraction"`) so it can never be mistaken for the sentinel. `_entity_entitlement_of`
now matches the sentinel exactly rather than checking for absence. Added the seeded negative the
review flagged as missing (`test_legacy_entity_with_no_extractor_model_property_is_refused`) plus
three more gaps a second review pass found (entity_types-only fallback, the `session.run` failure
path, and `query_memory_broad`'s Cypher aggregation correctness — all previously verified only by
reading the code, not by a test). Full quality gate sequence re-run clean after the fix.

Deliberately not fixed here, documented as follow-up: `_multipath_broad_entities`
(`memory/service.py`, the `multipath_recall_enabled` path) still hand-selects a narrow field list
that omits `provenance_state`/`extractor_model` — dormant since that flag defaults `False` and is
unset in this worktree, and fails closed (denies citation) rather than over-admitting, so it is not
a correctness regression, only an incompleteness one, to close before that flag is ever enabled.
