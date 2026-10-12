# Hybrid Search

**Target: Neo4j 5.26 Community, Cypher 5, APOC only, no GDS, no GenAI procedures.** Examples use this server's index names (`entity_embedding`, `turn_entity_fulltext`) and the `Entity` stable key `entity_id`. Substitute your own.

Hybrid search is useful when one retrieval signal is not enough:
- Semantic vector search finds paraphrases; misses exact names, acronyms, codes, and domain terms.
- Lexical fulltext search finds exact words; misses related concepts that do not share words.
- Structural search uses graph topology or paths; captures relationships text does not contain.

Combining ranked sources improves recall and can boost results that are supported by more than one signal. The common pattern is vector + fulltext, but the same query shape works for any two or more ranked/scored sources: several vector indexes, title/body fulltext indexes, graph-derived candidate scores, or external retrieval scores.

Use when the user asks for custom Cypher hybrid search, WRRF/RRF, vector + fulltext, semantic + lexical + structural search, multiple vector indexes, or combining two+ ranked/scored retrieval sources.

## When NOT to Use
- Fulltext-only / keyword-only search -> use `neo4j-cypher-skill`
- Single vector search -> use main `neo4j-vector-index-skill`

## Rules
- Run each source independently.
- Rank each source by `score DESC, stable_id ASC`.
- Do not compare raw scores from different sources.
- Compute `contribution = sourceWeight / (rrfConstant + sourceRank)`.
- Sum contributions per node.
- Order final rows by `wrrf DESC, stable_id ASC`.
- Use `sourceK > finalK`; combine before final limiting.
- Use stable unique property for tie breaks. If no stable key exists, add one before production use.
- A structural source must already exist as a stored property or as a query-time score. This server has no GDS.

## Index Setup

Vector index:
```cypher
// WRITE - not run
CREATE VECTOR INDEX entity_embedding IF NOT EXISTS
FOR (e:Entity) ON (e.embedding)
OPTIONS {
  indexConfig: {
    `vector.dimensions`: 1024,
    `vector.similarity_function`: 'cosine'
  }
};
```

Fulltext index (one index can cover several labels):
```cypher
// WRITE - not run
CREATE FULLTEXT INDEX turn_entity_fulltext IF NOT EXISTS
FOR (n:Turn|Entity) ON EACH [n.user_message, n.name];
```

If fulltext analyzer, multi-property, or Lucene query syntax details matter, load `neo4j-cypher-skill`.

The fulltext branch below uses `db.index.fulltext.queryNodes`. The index above covers `Turn` and `Entity`, so the branch keeps `Entity` rows after the index returns `sourceK` rows. Raise `sourceK` when `Turn` rows crowd out `Entity` rows.

## Parameters

```json
{
  "query": "graph database search",
  "queryVector": [0.12, -0.03, 0.45],
  "sourceK": 20,
  "finalK": 10,
  "rrfConstant": 60.0,
  "sourceWeights": {
    "fulltext": 1.0,
    "vector": 1.0
  }
}
```

`queryVector` must hold exactly `vector.dimensions` floats (1024 here). The three values above only show the shape.

## Query Template

```cypher
CALL {
  CALL db.index.fulltext.queryNodes('turn_entity_fulltext', $query, {limit: $sourceK})
  YIELD node AS e, score
  WHERE e:Entity
  WITH e, score ORDER BY score DESC, e.entity_id ASC
  WITH collect(e) AS ranked
  UNWIND range(0, size(ranked) - 1) AS rankIndex
  RETURN
    ranked[rankIndex] AS e,
    coalesce($sourceWeights['fulltext'], 1.0) / ($rrfConstant + rankIndex + 1) AS contribution

  UNION ALL

  CALL db.index.vector.queryNodes('entity_embedding', $sourceK, $queryVector)
  YIELD node AS e, score
  WITH e, score ORDER BY score DESC, e.entity_id ASC
  WITH collect(e) AS ranked
  UNWIND range(0, size(ranked) - 1) AS rankIndex
  RETURN
    ranked[rankIndex] AS e,
    coalesce($sourceWeights['vector'], 1.0) / ($rrfConstant + rankIndex + 1) AS contribution
}
WITH e, sum(contribution) AS wrrf
ORDER BY wrrf DESC, e.entity_id ASC
LIMIT $finalK
RETURN e.entity_id AS id, e.name AS name, wrrf
```

Notes:
- An empty source yields `range(0, -1)`, which is `[]`, so that branch adds no rows and the other branch still ranks.
- Parameters are used directly. Cypher 5 has no clause that binds a query-wide alias.
- The branches are plain procedure calls, so each can use its own `YIELD ... WHERE` before ranking.

## Add More Sources

Add one `UNION ALL` branch per source. Each branch must return:

```
RETURN
  matchedNode AS e,
  weight / ($rrfConstant + rankIndex + 1) AS contribution
```

Examples:
- second vector index with different embedding model -> `$sourceWeights['vector_large']`
- fulltext index over title fields -> `$sourceWeights['title_fulltext']`
- graph-derived candidate score (for example, shared-neighbor count) converted to source rank -> `$sourceWeights['graph']`

## Checklist
- [ ] Vector and fulltext indexes `ONLINE`
- [ ] Query embedding generated with same model as stored embeddings
- [ ] Structural scores already computed before query
- [ ] `sourceK` larger than `finalK`
- [ ] Stable unique property used for tie-breaks
- [ ] Raw scores not compared across sources
- [ ] Missing source weights intentionally default to `1.0`
- [ ] Additional source branches return same columns
