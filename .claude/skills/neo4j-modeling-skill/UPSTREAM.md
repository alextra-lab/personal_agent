# Upstream provenance — neo4j-modeling-skill

| | |
|---|---|
| Source | https://github.com/neo4j-contrib/neo4j-skills, folder `neo4j-modeling-skill/` |
| Commit | `bb30e1f031af84a42fd2fdfd502ad52570e427cf` (2026-10-09) |
| Upstream skill version | 1.0.7 |
| Licence | MIT, Copyright (c) 2026 Neo4j Contrib. The notice is the file `LICENSE` in this folder. |
| Copied and adapted | 2026-10-12, ticket FRE-1586 |
| Target | Neo4j 5.26 Community (checked on 5.26.24), Cypher 5, APOC only, no GDS, no GenAI procedures |

This is a developer reference for the seats that review or design the graph model. It is not part of the Seshat agent catalog.

## What changed from upstream

**Not copied**
- `README.md`: an install note for the upstream repository.

**Frontmatter and first line**
- `description` rewritten for the pin and the out-of-scope lines for skills that are not in this copy removed. `version` and `allowed-tools` removed. The last one pre-approved `Bash` and `WebFetch` for any seat that loads the skill, which a copied file must not do.
- A pin line is the first line of the body.

**Removed or replaced**
- Default 11 (graph-type DDL for 2026 releases) became a statement that 5.26 declares the model with individual constraint and index statements.
- The MCP tool map (`get-schema`, `read-cypher`, `write-cypher`) became one sentence: run inspection with `cypher-shell --access-mode read`. No Neo4j MCP server is configured.
- The link-outs to Spring Data, GraphQL and import skills became one line of "out of scope".
- The fulltext comment no longer names the 2026 index-query clause.
- The bucket pattern was split into a pattern sketch (text fence) and a runnable query.

**Marking**
- The DDL block has `// WRITE - not run` as its first line. Three statements in it are Enterprise-only and are refused on Community ("requires Neo4j Enterprise Edition"). The block comments already say Enterprise.

## Checks that were run

Every unmarked `cypher` block ran in read mode (`cypher-shell --access-mode read`). The DDL block ran on the test container and was dropped afterwards: the Community statements were accepted, the three Enterprise statements were refused. The run output is on the ticket.
