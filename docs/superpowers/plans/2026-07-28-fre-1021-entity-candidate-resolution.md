# FRE-1021 — Entity-candidate resolution fix in the multipath fused pool

**Ticket:** FRE-1021 (Approved by owner mid-session 2026-07-28, Tier-1:Opus, PersonalAgent).
**Backs:** ADR-0126 D2's premise for FRE-1015 (topic-scoped Stance push rides "the entity the
existing recall path has already selected"). Also the FRE-1010/FRE-636 empty-entity-participation
lineage.
**Blocks:** FRE-1015 (ADR-0126 T1), held by MASTER_PLAN pending this ticket's measurement.

## Measured, before any code changes

`scripts/audit/fre1021_entity_participation_census.py` against the live cluster
(`agent-captains-captures-*`, **the 20 turns that have a recorded `recall_admission` with ≥1
candidate**, out of whatever larger population of turns ran since the evidence contract shipped,
2026-07-27→28 — the script's denominator excludes turns with no candidates at all, so this is a
rate *among turns recall actually fired on*, not an all-turns rate):

- **entity_offered_rate = 20%** (4/20 turns offered any `kind=entity` recall candidate).
- **entity_admitted_rate = 20%** (4/4 offered were also admitted — no extra loss at admission in
  this small sample).
- Kind distribution across all 89 offered candidates: 80 episode, 9 entity.
- **Caveat, added after codex plan-review:** the census reads `kind`/`admitted` only — it does not
  record which code path produced each turn's candidates. The claim that all 4 entity-bearing turns
  came from the (unaffected) proactive/broad paths, and all 16 zero-entity turns came from the
  broken branch, is **consistent with** the source-level mechanism below but **not independently
  proven** by this data. Treat "resolution-contract bug is the dominant mechanism" as a
  source-grounded hypothesis, not a measured fact, until Step 4's path-isolated live verification.

## Root cause, verified against source (not the ticket's own framing)

The ticket's hypothesis was **kind-blind ranking competition** — episodes and entities fused into
one RRF-ranked, capped pool, with episodes winning the rank and displacing entities. That mechanism
is real (`memory/fusion.py::reciprocal_rank_fusion` sums RRF score keyed only by `item_id`, `kind`
never enters the score or sort key) but **it is not the dominant mechanism** on the only live call
path.

Confirmed by direct read of `memory/service.py`:

- `orchestrator/executor.py`'s duplicate entity-match block (~L3636-3739) is **dead in
  production** — gated by `enable_memory_graph`, code default `False`, no override in the live
  `.env`. The only live path is `request_gateway/context.py::_query_memory_for_intent`'s
  entity-name-match fallback (L244-258), which calls `memory_adapter.recall()` →
  `MemoryService.query_memory()` → `_multipath_query_memory()` (`service.py:4897-5008`) whenever
  `multipath_recall_enabled` is on (live: `True`).
- `_multipath_query_memory`'s resolution loop (L4950-4970) resolves **every** fused item — `kind
  == "entity"` or `kind == "turn"` alike — through `_resolve_fused_turns` (L5009-5080), and for an
  `entity`-kind item that function's Cypher (`MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn) ...
  collect(t)[0..$cap]`) discards the entity node itself and returns **only its own most-recent
  turns**. The final `return MemoryQueryResult(conversations=conversations,
  relevance_scores=relevance_scores)` (L5004-5007) never passes an `entities=` kwarg, so
  `MemoryQueryResult.entities` is `[]` unconditionally on this path — win or lose the fusion rank.
- This is a **resolution-contract bug**, not a probabilistic ranking outcome. Whenever this branch
  fires, entity participation is structurally zero. The 20% measured above is entirely accounted
  for by turns that instead went through the (unaffected) proactive path
  (`suggest_relevant`/`suggest_proactive_raw`, kind-segregated by construction) or the
  `MEMORY_RECALL`-intent broad path (`recall_broad`, also unaffected) — never through this branch
  succeeding.
- `request_gateway/context.py` (L258: `for entity in result.entities:`) and
  `memory/protocol_adapter.py::recall()` (L82-92) are **already correctly wired** to consume
  `MemoryQueryResult.entities` — this exact shape is what the legacy/broad paths already feed them
  today. No change needed in either file.

**Correction after codex plan-review:** "the only other call site is dead code" was too broad as
originally written and is narrowed here. `orchestrator/executor.py`'s duplicate entity-match block
(~L3636-3739) — the other *automatic context-assembly* call site doing this exact
`entity_names[:5]/limit=5` pattern — is dead (gated by `enable_memory_graph`, default `False`, no
live override observed in this worktree's `.env`; codex additionally notes it could still be
reached if gateway assembly itself fails upstream and the flag were enabled, so "dead" means
"gated off today," not "unreachable in principle"). Separately, `MemoryService.query_memory()` (and
therefore `_multipath_query_memory`) has other **legitimate, live callers** that are not part of
automatic context assembly and are unaffected by this distinction either way: `tools/memory_search.py`
(the explicit `search_memory` tool), `ui/memory_cli.py` (a debug CLI), and an HTTP endpoint in
`service/app.py`. These all share whatever `_multipath_query_memory` returns — they benefit from
this fix identically, they are not a second instance of the bug to fix separately, and they are not
"dead code" in the sense the plan originally implied.

## Design decision

**Narrow resolution fix, not a kind-reservation/re-ranking redesign.** Teach
`_multipath_query_memory`'s resolution step to resolve an `entity`-kind fused item into the
**Entity node itself** (name/entity_type/description/mention_count, via the existing shared
`_entity_node_from_record` parser used by the broad/structural paths) instead of expanding it into
its turns. Both kinds continue to share the single rank-ordered `query.limit` budget, walked in
fused order — this is what makes the fix "let ADR-0100's relevance-bounded rules govern the mix"
(the ticket's own Option 4) rather than inventing new kind-reservation logic: the RRF ranking is
already correct, only the resolution step was discarding kind.

Rejected alternatives (per the ticket's own option-space, all require more surface than the bug
warrants):
- **Per-kind slot reservation** — solves a genuine-but-secondary problem (RRF *does* still let
  episodes outrank entities sometimes) but is new mechanism; premature before the resolution bug
  (the dominant mechanism, ~structural on this branch) is fixed. If post-fix measurement still
  shows material entity starvation from ranking alone, this is the next thing to reconsider — not
  bundled here.
- **Separate scoring scales for episodes vs entities** — same reasoning; no evidence yet that RRF
  competition (as opposed to the resolution bug) is the dominant term.

## Files touched

1. `src/personal_agent/memory/service.py`
   - `_resolve_fused_turns` → rename return contract: `by_entity: dict[str, list[TurnNode]]`
     becomes `by_entity: dict[str, EntityNode]` (keyed by the fused item's `item_id`, i.e. the
     entity's `elementId`). New Cypher: `MATCH (e:Entity) WHERE elementId(e) = eid AND {vis_e}
     RETURN eid AS eid, e` (drops the `<-[:DISCUSSES]-(t:Turn)` traversal and `per_entity_cap`
     entirely — no longer needed once entities resolve to themselves, not their turns). Uses a new
     `_build_visibility_filter("e", user_id, authenticated)` (the existing `vis_t` filter no longer
     applies since there's no `t` alias in this query anymore).
   - `_multipath_query_memory` — restructure the resolution loop: `kind == "turn"` items append to
     `conversations` as today; `kind == "entity"` items append to a new `entities: list[EntityNode]`
     accumulator (deduped by `entity_id`, mirroring the existing name-based `MERGE (e:Entity {name:
     ...})` identity convention); the stopping condition becomes
     `len(conversations) + len(entities) >= query.limit` (was `len(conversations) >= query.limit`),
     checked only **after** a successful, non-duplicate append — a duplicate or an unresolved node
     (visibility-filtered or a stale fused id) must not consume a budget slot, so the loop keeps
     scanning subsequent fused items to backfill. `relevance_scores` stays keyed by `turn_id` only
     (unchanged contract — `context.py` already only reads it for episode ordering).
     `return MemoryQueryResult(conversations=conversations, entities=entities,
     relevance_scores=relevance_scores)`.
   - **Fix 1 (codex confirmed-bug) — `accessed_entity_ids` completeness.** The freshness-event
     accumulator today seeds from `query.entity_names` then extends with each returned
     conversation's `key_entities` (L4972-4975). An entity surfaced directly via the new `entities`
     list may be in neither source. Extend the accumulator with `entity.entity_id` (name-based) for
     every item in the new `entities` list, before the `dict.fromkeys` dedup — otherwise a
     freshness-access event silently under-reports which entities this turn actually touched.
   - **Fix 2 (codex confirmed-bug) — telemetry undercount.** `memory_query_completed`'s
     `result_count=len(conversations)` (L4977-4983) must become `result_count=len(conversations) +
     len(entities)` (or emit both counts separately) — an entity-only recall would otherwise log
     `result_count=0` despite returning real content.
   - **Fix 3 (codex open concern, decided here) — hard-recency semantics for entities.** Decision:
     `hard_recency_days` (an explicit caller-supplied window, set only by `search_memory` when the
     caller passes a positive `recency_days` — a no-op on the automatic context-assembly path, which
     never sets it) applies to entities too, for consistency with turns: a caller who explicitly
     asked "only the last N days" should not get an entity whose `last_seen` predates that window
     either. New small helper `_filter_entities_by_hard_recency(entities, hard_recency_days)`,
     structurally mirroring `_filter_turns_by_hard_recency` (L532-562) but keyed on
     `EntityNode.last_seen`, applied before the entity is appended (a filtered entity does not
     consume a budget slot, same as a filtered turn).
   - Docstrings on both methods updated to state the corrected contract.
2. `tests/personal_agent/memory/test_multipath_query_memory.py` (new) — unit tests against a fake
   Neo4j driver (plain-dict fake nodes; `_entity_node_from_record`/`_turn_node_from_node` only call
   `.get()`, so a dict satisfies the duck-typed interface without a fake-node class).
3. No changes needed in `request_gateway/context.py`, `memory/protocol_adapter.py`, or
   `memory/models.py` — confirmed already correctly wired to `MemoryQueryResult.entities`.

## Step 1 — failing test first (TDD)

New file `tests/personal_agent/memory/test_multipath_query_memory.py`. Revised after codex
plan-review — the original four-test list undercounted gaps and mischaracterized one test as
red-first when it already passes on current code:

**Red on current code (the actual regression proof):**
- `test_entity_kind_item_resolves_to_entities_not_turns` — mock `service._multipath_fused_recall`
  to return one `entity`-kind and one `turn`-kind `FusedResult`; fake driver returns one entity node
  dict and one turn node dict; assert `result.entities` has length 1 with the right
  `name`/`entity_type`/`description`, `result.conversations` has length 1 with the right `turn_id`,
  and `relevance_scores` has exactly one key (the turn's) — not the old behaviour where the entity
  item silently produced turns and polluted `conversations`.
- `test_combined_limit_shared_across_kinds` — 3 entity-kind + 3 turn-kind fused items in alternating
  rank order, `query.limit=4`; assert the **exact ordered identities** kept in each accumulator
  match the four highest-ranked fused items (not just the two lengths) — proves fused rank order,
  not turn-then-entity, decides what's kept.
- `test_freshness_access_ids_include_direct_entities` (codex confirmed-bug) — an entity resolved
  directly via the new path, absent from `query.entity_names` and from any conversation's
  `key_entities`; assert the published `MemoryAccessedEvent.entity_ids` includes it.
- `test_telemetry_result_count_includes_entities` (codex confirmed-bug) — an entity-only fused
  result (no turn-kind items); assert `memory_query_completed`'s logged `result_count` is `1`, not
  `0`.

**Regression / boundary coverage (may already pass — still required, not part of the red set):**
- `test_entity_resolution_still_visibility_scoped` — assert the recorded entity-query Cypher
  carries the `{vis_e}` fragment **and** the recorded params include `vis_authenticated`/
  `vis_user_id` (asserting the fragment string alone doesn't prove the params were wired).
- `test_no_entity_ids_skips_entity_query` / `test_no_turn_ids_skips_turn_query` — symmetric: an
  items list with only one kind never issues the other kind's `session.run` call. (Codex confirmed
  the entity-only direction already passes on current code via the existing `if entity_ids:` guard
  — kept as a regression guard, not claimed as red-first.)
- `test_duplicate_and_unresolved_items_do_not_consume_budget` (codex confirmed-bug) — a fused list
  with a duplicate entity id and an id the fake driver resolves to nothing; assert neither consumes
  a slot and a lower-ranked resolvable item backfills to reach `query.limit`.
- `test_hard_recency_filters_entities` — once Fix 3 is implemented, an out-of-window entity
  alongside an in-window entity and an in-window turn under an explicit `hard_recency_days`; assert
  the out-of-window entity is dropped and does not consume a slot.
- `test_limit_one_boundary` — `query.limit=1` with both kinds present in the fused list; assert
  exactly one item total is returned, whichever kind ranks first.

Run: `make test-file FILE=tests/personal_agent/memory/test_multipath_query_memory.py` — confirm the
four "red on current code" tests fail against current `service.py`; the regression/boundary tests
are written now but are not claimed as proof of the bug.

## Step 2 — implement

Apply the `_resolve_fused_turns` / `_multipath_query_memory` changes described above. Re-run the
same file — confirm all four pass.

## Step 3 — regression sweep

`make test-file FILE=tests/personal_agent/memory/test_multipath_core.py` (untouched function,
confirms no collateral break) and `make test-k K=multipath` (catches any other test relying on the
old `by_entity: dict[str, list[TurnNode]]` contract — grep confirmed `_resolve_fused_turns` has
exactly one caller, so this is a belt-and-suspenders check, not an expected-fail).

## Step 4 — live verification (post-deploy, master's runbook)

Re-run `python scripts/audit/fre1021_entity_participation_census.py --since <deploy-date> --json`
against the live cluster. **Proof required (ticket's own bar):** `entity_offered_rate` on turns
*after* deploy materially exceeds the 20% pre-fix baseline. This is the "measured rate, stated
before and after" the ticket's PROOF REQUIRED section demands — falls out of the census script
already written, no new instrumentation needed.

**Path-isolated proof (added after codex plan-review — the global rate alone is not sufficient).**
The global rate can move for reasons unrelated to this fix (more turns routing through the
proactive/broad paths, which were never broken). Additionally: fire at least one turn known to hit
`_query_memory_for_intent`'s entity-name-match fallback specifically — e.g. re-ask, verbatim, one of
the pre-fix sample's 16 zero-entity questions (their trace_ids are in the pre-fix run's output) on a
subject with no fresh episode competing for rank — and confirm its `recall_admission` record now
shows an admitted `kind=entity` item. This isolates the fix's effect from the other paths'
already-correct behaviour.

## Acceptance criteria this ticket carries (from the ticket body)

1. **A measured rate of entity participation, before and after.** Before: 20% (N=20, measured
   above). After: re-run post-deploy (Step 4) — fails if unchanged.
2. **A turn on a well-discussed subject where an entity carrying a description reaches the model,
   demonstrated from the evidence record rather than a unit test.** Live-verify with a real turn on
   a subject known to have accumulated episodes (e.g. re-ask a question from one of the 16
   zero-entity turns in the pre-fix sample) and confirm the post-deploy `recall_admission` record
   shows an admitted `kind=entity` item.
3. **Fails if the fix is a larger constant rather than a stated rule.** This fix changes no
   constant (`query.limit`, `reranker_input_cap`, `recall_per_entity_turn_cap` are all untouched) —
   it corrects what each fused item resolves to. Documented explicitly here to make this checkable
   without re-deriving it.

## Self-classification (build skill Step 3)

**Standard/Complex** — touches `src/` core recall logic on the live hot path used by every turn.
Codex plan-review required before implementation.
