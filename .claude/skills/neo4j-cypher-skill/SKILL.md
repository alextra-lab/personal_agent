---
name: neo4j-cypher-skill
description: Generates, optimizes, and validates Cypher 5 queries for Neo4j 5.26 Community.
  Use when writing new Cypher queries, optimizing slow queries, graph pattern matching,
  vector or fulltext search, subqueries, or batch writes. Covers MATCH, MERGE, CREATE, WITH,
  RETURN, CALL, UNWIND, FOREACH, LOAD CSV, expressions, functions, indexes, and subqueries.
  Does NOT cover DB administration or server ops.
---

**Target: Neo4j 5.26 Community, Cypher 5, APOC only, no GDS, no GenAI procedures.** Copied from neo4j-contrib/neo4j-skills (MIT) and adapted. Changes and source commit: `UPSTREAM.md`. Every claim below that names a 5.26 behavior was checked on a 5.26.24 Community server.

## When to Use
- Writing, optimizing, or debugging Cypher queries
- Graph pattern matching, QPEs, variable-length paths
- Vector/fulltext search, subqueries, batch writes, LOAD CSV

## When NOT to Use
- **DB admin** (users, config, backups)
- **Hybrid search that combines vector with fulltext or other ranked sources** → `neo4j-vector-index-skill`
- **Choosing labels, relationship types, constraints** → `neo4j-modeling-skill`

Upstream documents a newer dialect. Section "Not on 5.26" lists what it names and this server refuses. Do not copy those forms from upstream pages.

---

## Pre-flight

| ? | Known | Unknown |
|---|---|---|
| Schema (from context or live DB) | Use directly | Run Schema-First Protocol |
| Executing (not generating)? | Use EXPLAIN + write gate | State query is unvalidated |

Schema unknown + no tool → produce non-executable sketch outside a code block:
```
(<SOURCE_LABEL> {<KEY>: $value})-[:<REL_TYPE>]->(<TARGET_LABEL>)
```
Never fill guessed names — realistic guesses get copied blindly.

---

## Defaults — apply every query

1. No version prefix. The default is Cypher 5, and `CYPHER 5` is the only version option the server accepts. Query options such as `CYPHER inferSchemaParts = most_selective_label` are valid.
2. Schema first — inspect before writing; if schema in prompt, use it directly
3. `MERGE` on constrained key only; rel `MERGE` on already-bound endpoints only
4. Label-free `MATCH (n)` forbidden unless `n` is already bound. Use a dynamic label in the pattern: `MATCH (n:$($label))`. `WHERE n:$($label)` is refused on 5.26 ("Dynamic Label and Types are only allowed in MATCH, CREATE, MERGE, SET and REMOVE clauses")
5. `LIMIT 25` default on all exploratory reads; push `WITH n LIMIT` before high-cardinality operations (variable-length traversals, fan-out MATCH, Cartesian products)
6. Comments: `//` only — `--` is SQL, invalid
7. `SHOW` commands: `YIELD` before `WHERE`, `RETURN` after. `SHOW DATABASES` needs the system db (`USE system`)
8. Inline node predicates `(:Label WHERE p=x)` — valid in `MATCH` only
9. `WHERE` cannot follow bare `UNWIND` — use `WITH x WHERE`
10. `(a)-[:R]-(b)` — undirected matches both directions, double-counts; use directed unless unknown
11. `DETACH DELETE` — plain `DELETE` throws if node has relationships

---

## Style

| Element | Convention |
|---|---|
| Node labels | PascalCase `:Person` |
| Rel types | SCREAMING_SNAKE_CASE `:KNOWS` |
| Properties/vars | camelCase `firstName` |
| Clauses | UPPERCASE `MATCH` |
| Booleans/null | lowercase `true false null` |
| Strings | single-quoted; double only if contains `'` |

> Schema is truth. `:Person`, `:KNOWS`, `name` in examples are illustrative — substitute real names from schema.

---

## Schema-First Protocol

1. Schema in context → use it, skip inspection. Validation rules: [references/schema-guardrail.md](references/schema-guardrail.md).
   - **Existence** — labels/rel-types/properties must be in schema; try synonym resolution before asking
   - **Property type** — reason about intent first (e.g. string vs INTEGER may be null check); ask only if unclear
   - **Relationship direction** — wrong direction → correct silently and note
   - **Synonym mapping** — unambiguous → resolve silently; ambiguous → pick most likely, note; ask if unresolvable

2. Schema missing → run:
```cypher
CALL db.schema.visualization() YIELD nodes, relationships RETURN nodes, relationships;
SHOW INDEXES YIELD name, type, labelsOrTypes, properties, state WHERE state = 'ONLINE';
SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties;
SHOW PROCEDURES YIELD name RETURN split(name,'.')[0] AS namespace, count(*) AS procedures;
```

Property types per label — check APOC first:
```cypher
// APOC is installed (preferred — use this):
CALL apoc.meta.schema() YIELD value RETURN value;
```

```cypher
// Without APOC, and only on a database of 100k nodes/rels or fewer (expensive on large graphs):
CALL db.schema.nodeTypeProperties() YIELD nodeType, propertyName, propertyTypes, mandatory;
CALL db.schema.relTypeProperties() YIELD relType, propertyName, propertyTypes, mandatory;
```

Validate before returning any query: label exists · rel type+direction correct · property on that label · index ONLINE.

---

## Key Patterns

### MERGE
```cypher
// WRITE - not run
// MERGE on constrained key; set extras in ON CREATE/ON MATCH
MATCH (a:Person {id: $a}) MATCH (b:Person {id: $b})
MERGE (a)-[r:KNOWS]->(b)
  ON CREATE SET r.since = date()
  ON MATCH  SET r.lastSeen = date()
```
`SET n = {}` replaces all props. `SET n += {}` merges (safe partial update). Use `+=` for updates.

### WITH scope
```cypher
MATCH (a:Person)-[:KNOWS]->(b:Person)
WITH a, count(*) AS friends   // b dropped here
WHERE friends > 5
RETURN a.name, friends ORDER BY friends DESC
```
Every var not listed in `WITH` is dropped. `WITH *` carries all forward.

### Subqueries — cheat sheet
```
EXISTS { (a)-[:R]->(b) }                           // boolean check
COUNT  { (a)-[:R]->(b) WHERE a.x > 0 }             // count
COLLECT { MATCH (a)-[:R]->(b) RETURN b.name }       // collect list (full MATCH+RETURN required)
CALL (p) { MATCH (p)-[:ACTED_IN]->(m) RETURN m }    // correlated subquery (explicit import)
OPTIONAL CALL (p) { ... }                           // nullable subquery
```
`CALL { WITH x ... }` deprecated → `CALL (x) { ... }`. `COLLECT {}` returns exactly one column.

### CALL IN TRANSACTIONS (bulk writes)
```cypher
// WRITE - not run
LOAD CSV WITH HEADERS FROM 'file:///data.csv' AS row
CALL (row) {
  MERGE (p:Person {id: row.id}) SET p += row
} IN TRANSACTIONS OF 1000 ROWS ON ERROR CONTINUE REPORT STATUS AS s
RETURN s.committed AS committed, s.errorMessage AS error
```
Input stream must be outside subquery. Auto-commit only — never wrap in `beginTransaction()`. `USING PERIODIC COMMIT` is refused on 5.26. With `REPORT STATUS`, the query must end in `RETURN` (a bare `CALL` is refused).

### QPE basics
```cypher
MATCH SHORTEST 1 (a:Person {name:'Alice'})(()-[:KNOWS]->()){1,}(b:Person {name:'Bob'})
RETURN b.name
```
Quantifier outside group: `(pattern){N,M}`. Groups start+end with node.

No repeated nodes in a path — post-filter (no `ACYCLIC` path mode on 5.26):
```cypher
MATCH p = (start:Router {name: $from})-[:LINK*1..6]-(end:Router {name: $to})
WHERE size(nodes(p)) = size(apoc.coll.toSet(nodes(p)))
RETURN [n IN nodes(p) | n.name] AS route
ORDER BY length(p) LIMIT 5
```
Bound variable-length patterns unless the graph is known to be small (the DAG and component examples in `references/advanced-patterns.md` are unbounded on purpose and say so). Match mode keywords (`REPEATABLE ELEMENTS`, `DIFFERENT RELATIONSHIPS`) are refused: "Match modes ... are not supported yet".

### Conditional write logic
No `WHEN … THEN … ELSE` subquery on 5.26. Use `apoc.do.when` / `apoc.when` (see [references/apoc.md](references/apoc.md)) or split the work into separate `CALL { ... }` subqueries with a `WHERE` guard.

### Dynamic labels and relationship types
```cypher
// WRITE - not run
// Create/match/merge with dynamic rel type (must resolve to exactly one STRING)
CREATE (a:Node)-[:$($relType)]->(b:Node)
```

```cypher
MATCH (a:Node)-[:$($relType)]->(b:Node) RETURN a.name, b.name
```
Dynamic labels use the same wrapper in `MATCH`, `CREATE`, `MERGE`, `SET` and `REMOVE`: `MATCH (n:$($label))`, `SET n:$($label)`. They are not allowed in `WHERE`.

### Spatial / Point
```cypher
// WRITE - not run
// WGS84 geographic point
MATCH (n:Place {name: $name})
SET n.coords = point({longitude: $lon, latitude: $lat})
```

```cypher
// Distance in metres; requires POINT index for performance
MATCH (a:Place {name: $origin}) MATCH (b:Place)
RETURN b.name, point.distance(a.coords, b.coords) AS distM
ORDER BY distM LIMIT 10
```

```cypher
// Bounding-box pre-filter (uses POINT index) then distance
MATCH (b:Place)
WHERE point.withinBBox(b.coords,
        point({longitude: $west, latitude: $south}),
        point({longitude: $east, latitude: $north}))
RETURN b.name, point.distance(b.coords, point({longitude: $west, latitude: $south})) AS distM
```
Create POINT index: `CREATE POINT INDEX name IF NOT EXISTS FOR (n:Place) ON (n.coords)`

### Aggregation grouping keys
Non-aggregating expressions in `RETURN`/`WITH` are implicit grouping keys. There is no `GROUP BY` clause on 5.26:
```cypher
// actor + director are grouping keys; count(*) is the aggregate
MATCH (a:Person)-[:ACTED_IN]->(m:Movie)<-[:DIRECTED]-(d:Person)
RETURN a.name, d.name, count(*) AS collaborations
ORDER BY collaborations DESC
```
`count(n)` counts non-null; `count(*)` counts rows including nulls. `collect(DISTINCT expr)` deduplicates.
`count()` is faster than `size(collect())` — count() reads the internal store; collect() builds a list first.

---

## Common Syntax Traps (top causes of broken queries)

| Wrong | Right |
|---|---|
| `ORDER BY n.prop AS x DESC` | `ORDER BY n.prop DESC` |
| `ORDER BY preAggVar` after agg RETURN | Use RETURN alias |
| `count(r WHERE r.x=5)` | `sum(CASE WHEN r.x=5 THEN 1 ELSE 0 END)` |
| `UNWIND list AS x WHERE x>5` | `UNWIND list AS x WITH x WHERE x>5` |
| `least(a,b)` / `greatest(a,b)` | `CASE WHEN a<b THEN a ELSE b END` |
| `-- comment` | `// comment` |
| `shortestPath((a)-[*]->(b))` (still valid) | Prefer `SHORTEST 1 (a)(()-[]->()){1,}(b)` |
| `id(n)` (deprecated, works on 5.26) | `elementId(n)` |
| `[:REL*1..5]` (still valid) | Prefer `(()-[:REL]->()){1,5}` |
| `CALL { WITH x ... }` (deprecated) | `CALL (x) { ... }` |
| `COLLECT { (a)-[:R]->(b) }` | `COLLECT { MATCH ... RETURN b }` |
| `SET n = {k:v}` partial update | `SET n += {k:v}` |
| `DELETE n` with relationships | `DETACH DELETE n` |
| `WHERE n.x = null` | `WHERE n.x IS NULL` |
| `n.$key` dynamic property | `n[$key]` |
| `SET n:$label` | `SET n:$($label)` |
| `ZONED DATETIME >= date(...)` → 0 rows | Use `datetime(...)` or `.year` accessor |
| ISO string with `Z` suffix stored/compared as UTC | **`Z` ≠ UTC in Neo4j** — `Z` is parsed as an offset, not the UTC timezone; planner and range indexes treat them differently. Explicitly coerce: `datetime({datetime: datetime('2025-09-10T03:43:00Z'), timezone: 'UTC'})` ([neo4j#13519](https://github.com/neo4j/neo4j/issues/13519)) |
| `FOREACH ... RETURN` | `UNWIND ... RETURN` |

Full trap table → [references/syntax-traps.md](references/syntax-traps.md)

---

## Not on 5.26 — what upstream documents and this server refuses

Each row was run on 5.26.24 Community. The error is the server's own.

| Upstream documents | 5.26 result | Use instead |
|---|---|---|
| A `CYPHER` version prefix other than 5 | "Valid options are: 5" | No prefix |
| The index-query clause (vector 2026.01, fulltext 2026.09) | Syntax error | `CALL db.index.vector.queryNodes(...)`, `CALL db.index.fulltext.queryNodes(...)` |
| The GQL variable-binding clause, `FILTER`, `FOR`, `NEXT` | Syntax error | `WITH`, `WHERE`, `UNWIND`. `FINISH` and `INSERT` do parse |
| Match modes `REPEATABLE ELEMENTS`, `DIFFERENT RELATIONSHIPS` | "not supported yet" | Bound the pattern; see QPE basics |
| `ACYCLIC` path mode | Syntax error | Post-filter with `apoc.coll.toSet` (QPE basics) |
| `WHEN … THEN … ELSE` conditional `CALL` | Syntax error | `apoc.do.when`, `apoc.when` |
| `IN CONCURRENT TRANSACTIONS ... DISJOINT BY` | Syntax error | Plain `IN TRANSACTIONS OF n ROWS` |
| `ON ERROR RETRY FOR n SECONDS` | "expected 'BREAK', 'CONTINUE' or 'FAIL'" | `ON ERROR CONTINUE` or `BREAK` |
| `GROUP BY` subclause | Syntax error | Implicit grouping |
| String interpolation `s"..."`, `uuid()`, UUID type | Syntax error / unknown function | `+` with `toString()`, `randomUUID()` as STRING |
| `cardinality()`, `string.indexOf()`, `string.join()`, `coll.sort()` | Unknown function | `size()`/`length()`, `apoc.text.indexOf`, `apoc.text.join`, `apoc.coll.sort` |
| `allReduce(...)`, map comprehension `{k: v IN m \| ...}` | Syntax error | `reduce()`, `apoc.map.fromPairs` |
| `PROPERTY_EXISTS()`, `IS LABELED`, other GQL aliases | Unknown function / syntax error | `IS NOT NULL`, `n:Label` |
| `1/0` returning `null` | Raises "/ by zero" | `CASE WHEN d = 0 THEN null ELSE n / d END` |
| `toString()` on a LIST, MAP or NODE | Type mismatch | Convert the scalar parts |
| `SHOW TRANSACTIONS ... currentQueryProgress` | "non-existing column" | `currentQuery`, `status`, `elapsedTime` |
| `ALTER CURRENT GRAPH TYPE`, `SHOW CURRENT GRAPH TYPE` | Syntax error | `CREATE CONSTRAINT`, `CREATE INDEX` |
| Vector index `WITH [...]` filter properties, `vector.quantization.type` | Syntax error / unrecognized setting | See `neo4j-vector-index-skill` |

---

## Output Mode and Write Gate

Default: parameterized queries. **Return named properties, not full nodes or `RETURN *`.**
```cypher
// RIGHT: agent gets named fields it can reason over
MATCH (n:Organization {name: $name}) RETURN n.name, n.founded, n.industry LIMIT 10
```

```text
// WRONG: full node object wastes tokens, leaks all properties, agent can't extract fields cleanly
MATCH (n:Organization {name: $name}) RETURN n LIMIT 10
```
Exception: schema/diagnostic queries (`CALL db.schema.visualization()`, `SHOW INDEXES YIELD *`, `EXPLAIN`) where the object is the point.

**Validation workflow:**
1. `EXPLAIN` before any write — catches syntax errors, missing indexes
2. New read: test with `LIMIT 1` first
3. Write: verify read half as `RETURN` before replacing with `SET`/`CREATE`/`DELETE`
4. `PROFILE` to measure db hits; check for `AllNodesScan`, `CartesianProduct`, `Eager`

**Query API v2** (no driver needed — works for schema inspection, EXPLAIN, reads, writes; checked on 5.26):
```bash
curl -X POST http://localhost:7474/db/<database>/query/v2 \
  -u <user>:<password> -H "Content-Type: application/json" \
  -d '{"statement": "EXPLAIN MATCH (n:Person {name: $name}) RETURN n", "parameters": {"name": "Alice"}}'
# Response: {"data": {"fields": [...], "values": [...]}}  — prefix EXPLAIN to plan without executing
```

**Write execution gate** — only when agent executes (cypher-shell/HTTP), NOT when generating for code/scripts/user to run:
1. Run `EXPLAIN` → report estimated rows affected
2. Wait for user confirmation before executing

---

## Performance

EXPLAIN/PROFILE red flags: `AllNodesScan` `CartesianProduct` `NodeByLabelScan` `Eager`

Fix Eager — three approaches (choose simplest that works):
1. **Add specific labels** to MATCH nodes to eliminate read/write ambiguity:
   `MATCH (x:CallingPoint)` instead of bare `MATCH (x)` when writing `:City` nodes
2. **Collect first, then write**: `WITH collect(u) AS users UNWIND users AS u ...`
3. **CALL IN TRANSACTIONS**: isolates each batch in its own transaction

Label inference — when planner underestimates selectivity on multi-label queries:
```cypher
CYPHER inferSchemaParts = most_selective_label
MATCH (admin:Administrator {name: $name}), (resource:Resource {name: $res})
MATCH p=(admin)-[:MEMBER_OF]->()-[:ALLOWED_INHERIT]->(company)
RETURN count(p)
```

Index anchors: every MATCH/MERGE/WHERE on a property needs an index on the lookup property or Neo4j scans all nodes. **Index only activates when the node has a label** — `MATCH (n {prop: $v})` never uses an index; `MATCH (n:Label {prop: $v})` does. MERGE without a constraint has no atomicity guarantee (two concurrent MERGEs can create duplicates). `CONTAINS`/`ENDS WITH` → TEXT index (RANGE does not support them). Force a plan with `USING INDEX n:Label(prop)` when EXPLAIN shows a scan.
Chained `OPTIONAL MATCH` for nested data → replace with `COLLECT { MATCH ... RETURN }`.
Dynamic labels (`$($label)`) → `AllNodesScan`+Filter; use static labels when possible.

Full anti-patterns → [references/performance.md](references/performance.md)

---

## Failure Recovery

- 0 results: check param types, remove WHERE predicates one-by-one, EXPLAIN for index use
- TypeErrors: use `toIntegerOrNull()`/`toFloatOrNull()`; guard with `IS NOT NULL`. Note that `toInteger('abc')` and `toInteger(null)` return `null` on 5.26 and do not throw
- Variable out of scope: not listed in `WITH` → use `count(*)` not `count(droppedVar)`
- Timeouts: fix AllNodesScan → add early `LIMIT` → `CALL IN TRANSACTIONS OF 1000 ROWS`
- Long-running query: `SHOW TRANSACTIONS YIELD currentQuery, status, elapsedTime`
- DateTime mismatch: `ZONED DATETIME >= date(...)` → 0 rows; use `datetime()` or `.year`
- `Z` suffix ≠ UTC timezone: ISO strings with `Z` are stored as a UTC-offset, not the UTC zone — range queries across `Z` and `UTC` stored values return 0 rows. Coerce on write: `datetime({datetime: datetime($isoStr), timezone: 'UTC'})`
- Duration: `duration.between(d1,d2).days` = days *component* (Jan 1 → Mar 15: 14), not total. Total days: `duration.inDays(d1,d2).days` (73); same for `inMonths`, `inSeconds`. `.inDays` is not an accessor on duration values
- `Cannot merge node using null property value`: MERGE key resolved to null — validate params first
- `IndexNotFoundError`: `SHOW INDEXES YIELD name, state WHERE state <> 'ONLINE'`

---

## References

Load on demand:
- [references/indexes.md](references/indexes.md) — index types (RANGE/TEXT/FULLTEXT/POINT/COMPOSITE/LOOKUP), constraints, MERGE lock semantics, fulltext Lucene syntax, import pre-flight
- [references/cypher-syntax.md](references/cypher-syntax.md) — full syntax reference: WITH, DELETE, ORDER BY, CASE, null, lists, strings, dates, spatial/point, LOAD CSV, subqueries, QPEs, dynamic labels, label pattern expressions, index/constraint types table
- [references/syntax-traps.md](references/syntax-traps.md) — syntax trap table
- [references/performance.md](references/performance.md) — anti-patterns, text vs fulltext indexes, Eager (3 fix strategies), label inference, batching best practices
- [references/advanced-patterns.md](references/advanced-patterns.md) — multi-stop QPE, DAG critical path, component graph, cycle detection, OPTIONAL CALL
- [references/apoc.md](references/apoc.md) — APOC Core: refactoring, virtual graph, merge helpers, path expanders, triggers, collections, conditional execution
- [references/schema-guardrail.md](references/schema-guardrail.md) — schema validation rules and examples

## WebFetch

| Need | URL |
|---|---|
| Clause semantics | `https://neo4j.com/docs/cypher-manual/5/clauses/{clause}/` |
| Function signatures | `https://neo4j.com/docs/cypher-manual/5/functions/{type}/` |
| Patterns / paths | `https://neo4j.com/docs/cypher-manual/5/patterns/` |
| Spatial/point functions | `https://neo4j.com/docs/cypher-manual/5/functions/spatial/` |
| Index/constraint reference | `https://neo4j.com/docs/cypher-manual/5/indexes/` |
| Full cheat sheet | `https://neo4j.com/docs/cypher-cheat-sheet/5/all/` |

---

## Checklist
- [ ] Schema inspected or confirmed in context
- [ ] No version prefix (or `CYPHER 5`)
- [ ] `$parameters` used (not literals)
- [ ] `LIMIT` on exploratory reads (default 25)
- [ ] `EXPLAIN` run; red flags resolved
- [ ] Write half verified as `RETURN` before executing
- [ ] Write execution gate applied if agent is executing (not generating)
- [ ] `MERGE` on constrained key only
- [ ] No label-free `MATCH (n)`
- [ ] Schema ops not inside explicit transaction
