# Cypher Syntax Reference

Full syntax reference for clauses, patterns, and functions on Neo4j 5.26 Community (Cypher 5).
Unmarked = stable, well-known priors. Items that upstream documents for newer releases and that 5.26 refuses are listed in `SKILL.md`, section "Not on 5.26".

---

## Index and Constraint Types

### Index decision table

| Index type | Best for | `CONTAINS`/`ENDS WITH` | Spatial | Fulltext |
|---|---|---|---|---|
| `RANGE` | `=`, `>`, `<`, `STARTS WITH`, `IS NOT NULL` | Slow (use TEXT instead) | ❌ | ❌ |
| `TEXT` | `CONTAINS`, `ENDS WITH`, `=` on strings, list `IN` with strings | ✅ | ❌ | ❌ |
| `POINT` | `point.distance()`, `point.withinBBox()` | ❌ | ✅ | ❌ |
| `FULLTEXT` | Lucene tokenized search; multiple labels/props | ❌ | ❌ | ✅ |
| `COMPOSITE` | Queries always testing 2+ properties together | — | ❌ | ❌ |

Create syntax:
```cypher
// WRITE - not run
CREATE RANGE INDEX    name1 IF NOT EXISTS FOR (n:Label) ON (n.prop);
CREATE TEXT INDEX     name2 IF NOT EXISTS FOR (n:Label) ON (n.prop);
CREATE POINT INDEX    name3 IF NOT EXISTS FOR (n:Label) ON (n.prop);
CREATE INDEX          name4 IF NOT EXISTS FOR (n:Label) ON (n.p1, n.p2);
CREATE FULLTEXT INDEX name5 IF NOT EXISTS FOR (n:Label|OtherLabel) ON EACH [n.p1, n.p2];
// Relationship index:
CREATE RANGE INDEX    name6 IF NOT EXISTS FOR ()-[r:TYPE]-() ON (r.prop)
```

### Constraint types

```cypher
// WRITE - not run
// Uniqueness (+ implicitly creates RANGE index)
CREATE CONSTRAINT constraint1 IF NOT EXISTS FOR (n:Label) REQUIRE n.prop IS UNIQUE;

// Existence (node property must not be null) -- Enterprise only
CREATE CONSTRAINT constraint2 IF NOT EXISTS FOR (n:Label) REQUIRE n.prop IS NOT NULL;

// Relationship existence -- Enterprise only
CREATE CONSTRAINT constraint3 IF NOT EXISTS FOR ()-[r:TYPE]-() REQUIRE r.prop IS NOT NULL;

// Node key = unique + existence (Enterprise only)
CREATE CONSTRAINT constraint4 IF NOT EXISTS FOR (n:Label) REQUIRE n.prop IS NODE KEY;
// Multi-property node key:
CREATE CONSTRAINT constraint5 IF NOT EXISTS FOR (n:Label) REQUIRE (n.p1, n.p2) IS NODE KEY;

// Relationship key (Enterprise only)
CREATE CONSTRAINT constraint6 IF NOT EXISTS FOR ()-[r:TYPE]-() REQUIRE r.prop IS RELATIONSHIP KEY
```

Rules:
- Add uniqueness constraint on MERGE key before loading data
- `IF NOT EXISTS` prevents error on re-run
- `SHOW CONSTRAINTS YIELD name, type` to inspect
- Community refuses every constraint type except uniqueness ("... requires Neo4j Enterprise Edition")

---

## MERGE Safety

```cypher
// WRITE - not run
// DO: MERGE on constrained key only; set other properties in ON CREATE / ON MATCH
MATCH (a:Person {id: $a}) MATCH (b:Person {id: $b})
MERGE (a)-[r:KNOWS]->(b)
  ON CREATE SET r.since = date()
  ON MATCH SET r.lastSeen = date()

// DON'T: MERGE on multiple non-constrained properties -- can create duplicates
// DON'T: MERGE a full path with unbound endpoints -- creates ghost nodes
// DON'T: MERGE key properties that are not in a constraint -- slow and creates duplicates
```

---

## Property Updates

`SET n = {}` **replaces all properties** (destructive). `SET n += {}` **merges** (additive — unmentioned properties are preserved).

```cypher
// WRITE - not run
// SET = replaces -- wipes all other properties not in the map
MATCH (n:Person {id: $id})
SET n = {name: $name, age: $age};         // every other property is removed

// SET += merges -- safe partial update
MATCH (n:Person {id: $id})
SET n += {name: $name};                   // other properties preserved

// Bulk import with parameter map -- set all map keys onto node
UNWIND $rows AS row
MERGE (n:Person {id: row.id})
SET n += row
```

---

## DELETE and REMOVE

```cypher
// WRITE - not run
// DETACH DELETE -- removes node AND all its relationships
MATCH (n:TempNode {id: $id})
DETACH DELETE n;

// DELETE relationship only
MATCH (a:Person {id: $a})-[r:KNOWS]->(b:Person {id: $b})
DELETE r;

// Plain DELETE on a node with relationships -> runtime error; always DETACH DELETE nodes

// REMOVE a property (sets it absent -- not null, absent)
MATCH (n:Person {id: $id})
REMOVE n.nickname;

// REMOVE a label
MATCH (n:Person {id: $id})
REMOVE n:VIPMember;

// Remove ALL properties -- SET to empty map (REMOVE cannot do this)
MATCH (n:Person {id: $id})
SET n = {}
```

---

## WITH Scope and Aggregation

`WITH` defines a new scope — every variable not listed is dropped. Use `WITH *` to carry all forward.

```cypher
// Variable 'b' dropped after WITH
MATCH (a:Person)-[:KNOWS]->(b:Person)
WITH a, count(*) AS friends         // 'b' is out of scope after this line
WHERE friends > 5
RETURN a.name, friends
ORDER BY friends DESC
```

`WITH` resets aggregation scope — filter on aggregates before further traversal:

```cypher
MATCH (p:Person)-[:ACTED_IN]->(m:Movie)
WITH p, count(m) AS movieCount
WHERE movieCount > 3
MATCH (p)-[:KNOWS]->(f:Person)       // second MATCH uses filtered 'p'
RETURN p.name, f.name
```

**`count(*)` vs `count(expr)`**: `count(*)` counts all rows including nulls; `count(n)` counts only non-null values. Use `count(DISTINCT n.prop)` to deduplicate.

**Aggregation grouping keys**: every non-aggregating expression in `RETURN`/`WITH` is implicitly a grouping key. There is no `GROUP BY` subclause on 5.26.

---

## ORDER BY

- No `AS alias` in ORDER BY items — `ORDER BY n.prop DESC` not `ORDER BY n.prop AS p DESC`
- No `NULLS LAST` / `NULLS FIRST` — SQL syntax; nulls sort last ascending / first descending by default
- After aggregation, sort by the RETURN alias, not the pre-aggregation variable

```cypher
// DO:
MATCH (p:Person)-[:ACTED_IN]->(m:Movie)
RETURN p.name, count(m) AS movies
ORDER BY movies DESC, p.name ASC
LIMIT 10
```

---

## Conditional Expressions

```cypher
// Generic CASE (if-elseif-else)
MATCH (n:Movie)
RETURN n.title,
  CASE
    WHEN n.rating >= 8 THEN 'great'
    WHEN n.rating >= 6 THEN 'good'
    ELSE 'skip'
  END AS verdict;

// Simple CASE (switch on one expression)
MATCH (n:Movie)
RETURN n.status,
  CASE n.status
    WHEN 'A' THEN 'Active'
    WHEN 'I' THEN 'Inactive'
    ELSE 'Unknown'
  END AS label
```

No `least()` / `greatest()` — use `CASE WHEN a < b THEN a ELSE b END`.

Conditional counting — `count(x WHERE ...)` is SQL, not Cypher:
```cypher
// DO:
MATCH (r:Review)
RETURN sum(CASE WHEN r.rating = 5 THEN 1 ELSE 0 END) AS fiveStarCount;

RETURN COUNT { MATCH (r:Review) WHERE r.rating = 5 } AS fiveStarCount
```

---

## Null Handling

```cypher
MATCH (n:Person)
WHERE n.email IS NOT NULL     // correct
RETURN n.name;

// WHERE n.email = null       // always null, never matches

// coalesce() -- returns first non-null argument
MATCH (n:Person)
RETURN coalesce(n.nickname, n.name) AS displayName
```

`collect()` and aggregation functions ignore null values. `null = null` is `null` (not `true`). `WHERE` treats `null` as `false`.

Division by zero raises an error on 5.26, also for `null / 0` ("/ by zero"). Guard divisors: `CASE WHEN d = 0 THEN null ELSE n / d END`.

---

## Type Coercion

Prefer **OrNull variants** — they return `null` on unconvertible input instead of throwing. The base forms already return `null` for a null or unparsable STRING (`toInteger('abc')` is `null`), but they throw for a type they cannot convert (`toInteger([1])` raises "Invalid input for function 'toInteger()'"):

```cypher
MATCH (n:Event)
RETURN toIntegerOrNull(n.score) AS score,
       toFloatOrNull(n.score) AS scoreF,
       toBooleanOrNull(n.flag) AS flag,
       toStringOrNull(n.value) AS value
```

Type predicates for mixed-type properties:
```cypher
MATCH (n:Event)
WHERE n.value IS :: INTEGER NOT NULL  // true only for non-null INTEGER values
RETURN n.name, n.value
```

**DateTime vs date() mismatch**: `datetime_prop >= date('2025-01-01')` returns 0 rows — use `.year` accessor or `datetime()` literals for `ZONED DATETIME` properties.

---

## List Expressions

```cypher
WITH [3, -1, 2, null] AS list
RETURN [x IN list WHERE x > 0] AS filtered,          // filter only
       [x IN list | x * 2] AS transformed,            // transform only
       [x IN list WHERE x > 0 | x * 2] AS both,       // filter + transform
       ANY(x IN list WHERE x > 0) AS anyPositive,
       ALL(x IN list WHERE x IS NOT NULL) AS allSet,
       NONE(x IN list WHERE x > 5) AS noneBig,
       SINGLE(x IN list WHERE x < 0) AS oneNegative,
       size(list) AS n,
       head(list) AS first, tail(list) AS rest, last(list) AS lastItem,
       list[0..2] AS slice,                           // slice
       list + [9] AS appended
```

`2 IN [1, null, 3]` returns `null` — guard with `IS NOT NULL` before membership tests. There is no native `cardinality()` or `coll.sort()` on 5.26: use `size()` and `apoc.coll.sort()`.

**Pattern comprehension:**
```cypher
MATCH (n:Person {id: $id})
RETURN [(n)-[:KNOWS]->(f:Person) | f.name] AS friends,
       [(n)-[:ACTED_IN]->(m:Movie) WHERE m.year > 2020 | m.title] AS recentFilms
```

Use pattern comprehensions for simple one-hop inline collections; for multi-step traversals use `COLLECT { MATCH ... RETURN ... }`.

**Map from a list or map** — there is no map comprehension on 5.26; use `apoc.map.fromPairs`:
```cypher
WITH {a: 1, b: 2} AS m
RETURN apoc.map.fromPairs([k IN keys(m) | [k, m[k] * 10]]) AS scaled
```

---

## String Functions

```cypher
WITH '  Hello, World  ' AS s
RETURN toLower(s) AS lowered, toUpper(s) AS uppered,         // lower() / upper() also exist
       trim(s) AS trimmed, ltrim(s) AS leftTrimmed, rtrim(s) AS rightTrimmed,
       btrim('xxabcxx', 'x') AS custom,                       // strips custom chars
       split(trim(s), ', ') AS parts,                         // LIST<STRING>
       substring(trim(s), 0, 5) AS head,                      // 0-indexed; length optional
       left(trim(s), 5) AS first5, right(trim(s), 5) AS last5,
       replace(s, 'l', 'L') AS replaced,                      // replaces all occurrences
       size(s) AS chars,                                      // same as char_length
       reverse(s) AS backwards,
       toString(42) AS str, toStringOrNull(null) AS nul,
       toStringList([1, 2]) AS strs                           // element-wise STRING conversion
```

All string functions return `null` when any argument is `null`.

`replace()` takes three arguments on 5.26 (no replacement limit). `toString()` accepts scalars, not LIST, MAP, NODE, RELATIONSHIP or PATH. There is no string interpolation, and `string.indexOf`, `string.join`, `string.regexReplace` do not exist: use `+`, `apoc.text.indexOf`, `apoc.text.join`, `apoc.text.replace`.

---

## Introspection Functions

```cypher
MATCH (n:Entity)-[r]->()
RETURN labels(n) AS labels,        // LIST<STRING> of all labels
       type(r) AS relType,         // STRING relationship type name
       keys(n) AS propertyKeys,    // LIST<STRING> of property keys
       properties(n) AS props,     // MAP of all properties
       elementId(n) AS elementId   // STRING internal ID [id(n) is deprecated but still works on 5.26]
LIMIT 1
```

---

## FOREACH vs UNWIND

| Use | When |
|---|---|
| `FOREACH (x IN list \| write-clause)` | Side-effect writes only — no RETURN needed |
| `UNWIND list AS x` | Need to read, filter, or return list items |

Variables bound inside `FOREACH` not visible after it; `RETURN`/`WITH` may follow (`MATCH ... FOREACH (...) RETURN n.f` valid). Use `UNWIND` to read, filter, return items.

```cypher
// WRITE - not run
// FOREACH -- side-effect only
MATCH p = (a:Person {name:'Alice'})-[:KNOWS*1..3]->(b:Person)
FOREACH (n IN nodes(p) | SET n.visited = true);

// UNWIND -- when you need to process and return
UNWIND $items AS item
WITH item WHERE item.active = true
MERGE (n:Item {id: item.id})
  ON CREATE SET n.name = item.name
RETURN count(n) AS created
```

---

## OPTIONAL MATCH

Returns `null` for the optional pattern rather than eliminating the row.

```cypher
MATCH (p:Person {id: $id})
OPTIONAL MATCH (p)-[:MANAGES]->(d:Department)
RETURN p.name, d.name AS department    // d.name is null when no match
```

Boolean check -- use EXISTS instead of OPTIONAL MATCH:
```cypher
MATCH (p:Person {id: $id})
RETURN p.name, EXISTS { (p)-[:MANAGES]->() } AS isManager
```

Do NOT chain multiple `OPTIONAL MATCH` for nested optional data — each fan-out multiplies row count. Use `COLLECT {}` instead.

---

## UNION and UNION ALL

`UNION` deduplicates (slow). `UNION ALL` keeps all rows (fast). Both branches must return identical column names and count.

```cypher
MATCH (n:Employee) RETURN n.name AS name, n.email AS email
UNION ALL
MATCH (n:Contractor) RETURN n.name AS name, n.email AS email
```

`SHOW` commands cannot be `UNION` branches on 5.26 (`Invalid input 'UNION'`).

---

## Spatial / Point

```cypher
// Create a point (WGS84 geographic, Cartesian; 2D and 3D)
RETURN point({longitude: -122.4194, latitude: 37.7749}) AS wgs2d,
       point({longitude: -122.4194, latitude: 37.7749, height: 100}) AS wgs3d,
       point({x: 1.5, y: 2.3}) AS cart2d,
       point({x: 1.5, y: 2.3, z: 4.0}) AS cart3d
```

```cypher
// WRITE - not run
// Store on node
MATCH (p:Location {id: $id})
SET p.coords = point({longitude: $lon, latitude: $lat})
```

```cypher
// Distance in metres
MATCH (a:Location) WHERE a.name = 'HQ'
MATCH (b:Location)
RETURN b.name, point.distance(a.coords, b.coords) AS distM
ORDER BY distM LIMIT 10;

// Bounding-box filter before distance (uses POINT index)
MATCH (b:Location)
WHERE point.withinBBox(b.coords,
        point({longitude: -123.0, latitude: 37.0}),
        point({longitude: -122.0, latitude: 38.0}))
RETURN b.name,
       point.distance(b.coords, point({longitude: -122.4194, latitude: 37.7749})) AS distM
```

POINT index (required for fast spatial queries):
```cypher
// WRITE - not run
CREATE POINT INDEX location_coords IF NOT EXISTS
FOR (n:Location) ON (n.coords)
```

Point components: `.x` / `.y` / `.z` (Cartesian) and `.longitude` / `.latitude` / `.height` (WGS84).

---

## Date and Time

```cypher
RETURN date() AS today,                         // DATE
       datetime() AS now,                       // ZONED DATETIME
       localdatetime() AS localNow,             // LOCAL DATETIME
       localtime() AS localClock,               // LOCAL TIME
       date('2025-01-15') AS parsedDate,
       datetime('2025-01-15T10:30:00+02:00') AS parsedDatetime,
       duration({days: 7, hours: 2}) AS span,
       date() + duration({months: 3}) AS inThreeMonths,
       duration.between(date('2026-01-01'), date('2026-03-15')) AS between,
       date.truncate('month', date()) AS monthStart   // first day of current month
```

Property components: `n.birthDate.year / .month / .day`, `n.createdAt.hour / .minute / .second / .timezone`.

Type rule: `ZONED DATETIME` properties must be compared with `datetime()` literals, not `date()` — mixing types returns 0 rows.

Duration components: `.years`, `.months`, `.days`, `.hours`, `.minutes`, `.seconds`. `duration.between(d1,d2).days` = days component after whole months: `2026-01-01` → `2026-03-15` gives `14`, not `73`. **Total** days: `duration.inDays(d1,d2).days` (`73`); `duration.inMonths`, `duration.inSeconds` same. `duration.between(d1,d2).inDays` errors.

---

## LOAD CSV

```cypher
// WRITE - not run
// With headers
LOAD CSV WITH HEADERS FROM 'file:///persons.csv' AS row
MERGE (p:Person {id: toInteger(row.id)})
SET p.name = row.name, p.score = toFloat(row.score);

// Large files -- always wrap in CALL IN TRANSACTIONS
LOAD CSV WITH HEADERS FROM 'file:///large.csv' AS row
CALL (row) {
  MERGE (p:Person {id: row.id})
  SET p += row
} IN TRANSACTIONS OF 1000 ROWS ON ERROR CONTINUE
```

All CSV fields are `STRING` — coerce explicitly. `USING PERIODIC COMMIT` is refused on 5.26; use `CALL IN TRANSACTIONS`.

---

## Subqueries

**Expression subqueries** (auto-import outer variables — no `WITH` needed):

```text
EXISTS { (a)-[:R]->(b) }
EXISTS { MATCH (a)-[:R]->(b) WHERE a.x > 0 }
NOT EXISTS { (a)-[:R]->(b) }
COUNT  { (a)-[:R]->(b) WHERE a.x > 0 }
COLLECT { MATCH (a)-[:R]->(b) RETURN b.name }   // COLLECT: full MATCH+RETURN required
// COLLECT { (a)-[:R]->(b) }                    // SYNTAX ERROR -- bare pattern invalid
```

`COLLECT {}` returns exactly one column.

**`CALL` subqueries** — outer variables NOT auto-imported; declare explicitly in `CALL (x) { ... }`:

```cypher
MATCH (p:Person)
CALL (p) {
  MATCH (p)-[:ACTED_IN]->(m:Movie)
  RETURN count(m) AS movieCount
}
RETURN p.name, movieCount
// CALL (*) imports all outer variables; CALL () imports nothing
// CALL { WITH x ... } is deprecated -- use CALL (x) { ... }
```

| Goal | Use |
|---|---|
| Boolean existence check | `EXISTS { (a)-[:R]->(b) }` |
| Count matching subgraph | `COUNT { (a)-[:R]->(b) }` |
| Collect related items into a list | `COLLECT { MATCH (a)-[:R]->(b) RETURN b.name }` |
| Nullable join | `OPTIONAL MATCH` (simple) or `OPTIONAL CALL` (complex) |
| Subquery with own aggregation or writes | `CALL (x) { ... }` |

---

## Quantified Path Expressions (QPEs)

Replace `shortestPath()` / `allShortestPaths()` and the `[:R*m..n]` syntax. All of the forms below were run on 5.26.

```cypher
// Reachability: 1-3 hops with relationship predicate
MATCH (start:Person {name: 'Alice'})
      (()-[rel:KNOWS WHERE rel.since > date('2024-01-01')]->(:Person)){1,3}
      (end)
WITH DISTINCT end
RETURN end.name;

// Inner variables become lists -- access with list comprehension
MATCH (src:Person {name: 'Alice'})
      ((n:Person)-[:KNOWS]->()){1,3}(dst:Person)
RETURN [x IN n | x.name] AS via, dst.name AS reached
```

Syntax rules:
- Prefer `{1,}` over `+`, `{0,}` over `*`
- Quantifier goes **outside** the group: `(pattern){N,M}`
- Groups must start AND end with a node

Match modes (`DIFFERENT RELATIONSHIPS`, `REPEATABLE ELEMENTS`) and the `ACYCLIC` path mode are refused on 5.26. Within one `MATCH`, a relationship is traversed at most once per path. To drop paths with a repeated node, post-filter with `size(nodes(p)) = size(apoc.coll.toSet(nodes(p)))`.

Path selectors (immediately after `MATCH`, before the pattern):

| Selector | Semantics |
|---|---|
| `SHORTEST 1` | One shortest path |
| `ALL SHORTEST` | All shortest paths of equal minimum length |
| `ANY` | Any single path (no length guarantee) |
| `SHORTEST k GROUPS` | All paths grouped by length up to k distinct lengths |

```cypher
MATCH SHORTEST 1 (a:Person {name:'Alice'})(()-[:KNOWS]->()){1,}(b:Person {name:'Bob'})
RETURN b.name
```

---

## Dynamic Labels and Properties

Dynamic labels and types are allowed in `MATCH`, `CREATE`, `MERGE`, `SET` and `REMOVE`. They are not allowed in `WHERE`.

```cypher
// Filter by dynamic label
MATCH (n:$($label))
RETURN n.name;

// Dynamic property key -- bracket notation required
MATCH (n:Config)
RETURN n[$key]
```

```cypher
// WRITE - not run
// Set label dynamically
MATCH (n:Pending)
SET n:$(n.category);

// Set a dynamic property key
MATCH (n:Config {id: $id})
SET n[$key] = $value;
// DON'T: SET n.$key = $value     // SyntaxError

// Copy properties between elements
MATCH (n:Person {id: $id})-[r:KNOWS]->()
SET n = properties(r)
// DON'T: SET n = r               // TypeError -- assigns reference, not properties
```

---

## Vector and Fulltext Queries

Call the index procedures. The index-query clause that upstream documents is refused on 5.26.

```cypher
CALL db.index.vector.queryNodes('entity_embedding', 10, $embedding) YIELD node AS c, score
WHERE score > 0.8
RETURN c.name, score
ORDER BY score DESC
```

```cypher
CALL db.index.fulltext.queryNodes('turn_entity_fulltext', $query) YIELD node, score
RETURN labels(node) AS labels, score LIMIT 20
```

Fulltext and vector scores are not comparable — rank each source separately. See `neo4j-vector-index-skill` for hybrid search.

---

## CALL IN TRANSACTIONS (write batching only)

Input stream must be **outside** the subquery — filtering inside collapses everything into one transaction.

```cypher
// WRITE - not run
// Basic batch update
MATCH (c:Customer)
CALL (c) {
  SET c.flag = 'done'
} IN TRANSACTIONS OF 1000 ROWS
RETURN count(c);

// With error handling and status reporting
LOAD CSV WITH HEADERS FROM 'file:///data.csv' AS row
CALL (row) {
  MERGE (p:Person {id: row.id})
    ON CREATE SET p.name = row.name
} IN TRANSACTIONS OF 500 ROWS
  ON ERROR CONTINUE
  REPORT STATUS AS s
WITH s WHERE s.errorMessage IS NOT NULL
RETURN s.transactionId, s.errorMessage;

// Parallel batches
UNWIND $rows AS row
CALL (row) {
  MERGE (:Movie {id: row.id})
} IN 4 CONCURRENT TRANSACTIONS OF 10 ROWS
  ON ERROR CONTINUE
```

`IN TRANSACTIONS` comes **after** the `{ }` block. Read-only use prohibited. Requires auto-commit — do not wrap in `beginTransaction()`.

**ON ERROR options on 5.26**: `FAIL` (default) | `CONTINUE` (skip failed batch) | `BREAK` (stop after first error). `RETRY FOR N SECONDS` is refused.

---

## Label Pattern Expressions

Boolean logic on labels using `|` (OR), `&` (AND), `!` (NOT):

```cypher
// Nodes with label A OR B
MATCH (n:Person|Organization) RETURN n;

// Nodes with label A AND B
MATCH (n:Employee&Manager) RETURN n;

// Nodes with label A but NOT B
MATCH (n:Person&!VIP) RETURN n;

// Complex expression
MATCH (n:Marvel|(DCComics&!Batman)) RETURN n
```

Dynamic label quantifiers in MATCH (require `$()` wrapper):
```cypher
// Node must have ALL labels in the list
MATCH (n:$all($labelList)) RETURN n;

// Node must have ANY label in the list
MATCH (n:$any($labelList)) RETURN n
```

---

## Compact CASE WHEN

Multiple values and comparison operators in a single WHEN branch.

```cypher
// Multiple values in WHEN (simple CASE)
MATCH (n:Event)
RETURN CASE n.dayOfWeek
  WHEN 1, 7 THEN 'weekend'
  WHEN 2, 3, 4, 5, 6 THEN 'weekday'
  ELSE 'unknown'
END AS dayType;

// Comparison operators in WHEN (generic CASE)
MATCH (n:Event)
RETURN CASE n.age
  WHEN > 65 THEN 'senior'
  WHEN > 18 THEN 'adult'
  WHEN < 0  THEN 'invalid'
  ELSE 'minor'
END AS ageGroup
```

---

## String Normalization

`normalize(s)` converts to NFC Unicode — solves accented character comparison where identical glyphs have different code points:

```cypher
// Match regardless of Unicode encoding differences (e.g., 'ö' as U+00F6 vs o + combining diacritic)
MATCH (c:City)
WHERE normalize(c.name) = normalize($cityName)
RETURN c
```

```cypher
// WRITE - not run
// Index on normalized form for consistent lookups
CREATE RANGE INDEX city_name IF NOT EXISTS FOR (c:City) ON (c.normalizedName);
MATCH (c:City) SET c.normalizedName = normalize(c.name)
```
