# FRE-1216: Nine measured KG and sysgraph data-quality defects — triage

The ticket explicitly says these nine are **not one fix**: "expect a triage pass that splits
execution, or fixes the cheap ones and escalates the rest." This plan is that triage, backed by a
fresh live-graph measurement (2026-08-18, ten days after the ticket's original 2026-08-08 reading)
rather than trusting the original numbers as still current.

## Access used for investigation

- Neo4j: `docker exec cloud-sim-neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD "<query>"` (creds in
  `/opt/seshat/.env`: `AGENT_NEO4J_URI=bolt://localhost:7687`). Read-only for all investigation
  queries below.
- Postgres/sysgraph: `docker exec cloud-sim-postgres psql -U agent -d personal_agent -c "<query>"`.

## Fresh measurement (2026-08-18) — confirms the ticket, with corrections

| # | Ticket (2026-08-08) | Measured now (2026-08-18) | Delta |
|---|---|---|---|
| 1 | `USEs`=1, `USES`=2,939 | `USEs`=1, `USES`=2,950 | unchanged defect, +11 legitimate edges |
| 2 | first_seen STRING, last_seen DATE_TIME | fsType/lsType: (STRING,DATE_TIME)=5,985 · (STRING,STRING)=1,383 · (DATE_TIME,DATE_TIME)=504 | **worse than described** — `last_seen` is *also* STRING on 1,383 entities, not just `first_seen`. Zero (DATE_TIME,STRING) rows exist, so wherever `first_seen` got fixed to DATE_TIME, `last_seen` was too — one write-path fix, no backfill, ever. |
| 3 | visibility='group' on 7,689 | visibility='group' on **all 7,872** (100%) | unchanged |
| 4 | 1,202 missing description/embedding/entity_id/properties, cohort stopped 08-03 | **still exactly 1,202**, latest first_seen still 2026-08-03T09:59:09Z | **confirmed the cohort is closed, not growing** — see below |
| 5 | 1,003 missing all four freshness-access props, "different cohort from #4" | **1,003 — and all 1,003 are a strict subset of #4's 1,202** | ticket's own caveat ("don't assume without checking") — checked, and they turned out *not* independent |
| 6 | 258 dup-name groups / 526 entities, "disagreeing on type" | 258 groups / 526 entities total; **only 125 groups (253 entities) actually disagree on type** — the other 133 groups agree on type, differ only in case/whitespace | ticket's framing overstated: not all 258 are type-disagreeing |
| 7 | 212 Claim nodes, all degree 1 | **222**, all still degree 1 | unchanged pattern, +10 nodes |
| 8 | 409/2,416 turns (16.9%) no DISCUSSES | 410/2,442 (16.8%) | unchanged proportion |
| 9 | sysgraph provenance tables all 0 | still all 0; proposal=27, stat=1,319 (grown from 26/1,302) | unchanged |
| — | "526 dup-group entities, 526 degree-1 entities — possibly same set, not tested" (ticket's own unverified footnote) | **tested: only 33 of 526 overlap (6%)** | resolves the footnote — the matching counts are coincidence, not identity |

### AC-4 evidence (stop-date claim)

Naively comparing `e.first_seen > '2026-08-08'` as a **string** returns 0 rows — which would wrongly
suggest entity creation itself stalled. That's item #2's exact bug biting the investigation in real
time. Comparing type-safely (`datetime(e.first_seen)` for the STRING-typed rows) shows **279 entities
created after 2026-08-08**, latest 2026-08-15, and **100% of them are fully enriched** (have a
description). The 1,202-entity gap cohort has not grown in ten days.

**Conclusion: confirmed, not refuted — enrichment genuinely stopped for entities created
2026-05-31→2026-08-03, and has been working correctly for every entity since.** This reads as a
closed historical incident, not an active one. AC-4 says "if confirmed, a separate incident ticket is
filed" — filing one for traceability (closed-on-open, since live evidence already shows it resolved),
distinct from the optional backfill-the-1,202 work (not mandated by any AC, noted as deferred).

### AC-5 evidence (cohort characterization + overlap)

The 1,003 entities missing freshness-access properties share the **exact same first_seen range**
(2026-04-17 → 2026-08-03) as item #4's cohort, and the overlap query confirms **all 1,003 are inside
the 1,202**. Causal read: an entity with no `embedding` is structurally unreachable by vector-search
recall (ADR-0087's retrieval paths), so it can never trigger a `memory.accessed` publish, so it never
accumulates freshness-access properties either. Item #5 is not an independent freshness-tracker bug —
it is a **downstream consequence of item #4** for the subset of the enrichment-gap cohort that also
happens to never get accessed via a non-embedding path (the other 199 of the 1,202 presumably get
found by exact-name lookup at least once). This is a more precise, more useful answer than "predates
FRE-161" — it does too (FRE-161/ADR-0042 shipped after 2026-08-03 per the ADR's dates), but the
overlap is the load-bearing fact.

## Codex plan-review corrections (applied below, not re-derived)

Independent codex review found this plan's first draft wrong or incomplete in three places,
verified against the actual code and a live query before accepting:

1. **The #1 migration would have silently dropped edge properties.** Confirmed live: a parallel
   `USES` edge already exists between the same nodes (`DataForge`→`GKE`) the single `USEs` edge
   connects — this is a genuine merge-with-conflict, not a rename. Fixed below.
2. **`create_relationship` is not a third `first_seen`/`last_seen` writer.** It writes
   relationship-level `created_at`/`first_accessed_at`/`last_accessed_at` — different properties,
   on the edge, not the entity. The only two entity-timestamp writers are the mention path
   (`service.py:1300-1310`) and `create_entity` (`service.py:2142-2148`); both already correct.
   Corrected below; the "no application code change needed" conclusion still holds.
3. **Item #9's "dead code" diagnosis was wrong for 2 of 3 tables.** `promoted_to` (via
   `record_promotion`, called from `captains_log/promotion.py:695`) and `produced` (via
   `record_outcome`, called from `brainstem/jobs/outcome_ingestion.py:115`, scheduled daily) are
   genuinely wired with live callers — not dead code. Traced further: `record_promotion` writes
   `proposal`+`ticket`+`promoted_to` in **one transaction** (`repository.py:359-395`), and
   `produced`'s eligibility query (`_TICKETS_AWAITING_OUTCOME_QUERY`) requires a `sysgraph.ticket`
   row via join. Since `sysgraph.ticket` is 0 rows (the ticket's own already-identified "Linear-side
   suppression" finding), **`promoted_to`=0 and `produced`=0 are downstream consequences of that
   same single root cause**, not three independent wiring gaps. Only `derives_from`,
   `correlates_with`'s write side, and `influence` have **no writer code at all** — that's the
   genuinely-unimplemented subset. Item #9's disposition below is corrected accordingly.

## Triage disposition

### Fix in this PR (cheap, mechanism-level)

**#1 — `USEs`/`USES` casing.**
- Root cause found: `MemoryService.create_relationship` (`memory/service.py:3780-3856`) uses
  `apoc.merge.relationship(source, $relationship_type, ...)` with `relationship.relationship_type`
  passed straight through — no normalization. This is the only dynamic-type relationship-creation
  path in the codebase (everything else uses a hardcoded `-[:TYPE]->`).
- Fix: normalize to uppercase before the Cypher call (`relationship.relationship_type.upper()`),
  so a casing variant becomes structurally impossible to write from this point forward.
- Guard (AC-2 "fails on a newly introduced casing variant") — **two tests, per codex review** (a
  mocked-driver test alone only guards the one dynamic writer; it wouldn't catch a future
  hardcoded mixed-case relationship added anywhere else):
  1. Unit test on `create_relationship` (mocked driver) asserting the query is invoked with the
     **normalized** type when given a lowercase/mixed-case input — fails if the normalization is
     ever removed from this call site.
  2. A small invariant-check function, `check_no_relationship_casing_variants(driver) ->
     list[str]` (returns any case-insensitive type collisions found in the graph), plus a
     TEST-substrate test that seeds a mixed-case relationship type directly via Cypher (bypassing
     `create_relationship` entirely, simulating a hypothetical future regression introduced
     anywhere) and asserts the function flags it. This is the literal "guard fails on a newly
     introduced casing variant" the AC names, independent of *where* the variant comes from.
- One-time data fix: `scripts/migrate_fre1216_relationship_casing.py`. **Not a blind rename** — a
  live check found a parallel `USES` edge already exists between the same nodes the `USEs` edge
  connects (`DataForge`→`GKE`), so this is a merge-with-conflict:
  - If no parallel canonical-cased edge exists between the same endpoints: rename in place
    (`MATCH (a)-[r:UseS... i.e. any non-canonical case]->(b) WHERE NOT (a)-[:USES]->(b) CALL
    apoc.refactor.setType(r, upperCase) YIELD input, output ...` or the simpler
    create-copy-properties-then-delete form).
  - If a parallel canonical edge already exists: merge properties onto the canonical edge
    (prefer non-null; `access_count` takes the max, not a sum, since both edges' counts already
    reflect real historical access — summing would double-count; timestamps take the latest),
    then delete the variant-cased edge. Idempotent (`apoc.meta.cypher.type`-style guard isn't
    needed here since relationship *type* isn't introspectable the same way — guard on "no
    remaining non-canonical-cased edge of this pair" instead).
  - `--confirm-prod` gated, follows `scripts/migrate_fre229_visibility_backfill.py`'s pattern.
- AC-2 verification: `MATCH ()-[r]->() RETURN DISTINCT type(r)` shows no two types differing only
  by case, post-migration.

**#2 — `first_seen`/`last_seen` STRING vs DATE_TIME.**
- Root cause found: the two entity-timestamp write sites — the mention/turn path
  (`memory/service.py:1300-1310`, `SET e.last_seen = datetime($timestamp), e.first_seen =
  COALESCE(e.first_seen, datetime($timestamp))`) and `create_entity` (`:2142-2148`, `datetime()`
  directly) — both already write native Neo4j temporal type. (`create_relationship`'s
  `:3818-3825` was originally miscited as a third writer — codex review caught this: it writes
  relationship-level `created_at`/`first_accessed_at`/`last_accessed_at`, not the entity's
  `first_seen`/`last_seen`, so it's irrelevant here.) The STRING values are historical: written
  before whatever change introduced these `datetime()` calls, never backfilled. **No application
  code change is needed** — only data. Note also: `COALESCE(e.first_seen, ...)` means an existing
  STRING-typed `first_seen` is deliberately preserved (never overwritten) by the live write path —
  only a migration can repair it.
- Fix: `scripts/migrate_fre1216_temporal_backfill.py` — for every `:Entity` where `first_seen` or
  `last_seen` is typed STRING, `SET e.first_seen = datetime(e.first_seen)` (and same for
  `last_seen`), idempotent (`apoc.meta.cypher.type(...) = 'STRING'` guard), `--confirm-prod` gated.
  Extending to `last_seen` too (not just `first_seen` as AC-3 literally names) because it's the
  identical defect on the identical nodes — running two migrations for one root cause would be
  pure waste (Step 5 fold-in).
- Regression tests (corrected per codex — the original plan named the wrong write path):
  1. TEST-substrate integration test exercising the **actual** entity-timestamp write paths — the
     mention/turn path (`service.py:1300-1310`) and `create_entity` (`:2142-2148`) — asserting a
     freshly-created/updated entity's `first_seen`/`last_seen` are DATE_TIME-typed. Proves the
     write path is correct today and stays that way.
  2. Migration-level test: seed STRING-typed `first_seen`/`last_seen` on a TEST-substrate entity,
     run `migrate_fre1216_temporal_backfill.py` against it, assert both are DATE_TIME afterward,
     then run it again and assert no error / same result (idempotency). This is the test that
     actually proves the *data* is fixed — AC-3's "not worked around" wording is about the data,
     not just the write path, and only this test exercises the migration itself.
- AC-3 verification: after migration, `apoc.meta.cypher.type(e.first_seen)` is `DATE_TIME` for all
  entities; a range query using native `datetime()` comparison against a date pair chosen so
  lexicographic string ordering would give the wrong answer (e.g. `2026-9-1` vs `2026-10-1` —
  lexicographically `"2026-9-1" > "2026-10-1"` is false when it should be true, a classic
  single-digit-month trap) returns the chronologically correct result.

### Investigated and characterized — no code fix required by this ticket's ACs

**#4** — confirmed via live measurement above; files a closed-on-open incident ticket (traceability,
not remediation — the incident already self-resolved). Backfilling the 1,202 historical entities
(re-running enrichment on them) is **not** mandated by AC-4 and is noted as optional future work, not
done here — re-enrichment means re-running the extraction/embedding pipeline against 1,202 old turns,
a materially different and larger job than this ticket's data-quality triage.

**#5** — characterized via the overlap measurement above (satisfies AC-5 in full — cause identified,
overlap measured, not assumed). No separate fix; same optional backfill note as #4 covers it (fixing
#4 would fix #5 as a side effect, once the entities are re-enrichable).

**#6** — quantified via the live measurement above (satisfies AC-6's "duplicate-group count is
re-measured" — using the *current*, more precise 125-groups/253-entities type-disagreement number,
not the ticket's blanket 258/526). **Fix deferred**: merging duplicate entities is an identity-
resolution feature (which node survives, how do conflicting properties/relationships resolve, does
type disagreement block auto-merge or need a human decision) — a design problem, not a cheap
mechanism fix, and out of proportion to this ticket's "triage the cheap ones" framing. AC-6's
"reported as partial" language anticipates exactly this: quantified, not merged.

### Flagged for owner decision — not decided by this session

**#3 — `visibility='group'` universal.** Investigated `scripts/migrate_fre229_visibility_backfill.py`
(FRE-229, the original visibility rollout): it defaulted every node to `'public'`, and the current
live data shows `'group'` instead — meaning either the entity-creation default changed from
`'public'`→`'group'` at some later point and nothing ever diverges from it, or `'group'` has always
functionally meant "the owner's one graph" in a system with no second user. This is genuinely
undecidable from code/data alone — it is a product-scoping question (does this system need
per-user/private visibility at all, or is the field vestigial) that the ticket itself frames as a
decision, not a bug. **Disposition: not fixed — flagged in the Linear handoff for the owner.**

### Noted, deferred — no acceptance criterion names these

**#7 — 212→222 Claim nodes, all degree 1 (`HAS_FACT` effectively unused).** No AC in this ticket
covers it. Wiring up the claim layer's actual usage is a separate feature (something needs to start
creating multi-entity claims), not a data-quality fix. Noted for a possible future ticket.

**#8 — 409→410/2,442 turns with no `DISCUSSES` edge.** No AC in this ticket covers it, and
distinguishing "genuinely nothing extractable" from "extraction silently failed" needs new
instrumentation (a signal the extraction pipeline doesn't currently emit) — real work, not a
data-quality triage item. Noted for a possible future ticket.

**#9 — sysgraph provenance edges (`derives_from`/`promoted_to`/`produced`/`correlates_with`/
`influence`/`signal`) all 0 rows.** Corrected per codex review — the original "all dead code" read
was wrong for 2 of 3 tables that matter here. Traced precisely:
- `promoted_to` (`record_promotion`, called from `captains_log/promotion.py:695`) and `produced`
  (`record_outcome`, called from the daily-scheduled `brainstem/jobs/outcome_ingestion.py:115`)
  **are wired with live callers** — not dead code.
- `record_promotion` writes `sysgraph.proposal` + `sysgraph.ticket` + `sysgraph.promoted_to` in
  **one transaction**. `produced`'s eligibility query joins through `sysgraph.ticket`. Since
  `sysgraph.ticket` is 0 rows — the ticket's *own* already-identified "Linear-side suppression"
  finding — **`promoted_to`=0 and `produced`=0 are structural downstream consequences of that one
  root cause**, not three independent gaps. `sysgraph.proposal` (27 rows) is populated by a
  separate, earlier detection-time write, unaffected by the ticket-write suppression.
- `derives_from`, `correlates_with`'s write side, and `influence` genuinely have **no writer code
  at all** (`derives_from` appears only in a future-work comment; `correlates_with` has a read
  query but no write).

Diagnosing *why* `sysgraph.ticket` never receives a row despite a live, wired promotion flow is real
investigative work (is the Linear API call failing? is a condition in the promotion pipeline never
met? is this the same suppression the ticket already named, or a second cause?) — not a cheap fix,
and distinct from writing the three genuinely-missing edge types. FRE-1210 already ships a
continuously-visible instrument for the overall gap regardless of whether this ticket touches it,
per the ticket's own note. **Deferred to a follow-up ticket**, scoped as two sub-problems so the
follow-up doesn't have to re-discover either: (a) diagnose the `sysgraph.ticket` write-suppression
root cause that starves `promoted_to`/`produced`, (b) implement `derives_from`/`correlates_with`
(write)/`influence`, which have no dependency on (a).

## Implementation steps

1. `memory/service.py`: normalize `relationship_type` to uppercase in `create_relationship`.
2. `memory/service.py` (or a small new `memory/graph_invariants.py`): add
   `check_no_relationship_casing_variants(driver) -> list[str]`.
3. `tests/personal_agent/memory/test_service.py`: casing-normalization regression test (mocked
   driver, assert on the query param) + the invariant-check test (seed a mixed-case type directly
   via Cypher against TEST Neo4j, assert the checker flags it).
4. `tests/personal_agent/memory/test_service.py`: DATE_TIME-write regression test against the TEST
   Neo4j substrate (`make test-infra-up` first), exercising the mention-path and `create_entity`
   write sites specifically (not `create_relationship`, which doesn't touch entity timestamps).
5. `scripts/migrate_fre1216_relationship_casing.py` — new; handles the confirmed
   parallel-edge-exists case (property merge, `access_count` takes max not sum) and the
   no-parallel-edge case (straight rename); `--confirm-prod` gated, follows FRE-229's shape.
6. `scripts/migrate_fre1216_temporal_backfill.py` — new; converts STRING-typed `first_seen`/
   `last_seen` to native DATE_TIME on all `:Entity` nodes; `--confirm-prod` gated; idempotent.
7. Migration-level idempotency test for step 6 (seed STRING timestamps in TEST substrate, run
   migration, assert DATE_TIME, run again, assert stable).
8. Run both migrations against the **TEST** substrate (`make test-infra-up`) to prove correctness;
   do **not** run against the `cloud-sim-neo4j` production-mirrored instance from this build
   session — that's a live data mutation, left as an explicit runbook step in the PR/Linear handoff
   for master/owner to execute deliberately (matches the `--confirm-prod` gate's own intent and this
   project's general caution around production writes).
9. File the FRE-1216-item-4 incident-tracking ticket (Needs Approval, closed-on-open with the
   evidence above).
10. Post the full nine-item disposition table (this document's triage section, condensed) in the
    Linear handoff, since AC-1 requires every item accounted for and several have no code diff to
    point to as evidence.

## Test plan

```
make test-infra-up
uv run pytest tests/personal_agent/memory/test_service.py -k "relationship_type or casing or temporal or first_seen" -v
uv run python scripts/migrate_fre1216_relationship_casing.py            # against test substrate (AGENT_ENVIRONMENT=test, no --confirm-prod needed)
uv run python scripts/migrate_fre1216_temporal_backfill.py              # same
make test                                                                # full suite, unaffected areas unchanged
make mypy
make ruff-check && make ruff-format
```

## Self-review routing (build skill § 8)

**Diff class: escalated.** Trigger 1 (production write path) — the two migration scripts, and
`create_relationship`'s normalization, sit directly in a Neo4j production write path, even though
the actual production *execution* of the migrations is left as a runbook step rather than run by
this session. Self-serve review (`feature-dev:code-reviewer` + `security-review`, since the scripts
touch subprocess-adjacent DB connections) still runs and fixes on-branch; PR body + Linear handoff
flag "diff class: escalated — flagged for owner `/code-review ultra` before merge" per the skill.
