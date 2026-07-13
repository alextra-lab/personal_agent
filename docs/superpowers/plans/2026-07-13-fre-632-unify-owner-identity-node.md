# FRE-632 — Fix ADR-0052 owner-identity node split (two disconnected "Alex" nodes)

**Ticket:** FRE-632 (Approved, Tier-1:Opus, stream:build2) · **Backing ADR:** ADR-0052 (Owner Identity Primitive, Accepted; amendment 2026-05-09) · related ADR-0107 (per-user Claim resolution).

**Owner decision (2026-07-13):** *Unify (name-aware route)* — the owner node becomes the canonical `:Person:Entity`; the one configured owner name routes to it; ADR-0052's "never by name" gets a narrow, documented exception for the configured owner name only. Bounded, accepted residual risk: a third party with the owner's *exact* name would collide.

---

## Problem (verified live, read-only, 2026-07-13)

Two nodes on `cloud-sim-neo4j`, no path between them:

- **Node A — `:Entity {name:'Alex', is_owner:NULL, user_id:NULL}`** — the extraction node. Carries `embedding`, `class`, `entity_type`, `description`, `mention_count:112`. OUT: `RELATED_TO ×31`, `USES ×15`. IN: `DISCUSSES ×77`, `RELATED_TO ×3`.
- **Node B — `:Person {name:'Alex', is_owner:true, user_id:1f7cc4bc…, source:config_bootstrap}`** — the identity node. OUT: `HAS_FACT ×26`, `HAS_STANCE ×14`, `RELATED_TO ×27`, `USES ×15`, `LOCATED_IN`, `CURRENTLY_AT`, `VISITED`, `PARTICIPATED_IN ×263`. IN: `OPERATED_BY ×1`, `RELATED_TO ×3`. **No `embedding`.**

The `USES ×15` targets are 100% identical on both (accidental bridge: `create_relationship` name-matches `'Alex'` with no owner guard → splices onto both). Node A's genuinely-unique value: `embedding` + entity-substrate props + `DISCUSSES` turn-provenance.

**Root of the fork:** extraction MERGEs `(:Entity {name})` (`service.py:1449` create_entity; `:1083` DISCUSSES) while dedup deliberately excludes `user_id`-bearing nodes (`dedup.py:154`) so third-party same-names can't collide into the owner. That guard is what re-forks the graph the moment Node A is deleted — hence a migration alone is insufficient (ticket ask #3).

---

## Target end state

A single owner node: `:Person:Entity {name:'Alex', user_id, is_owner:true, embedding, class, entity_type, description, mention_count, …}` — simultaneously the identity anchor and the searchable entity. All of Node A's relationships redirected onto it; parallel `USES`/`RELATED_TO` edges de-duplicated.

---

## Implementation

### Part 1 — Forward-fix (code): bootstrap labels the owner node `:Person:Entity`

The re-fork is structurally prevented by making the owner node *occupy* the `:Entity {name}` MERGE slot. `MERGE (e:Entity {name:$name})` matches any node with the `:Entity` label + that `name`; if the owner node carries `:Entity`, the owner's named self-references land on it instead of forking a bare node.

**Change:** `bootstrap_owner_identity()` (`service.py:2108`) adds the `:Entity` label to the owner `:Person` on create *and* on match (idempotent, re-asserted every startup — holds on fresh graphs, not only post-migration):

```cypher
MERGE (person:Person {user_id: $user_id})
  ON CREATE SET person:Entity, person.is_owner=true, person.name=$name, person.email=$email,
                person.created_at=datetime(), person.source="config_bootstrap"
  ON MATCH  SET person:Entity, person.is_owner=true,
                person.email=coalesce(person.email,$email),
                person.name=coalesce(person.name,$name)
```

No per-call owner-name branching in the extraction paths is needed — the label + exact-name MERGE does the routing. dedup already excludes the owner (`user_id IS NULL` filter), so similarity-dedup is unaffected; only the exact-name `MERGE` resolves to the owner, which is the intended unification.

**Casing caveat (documented, not fixed):** exact-name `MERGE` is case-sensitive; an extraction that yields a differently-cased/whitespaced owner name would still fork. This is a pre-existing property of name-keying generally; out of scope to normalize here.

### Part 2 — Migration (`scripts/migrate_fre632_unify_owner_identity.py`)

One-shot, idempotent, `--dry-run` default; follows the `migrate_freXXX_*.py` pattern (settings-driven, `AsyncGraphDatabase`, before/after verification logged). **Codex-reviewed before master runs it on prod.**

Core (APOC 5.26 confirmed available):
```cypher
MATCH (keep:Person {is_owner:true}) WHERE toLower(keep.name)='alex'
MATCH (drop:Entity {name:'Alex'})
  WHERE drop.user_id IS NULL AND elementId(drop) <> elementId(keep)
// 1. copy A-only entity props onto B — never overwrite B's identity props
SET keep += apoc.map.removeKeys(properties(drop), keys(keep))
WITH keep, drop
// 2. move + de-dupe relationships, union labels (→ :Person:Entity), discard drop's (already-copied) props
CALL apoc.refactor.mergeNodes([keep, drop], {properties:'discard', mergeRels:true}) YIELD node
RETURN node
```
- Idempotent: if no `drop` node matches (already merged), no-op.
- `mergeRels:true` collapses the duplicated `USES`/`RELATED_TO` parallels into one edge each; `DISCUSSES ×77` and `RELATED_TO(in)` redirect onto the owner (turn-provenance now points at the owner); `PARTICIPATED_IN`/`OPERATED_BY`/`HAS_*` on B are untouched. No A↔B edge exists → no self-loops created.
- Post: exactly one node named 'Alex', labels `:Person:Entity`, `is_owner:true`, `user_id` set, `embedding` present, all rel types preserved.

**Verify the `properties:'discard'` + `SET += removeKeys` behavior empirically on the test graph (:7688) first** — the two-step guarantees A-only props (embedding/class/…) survive regardless of APOC's conflict semantics.

### Part 3 — Tests (test Neo4j :7688; memory-integration markers)

1. **Bootstrap labels owner `:Entity`** — after `bootstrap_owner_identity`, owner node has both `:Person` and `:Entity` labels (fresh graph).
2. **Re-fork guard (the ticket's ask #3)** — bootstrap owner 'Alex'; run the entity-creation path (`create_entity` and the DISCUSSES MERGE) with name 'Alex'; assert exactly **one** node named 'Alex', it is the owner (is_owner:true, user_id set, :Person:Entity), enriched — **no** second bare `:Entity`.
3. **Third-party non-owner name still forks-free** — an entity named 'Bob' creates a normal bare `:Entity` (owner routing does not over-capture).
4. **Migration** — seed two split nodes on the test graph, run the script; assert single `:Person:Entity` owner with unioned rels, de-duped `USES` (count 15 not 30), transferred `embedding`; assert idempotent (2nd run no-op, count stable).

### Part 4 — ADR-0052 amendment (docs, in this PR)

Amendment section documenting the FRE-632 reconciliation: owner identity is now `:Person:Entity`; the one configured owner name is a deliberate, narrow exception to "anchor by user_id, never by name"; the owner's self-entity resolves to the owner node by name+label; bounded third-party-exact-name collision is accepted. Keeps the decision trail with the code.

---

## Acceptance criteria (proof for master's gate)

| # | Criterion (ADR-0052 / ticket) | Proof |
|---|---|---|
| AC1 | Owner Person and extraction entity are one node (one label set / MERGE slot). | Test 1 + Test 2: owner is `:Person:Entity`; owner-name extraction lands on it. |
| AC2 | Existing split migrated losslessly, relationships preserved, prod-safe. | Migration test (Part 3.4) + codex review + live before/after in the runbook. |
| AC3 | Bootstrap + extraction cannot re-fork. | Test 2 (re-fork guard) — exactly one 'Alex' node after extraction. |
| AC4 | Third-party entities unaffected (no over-capture). | Test 3. |

## Out of scope / follow-ups
- Name-casing normalization for entity keys (pre-existing, general).
- Removing the accidental `create_relationship` double-write bridge (`service.py:2505`) — now harmless once unified; note as a possible cleanup, do not change here unless codex flags it.

## Codex plan-review (2026-07-13) — findings & dispositions

Codex verdict was *NEEDS REVISION* (under-specification, not a fatal flaw). Dispositions, verified against the live graph:

- **Multi-user privacy / third-party-name collision (Q1.1, Q4.1)** — there are 3 other real users (Laurent, Susan, Erika) besides the owner. But `:Entity {name:'Alex'}` (Node A) **already** surfaces the owner in shared broad recall with a description, and that path (`service.py:4312`) returns only `name/type/description` — never `email` or claims. Unification **consolidates** this pre-existing exposure; it adds no new field leak. Residual, corrected risk (accepted, documented): any user's mention of the owner's name globally MERGEs onto the owner node (already true for Node A today) — so third-party facts about a same-named person land on the owner node. Bounded; entity extraction writes only non-identity props.
- **Relationship-property policy on merge (Q2.1)** — **FIXED in plan**: specify `apoc.refactor.mergeNodes(..., {mergeRels:true, properties:'discard'})` and verify on the test graph that (a) no distinct-target edge is dropped (USES count → 15, not 30; RELATED_TO de-duped), (b) lost props are only operational metadata on *duplicate* edges (weight/timestamps), never a semantic edge. Migration verification asserts per-type edge counts, not just presence.
- **Vector-index retrievability (Q2.4)** — **FIXED in plan**: post-migration proof runs an actual `db.index.vector.queryNodes` and asserts the owner node is returned, not merely `embedding IS NOT NULL`.
- **quality_monitor entity-count skew (Q1.3)** — **FOLD IN**: add `WHERE e.user_id IS NULL` to the entity-count / duplicate / name-length / graph-health `MATCH (e:Entity)` aggregates (`quality_monitor.py:194-208, 252-321`) so the owner isn't miscounted as a third-party extracted entity. Small, correct, in-scope.
- **Bootstrap ordering (Q3.2)** — `bootstrap_owner_identity` runs in the FastAPI lifespan (`app.py:633-675`) after `connect` and before serving requests, so the fresh-graph forward-fix holds on the production path. The `store_episode` adapter path is dev/test only. Documented; the re-fork test covers the post-bootstrap path.
- **Name-change coalesce (Q4.2)** — bootstrap keeps `coalesce(person.name,$name)`, so a *renamed* owner keeps the old name on the `:Entity` slot. Owner rarely renames; documented limitation, not fixed here.
- **Casing/whitespace/unicode (Q4.3)** — already out of scope (general name-keying property).

## Quality gates
`make test` (memory module then full) · `make mypy` · `make ruff-check`/`ruff-format` · `pre-commit` · code-review (high — memory/prod) · security-review (migration touches prod data path).
