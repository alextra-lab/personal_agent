---
name: neo4j-vector-index-skill
description: Create and manage Neo4j vector indexes on Neo4j 5.26 Community (Cypher 5), run
  vector similarity search (ANN/kNN) with db.index.vector.queryNodes(), store embeddings on
  nodes or relationships, configure HNSW options, pick similarity function and embedding
  dimensions, and batch-update embeddings. Use when tasks involve CREATE VECTOR INDEX,
  vector.dimensions, cosine/euclidean search, embedding ingestion pipelines, semantic
  nearest-neighbor lookup, or hybrid search (vector + fulltext, multiple vector sources, or
  graph-derived scores).
  Does NOT handle fulltext-only/keyword-only search — use neo4j-cypher-skill.
---

**Target: Neo4j 5.26 Community, Cypher 5, APOC only, no GDS, no GenAI procedures.** Copied from neo4j-contrib/neo4j-skills (MIT) and adapted. Changes and source commit: `UPSTREAM.md`. Examples use this server's index names (`entity_embedding`, 1024 dimensions, cosine) and its existing `Entity` label, so that they run. `neo4j-modeling-skill` lists a single generic label as an anti-pattern: that is a finding about the graph, not about these examples. Substitute your own names.

## When to Use
- Creating a vector index (`CREATE VECTOR INDEX`) on nodes or relationships
- Running vector similarity / nearest-neighbor search
- Storing embeddings on graph nodes during ingestion
- Choosing similarity function, dimensions or HNSW parameters
- Batch-updating embeddings after a model change
- Hybrid search that combines vector results with fulltext or other ranked sources

## When NOT to Use
- **Fulltext-only / keyword-only search** (FULLTEXT INDEX, `db.index.fulltext.queryNodes`) → `neo4j-cypher-skill`
- **Index admin** (list all indexes, drop range/text/lookup indexes) → `neo4j-cypher-skill`
- **Computing graph embeddings** (FastRP, Node2Vec): GDS is not installed on this server

---

## Pre-flight — What 5.26 Has and Lacks

Check the server version:
```cypher
CALL dbms.components() YIELD versions RETURN versions[0] AS neo4j_version
```

| Need | 5.26 form |
|---|---|
| Nearest neighbors | `CALL db.index.vector.queryNodes(indexName, k, queryVector)` |
| Relationship nearest neighbors | `CALL db.index.vector.queryRelationships(indexName, k, queryVector)` |
| Filter on a property | Post-filter: `WHERE` after the procedure, with `k` larger than the final limit |
| Exact search on a subset | `MATCH` the subset, then `vector.similarity.cosine(a, b)` with `ORDER BY` and `LIMIT` |
| Pairwise similarity | `vector.similarity.cosine(a, b)`, `vector.similarity.euclidean(a, b)` |
| Embed text in Cypher | Not available. Generate the embedding in application code and pass it as a parameter |

Not available on this server, and refused with a syntax or config error: a vector query clause, in-index filter properties (`WITH [...]` on `CREATE VECTOR INDEX`), multi-label vector indexes (`Label1|Label2`), the `vector.quantization.type` option, and the functions `vector()`, `vector_distance()`, `vector_dimension_count()`, `vector_norm()`. Upstream documents all of them for 2026 releases. Do not copy them from upstream pages.

---

## Step 1 — Create Vector Index

Node index (single label). Only the options below exist on 5.26 (index provider `vector-2.0`):
```cypher
// WRITE - not run
CREATE VECTOR INDEX entity_embedding IF NOT EXISTS
FOR (e:Entity) ON (e.embedding)
OPTIONS {
  indexConfig: {
    `vector.dimensions`: 1024,
    `vector.similarity_function`: 'cosine',
    `vector.hnsw.m`: 16,
    `vector.hnsw.ef_construction`: 100
  }
}
```

Relationship index:
```cypher
// WRITE - not run
CREATE VECTOR INDEX rel_embedding IF NOT EXISTS
FOR ()-[r:HAS_CHUNK]-() ON (r.embedding)
OPTIONS { indexConfig: { `vector.dimensions`: 768, `vector.similarity_function`: 'cosine' } }
```

**Index config reference (checked on 5.26.24):**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `vector.dimensions` | INTEGER 1–4096 | none | Required; must match embedding model exactly |
| `vector.similarity_function` | STRING | `'cosine'` | `'cosine'` or `'euclidean'` |
| `vector.quantization.enabled` | BOOLEAN | `true` | Index-level quantization, reduces storage. Changing it needs drop + re-create + re-population |
| `vector.hnsw.m` | INTEGER 1–512 | `16` | HNSW graph connections; higher = better recall, more memory |
| `vector.hnsw.ef_construction` | INTEGER 1–3200 | `100` | Build-time candidates; higher = better recall, slower build |

**Similarity function choice:**

| Use case | Function |
|---|---|
| Normalized embeddings (OpenAI, Cohere, Voyage, Google) | `'cosine'` |
| Unnormalized / raw distance matters | `'euclidean'` |

**Memory** — vector index files live in the OS filesystem cache, not the page cache. Leave RAM for them. Details → [Memory configuration](https://neo4j.com/docs/operations-manual/5/performance/memory-configuration/).

---

## Step 2 — Wait for Index ONLINE

Index builds asynchronously — do NOT query until ONLINE:
```cypher
SHOW VECTOR INDEXES YIELD name, state, populationPercent
WHERE name = 'entity_embedding'
RETURN name, state, populationPercent
```

Poll every 5s until `state = 'ONLINE'` and `populationPercent = 100.0`. If `state = 'FAILED'` → stop, check logs.

Shell poll (cypher-shell):
```bash
until cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
  "SHOW VECTOR INDEXES YIELD name, state WHERE name='entity_embedding' RETURN state" \
  | grep -q ONLINE; do
  sleep 5
done
```

---

## Step 3 — Ingest Embeddings

Batch UNWIND pattern (use for > 100 nodes — never one-node-per-transaction):
```python
from collections.abc import Callable, Sequence

from neo4j import Driver


def store_embeddings(
    driver: Driver,
    records: Sequence[dict[str, str]],
    embed_batch: Callable[[list[str]], list[list[float]]],
    expected_dim: int = 1024,  # must match vector.dimensions
    batch_size: int = 500,
) -> None:
    embeddings = embed_batch([r["text"] for r in records])
    for emb in embeddings:
        assert len(emb) == expected_dim, f"Dim mismatch: {len(emb)} != {expected_dim}"
    rows = [{"id": r["id"], "embedding": emb} for r, emb in zip(records, embeddings)]
    for i in range(0, len(rows), batch_size):
        driver.execute_query(
            "UNWIND $rows AS row MATCH (e:Entity {entity_id: row.id}) SET e.embedding = row.embedding",
            rows=rows[i : i + batch_size],
        )
```

Preferred order: create index → poll ONLINE → ingest embeddings, so that the index is built incrementally.
Creating the index after the embeddings are stored is valid (checked on 5.26.24): the index is `POPULATING` until the build ends, and queries return partial results until it is `ONLINE`.

---

## Step 4 — Run Vector Search

### Procedure call (the only vector query form on 5.26)

```cypher
CALL db.index.vector.queryNodes('entity_embedding', 10, $queryEmbedding)
YIELD node AS e, score
RETURN e.name, score
ORDER BY score DESC
```

The procedure returns at most `k` rows, best first. `score` is a similarity (higher is better).

**Filtering strategy — choose one:**

| Strategy | When to use | Tradeoff |
|---|---|---|
| Post-filter (procedure, then `WHERE`) | Arbitrary Cypher predicates, graph traversal, OR/NOT | Full flexibility; may return fewer than `k` after the filter. Over-fetch, then cut |
| Pre-filter (`MATCH` the subset, then exact similarity) | Small known candidate set; exact nearest neighbor within the subset | Deterministic; scans the whole subset, slow on large sets |

### Post-filter pattern

```cypher
CALL db.index.vector.queryNodes('entity_embedding', 50, $queryEmbedding)
YIELD node AS e, score
WHERE e.entity_type = $entityType    // post-filter: fetch more, then filter
RETURN e.name, score
ORDER BY score DESC LIMIT 10
```

### Pre-filter pattern (exact, on a subset)

```cypher
MATCH (e:Entity)
WHERE e.entity_type = $entityType AND e.embedding IS NOT NULL
WITH e, vector.similarity.cosine(e.embedding, $queryEmbedding) AS score
ORDER BY score DESC LIMIT 10
RETURN e.name, score
```

Procedure hard limits:
- The index name is a string argument (a parameter works there). It must name an existing, ONLINE vector index.
- `k` is a required positive integer.
- The query vector length must equal `vector.dimensions`.

---

## Step 5 — Combine with Graph Traversal (simple cases)

Vector search as entry point, then a graph hop:
```cypher
CALL db.index.vector.queryNodes('entity_embedding', 10, $queryEmbedding)
YIELD node AS e, score
OPTIONAL MATCH (t:Turn)-[:DISCUSSES]->(e)
RETURN e.name, score, count(DISTINCT t) AS turns
ORDER BY score DESC
```

For a bounded hop, put `LIMIT` on the seed set first (`k`), then match one relationship type. Never expand an unbounded variable-length path from a seed set.

---

## Step 6 — Hybrid Search

Use hybrid search when one signal misses useful candidates: semantic vectors miss exact terms, lexical fulltext misses paraphrases, structural graph signals find topology not present in text.
The common pattern is vector + fulltext, but the same approach works for several vector indexes, graph traversal scores, or any two+ ranked/scored sources.
Load [references/hybrid-search.md](references/hybrid-search.md) and apply its query shape.

Rules:
- Run each source independently; rank each by `score DESC, stable_id ASC`.
- Combine by rank, not raw scores; fulltext and vector scores are not comparable.
- Every `UNION ALL` branch returns same columns: matched node + contribution.
- Use `sourceK > finalK`; combine before final limiting.
- Sum contributions per node; order final rows by `wrrf DESC, stable_id ASC`.
- Add more sources with extra `UNION ALL` branches and new `sourceWeights` keys.

---

## Embedding Provider Quick-Reference

| Provider / Model | Dimensions | Similarity | Notes |
|---|---|---|---|
| OpenAI text-embedding-3-small | 1536 | cosine | Default; reducible to 256–1536 via `dimensions=` param |
| OpenAI text-embedding-3-large | 3072 | cosine | Reducible to 256–3072 |
| OpenAI text-embedding-ada-002 | 1536 | cosine | Legacy; prefer 3-small |
| Cohere embed-v3 (English) | 1024 | cosine | Use `input_type='search_document'` at ingest, `'search_query'` at query |
| Voyage voyage-3-large | 1024 | cosine | High quality; needs `voyage-ai` package |
| Google text-embedding-004 | 768 | cosine | Via Vertex AI |
| Ollama nomic-embed-text | 768 | cosine | Local dev/testing |
| Ollama mxbai-embed-large | 1024 | cosine | Local; production-quality |

`vector.dimensions` must exactly match model output — no auto-truncation.

---

## Vector Functions

Ad-hoc similarity (not for kNN search — use the index for that):
```cypher
MATCH (a:Entity), (b:Entity)
WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
WITH a, b LIMIT 1
RETURN vector.similarity.cosine(a.embedding, b.embedding) AS sim,
       vector.similarity.euclidean(a.embedding, b.embedding) AS euclideanSim
```
`vector.similarity.cosine` and `vector.similarity.euclidean` are the only vector functions on 5.26. Both return a score in the 0–1 range, higher is more similar.

---

## Index Management

```cypher
// Show all vector indexes with config
SHOW VECTOR INDEXES YIELD name, state, populationPercent, labelsOrTypes, properties, options
RETURN name, state, populationPercent, labelsOrTypes, properties, options;
```

```cypher
// WRITE - not run
// Drop (node data unchanged — only index structure removed)
DROP INDEX entity_embedding IF EXISTS;

// No ALTER VECTOR INDEX — to change dimensions or similarity function:
// 1. DROP INDEX old_index IF EXISTS
// 2. CREATE VECTOR INDEX new_index ... with new OPTIONS
// 3. Re-generate all embeddings with new model
// 4. Poll until ONLINE
```

---

## Common Errors

| Error | Cause | Fix |
|---|---|---|
| `Index query vector has N dimensions, but indexed vectors have M` | Query vector length ≠ `vector.dimensions` | Fix embed generation or the model name; drop + recreate the index only if the model changed |
| `There is no such vector schema index: <name>` | Wrong index name, or the index is not a vector index | `SHOW VECTOR INDEXES YIELD name` |
| `'numberOfNearestNeighbours' must be positive` | `k` is 0 or negative | Pass `k` ≥ 1 |
| Search returns incomplete results | Index still `POPULATING`, or some stored vectors have the wrong length (silently not indexed) | Poll until `state = 'ONLINE'`; validate dimensions on write |
| `Unknown procedure db.index.vector.queryNodes` | Neo4j < 5.11 | No vector index support below 5.11; upgrade |
| `'vector.quantization.type' is an unrecognized setting` | Option from a 2026 release | Use `vector.quantization.enabled` (BOOLEAN) |
| `Invalid input 'WITH': expected 'OPTIONS'` on `CREATE VECTOR INDEX` | In-index filter properties are a 2026 feature | Post-filter after the procedure |
| Zero results from correct query | Wrong similarity function or all-zeros embedding | Verify with `vector.similarity.cosine()`; check embed call succeeded |
| Score always 1.0 | All-zeros or identical vectors | Embedding generation failed; add dimension assertion before ingest |

---

## Checklist
- [ ] `vector.dimensions` matches embedding model output exactly
- [ ] Vector index `ONLINE` before the first query (create it before a bulk ingest when you can)
- [ ] Similarity function chosen explicitly (`cosine` for normalized, `euclidean` for distance-based)
- [ ] Index polled to `state = 'ONLINE'` before first query
- [ ] Dimension validated on every embedding before ingest
- [ ] Filter by post-filter with `k` larger than the final limit, or by exact pre-filter on a small subset
- [ ] Batch UNWIND pattern used for > 100 nodes
- [ ] If model changes: drop index → recreate with new dimensions → re-generate all embeddings

---

## Cypher-Based Embedding Ingestion — db.create.setNodeVectorProperty

Set vector property via Cypher (e.g. during LOAD CSV or MERGE pipeline):
```cypher
// WRITE - not run
LOAD CSV WITH HEADERS FROM 'https://example.com/data.csv' AS row
MERGE (q:Question {text: row.question})
WITH q, row
CALL db.create.setNodeVectorProperty(q, 'embedding', apoc.convert.fromJsonList(row.question_embedding))
```

`apoc.convert.fromJsonList()` converts `"[0.1,0.2,...]"` STRING to `LIST<FLOAT>`. Python-generated embeddings → UNWIND batch pattern (Step 3). The relationship form is `db.create.setRelationshipVectorProperty`.

---

## Similarity Function — Extended Guidance

**Choose based on training loss function:**
- Check embedding model docs — models trained with cosine loss → use `'cosine'`
- Models trained with L2/Euclidean loss → use `'euclidean'`
- When docs are silent: default to `'cosine'` (all major hosted APIs use it)

**Common pitfall — wrong similarity function:**
```
❌ Created index with 'euclidean' but model outputs L2-normalized vectors
   → scores are mathematically correct but rankings differ from expected cosine order
   → no error thrown; wrong results silently returned
✅ Verify: run vector.similarity.cosine(a.embedding, b.embedding) manually on known
   similar pairs — score should be > 0.9 for near-duplicate text
```

**Sanity check query after index creation:**
```cypher
MATCH (e:Entity) WHERE e.embedding IS NOT NULL WITH e LIMIT 2
WITH collect(e) AS nodes
RETURN vector.similarity.cosine(nodes[0].embedding, nodes[1].embedding) AS cosine_check,
       vector.similarity.euclidean(nodes[0].embedding, nodes[1].embedding) AS euclidean_check
```
If both return `null` → embeddings not set. If cosine returns `1.0` → identical vectors (embed call failed).

---

## Gotchas — Extended

| Gotcha | Detail | Fix |
|---|---|---|
| Index not ONLINE at ingest time | Inserting nodes before index exists is valid — index auto-populates. But querying during `POPULATING` returns partial results | Always poll `state = 'ONLINE'` before first query |
| Wrong dimensions — silent omission | A stored vector whose length ≠ `vector.dimensions`, or that is not a list of floats, is accepted on write and left out of the index (checked on 5.26.24). It never appears in results and raises no error | Assert `len(emb) == expected_dim` before every `SET e.embedding`. Compare `count(e.embedding)` with the number of indexed nodes when recall looks low |
| Different models at ingest vs query | No error; cosine scores ~0.3–0.5 for clearly similar text | Use same model string/version for both; store model name as node metadata |
| Large single-transaction ingest | One transaction for 10k nodes → OOM or timeout | Use `UNWIND $rows ... CALL IN TRANSACTIONS OF 500 ROWS` or Python batch loop |
| Chunk overlap not set | Adjacent chunks with no overlap → context at boundaries lost → poor recall for cross-paragraph queries | Set `chunk_overlap` ≥ 10% of `chunk_size` |

---

## References
Load on demand:
- [Hybrid search](references/hybrid-search.md) - combine semantic, lexical, structural, or other ranked sources with WRRF/RRF
- [Vector index docs](https://neo4j.com/docs/cypher-manual/5/indexes/semantic-indexes/vector-indexes/)
- [Vector functions docs](https://neo4j.com/docs/cypher-manual/5/functions/vector/)
- [db.create.setNodeVectorProperty docs](https://neo4j.com/docs/operations-manual/5/reference/procedures/)
