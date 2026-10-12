# Advanced Graph Patterns

Load when solving path-finding, fraud detection, DAG traversal, or temporal graphs.

Left out of this copy because 5.26 refuses them: `REPEATABLE ELEMENTS` and `DIFFERENT RELATIONSHIPS` match modes, `allReduce`, and everything that calls GDS (`gds.*`).

---

## Path Selector Reference

| Selector | Returns | Use case |
|---|---|---|
| `SHORTEST 1` | One shortest path | Existence + distance |
| `ALL SHORTEST` | All equal-minimum-length paths | Parallel routing |
| `ANY` | Any path (no length guarantee) | Fast existence check |
| `SHORTEST k` | k shortest paths | Top-k routing |
| `SHORTEST k GROUPS` | All paths grouped by length up to k distinct lengths | Tier-based routing |

```cypher
// k-shortest paths with cost
MATCH p = SHORTEST 3 (a:City {name: $from})(()-[:ROAD]->()){1,}(b:City {name: $to})
WITH *, reduce(c=0, r IN relationships(p) | c + r.cost) AS totalCost
ORDER BY totalCost
RETURN totalCost, [n IN nodes(p) | n.name] AS route
```

---

## DAG Traversal and Critical Path

Model: ActivityStart/ActivityEnd nodes connected by weighted `:ACTIVITY` edges; zero-weight `:DEPENDS_ON` edges for sequencing.

```cypher
// Longest path (critical path) — small graphs only
MATCH p=(a:ActivityStart)-[:ACTIVITY|DEPENDS_ON*]->(b:ActivityEnd)
WITH b.name AS task,
     reduce(t=0, r IN [x IN relationships(p) WHERE type(x)='ACTIVITY' | x] |
            t + r.expectedTime) AS totalTime
RETURN task, max(totalTime) AS criticalPathTime
ORDER BY criticalPathTime DESC
```

Limitation: path enumeration for the longest path fails on large graphs. GDS has a DAG longest-path algorithm, but GDS is not installed on this server. Compute the critical path in application code for large DAGs.

---

## Fraud Detection — Temporal Component Graph

Pattern: User-Event-Thing model where fraud rings = connected components sharing resources (IPs, devices, emails).

**Problem**: Standard WCC includes future events → "future leakage" in ML features.
**Solution**: Chronological `:SAME_CC_AS` forest — each event links only to components existing at its timestamp.

```cypher
// WRITE - not run
// Step 1: Build temporal connected components (process events in timestamp order)
MATCH (e:Event&!ConnectedComponent)
WITH e ORDER BY e.timestamp
CALL (e) {
  MATCH (e)(()-[:WITH]->(entity)<-[:WITH]-(:ConnectedComponent)){0,1}()<-[:COMMITS]-(u)
  WITH DISTINCT e, u
  MATCH (u)-[:SAME_CC_AS]->*(cc WHERE NOT EXISTS {(cc)-[:SAME_CC_AS]->()})
  MERGE (cc)-[:SAME_CC_AS]->(e)
  SET e:ConnectedComponent
} IN TRANSACTIONS OF 100 ROWS
```

```cypher
// Step 2: Point-in-time component snapshot (features as of $asOfDate)
MATCH (cc:Event)
WHERE cc.timestamp <= $asOfDate
  AND NOT EXISTS {(cc)-[:SAME_CC_AS]->(x:Event WHERE x.timestamp <= $asOfDate)}
RETURN cc
```

```cypher
// Step 3: Retrieve component membership for an event
MATCH p=(u:User)(()-[:SAME_CC_AS]->(ev))*(e:Event {event_id: $event_id})
UNWIND ev + [e] AS event
RETURN p, [(event)-[r:WITH]->(x) | [r, x]] AS with_things
```

**Avoid O(n²) clique projection** — link events through a linear path through the shared entity instead of all pairs:
```cypher
MATCH (thing:Thing|User)
CALL (thing) {
  MATCH (e:Event)-[:WITH|COMMITS]-(thing)
  WITH DISTINCT e
  WITH collect(e) AS events
  WITH CASE size(events) WHEN 1 THEN [events[0], null] ELSE events END AS events
  UNWIND range(0, size(events)-2) AS ix
  RETURN events[ix] AS source, events[ix+1] AS target
}
RETURN source, target
```

---

## Cycle Detection with QPE

Detect non-repeating cycles without artificial length limits. A relationship is traversed once per path by default. Match mode keywords are not accepted on 5.26.

```cypher
// All cycles through a node (bounded for safety)
MATCH (start:Account {id: $id})
MATCH p=(start)(()-[:TRANSFERS_TO]->()){2,10}(start)
RETURN p, length(p) AS cycleLength
ORDER BY cycleLength LIMIT 20
```

---

## Type Predicate for Schema Discovery

Identifies properties by runtime type — useful in GraphRAG pipelines to auto-detect text fields:

```cypher
// Find all STRING properties on nodes in a label
MATCH (n:Article)
WITH keys(n) AS props, n LIMIT 1
UNWIND props AS p
WITH n, p WHERE n[p] IS :: STRING NOT NULL
RETURN p AS textProperty
```

---

## OPTIONAL CALL

Left-outer join for procedures — row kept even if procedure returns no results:

```cypher
MATCH (m:Movie), (t:Movie {title: $target})
OPTIONAL CALL apoc.algo.dijkstra(m, t, 'ROAD', 'distance') YIELD path, weight
RETURN m.title, weight    // weight is null when no path found
```
