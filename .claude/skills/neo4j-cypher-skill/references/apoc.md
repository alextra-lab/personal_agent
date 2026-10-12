# APOC Core Reference

Load when using APOC procedures for graph refactoring, virtual graphs, merge helpers, path expansion, triggers, or utility operations.

Verify APOC available: `RETURN apoc.version()` (5.26.24 on this server).

APOC Core ships bundled with Neo4j. APOC Extended is a separate Labs plugin — procedures below are Core only. Every procedure and function name below was looked up on the 5.26.24 server.

Mode matters in a read session: a procedure with mode `WRITE` (`apoc.refactor.*`, `apoc.merge.*`, `apoc.do.*`, `apoc.trigger.install`) is refused in a read transaction.

---

## Graph Metadata

```cypher
// Schema snapshot: labels, rel types, properties, counts
CALL apoc.meta.schema() YIELD value RETURN value;

// Fast counts from the store counters
CALL apoc.meta.stats() YIELD labelCount, relTypeCount, propertyKeyCount, nodeCount, relCount, labels, relTypesCount
RETURN *;

// Per-label property details (name, type, nullable, indexed)
CALL apoc.meta.nodeTypeProperties()
YIELD nodeType, propertyName, propertyTypes, mandatory RETURN *;

// Per-rel-type property details
CALL apoc.meta.relTypeProperties()
YIELD relType, propertyName, propertyTypes, mandatory RETURN *
```

`apoc.meta.schema()` samples the graph; `apoc.meta.stats()` is near-instant from counters.

---

## Graph Refactoring

All refactoring procedures write.

### Rename labels / types / properties

```cypher
// WRITE - not run
// Rename label on all nodes (optional: pass list to limit scope)
CALL apoc.refactor.rename.label('OldLabel', 'NewLabel', []);

// Rename relationship type
CALL apoc.refactor.rename.type('OLD_TYPE', 'NEW_TYPE', []);

// Rename node property across all (or matched) nodes
CALL apoc.refactor.rename.nodeProperty('oldProp', 'newProp', []);

// Rename relationship property (the procedure is named typeProperty)
CALL apoc.refactor.rename.typeProperty('oldProp', 'newProp', [])
```

Config optional: `{batchSize: 10000, parallel: true}`. Third argument `[]` means all; pass a list of nodes/rels to scope.

### Merge nodes

```cypher
// WRITE - not run
// Merge person duplicates — first node is target
MATCH (a:Person {email: $email})
WITH collect(a) AS dupes
CALL apoc.refactor.mergeNodes(dupes, {
  properties: 'combine',   // 'overwrite' | 'discard' | 'combine'
  mergeRels: true
}) YIELD node RETURN node
```

### Clone nodes

```cypher
// WRITE - not run
// Clone without relationships
MATCH (n:Template {id: $id})
CALL apoc.refactor.cloneNodes([n], false, []) YIELD output RETURN output;

// Clone with relationships
MATCH (n:Template {id: $id})
CALL apoc.refactor.cloneNodes([n], true, ['internalId']) YIELD input, output, error
RETURN input, output, error
```

### Extract node from relationship

Splits a relationship into a node-rel-node triple:

```cypher
// WRITE - not run
// apoc.refactor.extractNode(rels, [labels], outRelType, inRelType)
MATCH ()-[r:TRANSACTION]->() WHERE r.amount > 10000
WITH collect(r) AS bigTxns
CALL apoc.refactor.extractNode(bigTxns, ['HighValueTx'], 'HAS_TX', 'FROM_ACCT')
YIELD input, output RETURN output
```

---

## Merge Helpers (dynamic MERGE)

Use when label or rel-type is a parameter — Cypher `MERGE` requires literal labels at compile time.

```cypher
// WRITE - not run
// apoc.merge.node(labels, identProps [, onCreateProps, onMatchProps])
CALL apoc.merge.node(['Person'], {email: $email},
  {createdAt: datetime()},
  {lastSeen: datetime()}
) YIELD node RETURN node;

// apoc.merge.relationship(startNode, relType, identProps, onCreateProps, endNode [, onMatchProps])
MATCH (a:Company {id: $from}), (b:Company {id: $to})
CALL apoc.merge.relationship(a, $relType, {}, {since: date()}, b, {})
YIELD rel RETURN rel
```

---

## Virtual Graph (in-memory, no write)

Virtual nodes/relationships exist only in query result — for projecting computed subgraphs to visualization tools or passing to other APOC procedures.

```cypher
// Virtual node — does NOT persist to DB
WITH apoc.create.vNode(['Person'], {name: 'Alice', score: 0.9}) AS vn
RETURN vn;

// Virtual relationship between two real nodes
MATCH (a:Person {id: $a}), (b:Person {id: $b})
WITH a, b, apoc.create.vRelationship(a, 'SIMILAR_TO', {score: 0.85}, b) AS vr
RETURN a, vr, b;

// Virtual subgraph from Cypher statement
CALL apoc.graph.fromCypher(
  'MATCH (a:Person)-[r:KNOWS]->(b:Person) WHERE a.age < 30 RETURN a, r, b',
  {}, 'youngNetwork', {}
) YIELD graph RETURN graph
```

---

## Path Expanders

Variable-depth traversal with label/rel-type filters. Use when depth is unknown at write time or needs runtime configuration.

### expandConfig — flexible traversal

```cypher
// apoc.path.expandConfig(startNode, config) :: (path)
MATCH (start:Person {id: $id})
CALL apoc.path.expandConfig(start, {
  minLevel: 1,
  maxLevel: 3,
  relationshipFilter: 'KNOWS>|WORKS_AT',   // direction: > out, < in, omit = both
  labelFilter: '+Person|+Company|-Blocked', // + whitelist, - blacklist
  uniqueness: 'NODE_GLOBAL',               // NODE_GLOBAL|RELATIONSHIP_GLOBAL|NODE_PATH
  bfs: true,
  limit: 100
}) YIELD path RETURN path
```

### subgraphAll — all nodes + rels in subgraph

```cypher
// Returns LIST<NODE> + LIST<RELATIONSHIP>
MATCH (root:Company {id: $id})
CALL apoc.path.subgraphAll(root, {
  maxLevel: 2,
  relationshipFilter: 'SUBSIDIARY_OF|OWNS'
}) YIELD nodes, relationships RETURN nodes, relationships
```

### spanningTree — spanning tree paths

```cypher
MATCH (root:Person {id: $id})
CALL apoc.path.spanningTree(root, {
  maxLevel: 3,
  relationshipFilter: 'FOLLOWS>'
}) YIELD path RETURN path
```

`labelFilter` syntax: `+WhitelistLabel`, `-BlacklistLabel`, `>TerminatorLabel`, `/EndNodeLabel`.

---

## Triggers

Fire Cypher on write events. Require `apoc.trigger.enabled=true` in `apoc.conf`.

On 5.26.24 both trigger APIs exist. The database-scoped one is `apoc.trigger.install(databaseName, name, statement, selector, config)`, `apoc.trigger.drop(databaseName, name)`, `apoc.trigger.stop(databaseName, name)`, `apoc.trigger.start(databaseName, name)`, `apoc.trigger.show(databaseName)`. The older one is `apoc.trigger.add`, `.remove`, `.pause(name)`, `.resume(name)`, `.list()`.

```cypher
// WRITE - not run
// Install — run from system database
USE system
CALL apoc.trigger.install(
  'neo4j',                                      // target database
  'stamp-created',                              // trigger name
  'UNWIND $createdNodes AS n SET n.createdAt = datetime()',
  {phase: 'before'}                             // before | after | rollback | afterAsync
) YIELD name, installed RETURN name, installed;

// List triggers for current database
CALL apoc.trigger.list()
YIELD name, query, selector, installed, paused RETURN *;

// Stop / drop (system db)
USE system
CALL apoc.trigger.stop('neo4j', 'stamp-created');
USE system
CALL apoc.trigger.drop('neo4j', 'stamp-created')
```

Available bindings in trigger statement: `$createdNodes`, `$deletedNodes`, `$assignedLabels`, `$removedLabels`, `$assignedNodeProperties`, `$removedNodeProperties`, `$createdRelationships`, `$deletedRelationships`.

---

## Conditional Execution

Read-only variants (mode `DEFAULT`, safe in a read session):

```cypher
// apoc.when — read-only branching
CALL apoc.when(
  size($ids) > 0,
  'MATCH (n:Person) WHERE n.id IN $ids RETURN count(n) AS count',
  'RETURN 0 AS count',
  {ids: $ids}
) YIELD value RETURN value;

// apoc.case — multi-branch, read-only
CALL apoc.case(
  [$score > 0.9, 'RETURN "high" AS tier',
   $score > 0.5, 'RETURN "mid" AS tier'],
  'RETURN "low" AS tier',
  {score: $score}
) YIELD value RETURN value.tier
```

Write variants (mode `WRITE`):

```cypher
// WRITE - not run
// apoc.do.when — write branching
CALL apoc.do.when(
  size($ids) > 0,
  'MATCH (n:Person) WHERE n.id IN $ids SET n.active = true RETURN count(n) AS count',
  'RETURN 0 AS count',
  {ids: $ids}
) YIELD value RETURN value;

// apoc.do.case — multi-branch write
CALL apoc.do.case(
  [$score > 0.9, 'MATCH (n:Person {id: $id}) SET n.tier = "high" RETURN n.tier AS tier',
   $score > 0.5, 'MATCH (n:Person {id: $id}) SET n.tier = "mid" RETURN n.tier AS tier'],
  'RETURN "low" AS tier',
  {score: $score, id: $id}
) YIELD value RETURN value.tier
```

There is no `WHEN … THEN … ELSE` subquery on 5.26. These procedures are the way to branch on a runtime condition.

---

## Collections

```cypher
// Flatten nested list
RETURN apoc.coll.flatten([[1,2],[3,[4,5]]], true);  // [1,2,3,4,5]

// Distinct union of two lists
RETURN apoc.coll.union([1,2,3], [2,3,4]);           // [1,2,3,4]

// Deduplicate list
RETURN apoc.coll.toSet([1,2,2,3]);                  // [1,2,3]

// Sort a list (there is no native coll.sort on 5.26)
RETURN apoc.coll.sort([3,1,2])                      // [1,2,3]
```

There are no native `coll.*` functions on 5.26. List comprehension `[x IN list | ...]` only maps/filters, keeps nesting.

---

## Maps

```cypher
// Merge two maps (right overwrites left on key collision)
RETURN apoc.map.merge({a:1, b:2}, {b:3, c:4});      // {a:1, b:3, c:4}

// Build map from list of [key, value] pairs
RETURN apoc.map.fromPairs([['k1',1],['k2',2]]);     // {k1:1, k2:2}

// Extract sub-map by keys
RETURN apoc.map.submap({a:1,b:2,c:3}, ['a','c'])   // {a:1, c:3}
```

---

## JSON Conversion

```cypher
// Serialize any Cypher value to JSON string
MATCH (n:Event {id: $id})
RETURN apoc.convert.toJson(n{.*});

// Parse JSON string → Cypher list
WITH '[{"name":"Alice"},{"name":"Bob"}]' AS raw
RETURN apoc.convert.fromJsonList(raw, '$[*].name', []);  // ['Alice','Bob']

// Parse JSON string → Cypher map
WITH '{"score":0.9,"tier":"A"}' AS raw
RETURN apoc.convert.fromJsonMap(raw, null, [])
```

---

## Date / Time Utilities

Prefer native `datetime()`, `date()`, `duration`. Use APOC date only when parsing non-ISO legacy format strings or converting epoch integers.

```cypher
// Parse legacy date string → epoch ms
RETURN apoc.date.parse('2024-03-15 09:00:00', 'ms', 'yyyy-MM-dd HH:mm:ss');

// Format epoch ms → string
RETURN apoc.date.format(1710489600000, 'ms', 'yyyy-MM-dd', 'UTC');

// Convert between units (ms → s)
RETURN apoc.date.convert(1710489600000, 'ms', 's')
```

---

## String Utilities

```cypher
// Split by regex
RETURN apoc.text.split('a,b,,c', ',', 0);          // ['a','b','','c']

// Join list of strings
RETURN apoc.text.join(['foo','bar','baz'], '-');    // 'foo-bar-baz'

// URL-safe slug
RETURN apoc.text.slug('Hello World! 2025', '-');   // 'hello-world-2025'

// Regex capture groups
RETURN apoc.text.regexGroups('2025-04-01', '(\\d{4})-(\\d{2})-(\\d{2})')
// [['2025-04-01','2025','04','01']]
```

---

## Node Lookup by ID

```cypher
// Fetch nodes by a list of element ids
MATCH (n:Person)
WITH collect(elementId(n))[0..3] AS ids
CALL apoc.nodes.get(ids) YIELD node RETURN node
```

Prefer `elementId(n)` over integer IDs — stable across restores. The procedure raises `EntityNotFoundException` for an id that does not exist, so pass ids that you read from the graph.

---

## Export

Pass `{stream:true}` to return data inline instead of file output. File output needs `apoc.export.file.enabled=true` in `apoc.conf`.

```cypher
// Export query results to CSV (inline)
CALL apoc.export.csv.query(
  'MATCH (p:Person) RETURN p.name AS name, p.age AS age',
  null,
  {stream: true}
) YIELD data RETURN data
```

The next two statements write files on the server:
```cypher
// WRITE - not run
// Export to JSON file
CALL apoc.export.json.query(
  'MATCH (n:Event)-[r:ATTENDED_BY]->(p:Person) RETURN n, r, p',
  '/var/lib/neo4j/import/events.json',
  {}
) YIELD file, nodes, relationships, properties RETURN *;

// Export as Cypher CREATE/MERGE statements
CALL apoc.export.cypher.query(
  'MATCH (n:Config) RETURN n',
  '/var/lib/neo4j/import/config.cypher',
  {format: 'cypher-shell'}   // cypher-shell | plain | neo4j-shell
) YIELD file RETURN file
```

---

## Not on 5.26

Upstream marks several APOC forms as deprecated in the 2025 dialect (the older trigger calls, `apoc.do.when` / `apoc.do.case`, `apoc.coll.flatten`, `apoc.coll.sort`). Their native replacements do not exist on 5.26 (`coll.flatten` and `coll.sort` are unknown functions there), so the APOC forms above are the current ones. `apoc.periodic.iterate` has a native alternative on 5.26: `CALL { ... } IN TRANSACTIONS OF N ROWS`.

---

## WebFetch

| Need | URL |
|---|---|
| Full procedure list | `https://neo4j.com/docs/apoc/5/overview/` |
| Path expander config | `https://neo4j.com/docs/apoc/5/graph-querying/path-expander/` |
| Trigger reference | `https://neo4j.com/docs/apoc/5/background-operations/triggers/` |
| Refactoring ops | `https://neo4j.com/docs/apoc/5/graph-refactoring/` |
| Export config | `https://neo4j.com/docs/apoc/5/export/` |
