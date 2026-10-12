# Upstream provenance — neo4j-cypher-skill

| | |
|---|---|
| Source | https://github.com/neo4j-contrib/neo4j-skills, folder `neo4j-cypher-skill/` |
| Commit | `bb30e1f031af84a42fd2fdfd502ad52570e427cf` (2026-10-09) |
| Upstream skill version | 1.0.27 |
| Licence | MIT, Copyright (c) 2026 Neo4j Contrib. The notice is the file `LICENSE` in this folder. |
| Copied and adapted | 2026-10-12, ticket FRE-1586 |
| Target | Neo4j 5.26 Community (checked on 5.26.24), Cypher 5, APOC only, no GDS, no GenAI procedures |

This is a developer reference for the seats that write Cypher. It is not part of the Seshat agent catalog.

## What changed from upstream

**Not copied**
- `README.md`: an install note for the upstream repository.
- `scripts/` (three Python helpers): they need the `neo4j` driver and a password in the environment. `references/schema-guardrail.md` lost the schema-file workflow that used them. The four schema queries in `SKILL.md` do the same job.
- `references/graph-type.md`: GRAPH TYPE DDL does not exist on 5.26 (checked).

**Frontmatter and first line**
- `description` rewritten for the pin. `version` and `compatibility` removed. A pin line is the first line of the body.

**Removed because the server refuses it** (each was run on 5.26.24 and failed)
- The version prefix of the 2025 dialect on every example.
- The index-query clause, the GQL variable-binding clause, `FILTER`, `FOR`, `NEXT`.
- Match modes, the `ACYCLIC` path mode, `allReduce`, the conditional-`CALL` form, `DISJOINT BY`, the `GROUP BY` subclause, map comprehension, string interpolation, the UUID type, `cardinality()`, `coll.sort()`, `string.*` functions, GQL function aliases.
- `ON ERROR RETRY`.
- `USING PERIODIC COMMIT` is refused ("no longer supported"), not merely deprecated as upstream says.

**Replaced**
- The "Version Gates" table became "Not on 5.26", with the server's own error and a fallback for each row.
- Spans of `SKILL.md` and the references that used a removed form now use the Cypher 5 form (for example the post-filter for a path with no repeated node).
- Doc URLs: manual path `/25/` became `/5/`. Each changed URL was fetched and returned HTTP 200. URLs that this copy did not change were not fetched.

**Corrected where 5.26 behaves differently from the upstream text**
- `WHERE n:$($label)` is refused. Dynamic labels work in `MATCH`, `CREATE`, `MERGE`, `SET`, `REMOVE`.
- `toInteger('abc')` and `toInteger(null)` return `null`. They do not throw.
- `-[:A|B]*->` is invalid. `-[:A|B*]->` is the form.
- `CALL ... IN TRANSACTIONS ... REPORT STATUS` must end in `RETURN`.
- `runtime=parallel` is accepted and falls back to `SLOTTED` on Community.
- `SHOW INDEXES` has no `indexConfig`, `indexSize`, `lastWrite` or `writeCount` column. The column is `options`.
- Community refuses non-unique constraints with "requires Neo4j Enterprise Edition".
- `apoc.meta.stats`, `apoc.export.json.query` and the trigger procedures have different signatures. `apoc.refactor.rename.relationshipProperty` is `apoc.refactor.rename.typeProperty`. `apoc.nodes.get` raises an error for an id that does not exist.
- The `standard-no-stop-words` analyzer is the default and keeps stop words. Upstream says that it removes them.
- The `OPTIONAL CALL` example and the spatial bounding-box example were made type-correct (they passed a string where a node or a point is needed).

**Marking**
- A fenced `cypher` block that writes (DML, DDL, file export) has `// WRITE - not run` as its first line.
- Expression fragments that are not statements moved from a `cypher` fence to a `text` fence.
- Statements in one block are separated by `;`.

## Checks that were run

Every unmarked `cypher` block ran in read mode (`cypher-shell --access-mode read`). Every marked block was planned with `EXPLAIN` on the test container. DDL ran on the test container only and was dropped afterwards. The run output is on the ticket.
