# Upstream provenance — neo4j-vector-index-skill

| | |
|---|---|
| Source | https://github.com/neo4j-contrib/neo4j-skills, folder `neo4j-vector-index-skill/` |
| Commit | `bb30e1f031af84a42fd2fdfd502ad52570e427cf` (2026-10-09) |
| Upstream skill version | 1.0.16 |
| Licence | MIT, Copyright (c) 2026 Neo4j Contrib. The notice is the file `LICENSE` in this folder. |
| Copied and adapted | 2026-10-12, ticket FRE-1586 |
| Target | Neo4j 5.26 Community (checked on 5.26.24), Cypher 5, APOC only, no GDS, no GenAI procedures |

This is a developer reference for the seats that build recall. It is not part of the Seshat agent catalog. It includes `references/hybrid-search.md`.

## What changed from upstream

**Not copied**
- `README.md`: an install note for the upstream repository.

**Frontmatter and first line**
- `description` rewritten for the pin. `version`, `compatibility` and `allowed-tools` removed. The last one pre-approved `Bash` and `WebFetch` for any seat that loads the skill, which a copied file must not do.
- A pin line is the first line of the body.

**Removed because the server refuses it** (each was run on 5.26.24)
- The version prefix of the 2025 dialect on every example.
- The vector query clause and the in-index filter properties (`WITH [...]`).
- Multi-label vector indexes, the `vector.quantization.type` option and its provider table.
- `vector()`, `vector_distance()`, `vector_dimension_count()`, `vector_norm()`.
- `ai.text.embed()` and the `genai.*` functions. No GenAI procedure is installed. Embeddings come from application code.
- The delegation to the GraphRAG, GDS and document-import skills, which are not part of this copy.

**Replaced**
- The query form is `db.index.vector.queryNodes`. Filtering is a post-filter with `k` larger than the final limit, or an exact pre-filter with `vector.similarity.cosine`.
- The index config table lists only the options that 5.26.24 accepts, with their checked ranges and defaults.
- Example names use this server's index (`entity_embedding`, label `Entity`, 1024 dimensions, key `entity_id`) so that the examples run.
- `references/hybrid-search.md` is rewritten as a Cypher 5 query: a `CALL { ... UNION ALL ... }` of two procedure calls, with parameters used directly. The fulltext branch uses the existing multi-label index.
- Doc URLs: manual path `/25/` and `/current/` became `/5/`. Each changed URL was fetched and returned HTTP 200. URLs that this copy did not change were not fetched.

**Corrected where 5.26 behaves differently from the upstream text**
- A stored vector with the wrong length, or one that is not a list of floats, is accepted on write and silently left out of the index. Upstream says that it raises `IllegalArgumentException` at query time. It does not.
- Creating the index after the data is stored is valid: the index populates from the existing data. Upstream says never to do it.
- The wrong query-vector length message is "Index query vector has N dimensions, but indexed vectors have M".
- `SHOW VECTOR INDEXES` has no `indexConfig` column. The column is `options`.

**Marking**
- A fenced `cypher` block that writes (DDL, DML) has `// WRITE - not run` as its first line.

## Checks that were run

Every unmarked `cypher` block ran in read mode (`cypher-shell --access-mode read`) against the real index. DDL forms were run on the test container on throwaway labels and dropped. The run output is on the ticket.
