# FRE-1546 — Does live recall walk typed knowledge-graph relationships? (explore seat, 2026-10-03)

Owner-requested study. The trigger was a public challenge to developers: "are you really using a
knowledge graph?" The question that this study answers is narrow. Does Seshat answer a question by
walking typed relationships in Neo4j, or only by similarity search?

Read-only throughout. Every graph query ran in a Neo4j read transaction (`execute_read` with
`READ_ACCESS`). No service method was called, because recall methods publish `memory.accessed` events
and those events write to the graph. No flag, container or ticket state was changed. The deployed
revision was verified by file hash (F1).

**Redaction.** The repository is public. Place names tied to the owner, names of real people other than
the owner, user ids, session ids, coordinates and health-related entity names are replaced by
placeholders in angle brackets. Each placeholder marks a value that was present in the real output.

---

## Verdict

**No. Live recall does not walk typed relationships.** The graph holds about 11,900 typed
entity-to-entity edges (F5). No recall path reads them (F6). Live recall is multi-query vector search
plus lexical full-text search, fused by Reciprocal Rank Fusion (RRF) and reranked (F2, F3). Recall reads
graph edges only for provenance: `DISCUSSES` to the source turn and `SOURCED_FROM` to the source
document.

The structural arm (FRE-707, FRE-866) is off live and was never enabled (F2, F4). Enabling it as wired
will not change the verdict. It adds the 50 most recently seen entities to every recall, whatever the
question (F7).

A simulated walk over typed edges answers some relationship questions far better than the lexical arm
(F9). It also shows that the graph data is not ready to be walked without guards: one project is spread
over many nodes (F10), the edge vocabulary is vague (F12), an agent inference is stored as a fact (F13),
and two-hop neighbourhoods grow fast (F14). Test identities from live verification remain in the
production graph and in the users table (F15, F16).

---

## Findings

### F1 — The deployed code is `origin/main` at `ed18a6b1`

**Verdict:** POSITIVE

**Query:** sha256 of every `.py` file under `src/personal_agent` in the running gateway container,
compared with the same files from `git archive origin/main`.

```
docker exec <gateway> sh -c 'cd /app && find src/personal_agent -name "*.py" | sort | xargs sha256sum'
(cd <archive of origin/main> && find src/personal_agent -name "*.py" | sort | xargs sha256sum)
diff <container> <main>
```

**Output:** `diff` printed nothing. `files: 350 vs 350`. Every code citation below is at this revision
and is the deployed code.

### F2 — Live recall flags: multipath, multi-query and lexical on; structural off

**Verdict:** POSITIVE

**Query:** the environment of the running server process (pid 27), and the settings object read in the
same container.

```
tr "\0" "\n" < /proc/27/environ | grep -E "MULTIPATH|STRUCTURAL|LEXICAL_ARM|MULTIQUERY|CONFIG"
python -c "from personal_agent.config import settings as s; print(...)"
grep -rn -i "structural_arm\|multipath\|lexical_arm\|multiquery" /app/config
```

**Output:**

```
AGENT_LEXICAL_ARM_ENABLED=true
AGENT_MULTIPATH_RECALL_ENABLED=true
AGENT_MULTIQUERY_ARM_ENABLED=true

multipath_recall_enabled True
structural_arm_enabled False
structural_type_predicate_enabled False
structural_class_predicate_enabled False
lexical_arm_enabled True
multiquery_arm_enabled True
```

The `grep` over `/app/config` printed nothing. The code default of `structural_arm_enabled` is `False`
(`config/settings.py:861`). Master confirmed that `AppConfig` reads only environment variables and
`.env` files, that the container logs `no_env_files_found`, and that the ADR-0121 config manager does
not cover these flags. Master also confirmed from the FRE-866 close comment (2026-07-19) that the
deployed environment never set the flag.

### F3 — Live recall ran the multi-query and lexical arms

**Verdict:** POSITIVE

**Query:** Elasticsearch, `agent-logs-*`, `event_type: multipath_recall`. The store holds one index,
`agent-logs-2026-10`, with `@timestamp` from `2026-10-01T00:00:00.688Z` to `2026-10-03T15:40:34.514Z`.

```
POST agent-logs-*/_count
{"query":{"bool":{"filter":[{"term":{"event_type":"multipath_recall"}},
                             {"match":{"arms_executed":"<arm>"}}]}}}
```

**Output:**

```
all multipath_recall: 2
arms_executed=multi_query: 2
arms_executed=lexical: 2
arms_executed=dense: 0
arms_executed=structural: 0
multipath_arm_failed: 0
```

A raw document (redacted):

```
"event_type": "multipath_recall",
"function": "_multipath_fused_recall",
"line_number": 5476,
"path": "entity",
"arms_executed": ["multi_query", "lexical"],
"per_arm_counts": {"multi_query": 50, "lexical": 50},
"fused_set_size": 25,
"reranked": true
```

Counts are provisional (FRE-1051). Both events predate the container restart at
`2026-10-03T14:56:52Z`.

### F4 — The structural arm did not run in the window

**Verdict:** NEGATIVE, at the scope below.

**Query:** the F3 query with `arms_executed: structural`.

**Output:** `0`.

- **Arm 1, provenance (1b, live producer).** The emit is `log.info("multipath_recall", ...,
  arms_executed=arm_names, ...)` at `memory/service.py:5476` in the deployed revision (F1). The arm name
  `"structural"` is appended at `service.py:5446` when `structural_arm_enabled` is true. The enclosing
  path executes: the raw document in F3 carries `"line_number": 5476` from the same emit.
- **Arm 2, path liveness.** The identical query with `lexical` in place of `structural` returns `2`.
- **Arm 3, scope.** Index `agent-logs-2026-10` only, from `2026-10-01T00:00Z` to `2026-10-03T15:40Z`,
  production gateway only. The verdict is "the structural arm did not run in these two recalls". No
  older `agent-logs` index exists, so no claim covers July to September. F2 and master's reading of the
  FRE-866 close comment carry the "never enabled" part.

### F5 — The live graph holds about 11,900 typed entity-to-entity edges

**Verdict:** POSITIVE

**Query:**

```
MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC LIMIT 30
MATCH (:Entity)-[r]->(:Entity)
WHERE type(r) IN ['USES','RELATED_TO','PART_OF','LOCATED_IN','SIMILAR_TO','CREATED_BY','VISITED','CAUSES']
RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC
```

**Output (entity to entity):**

```
RELATED_TO 6349
USES       3367
PART_OF    1131
LOCATED_IN  514
SIMILAR_TO  332
CREATED_BY  197
CAUSES        1
```

All edges, for comparison: `DISCUSSES 26105`, `PARTICIPATED_IN 2698`, `HAS_FACT 424`,
`SOURCED_FROM 390`, `HAS_STANCE 114`, `VISITED 2`, `CURRENTLY_AT 1`.

### F6 — No recall read query follows a typed entity-to-entity edge

**Verdict:** NEGATIVE for the deployed code. UNVERIFIABLE for live execution.

**Query:** text search over `src/personal_agent` at `ed18a6b1`.

```
git grep -n "<TYPE>" origin/main -- src/personal_agent        # for each of RELATED_TO USES PART_OF LOCATED_IN SIMILAR_TO
git grep -n -E "\-\[[a-z_]*\]\-|<\-\[[a-z_]*\]\-|\-\[[a-z_]*\]\->" origin/main -- src/personal_agent
git grep -n -E "\[[a-z_]*:\{|\[:\\\$|type\(r\)" origin/main -- src/personal_agent
git grep -n -E "\*[0-9]*\.\.|shortestPath|allShortestPaths|apoc\.path" origin/main -- src/personal_agent
```

**Output:**

- The five type names appear only in `second_brain/entity_extraction.py:118-123` and `:289` (the
  extractor prompt), `second_brain/consolidator.py:1044` (the write default) and a comment in
  `memory/models.py:64`.
- Untyped edge patterns appear only in statistics and quality jobs: `freshness_consumer.py:40`,
  `freshness_aggregate.py:203`, `freshness_backfill.py:24,121`, `kg_stats_aggregate.py:164`,
  `service.py:4154` (a case-collision check), `quality_monitor.py:275,319,330`, `memory_cli.py:261`.
- No variable-length path, `shortestPath` or `apoc.path` call exists.

- **Arm 1, provenance.** The identifiers are real in the store (F5) and have a live producer: the
  extractor prompt lists them (`entity_extraction.py:289`) and the consolidator writes them
  (`consolidator.py:1044`).
- **Arm 2, path liveness.** The same search method finds the provenance reads that recall does make:
  `service.py:1066`, `:1199`, `:5705`, `:5732` (`DISCUSSES`) and `:4632`, `:5706`, `:5733`, `:6019`,
  `:6475`, `:6502` (`SOURCED_FROM`).
- **Arm 3, scope.** `src/personal_agent` only, at `ed18a6b1`. A Cypher string built at run time from a
  variable relationship type will escape a text search. The dynamic-pattern search above found none in
  a read path.

**Live execution is UNVERIFIABLE.** This study had no instrument that records which Cypher statements
the live process runs. The claim rests on the code, which F1 shows is the deployed code.

### F7 — Simulation A: enabling the flag as wired adds the same 50 recent entities to every recall

**Verdict:** POSITIVE

**Method.** The Cypher was built in the container by the deployed pure function
`_build_structural_arm_query` with the arguments that `_multipath_fused_recall` passes
(`service.py:5445-5447`: no entity types, no recency window, no anchors), the live predicate flags, and
the owner's visibility. It ran in a read transaction.

**Query (as built):**

```
MATCH (e:Entity) WHERE (e.visibility IS NULL OR e.visibility = 'public'
  OR (e.visibility = 'group' AND $vis_authenticated = true)
  OR e.visibility = 'private:' + $vis_user_id)
RETURN e AS e, elementId(e) AS item_id
ORDER BY toString(e.last_seen) DESC, e.name LIMIT $top_k
```

**Output:** `ROWS: 50`. The first 15:

```
1 BBC | Organization | 2026-10-03T14:01:03
2 Dorsal fin | AnatomicalStructure | 2026-10-03T14:01:03
3 Fish head | AnatomicalStructure | 2026-10-03T14:01:03
4 Fish stock | MethodOrConcept | 2026-10-03T14:01:03
5 Foil-baked bass | KnowledgeArtifact | 2026-10-03T14:01:03
6 Gills | AnatomicalStructure | 2026-10-03T14:01:03
7 Marinade | MethodOrConcept | 2026-10-03T14:01:03
8 Olive oil | TechnicalArtifact | 2026-10-03T14:01:03
9 Pectoral fins | AnatomicalStructure | 2026-10-03T14:01:03
10 Salt crust | MethodOrConcept | 2026-10-03T14:01:03
11 Scaling | MethodOrConcept | 2026-10-03T14:01:03
12 Serious Eats | Organization | 2026-10-03T14:01:03
13 Whole-bass recipes | KnowledgeArtifact | 2026-10-03T14:01:03
14 Descaling | MethodOrConcept | 2026-10-03T13:46:59
15 Fish scaler | TechnicalArtifact | 2026-10-03T13:46:59
```

The query takes no input from the question. The docstring of `_multipath_fused_recall` states this:
"a plain closed-axis (recency-ordered) scan with no caller-supplied predicate, since this core has no
source for type/class/anchor filters from a raw `query_text`". RRF scores by rank only, so these rows
compete with relevant rows for the reranker input cap on every recall.

### F8 — Simulation A2: the anchored branch follows `DISCUSSES` only, and nothing calls it

**Verdict:** POSITIVE

**Method.** The same builder with `anchor_names=["Seshat","seshat"]`. No caller passes `anchor_names`:
`git grep -n "anchor_names=" origin/main -- src/personal_agent | grep -v "anchor_names=anchor_names"`
printed nothing.

**Query (as built, visibility fragments shortened to `<vis>`):**

```
MATCH (a:Entity)<-[:DISCUSSES]-(t:Turn)-[:DISCUSSES]->(e:Entity)
WHERE a.name IN $anchor_names AND e.name <> a.name AND <vis a> AND <vis t> AND <vis e>
WITH e, count(DISTINCT a) AS cooccur
RETURN e AS e, elementId(e) AS item_id
ORDER BY cooccur DESC, toString(e.last_seen) DESC, e.name LIMIT $top_k
```

**Output:** `ROWS: 32`. The first rows are `<person>`, `<owner>`, two event timestamps,
`interactive HTML artifact`, `Seshat telemetry`, `Cap utilization`, `Day-over-day change`,
`Budget cap`, `Budget B002`, `Budget B001`, `Role A`, `Role B`, `Role C`. These are entities from the
same turns, not facts about Seshat.

### F9 — Simulation B: a typed walk answers relationship questions that the lexical arm misses

**Verdict:** POSITIVE

**Method.** Four questions. Each walk starts at a seed node, follows a whitelist of edge types, applies
the owner's visibility to nodes and edges, and orders by edge `weight`. Stances with an `invalid_at`
value are dropped. For each question, the live lexical arm query (`service.py:5179`, the
`turn_entity_fulltext` index, entities only, top 8) ran on the same text. The multi-query vector arm was
not simulated, because it needs paid embedder calls. Live recall can therefore do better than the
lexical column shows.

**Queries (visibility predicate shown as `<vis>`):**

```
-- Q1 What is Seshat built with?
MATCH (s:Entity) WHERE toLower(s.name) = 'seshat'
MATCH (s)-[r:USES|PART_OF|CREATED_BY]-(x:Entity) WHERE <vis x> AND <vis r>
RETURN ... ORDER BY r.weight DESC LIMIT 25

-- Q2 What tools do I use?
MATCH (o:Person {is_owner:true})-[r:USES]->(x:Entity) WHERE <vis x> AND <vis r>
RETURN ... ORDER BY r.weight DESC LIMIT 25

-- Q3 How do I feel about the tech that Seshat uses? (2 hops)
MATCH (s:Entity) WHERE toLower(s.name) = 'seshat'
MATCH (s)-[:USES]->(x:Entity)<-[st:HAS_STANCE]-(o:Person {is_owner:true})
WHERE st.invalid_at IS NULL AND <vis x> RETURN ... LIMIT 25

-- Q4 Where have I been?
MATCH (o:Person {is_owner:true})-[r]->(l:Entity {entity_type:'Location'})
WHERE type(r) IN ['LOCATED_IN','VISITED','CURRENTLY_AT','RELATED_TO','HAS_STANCE'] AND <vis l>
  AND (type(r) <> 'HAS_STANCE' OR r.invalid_at IS NULL)
RETURN ... LIMIT 25

-- lexical arm, each question
CALL db.index.fulltext.queryNodes('turn_entity_fulltext', $q) YIELD node, score
WHERE node:Entity RETURN node.name AS hit, round(score,2) AS score ORDER BY score DESC LIMIT 8
```

**Output:**

| Question | Typed walk | Lexical arm, same text |
|---|---|---|
| Q1 | 2 rows: `Seshat -USES-> Neo4j (0.98)`, `Seshat -USES-> Seshat telemetry (0.92)` | `Seshat`, `seshat`, `seshat-gateway`, `Seshat PWA`, `Seshat Delegation`, `Seshat system`, `Seshat CLI`, `Seshat telemetry` |
| Q2 | 15 rows: Cloud VPS 0.88, Embeddings API 0.84, Reranker API 0.84, PostgreSQL 0.84, Python 0.82, Elasticsearch 0.82, Learning Profile 0.81, Qwen3 0.78, Grafana 0.76, Prometheus 0.76, Uvicorn 0.74, FastAPI 0.74, RTX 4070 0.72, Gemma 4 0.70, Devstral 0.70 | `Amenhotep I`, `Ferdinand I`, `Filesystem tools`, `MCP Tools`, `parquet-tools`, `Substance use`, `Antibiotic use`, `Tool use` |
| Q3 | 0 rows (see F11) | `<health entity>`, `Tech stack`, `seshat`, `Seshat`, `Ferdinand I`, `Amenhotep I`, `Backend Tech Stack`, `<place>` |
| Q4 | 13 rows: 7 `RELATED_TO` to places, 1 `LOCATED_IN` to `<place A>`, 5 `HAS_STANCE` to places with affects such as "likes it" and "wants a warm beach holiday" | `Sessions have owners`, `Ferdinand I`, `Amenhotep I`, `Disk I/O`, `I/O wait`, `I/O Wait`, `World War I`, `Disk I/O monitoring` |

On Q2 the walk gives a direct, weighted answer. The lexical arm matches the token "I".

### F10 — One project is spread over many nodes, so a single-seed walk misses most of it

**Verdict:** POSITIVE

**Query:**

```
MATCH (s:Entity) WHERE toLower(s.name) CONTAINS 'seshat' OR toLower(s.name) = 'personal agent'
OPTIONAL MATCH (s)-[r]-(:Entity) WHERE type(r) IN ['USES','PART_OF','CREATED_BY','RELATED_TO']
WITH s, count(r) AS typed RETURN s.name AS name, typed ORDER BY typed DESC LIMIT 12

MATCH (s:Entity)-[r:USES]->(x:Entity)
WHERE toLower(s.name) CONTAINS 'seshat' OR toLower(s.name) = 'personal agent'
RETURN s.name AS from, collect(x.name)[0..12] AS uses, count(*) AS n ORDER BY n DESC LIMIT 6
```

**Output:**

```
Seshat Knowledge Graph 9 | Seshat Gateway 8 | Seshat Personal Agent 7 | Seshat harness 7
Seshat Personal Agent System 6 | Seshat 6 | seshat-gateway 6 | Seshat Harness FSM Explainer 5
<deploy path> 3 | Seshat PWA 2 | Personal Agent 2 | seshat-knowledge 2

Seshat Gateway               USES FastAPI, LLM Server, PostgreSQL, Elasticsearch, Cypher (Bolt), SQL (Bolt)
Seshat Personal Agent System USES CRUD, PostgreSQL, pg_cron, FastAPI, SQLAlchemy 2.0, Alembic
Seshat Knowledge Graph       USES Neo4j, RRF, Cypher, HAS_STANCE, POST /knowledge/entities
seshat-gateway               USES ANTHROPIC_API_KEY, NEO4J_PASSWORD, POSTGRES_PASSWORD, stdout/stderr logging
Seshat Personal Agent        USES FastAPI, TaskState, ToolLoopGate
Seshat harness               USES TaskState, ToolLoopGate
```

The node names also exist in two case variants (`Seshat`, `seshat`; `Personal agent`, `Personal Agent`)
with different types and classes. Some `USES` targets are not things a system uses: secret names,
a relationship type name, an API route.

### F11 — The Q3 zero comes from the data, not from the query

**Verdict:** NEGATIVE ("no current owner stance attaches to a technology that a node named Seshat
uses"), at the scope below.

**Query:** F9 Q3. **Output:** `0 rows`.

- **Arm 1, provenance (1a, raw instance).** The same store holds current owner stances on technology
  nodes:
  `MATCH (o:Person {is_owner:true})-[st:HAS_STANCE]->(x:Entity {entity_type:'TechnicalArtifact'})
  WHERE st.invalid_at IS NULL RETURN x.name, st.affect LIMIT 10` returned 10 rows, for example
  `Apache Iceberg | sees it as maintenance-heavy in the general case` and
  `LM Studio | uses it`.
- **Arm 2, path liveness.** The same two-hop shape with the seed changed to the owner's own `USES`
  edges:
  `MATCH (o:Person {is_owner:true})-[:USES]->(x:Entity)<-[st:HAS_STANCE]-(o) WHERE st.invalid_at IS NULL
  RETURN x.name, st.affect` returned 2 rows: `Python | prefers over Java`,
  `Elasticsearch | is sure about the shard count`.
- **Arm 3, scope.** Seeds are only nodes whose lower-cased name equals `seshat`. The first hop is `USES`
  only. Only current stances count. The F10 variants were not seeded.

### F12 — The edge vocabulary is too vague for some questions

**Verdict:** POSITIVE

**Query:** F5. **Output:** `RELATED_TO` is 6,349 of 11,891 entity-to-entity edges (53%). The
extractor offers six types (`entity_extraction.py:118-123`). None of them separates "visited",
"lives in", "talked about" and "wants to go". `VISITED` has 2 edges, and both come from the device
location path, not from the extractor (F13). So Q4 in F9 cannot tell a visit from a mention or a wish.

### F13 — `LOCATED_IN` on the owner is an agent inference that the extractor stored as a fact

**Verdict:** POSITIVE

The owner asked whether `LOCATED_IN` records their device location at the time of a turn. It does not.
Two different writers exist.

**Query 1, device location:**

```
MATCH (o:Person {is_owner:true})-[r:CURRENTLY_AT|VISITED]->(l)
RETURN type(r), labels(l), l.timezone, l.source, toString(coalesce(r.since, r.at)), keys(r)
```

**Output:**

```
CURRENTLY_AT | ["Location"] | <timezone> | client | 2026-07-29T05:23:36.14Z | ["since","trace_id"]
VISITED      | ["Location"] | <timezone> | client | 2026-07-29T05:23:36.14Z | ["at","trace_id"]
VISITED      | ["Location"] | <timezone> | client | 2026-06-03T19:41:43.323Z | ["at","trace_id"]
```

The writer is `service.py:3922-3940`. It merges a `:Location` node keyed by latitude and longitude, moves
`CURRENTLY_AT`, and merges `VISITED`. The last device location arrived on 2026-07-29.

**Query 2, the owner's `LOCATED_IN` edge:**

```
MATCH (o:Person {is_owner:true})-[r]->(l:Entity {name:'<place A>'})
RETURN type(r), toString(r.created_at), r.weight, r.provenance_state, size(coalesce(r.source_ids,[]))
```

**Output:**

```
RELATED_TO | 2026-06-26T21:30:31.895Z | 0.86 | null | 0
LOCATED_IN | 2026-06-26T21:28:47.495Z | 0.96 | null | 0
```

The target is an `:Entity` with no coordinates. The edge carries extractor properties (`weight`,
`created_at`, access counters). The only writer of `LOCATED_IN` is the extractor (F6).

**Query 3, the turns that discuss `<place A>`:**

```
MATCH (t:Turn)-[:DISCUSSES]->(e:Entity {name:'<place A>'})
RETURN toString(t.timestamp), left(t.user_message, 220) ORDER BY 1 LIMIT 6
```

**Output (redacted):**

```
2026-05-15T09:35 "What is the altitude of <place A> France? Meters and feet"
2026-06-26T21:28:43 "Your memory call should be able to find it in the knowledge graph"
2026-06-26T21:30:27 "Wtf. I don't live in <place A>."
```

The assistant reply in the 21:28:43 turn says, in part: "the coordinates ... match the
<region> region", "No relationship linking <owner> → <place A> as your home", and
"Created `<owner>` → `[LIVES_IN]` → `<place A>` in Neo4j". The extractor wrote `LOCATED_IN` (weight
0.96) four seconds after that turn. It wrote `RELATED_TO` four seconds after the owner's denial.

So the owner's intuition is partly right. Device coordinates started the chain. The agent turned them
into a claim about the owner's home, and the extractor stored the agent's claim as a high-confidence
fact. The June cleanup deleted the `LIVES_IN` edge. This `LOCATED_IN` edge remains.

### F14 — Two-hop neighbourhoods grow fast

**Verdict:** POSITIVE

**Query:**

```
UNWIND ['<owner>','Seshat','Neo4j','<place B>'] AS seed
MATCH (s:Entity {name: seed})
CALL { WITH s MATCH (s)-[r1]-(x:Entity) WHERE type(r1) IN <typed list> RETURN count(DISTINCT x) AS hop1 }
CALL { WITH s MATCH (s)-[r1]-(:Entity)-[r2]-(y:Entity)
       WHERE type(r1) IN <typed list> AND type(r2) IN <typed list> AND y <> s
       RETURN count(DISTINCT y) AS hop2 }
RETURN seed, hop1, hop2
```

`<typed list>` is `['USES','RELATED_TO','PART_OF','LOCATED_IN','SIMILAR_TO','CREATED_BY','HAS_STANCE','VISITED','CAUSES']`.

**Output:**

```
<owner>    hop1 132  hop2 1401
Seshat     hop1   6  hop2  141
Neo4j      hop1   4  hop2   51
<place B>  hop1  57  hop2  253
```

The highest-degree entities are hubs: `Elasticsearch` 307 typed edges, `PostgreSQL` 192, the owner 174,
`FastAPI` 164. An open two-hop expansion from the owner returns 1,401 entities.

### F15 — 21 test identities remain in the production graph, with their data

**Verdict:** POSITIVE

**Query:**

```
MATCH (p:Person) WHERE coalesce(p.is_owner,false) = false
OPTIONAL MATCH (p)-[r]-(x) WITH p, collect(DISTINCT type(r)) AS rels, count(r) AS deg
OPTIONAL MATCH (p)-[:PARTICIPATED_IN]->(t:Turn)
RETURN p.name, toString(p.created_at), deg, count(t), min/max turn date, rels ORDER BY created
```

and, with `TEST` = names starting `fre`, `postrevert`, `fin-`, or equal to `eval-verify`:

```
MATCH (p:Person) WHERE TEST RETURN count(p)
MATCH (p:Person)-[:PARTICIPATED_IN]->(t:Turn) WHERE TEST RETURN count(DISTINCT t), count(DISTINCT t.session_id)
MATCH (p:Person)-[:HAS_FACT]->(c) WHERE TEST RETURN count(DISTINCT c)
MATCH (p:Person)-[:PARTICIPATED_IN]->(t:Turn)-[:DISCUSSES]->(e:Entity) WHERE TEST
WITH DISTINCT e
WHERE NOT EXISTS { MATCH (q:Person)-[:PARTICIPATED_IN]->(:Turn)-[:DISCUSSES]->(e) WHERE NOT <TEST on q> }
RETURN count(e)
```

**Output:**

```
non-owner Person nodes: 24 = 3 real people + 21 test identities
test identities created: 2026-06-02 (eval-verify) to 2026-10-03 (fre1539-live)
  names: eval-verify, fre1278-eval, fre1278-clean, fre1278-clean2, fre1278-check,
         postrevert-1..3, fin-1787556481-1, fin-1787556511-2, fin-1787556537-3, fin-1787556557-4,
         fre1448-live, fre1463-live, fre1487-live, fre1493-live, fre1489-live, fre1500-live,
         fre1325-live, fre1517-study, fre1539-live
test_persons 21
test_turns 72, test_sessions 60
test_claims 27   (HAS_FACT from fre1448-live, fre1463-live, fre1487-live)
entities_only_from_test_turns 265
```

The 265 entities are reachable by every recall. Most sampled names are places, for example museums and
archaeological sites. The newest identity was created today, so live verification still creates them.

### F16 — The same identities remain in the Postgres `users` table

**Verdict:** POSITIVE

**Query:**

```
select user_id, display_name, left(created_at::text,10) from users where user_id in (<8 ids from F15>)
select display_name, left(created_at::text,10) from users order by created_at
```

**Output:** 7 of the 8 ids are present (`eval-verify` is not). The table holds 30 rows: 4 named users
created 2026-04-26 to 2026-05-13, and 26 users with an empty `display_name` created 2026-08-23 to
2026-10-03.

### F17 — No record of a cleanup of these identities was found

**Verdict:** UNVERIFIABLE

The owner states that the test users and data were supposed to be cleaned up. This study found no
record of that cleanup. Searched:

- Linear, FrenchForest, title and description search for `test user`, `cleanup` and
  `live-verify identity`. 75 issues returned. None covers removing live-verification identities.
- FRE-375 (test substrate isolation, Done) has no comments.
- `git log origin/main --since=2026-08-20 -i --grep="test user\|test person\|cleanup\|clean up\|purge\|residue"`
  returned 15 commits. None concerns these identities.
- `git grep -n -i "postrevert\|fre1278-clean" origin/main` printed nothing.

A cleanup may be recorded where this search did not reach, for example in a ticket comment or a session
log. The result says only that this instrument did not find one.

---

## Proposals

1. **Do not enable `structural_arm_enabled` as wired.** As called by `_multipath_fused_recall`, the arm
   ignores the question (F7). Either feed it anchors from the top fused entities, or remove the
   no-anchor mode from fusion.
2. **Owner-led ADR for a typed-traversal recall arm.** The design question for the owner and cc-adrs is
   this: seeds from the top fused entities, an edge-type whitelist with weights, at most two hops with a
   per-hop cap, current stances only, fused by RRF as one more arm. Measure it on the FRE-489 probe set
   against today's recall. F9 shows the gain on relationship questions. F14 shows why open expansion is
   not an option.
3. **Merge entity variants before any traversal.** One project is spread over at least 12 nodes and two
   case variants (F10). A walk from one seed finds 2 of the facts. This relates to `memory/dedup.py` and
   the FRE-631 gate. Master must check the existing tickets before anything new is filed.
4. **Separate who asserted a fact.** The extractor stored the agent's own inference as a 0.96-weight
   fact about the owner (F13). An extracted edge needs to record whether the user said it or the
   assistant said it. This relates to ADR-0098 Amendment A (FRE-1338, attribution).
5. **Sharpen the person-to-place vocabulary.** `RELATED_TO` is 53% of entity edges (F12). Questions like
   "where have I been" need edges that separate a visit, a home, a mention and a wish.
6. **Owner decision: delete the `LOCATED_IN` edge from F13.** The owner denied this fact in the turn
   that produced the companion `RELATED_TO` edge. This is a data correction that needs the owner's
   confirmation and master's hands.
7. **Owner decision: purge the live-verification identities, and add teardown to live verification.**
   F15 and F16 measure the residue: 21 graph identities, 72 turns, 27 claims, 265 entities that only
   test turns produced, and 26 nameless users. Identities are still created (F15), so a one-time purge
   alone will not hold.

## Filed tickets

- **FRE-1546** — the anchor for this study (Backlog).

No other ticket was filed. The proposals above wait for master's disposition.

---

## Method appendix

**Stores.**

- Neo4j: the production graph behind the gateway. Every query ran in a read transaction through the
  Python driver inside the gateway container (`session(default_access_mode=READ_ACCESS)`,
  `execute_read`). Scripts were copied into the container's `/tmp` and deleted afterwards.
- Elasticsearch: `agent-logs-*` (only `agent-logs-2026-10` exists), counted with `_count`.
- Postgres: the production `users` table, `select` only.
- Linear and git, for the record searches in F17.

**Window.** 2026-10-03, about 15:30 to 16:10 UTC. The Elasticsearch window is
2026-10-01T00:00Z to 2026-10-03T15:40Z.

**Deployed revision.** `ed18a6b1`, confirmed by file hash (F1). The worktree HEAD at the start was an
older study branch, so all code reads used `git show origin/main:<path>`.

**Simulation fidelity.** The structural arm Cypher came from the deployed pure functions
`_build_structural_arm_query` and `_build_visibility_filter`, with the live settings object. The lexical
query copies `service.py:5179` and adds `WHERE node:Entity`. The typed walks in F9 are new queries
written for this study. They are not deployed code.

**Rejected or corrected measurements.**

- The first Postgres query counted users by `display_name` matching the test name pattern and returned
  `0`. That zero was the instrument: the test users have an empty `display_name`. The query by
  `user_id` (F16) replaced it.
- A text search for typed edges with a single-line Cypher pattern returned nothing. Because a new
  pattern can return a wrong zero, F6 rests on a plain search for each type name instead.
- The `pgrep -f "uvicorn personal_agent"` call in F2 also matched the shell that ran it. Only pid 1
  (`uv run`) and pid 27 (uvicorn) are server processes. Both carry the same flags.

**Identifier resolutions.** `multipath_recall` and `arms_executed` were resolved against a raw document
in the same index (F3). The structural arm name `"structural"` was resolved against the code at the
deployed revision (`service.py:5446`).

**Not measured.**

- The multi-query vector arm on the F9 questions (paid embedder calls).
- Whether the agent can still write graph edges through a tool, as it did in June (F13).
- Why only 2 recall events occurred in three days (F3).
