# Neo4j Agent Skills — do any of them improve memory recall or knowledge-graph search? (explore seat, 2026-10-11)

Commission: master, for the owner, 2026-10-11. The owner asked: "We should see if any of these skills
would help improve memory recall or knowledge graph searching." Source:
https://neo4j.com/labs/genai-ecosystem/agent-skills/neo4j-skills/

Read-only. No model run, no live turn: model testing is stopped until the FRE-1573 gate (ADR-0156 D10).
Every Neo4j query ran with `cypher-shell --access-mode read` on the production container. Elasticsearch
was read with `_count` and `_search` only. No ticket was filed and no ticket state was changed. The
commission named no ticket, so this document has no FRE number in its file name.

**Redaction.** The repository is public. This document quotes counts, schema names and index names only.
It quotes no entity name, no user id and no conversation text from the graph.

---

## Verdict

**No Neo4j skill improves recall by being installed.** All 29 skills are documentation for coding
agents (Claude Code, Cursor, Codex) that build Neo4j applications. None is a retrieval capability, and
none gives Seshat's model a new way to search. A recall gain can come only from a code change that a
build seat makes. Three skills contain patterns that are useful input for that work.

- **Skip, for Seshat's agent catalog: all 29.** None passes our ADR-0156 validator (N2). The Cypher and
  vector skills default to Cypher 25, which our server refuses (N3). In 30 days, Seshat's model ran no
  Cypher query (N6), so a Cypher skill in the agent catalog has no measured use.
- **Adapt, as developer references for the seats that build recall: 3.** `neo4j-cypher-skill`,
  `neo4j-vector-index-skill` (with its `references/hybrid-search.md`) and `neo4j-modeling-skill`. Each
  needs a pin to Neo4j 5.26 Community and Cypher 5.
- **Use as design input only: 2.** `neo4j-graphrag-skill` (its vector-seed-plus-hop retriever) for the
  typed-traversal arm that FRE-1546 proposed, and `neo4j-gds-skill` for the offline study stack of
  ADR-0114.
- **Skip with a specific conflict: 2.** `neo4j-agent-memory-skill` replaces our memory system, needs
  write access and an MCP server. `neo4j-mcp-skill` adds a Tier 3 MCP copy of the read-only path that
  `neo4j-direct` already gives. Both conflict with ADR-0156 D7.3 and ADR-0028.

No skill addresses the dominant recall failure: the system cannot say "nothing relevant exists"
(FRE-1118). The nearest feature, `response_fallback` in `neo4j-graphrag`, fires only when retrieval
returns nothing. Our failure is a non-empty set of irrelevant items.

---

## Findings

### N1 — The repository holds 29 skills under the MIT license; the page lists 22

**Verdict:** POSITIVE

**Query:** `git clone --depth 1 https://github.com/neo4j-contrib/neo4j-skills.git`, then
`git log -1`, `head -5 LICENSE`, and a list of each folder. The page was read with WebFetch.

**Output:**

```
bb30e1f031af84a42fd2fdfd502ad52570e427cf 2026-10-09 21:40:42 +0200
MIT License
Copyright (c) 2026 Neo4j Contrib
```

29 folders hold a `SKILL.md`. The page lists 22. The 7 that are only in the repository:
`neo4j-aura-agent-skill`, `neo4j-kafka-skill`, `neo4j-nvl-skill`, `neo4j-query-tuning-skill`,
`neo4j-security-skill`, `neo4j-snowflake-graph-analytics-skill`, `neo4j-spark-skill`.

The page puts every skill under the Neo4j Labs terms: community-supported, with no product support.
The repository file is MIT, so a copy is allowed if the notice is kept. The `neo4j-agent-memory`
package that one skill documents is Apache-2.0 (its `SKILL.md` table).

### N2 — The skills use the Agent Skills layout, but 0 of 29 pass our ADR-0156 validator

**Verdict:** POSITIVE (a count of failures)

**Query:** our own validator, `personal_agent.orchestrator.skills.validate_skill(folder, "agent")`, at
`origin/main` `812c82e0`, run over each of the 29 folders.

**Output:**

```
skills 29 pass 0 rule_fail_counts {'body_tokens': 5, 'evals': 29, 'frontmatter': 29,
  'sections': 29, 'body_lines': 2, 'file_refs': 4}
neo4j-cypher-skill frontmatter fields not allowed: ['compatibility', 'version']
neo4j-cypher-skill body_tokens body has about 5938 tokens
neo4j-vector-index-skill frontmatter fields not allowed: ['allowed-tools', 'compatibility', 'version']
neo4j-vector-index-skill file_refs '../neo4j-document-import-skill/SKILL.md' is not a file one level below neo4j-vector-index-skill/
```

Each skill is a folder with `SKILL.md` and, for 24 of them, `references/`. Four have `scripts/`. This is
the Agent Skills layout. Two kinds of failure are not ours to fix in their files:

- `compatibility` and `allowed-tools` are optional fields of the Agent Skills specification. ADR-0156
  D1.2 is stricter: it keeps only `name`, `description` and `metadata`. `version` and `status` are in
  neither.
- No skill has a success section, a recovery section or an `evals.yaml` (ADR-0156 D5.1, D5.2). The
  repository has one shared `evals/dataset.json` in another format.

A skill that enters `docs/skills/` therefore needs a rewrite, not a copy.

### N3 — Production Neo4j is 5.26.24 Community with APOC only, and it refuses Cypher 25

**Verdict:** POSITIVE for the version, the plugin list and the refusal. NEGATIVE for GDS and GenAI
procedures (arms below).

**Query:** container `cloud-sim-neo4j`, `cypher-shell --access-mode read`:

```
CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition
RETURN apoc.version() AS apoc
SHOW PROCEDURES YIELD name WHERE name STARTS WITH 'gds.' OR name STARTS WITH 'genai.' OR name STARTS WITH 'ai.' RETURN count(*)
SHOW PROCEDURES YIELD name WHERE name STARTS WITH 'db.index.' RETURN collect(name)
CYPHER 25 RETURN 1 AS ok
```

and `docker inspect` for `NEO4J_PLUGINS`, and `ls /var/lib/neo4j/plugins`.

**Output:**

```
"Neo4j Kernel", ["5.26.24"], "community"
apoc "5.26.24"
gds_genai_ai_procs 0
idx_procs ["db.index.fulltext.awaitEventuallyConsistentIndexRefresh", "db.index.fulltext.listAvailableAnalyzers",
  "db.index.fulltext.queryNodes", "db.index.fulltext.queryRelationships", "db.index.vector.createNodeIndex",
  "db.index.vector.queryNodes", "db.index.vector.queryRelationships"]
25 is not a valid option for cypher version. Valid options are: 5
NEO4J_PLUGINS=["apoc"]
/var/lib/neo4j/plugins: README.txt apoc.jar
```

**Arms for "no GDS or GenAI procedure in production":**

- *Arm 1 (1b, a live producer):* the GDS names are real. `scripts/study/consolidator.py:317-331` calls
  `gds.*`, and the study stack loads `NEO4J_PLUGINS: '["graph-data-science"]'`
  (`docker-compose.study.yml:30`). The GenAI names come from `neo4j-genai-plugin-skill`
  (`ai.text.embed()`, `genai.vector.encode()`). The plugin folder of the queried container holds
  `apoc.jar` only, which agrees.
- *Arm 2:* the same `SHOW PROCEDURES` with only the prefix changed to `db.index.` returned 7 names.
- *Arm 3:* scope is the container `cloud-sim-neo4j` on 2026-10-11. The eval and test containers
  (`cloud-sim-neo4j-eval`, `build-neo4j-test-1`) also set `NEO4J_PLUGINS=["apoc"]`; their procedures
  were not listed.

**What this means for the skills.** `neo4j-cypher-skill` states `compatibility: Neo4j >= 2025.01 (safe
baseline); Cypher 25` and tells the model to "Default to 2025.01 safe set" when the version is unknown.
`neo4j-cypher-skill/SKILL.md` holds `CYPHER 25` 17 times and `neo4j-vector-index-skill/SKILL.md` 14
times. On our server each of those examples fails. The `SEARCH` clause needs 2026.01. The skill has a
version table with fallbacks for Neo4j 5 (`SKILL.md:342-349`), so it can be pinned, but its default is
wrong for us.

### N4 — Live recall runs the dense, multi-query and lexical arms; the structural arm is off

**Verdict:** POSITIVE

**Query:** the running gateway process, `docker exec cloud-sim-seshat-gateway`, reading
`personal_agent.config.settings`.

**Output:**

```
multipath_recall_enabled True
lexical_arm_enabled True
multiquery_arm_enabled True
structural_arm_enabled False
relevance_bounded_recall_enabled True
proactive_memory_enabled True
recall_similarity_floor 0.6
multipath_rrf_k 60
multipath_arm_top_k 50
dedup_similarity_threshold 0.92
embedding_dimensions 1024
```

This agrees with FRE-1546 F2 (2026-10-03). The recall code under `src/personal_agent/memory/` and
`request_gateway/context.py` did not change between that study and `812c82e0`. Only
`tools/memory_search.py` and `tools/personal_history.py` changed (ADR-0156 T3 descriptions).

Our pipeline already does what the hybrid-search reference of `neo4j-vector-index-skill` teaches:
run each source on its own, fuse by rank with RRF, never compare raw scores, and take more per source
than in the final set (`_multipath_fused_recall`, `memory/service.py:5380`; arm top-k 50, final cap 25).
The skill adds two points that we do not have: a weight per source, and "a graph-derived candidate score
converted to source rank" as one more branch.

### N5 — The graph holds the anti-patterns that `neo4j-modeling-skill` names

**Verdict:** POSITIVE for each count. NEGATIVE for "exact duplicate names" (arms below).

**Query:** `cloud-sim-neo4j`, read mode:

```
CALL db.labels() ... count per label
CALL db.relationshipTypes() ... count per type
SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties
SHOW INDEXES YIELD name, type, labelsOrTypes, properties, state, options
MATCH (e:Entity) WITH e.name AS k, count(*) AS n WHERE n > 1 RETURN count(k), sum(n)
MATCH (e:Entity) WITH toLower(trim(e.name)) AS k, count(*) AS n WHERE n > 1 RETURN count(k), sum(n)
MATCH (e:Entity) RETURN count(e), count(e.embedding), count(e.description)
MATCH (n) WITH labels(n)[0] AS l, count{ (n)--() } AS d RETURN l, max(d), percentileCont(d,0.5)
```

**Output:**

```
Entity 9743 · Turn 2729 · Claim 440 · Session 326 · EntityDescriptionVersion 271 · Source 56 · Person 34
DISCUSSES 26371 · RELATED_TO 6414 · USES 3392 · PARTICIPATED_IN 2718 · PART_OF 1138 · ... (18 types)
(:Entity)-[]->(:Entity) edges: 12103, of which RELATED_TO 6414 (53%)
constraints: person_user_id_unique (Person.user_id), source_id_unique (Source.source_id)
indexes on Entity: entity_class_index (RANGE, class), entity_embedding (VECTOR, 1024, COSINE),
  turn_entity_fulltext (FULLTEXT, Turn.user_message + Entity.name)
exact_dup_names 0
case/space variant keys 284, covering 578 nodes
entities 9743, with_embedding 8555, with_description 8555
Entity max_degree 3234, median 3
```

Against the anti-pattern table of `neo4j-modeling-skill/SKILL.md:280-296`:

| Anti-pattern in the skill | Measured in our graph |
|---|---|
| "Generic rel types `:RELATED_TO`" | 6,414 of 12,103 entity-to-entity edges (53%). FRE-1546 F12 measured the same share |
| "MERGE without uniqueness constraint — Duplicate nodes silently created" | Every entity write is `MERGE (e:Entity {name: $name})` (`service.py:1396`, `create_entity`). `Entity.name` has no constraint and no RANGE index |
| "Generic labels `:Entity`" | All 9,743 entities carry one label. The type is a property (`entity_type`, `class`) |
| Supernode detection | One `Entity` node has degree 3,234. The median is 3 |

**Arms for "0 exact duplicate names":**

- *Arm 1 (1a):* the property `Entity.name` exists and is populated in this store. The lower-cased form of
  the same query returned 284 keys over 578 nodes, all built from `e.name`.
- *Arm 2:* the same label, the same aggregation, with only the key changed from `e.name` to
  `toLower(trim(e.name))`, returned 284.
- *Arm 3:* scope is the production graph on 2026-10-11. The claim is that no two nodes have a
  byte-equal name today. It is not a claim that the write path prevents it: no constraint enforces it.

**An embedding gap.** 1,188 entities (12.2%) have neither an embedding nor a description. The dense arm
and the dedup check (`memory/dedup.py`, a vector top-5) cannot see them. Only the lexical arm, on the
name, can. By `first_seen`: 2026-04 232, 2026-05 775, 2026-06 86, 2026-07 88, 2026-08 7, none later.
1,187 of them have a `DISCUSSES` edge from a turn. FRE-1115 (Done) covers this stock. On 2026-08-04 its
fix stopped the write path from creating entities with no description. Its repair script merged 203
duplicates and kept 1,203 distinct entities with no description on purpose. Its closing comment assigns
their recall question to FRE-1114 (Done). No open ticket backfills their descriptions. The 1,188 agrees
with that kept stock, and none was created after 2026-08. This is not a new defect (master, 2026-10-11).

### N6 — Seshat's model ran no Cypher query in 30 days; the memory tools ran 149 times

**Verdict:** NEGATIVE for Cypher use, POSITIVE for the memory-tool count.

**Query:** `agent-logs-*`, `event_type: tool_call_started`, `@timestamp >= now-30d`.

- Memory tools: `_count` with `terms tool_name: [search_memory, recall_personal_history]`.
- Cypher: `_search` (size 2000) with `terms tool_name: [bash, run_python]`. The `arguments` field was
  matched client-side by regular expression, because a `query_string` on `arguments` returned 0 for the
  control pattern too (method appendix).

**Output:**

```
all tool_call_started 30d: 2220
search_memory + recall_personal_history: 149
bash/run_python rows: 322
curl 70 · cypher 0 · "MATCH " 0 · neo4j 3 · 7474 3 · bolt 1
```

The 3 `neo4j` rows are reachability probes, not queries: two `run_python` socket checks (2026-10-09,
2026-10-10, network on) and one `curl -fsS http://neo4j:7474/` (2026-10-10).

**Arms:**

- *Arm 1 (1b):* the producer is `execute_tool` (`tools/executor.py:804-805`, the `TOOL_CALL_STARTED` log), and it writes the full
  `arguments` of each call. A sibling from the same path returned 322 `bash` and `run_python` rows in
  the window. The text that a Cypher call would carry is known: `neo4j-direct` runs
  `python3 docs/skills/neo4j-direct/scripts/cypher.py "MATCH ..."` since 2026-10-11, and before that a
  driver script or `curl` to `/db/neo4j/tx/commit`. The `neo4j`, `7474` and `bolt` matches show that
  this field does carry Neo4j text when the model uses it.
- *Arm 2:* the same rows with only the pattern changed to `curl` gave 70.
- *Arm 3:* scope is `agent-logs-*`, tools `bash` and `run_python`, the 30 days before 2026-10-11, all
  sessions. ES counts are provisional (FRE-1051). The new `neo4j-direct` script is one day old, so this
  window measures the old skill.

**What this means.** A Cypher-writing skill in Seshat's agent catalog would serve a path that the model
does not use. Recall happens in Stage 6 context assembly and in `search_memory`, which are code, not
model-written Cypher.

---

## Mapping — each skill against our recall and graph paths

Our paths, from the code at `812c82e0` and the records:

- **Stage 6** (`request_gateway/context.py:566`): proactive recall (`suggest_proactive_raw`), entity
  resolution through the full-text index, then `query_memory`. A `MEMORY_RECALL` intent goes to
  `query_memory_broad`.
- **Fused recall** (`memory/service.py:5380`): dense arm on `entity_embedding`, multi-query paraphrase
  arm, lexical arm on `turn_entity_fulltext`, RRF k=60, reranker (Voyage), admission gates (ADR-0148 D4).
- **Tools:** `search_memory`, `recall_personal_history` (own Cypher, `tools/personal_history.py`).
- **Direct graph reads:** the `neo4j-direct` skill. Its script opens a `READ_ACCESS` session and an
  `execute_read` transaction, and it allows only six schema procedures in a `CALL`.
- **Known failures:** absence is not expressible (FRE-1118); no score gives a clean floor (ADR-0103,
  FRE-694); recall does not walk typed edges (FRE-1546); entity identity (FRE-1126, FRE-1546 F10);
  provenance leaks across sessions (FRE-1338).

| Skill | What it gives | Our failure it touches | Verdict | Change needed, and conflicts |
|---|---|---|---|---|
| `neo4j-cypher-skill` | Cypher authoring, a version table, a schema-first protocol, `references/indexes.md` | None directly. Fewer wrong queries from the seats that build recall | **Adapt** (developer use) | Pin to 5.26 Community and Cypher 5 (N3). Body is 5,938 tokens, over D1. For the agent catalog: skip (N6) |
| `neo4j-vector-index-skill` + `references/hybrid-search.md` | Vector index use, post-filter on 5.x, weighted RRF, a graph-derived source as an RRF branch | FRE-1546 P2 (typed-traversal arm) as one more RRF branch | **Adapt** (developer use) | We already fuse by rank (N4). New: per-source weights, a graph branch. `SEARCH` and `CYPHER 25` examples fail here (N3) |
| `neo4j-modeling-skill` | Anti-pattern table, supernode check, constraint guidance | FRE-1126, FRE-1546 P3/P5 (fragmented entities, vague edges) | **Adapt** (developer use, as a review checklist) | Each item is measured in N5. A constraint on `Entity.name` is a schema decision, not a skill install |
| `neo4j-graphrag-skill` | `VectorCypherRetriever` and `HybridCypherRetriever`: a vector seed, then a `retrieval_query` hop | FRE-1546 P2 | **Design input only** | Adopting the `neo4j-graphrag` library replaces our fused pipeline and its gates. `response_fallback` fires only on an empty result, so it does not touch FRE-1118 |
| `neo4j-gds-skill` | Node Similarity, KNN, WCC, Leiden, FastRP | FRE-1126 candidate pairs; ADR-0114 arms E/F (FRE-854, FRE-855) | **Design input only**, study stack | GDS is not in production (N3). ADR-0114 D5 keeps GDS offline, never in the recall path |
| `neo4j-document-import-skill` | `SimpleKGPipeline`, entity resolvers (exact, Levenshtein, spaCy cosine) | FRE-1126 | **Skip** | The resolvers measure string or topical similarity. FRE-1126's defect is that similarity is not identity. Our FRE-1115 rule already covers exact and accent-folded names |
| `neo4j-agent-memory-skill` | A reference for the `neo4j-agent-memory` package (Apache-2.0, "Experimental"), with sections titled "Positioning Language", "Do Say", "Don't Say" | None. It replaces the memory system | **Skip** | Needs an MCP server and write access: ADR-0156 D7.3, ADR-0028, FRE-1338. Its dedup ("embedding similarity (threshold ~0.95)") is the method that FRE-1126 found wrong at 0.92 |
| `neo4j-mcp-skill` | The official Neo4j MCP server, `read-cypher`, `NEO4J_READ_ONLY` | None | **Skip** | A Tier 3 copy of `neo4j-direct`. ADR-0156 D7.3 (no new MCP server) and D7.5 (MCP copies of native paths removed) |
| `neo4j-genai-plugin-skill` | `ai.text.*` in Cypher | None | **Skip** | No GenAI procedure is installed (N3). `ai.text.*` needs Cypher 25. It sends text from the database to a provider, which the substrate profile does not route |
| `neo4j-query-tuning-skill` | Plan reading, operators, hints | Latency only | **Skip** for now | No recall latency defect is open. Revisit if one is |
| The other 19 (drivers for other languages, Aura, Snowflake, Spark, Kafka, GraphQL, Spring, NVL, import, migration, CLI, security, getting-started, provisioning, Aura agents, Aura analytics) | Application building and operations | None | **Skip** | Not about retrieval |

---

## Linear tickets on recall and graph search

The owner asked for this list during the study. Searches on 2026-10-11, team FrenchForest: "graph
traversal" (2 pages), "memory recall", "knowledge graph", "dedup", "GDS", "entity resolution". Done
and Canceled tickets are left out, except the studies that this document builds on.

**Open, and touched by a skill above:**

| Ticket | State | Subject | Skill input |
|---|---|---|---|
| FRE-1126 | Approved | Entity dedup conflates distinct entities at 0.92 cosine | modeling (constraint), gds (offline candidates) |
| FRE-855 | Approved | ADR-0114 arm F — SUBSUMES hierarchy traversal (GDS Leiden) | gds |
| FRE-854 | Approved | ADR-0114 arm E — open categories and consolidation | gds |
| FRE-856 | Approved | ADR-0114 arm G — salience and novelty reranking | — |
| FRE-843 | Approved | ADR-0114 v0 synthesis and verdict | — |
| FRE-1121 | Approved | `recall_personal_history` cannot match a paraphrase | vector-index (it is a vector question) |
| FRE-1338 | Approved | Extraction leaks one session's sources into the next | conflicts with agent-memory (write access) |

**Open, and no skill touches it:**

- Absence and floors: FRE-1118 (Backlog, "cannot express nothing relevant exists"), FRE-1122 (Awaiting
  Deploy, absence probe fixture), FRE-1140 (Approved, window-amnesia probes), FRE-1152 (Backlog).
- Recall paths: FRE-750 (Backlog, proactive full core convergence), FRE-751 (Approved, "0 = all
  history" coerced to 90 days), FRE-1120 (Approved, embedder failure fails open into empty recall),
  FRE-465 (Approved, cold-tier session history), FRE-1134 (Approved, conversation window ADR).
- Graph data: FRE-381 (Approved, decouple Turn creation from extraction), FRE-674 (Approved, PRIVATE
  visibility), FRE-761 (Backlog, extraction benchmark phase 2), FRE-764 (Backlog, DSPy extraction),
  FRE-1088 (Backlog, is the entity payload load-bearing), FRE-805 and FRE-322 (Approved, stale
  `:Conversation` label), FRE-633 (Backlog, freshness audit).
- Umbrellas and audits: FRE-435 (Backlog, the recall research initiative), FRE-837 (Backlog, ADR-0114),
  FRE-1131 (Approved, context alignment audit), FRE-1139 (Approved, ADR status headers for 0104/0100).
- Eval isolation: FRE-1372 (Awaiting Deploy), FRE-1506 (Approved).

**Done, and the base for this document:** FRE-1546 (the typed-traversal study, 7 proposals),
FRE-707 and FRE-866 (the structural arm, wired, flag off), FRE-723 and FRE-724 (multi-path recall,
live), FRE-1479 and FRE-1545 (reranker admission gates), FRE-842 (ADR-0114 offline consolidator with
GDS Node Similarity and KNN).

---

## Proposals

1. **Install no Neo4j skill in Seshat's agent catalog.** None passes ADR-0156 D1 (N2), the Cypher and
   vector skills default to a Cypher version that our server refuses (N3), and the model ran no Cypher
   query in 30 days (N6). If `neo4j-direct` needs more Cypher guidance, add a short
   `references/cypher-5.md` to that skill, written for 5.26 and our labels and indexes.

2. **Adapt three skills as developer references for the seats that build recall.** These are
   `neo4j-cypher-skill`, `neo4j-vector-index-skill` with `references/hybrid-search.md`, and
   `neo4j-modeling-skill`. They belong with the Claude Code skills of the build and ADR seats, not in
   `docs/skills/`, so ADR-0156 D1 does not govern them. Each needs a first line that pins Neo4j 5.26
   Community, Cypher 5, APOC only, no GDS. The MIT notice stays. The owner decides whether developer
   skills from a third party are wanted. Master routes it.

3. **Give the typed-traversal design (FRE-1546 proposal 2) two patterns, and no library.** First: a
   graph-derived source as one more RRF branch with its own weight (`hybrid-search.md`, "Add More
   Sources"). Second: a vector seed followed by a bounded hop (`VectorCypherRetriever`'s
   `retrieval_query`, and `neo4j-vector-index-skill` Step 5). Both fit `_multipath_fused_recall` as it
   is. Adopting `neo4j-graphrag` is not proposed: it replaces the pipeline and its admission gates.

4. **Use the modeling anti-pattern table as a review of the graph, inside the existing tickets.** N5
   measures four items: `RELATED_TO` at 53%, `MERGE` on `Entity.name` with no constraint and no index,
   one generic `:Entity` label, and one entity with degree 3,234 against a median of 3. Route these to
   FRE-1126 and to FRE-1546 proposals 3 and 5, as comments, with no new ticket (approved by master on
   2026-10-11, comments posted). The 1,188 entities with no embedding are the stock that FRE-1115 kept
   on purpose (N5), so no action is proposed for them. A uniqueness constraint on `Entity.name` changes
   the write path, so it is a design decision for the owner.

5. **Keep GDS in the study stack.** `neo4j-gds-skill` is a reference for FRE-854, FRE-855 and an
   offline candidate list for FRE-1126 (Node Similarity, WCC). ADR-0114 D5 already keeps GDS out of the
   recall path. A production install is not proposed.

6. **Measure later, after the FRE-1573 gate, on the eval stack only (FRE-375).**
   - For proposal 3, a flag-gated typed-traversal arm against today's fused recall. Use the FRE-489
     probe set, the relationship questions of FRE-1546 F9, and the FRE-1122 absence fixture. Pre-register
     these: false-negative rate (the ADR-0087 headline), recall@5, the rate at which the answer asserts a
     subject that is not in memory (must not rise), and recall tokens per turn.
   - For proposal 2, no model run of Seshat is needed. Take 10 Cypher tasks on our schema. A build seat
     writes each query with and without the pinned skills. Count the queries that pass `EXPLAIN` on the
     5.26 eval container (`cloud-sim-neo4j-eval`). The owner approves the task list before the run.

## Filed tickets

None. The commission said to file nothing.

---

## Method appendix

**Stores and windows.**

- Neo4j: container `cloud-sim-neo4j` (production), `cypher-shell --access-mode read`, credentials read
  inside the container from its own environment and never printed. Counts and schema only.
- Gateway: `cloud-sim-seshat-gateway`, the live `settings` object and the `AGENT_*` environment.
- Elasticsearch: `localhost:9200`, `agent-logs-*`, the 30 days before 2026-10-11 07:55 UTC.
- Code: `origin/main` at `812c82e0` (2026-10-11), which includes ADR-0156 T5 and T6 (FRE-1577, FRE-1578).
- Linear: `list_issues` searches, team FrenchForest, and `get_issue` for FRE-1546, FRE-1126 and FRE-842.
- Skills: the `neo4j-contrib/neo4j-skills` repository at `bb30e1f0` (2026-10-09), and the page
  through WebFetch.

**What was rejected, and why.**

- *The page's summary.* WebFetch's summary listed 22 skills and "no explicit OSS license". The
  repository holds 29 skills and an MIT `LICENSE`. This document quotes the repository.
- *A code map from an older tree.* A first code map read the worktree at `95942ae3`. There,
  `neo4j-direct` was a flat file whose read-only rule was text only. At `812c82e0` it is a folder with
  a script that opens a read transaction and allows six schema procedures. Every statement about
  `neo4j-direct` here comes from `812c82e0`. The recall code itself did not change between the two
  (N4).
- *`query_string` on `arguments`.* `"*curl*"` as a `query_string` on `arguments` returned 0 over 30
  days, and the client-side match of the same rows returned 70. The ES query was the broken instrument,
  so N6 uses the client-side match only.
- *Equality on `event_type.keyword`.* It returned 0 for every query. `event_type` is a keyword field
  itself, and a term query on `event_type` returned 2,220.

**Identifier resolutions.**

- The vector index is `entity_embedding` on `Entity.embedding`, 1024 dimensions, cosine, quantization
  on (`SHOW INDEXES`).
- The full-text index is `turn_entity_fulltext` on `Turn.user_message` and `Entity.name`
  (`SHOW INDEXES`). No full-text index covers `Entity.description` or a turn summary.
- `Turn` nodes carry no embedding: `count(t.embedding)` is 0 of 2,729.
- `tool_call_started` carries `tool_name` and `arguments` (one document read on 2026-10-11 at 07:49 UTC).
