---
name: neo4j-direct
description: Runs read-only Cypher queries on the live Neo4j knowledge graph through a tested script.
  Use when the task needs the live state of the graph - node and relationship counts, labels, recent
  entities, or a direct Cypher query.
metadata:
  requires-tools: bash
---

# neo4j-direct — Run read-only Cypher on the knowledge graph

## Rules

- The skill is read-only. Use `MATCH` and `RETURN`. Never write to the graph from a diagnostic query: no `CREATE`, `MERGE`, `SET`, `DELETE`, `REMOVE`, `DROP`.
- If the user asks to change the graph, do not do it. Show the `MATCH` that finds the nodes and leave the change to the user.
- Use the script, not a hand-written driver call. The `run_python` sandbox has no `neo4j` driver.
- Pass a value with `--param name=value`. Never put a value into the query text.
- A `CALL` is allowed only for the schema procedures `db.labels`, `db.relationshipTypes`, `db.propertyKeys`, `db.schema.visualization`, `db.schema.nodeTypeProperties` and `db.schema.relTypeProperties`. Any other procedure (an `apoc.*` call, `dbms.*`) is refused: it can write, reach a URL or show the configuration.
- Do not use the `/db/data/transaction/commit` HTTP endpoint. It does not exist in Neo4j 5 (404 or 401).

## Step 1 — Run one query

```bash
# ask: python3 can run any program, so this asks for approval (ADR-0156 D8.2).
python3 docs/skills/neo4j-direct/scripts/cypher.py "MATCH (e:Entity) RETURN count(e) AS n"
```

The reply is one JSON document: `{"rows": [...], "row_count": n, "truncated": false}`. The script
returns at most 100 rows. Use `--limit N` (up to 1000) for more. `truncated: true` means that the
query holds more rows than were returned.

The script opens a read transaction, so the server refuses a write (checked live on 2026-10-11,
including `apoc.cypher.doIt`). It checks for a write clause and for a procedure that is not on
the list first, to give a plain error before it connects. A word inside a string, a comment or a
backtick name is not read as a clause.

## Step 2 — Common diagnostic queries

```bash
# ask: python3 can run any program, so this asks for approval (ADR-0156 D8.2).
python3 docs/skills/neo4j-direct/scripts/cypher.py "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY cnt DESC"
```

```bash
# ask: python3 can run any program, so this asks for approval (ADR-0156 D8.2).
python3 docs/skills/neo4j-direct/scripts/cypher.py "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"
```

```bash
# ask: python3 can run any program, so this asks for approval (ADR-0156 D8.2).
python3 docs/skills/neo4j-direct/scripts/cypher.py "MATCH (e:Entity) RETURN e.name AS name, e.created_at AS created ORDER BY e.created_at DESC LIMIT 5"
```

```bash
# ask: python3 can run any program, so this asks for approval (ADR-0156 D8.2).
python3 docs/skills/neo4j-direct/scripts/cypher.py 'MATCH (e:Entity) WHERE toLower(e.name) CONTAINS $q RETURN e.name AS name, e.entity_type AS type LIMIT 10' --param q=memory
```

```bash
# ask: python3 can run any program, so this asks for approval (ADR-0156 D8.2).
python3 docs/skills/neo4j-direct/scripts/cypher.py "MATCH (e:Entity)-[r]-() RETURN e.name AS name, count(r) AS degree ORDER BY degree DESC LIMIT 10"
```

Labels in the graph include `Entity` and `Turn`. A relationship type of interest is `DISCUSSES`.
Run the label query first when you are unsure what exists.

## Environment

The script reads these variables. Never write a value into a command.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_NEO4J_URI` | `bolt://neo4j:7687` | Bolt address (a Docker DNS name inside the network) |
| `AGENT_NEO4J_USER` | `neo4j` | User |
| `AGENT_NEO4J_PASSWORD` | none | Password. The script stops with an error if it is empty |

## Success

Check the result before you answer:

- The answer quotes the number or the rows from the script output, with the query that produced them.
- `truncated` is `false`, or the answer says that only `row_count` rows are shown.
- A count of 0 is checked once with the label query (Step 2): a wrong label name also gives 0.
- No write was attempted.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| `error: this script is read-only and the query holds SET` | A write clause, or a property named like one | Remove the write. If it is a property or alias name, quote it with backticks |
| `error: the procedure apoc.x.y is not allowed` | A `CALL` outside the list | Use `MATCH` and `RETURN`, or one of the listed schema procedures |
| `error: AGENT_NEO4J_PASSWORD is not set in this environment` | The script runs where the variable is not set | Say that the graph cannot be read from here. Do not look for the password |
| `error: authentication failed` | The user or password is wrong | Stop. Tell the user. Do not try other credentials |
| `error: cannot connect to bolt://...` | Neo4j is down, or the name does not resolve from here | Run the `infrastructure-health` skill for `neo4j`. Do not use `localhost` inside the container |
| `error: the server refused the query: ...` | A syntax error, or the server refused a write | Fix the query as the message states |
| `error: the neo4j driver is not importable by this python3` | The command ran with another Python | Run it with `/app/.venv/bin/python` in place of `python3` |
| The driver is not usable at all | — | The HTTP API is the last resort (below). It asks the user |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |

The HTTP Cypher API, only when the Python driver cannot be used:

```bash
# ask: the shell expands ${AGENT_NEO4J_USER} and ${AGENT_NEO4J_PASSWORD} here, so this is not a plain command and asks for approval.
curl -fsS -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -u "${AGENT_NEO4J_USER}:${AGENT_NEO4J_PASSWORD}" \
  "http://neo4j:7474/db/neo4j/tx/commit" \
  -d '{"statements":[{"statement":"MATCH (e:Entity) RETURN count(e) AS n"}]}'
```
