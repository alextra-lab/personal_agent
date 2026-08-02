# FRE-1119 — recall_personal_history manufactures false absence

Revised after two rounds of adversarial codex:rescue plan review (round 1: DO NOT MERGE on a
tenant-isolation risk and a wrong write-path attribution; round 2, after fixes: DO NOT MERGE narrowed
to a single scope decision, resolved by the owner below). This is the plan as it will be built.

## Ticket

`recall_personal_history` (`src/personal_agent/tools/personal_history.py`) reports "no conversation
found" when the answer is present in the graph. Two named causes: (1) it anchors on `PARTICIPATED_IN`
edges present on a small minority of turns, (2) its `topic` filter is a literal substring match on
`user_message` only.

## Owner decision (resolves the ticket's own open scoping question)

The ticket names three options for the topic filter: semantic match, broadened lexical match, or
removal in favour of ranking. Its own reproduction case (a "harmony" query against entities named
`Voice leading`/`Counterpoint`/`Fusion Interval`/`Parallel octaves` — zero lexical overlap anywhere in
the graph) can only be closed by semantic/embedding matching, which means wiring into the existing but
separately-owned, flag-gated `dense_recall_arm`/multipath-fusion core (`memory/service.py:4672+`,
`4742+`, FRE-724). **Owner decision (2026-08-01): narrow this ticket's AC to identity-reachability +
broadened lexical ranking; do not claim the harmony example is closed; file a separate Needs-Approval
ticket for wiring semantic matching into this tool.**

## What was established before fixing — measured live, not inferred from code comments

All queries below were run read-only against the actual deployed Neo4j (`cloud-sim-neo4j`, no writes)
on 2026-08-01. Exact queries are reproduced so the numbers are independently re-runnable.

**1. Edge coverage today: 420 / 2,338 turns (18%).**
```cypher
MATCH (t:Turn) WITH count(t) AS total_turns
MATCH (t2:Turn) WHERE t2.user_id IS NOT NULL WITH total_turns, count(t2) AS property_covered
MATCH (:Person)-[:PARTICIPATED_IN]->(t3:Turn)
WITH total_turns, property_covered, count(DISTINCT t3) AS edge_covered
RETURN total_turns, edge_covered, property_covered
```
→ `total_turns=2338, edge_covered=420, property_covered=50`. Close to the ticket's cited 385/2338 (the
ticket was filed ~5 hours before this measurement).

**2. `Turn.user_id` alone is worse than the edge, not better — and property never conflicts with edge
today.**
```cypher
MATCH (t:Turn)
OPTIONAL MATCH (p:Person)-[:PARTICIPATED_IN]->(t)
WITH t, collect(DISTINCT p.user_id) AS edge_owners
RETURN
  sum(CASE WHEN t.user_id IS NOT NULL AND size(edge_owners)=0 THEN 1 ELSE 0 END) AS prop_only,
  sum(CASE WHEN t.user_id IS NULL AND size(edge_owners)>0 THEN 1 ELSE 0 END) AS edge_only,
  sum(CASE WHEN t.user_id IS NOT NULL AND size(edge_owners)>0 AND t.user_id IN edge_owners THEN 1 ELSE 0 END) AS agree,
  sum(CASE WHEN t.user_id IS NOT NULL AND size(edge_owners)>0 AND NOT t.user_id IN edge_owners THEN 1 ELSE 0 END) AS disagree,
  sum(CASE WHEN t.user_id IS NULL AND size(edge_owners)=0 THEN 1 ELSE 0 END) AS neither
```
→ `prop_only=0, edge_only=370, agree=50, disagree=0, neither=1918` (sums to 2338; edge_only+agree=420
matches finding #1). **Zero conflicts exist today** — every turn with both signals agrees. Switching
the query to `MATCH (t:Turn {user_id: $user_id})` alone would drop coverage from 18% to 2%. This plan
does not do that.

**3. Attribution correction (this was wrong in the first draft of this plan, caught by codex review).**
The first draft blamed the already-filed FRE-1115 ("bare-MERGE generator") for the 1,918 turns with
neither signal. Traced every `create_conversation(` call site in `src/`:
- `second_brain/consolidator.py:667,745` — both always pass `user_id=capture.user_id`. Not the gap.
- `memory/protocol_adapter.py:184` (`store_episode`) — the only call site that omits `user_id`. But
  `grep -rn "store_episode(" src/` returns zero callers; it is dead code in production, matching its
  own comment ("store_episode is unused in production").

The actual explanation is already documented in `create_conversation`'s own docstring
(`memory/service.py:1146-1164`): a 2026-05-30 replay run "stamped random UUIDs" and "left 1828 turns
with neither an edge nor any other record of who they belonged to" — a pre-existing historical gap from
before the property/edge write logic existed, not an ongoing generator bug. This is close to but not
exactly the measured 1,918 (**~90 turns remain otherwise unattributed**; treated here as part of the
same undifferentiated historical gap, not further investigated — auditing the full write history is
out of scope for this ticket). FRE-1115 is a real, separately-filed, smaller *ongoing* issue — just not
the cause of this measured gap. The corrected plan below no longer cites FRE-1115.

**4. The backfill's own summary counts are unreliable; independent measurement is required.**
`scripts/backfill_participated_in.py` exists to close the edge gap and tags every edge it *creates*
with `r.backfilled = true`. Measured: 0 of 420 edges carry that marker, and the monthly edge breakdown
(May 64 / Jun 169 / Jul 187, Apr 0) matches organic `create_conversation` growth with no visible
backfill contribution. This is *consistent with* the backfill never having added edges beyond the
organic set — not proof it never ran (e.g. it could have run before any of today's backfillable turns
existed). Either way, re-running it is the correct next step, but **its own printed summary cannot be
trusted for before/after verification**: `_backfill_session`'s `ON CREATE SET r.backfilled = true` only
marks edges it *creates*, but its returned `backfilled_count` sums `r.backfilled = true` unconditionally
— on any re-run, edges created by a *previous* run get relabelled as newly "created" again. Verification
must use an independent count query (below), not the script's stdout.

**5. Live-verified: a sampled cross-database join works, so the backfill mechanism itself is sound.**
Neo4j `Turn.session_id = '215452b4-...'` round-trips to a real Postgres `sessions.session_id` row with
a real `user_id`. Re-running the backfill is expected — not certain — to substantially close the gap
for turns whose session is still present in Postgres.

## Code changes — `src/personal_agent/tools/personal_history.py`

**Identity predicate: authoritative-property-with-edge-fallback, not a permissive OR.** An unconditional
`t.user_id = $user_id OR EXISTS{edge}` was flagged in review as a tenant-isolation risk: if a turn's
property and edge ever disagree about ownership (not observed today, per finding #2, but not
structurally prevented either), an OR would let *both* claimed owners retrieve it. Fixed by making the
property authoritative whenever present, and consulting the edge only when the property is absent —
this can never let a stale/conflicting edge override an authoritative property:

```cypher
CALL {
  MATCH (t:Turn {user_id: $user_id})
  RETURN t
  UNION
  MATCH (:Person {user_id: $user_id})-[:PARTICIPATED_IN]->(t:Turn)
  WHERE t.user_id IS NULL
  RETURN t
}
WITH t
WHERE t.timestamp >= $cutoff
OPTIONAL MATCH (t)-[:DISCUSSES]->(e:Entity)
WITH t, collect(DISTINCT e.name) AS entities
WITH t, entities,
     $topic IS NULL
       OR toLower(t.user_message) CONTAINS toLower($topic)
       OR toLower(coalesce(t.assistant_response, '')) CONTAINS toLower($topic)
       OR toLower(coalesce(t.summary, '')) CONTAINS toLower($topic)
       OR any(name IN entities WHERE toLower(name) CONTAINS toLower($topic))
     AS topic_matched
RETURN t.turn_id            AS turn_id,
       t.timestamp          AS timestamp,
       t.session_id         AS session_id,
       t.user_message       AS user_message,
       t.assistant_response AS assistant_response,
       t.summary            AS summary,
       entities             AS entities,
       topic_matched        AS topic_matched
ORDER BY topic_matched DESC, t.timestamp DESC
LIMIT $limit
```

Given `prop_only=0` and `agree=50` today (finding #2), this produces **identical results** to a naive
OR today — the fallback-vs-OR distinction is pure defense against a future conflict, at zero cost now.

**Why `CALL { ... UNION ... }` instead of one `WHERE (A OR B)` predicate — performance, verified live.**
The first draft used a single `MATCH (t:Turn) WHERE (...)` predicate, which the query planner cannot
serve from an index (no index exists on `Turn.user_id` or `Turn.timestamp`) — it forces a full `:Turn`
label scan. `PROFILE`d live: **31,019 DB hits** for a 22-row result, vs. **75 DB hits** for the original
edge-anchored query (`MATCH (p:Person {user_id:...})-[:PARTICIPATED_IN]->(t:Turn)`, which is served by
the existing `person_user_id_unique` index). At 2,338 turns this is still sub-second; it degrades
linearly as the corpus grows and this is a frequently-invoked tool. The `UNION` form lets each branch
use its own index — the edge branch keeps using `person_user_id_unique`, and the property branch needs
a new index (below) to get the same win.

**New index required — `ensure_turn_user_id_index()`.** Mirrors the existing `ensure_fulltext_index()` /
`ensure_entity_class_index()` idempotent pattern in `MemoryService` (`memory/service.py:3206+`), called
at startup alongside the other `ensure_*_index()` calls in `service/app.py` (~line 703). Pure read-path
addition — no data mutation, safe to ship in this PR (unlike the backfill, which is a write):
```cypher
CREATE RANGE INDEX turn_user_id_index IF NOT EXISTS FOR (t:Turn) ON (t.user_id)
```
(Existing `Turn` indexes, confirmed live via `SHOW INDEXES`: `turn_session_id_index` (RANGE on
`session_id`), `turn_entity_fulltext` (FULLTEXT on `user_message`, and `Entity.name`). No `user_id` or
`timestamp` index exists today.)

**Python return shape:**
- Add `"assistant_response": mark_truncated(r.get("assistant_response") or "", 400)` to each turn dict
  (same truncation pattern as `user_message`) — without it, a turn that matches only because the topic
  appears in `assistant_response` would come back with `topic_matched: true` and no visible evidence of
  why, which review flagged as hiding the match reason from the calling model.
- Include `"topic_matched": bool(r.get("topic_matched"))` only when `topic is not None` (keeps the
  no-topic response shape unchanged).

**Docs to update in the same file:**
- Module docstring (lines 1-7): drop "via the PARTICIPATED_IN edge" framing; describe
  property-authoritative-with-edge-fallback reachability; reference FRE-1119.
- `ctx` docstring (~line 96-97): correct "identifies the :Person node whose PARTICIPATED_IN edges
  anchor the query."
- `topic` `ToolParameter.description` (lines 42-52): currently promises a strict substring gate on
  `user_message` — a tool description that overpromises/undersells its real behavior misleads the
  calling model. New wording: a hint that ranks matching turns first across message/response/summary/
  entities; turns are still returned even without a topical match; not a semantic search.

## Tests (TDD — failing first)

**Unit (mocked driver, cheap tripwire only — not the proof) — extend
`tests/personal_agent/tools/test_recall_personal_history.py`:**
1. Cypher text sent to `session.run` contains `CALL {` and `UNION` and does **not** contain a bare
   `MATCH (p:Person {user_id: $user_id})-[:PARTICIPATED_IN]->(t:Turn)` as the sole anchor.

**Integration (live Neo4j, `pytest.mark.integration`) — new file
`tests/personal_agent/tools/test_recall_personal_history_integration.py`, fixture pattern from
`tests/personal_agent/memory/test_participated_in_edge.py` — this is the actual proof, the full identity
+ topic matrix codex review asked for:**

*Identity matrix:*
2. **Property-only** (property set via `create_conversation(user_id=uid)`, no `:Person` provisioned so
   no edge — mirrors `test_participated_in_skipped_when_person_missing`) → found.
3. **Edge-only** (property never set — `create_conversation()` with no `user_id`, then a
   `PARTICIPATED_IN` edge merged directly against a provisioned Person) → found.
4. **Both agree** (property = edge owner) → found exactly once (no duplication from the `UNION`).
5. **Both conflict — the tenant-isolation proof** (property = user A, a `PARTICIPATED_IN` edge from a
   *different* Person B merged onto the same turn) → querying as **B does NOT return the turn**
   (property is authoritative); querying as **A does**.
6. **Neither** (no property, no edge) → not found — expected, documents the residual historical gap,
   not a regression.
7. **Wrong-user control** — user C, unrelated to any seeded turn, gets nothing back.

*Topic ranking:*
8. Topic string present only in `assistant_response`, not `user_message` → turn found,
   `topic_matched is True`, and `assistant_response` is present (truncated) in the returned turn so the
   match reason is visible.
9. **True negative amid noise** — seed one topically-matching older turn plus `limit`-many newer
   unrelated in-window turns → the matching turn is still returned (ranking beats recency, not just
   "non-empty because something exists") and is ordered first.
10. Topic matches nothing anywhere → turns still returned (non-empty, via other in-window turns), not
    `[]`; all have `topic_matched is False`.
11. **Documents the deferred scope, not a regression** — seed the harmony-shaped fixture (turn whose
    text is unrelated to "harmony", `DISCUSSES` entities named `Voice leading`, `Counterpoint`) and
    query with `topic="harmony"` → the turn is still returned (non-empty, per the ranking-not-filtering
    fix) but with `topic_matched is False` — proves the identity fix works even here, while explicitly
    asserting (not just claiming in prose) that the semantic gap named in the owner decision above
    remains open.

## Explicitly out of scope (owner-confirmed)

- **Running `scripts/backfill_participated_in.py --confirm-prod` against production** — a data-mutation
  op; documented below as a runbook step for master, not executed in this PR.
- **Semantic/embedding topic matching** (owner decision above) — new Needs-Approval ticket to be filed
  referencing FRE-1119 and the existing `dense_recall_arm`/FRE-724 fusion core.
- **Auditing the full ~1,918-turn historical gap** beyond attributing the bulk to the documented
  2026-05-30 incident — the ~90-turn residual is noted, not chased further.
- **Distinguishing "found nothing" from "couldn't reach anything"** in the tool's return — ticket frames
  this as input to the separately-filed absence-epistemics work, not this ticket's AC. **Master
  confirmed mid-build (2026-08-02): this is explicitly FRE-1118's vocabulary to define, not FRE-1119's
  to invent** — three tickets (FRE-1118, -1119, -1120) each need a way to say "what was returned isn't
  what was asked for," and whichever lands first must not fix that vocabulary in place for the other
  two. Nothing in this PR adds such a signal: the per-turn `topic_matched` field added below is a
  narrower, different thing — whether *this specific returned turn* mentions the topic, used only for
  ranking — not a top-level reachable/absent signal on the tool's overall result. The return shape
  (`turns`/`total`/`window_days`/`user_id`, each turn now also carrying `assistant_response` and,
  when `topic` is set, `topic_matched`) is documented in the ticket handoff as candidate input for
  FRE-1118 to adopt or not — not as a precedent it has to work around.

## Post-merge runbook (for the ticket comment / master)

1. **Preflight** (read-only, before running the backfill) — check for anything that would make the
   backfill's blind MERGE risky or silently incomplete:
   - Sessions whose `user_id` (or owner fallback) has no matching `:Person` node — `_verify_owner_person`
     only checks the *owner* fallback UUID, not every session's actual `user_id`; a session-level MATCH
     miss silently writes nothing for that session with no error.
   - Turns whose `session_id` has no corresponding Postgres `sessions` row (unreachable by this backfill
     regardless).
   - Expected eligible/skipped counts computed independently before running, to compare against actual
     results after.
2. `uv run python -m scripts.backfill_participated_in --confirm-prod`
3. **Verify with an independent count query** (not the script's own stdout — see finding #4):
   ```cypher
   MATCH (t:Turn)
   OPTIONAL MATCH (p:Person)-[:PARTICIPATED_IN]->(t)
   WITH t, collect(DISTINCT p.user_id) AS edge_owners
   RETURN sum(CASE WHEN size(edge_owners)>0 THEN 1 ELSE 0 END) AS edge_covered,
          sum(CASE WHEN t.user_id IS NOT NULL AND size(edge_owners)>0 AND NOT t.user_id IN edge_owners THEN 1 ELSE 0 END) AS new_disagreements
   ```
   Expect `edge_covered` to rise substantially above 420/2338. **`new_disagreements` must stay 0** — a
   nonzero value means the backfill assigned an edge that contradicts an existing authoritative property
   (e.g. via the owner-fallback path), which would be a real bug to fix before trusting the new edges.
