---
name: neo4j-direct
description: Run Cypher queries against the live Neo4j knowledge graph via bash python3. Do not use run_python (no neo4j driver in sandbox).
when_to_use: When you need to inspect Neo4j graph state, entity counts, relationships, or run Cypher queries directly.
tools: [bash]
keywords:
  - cypher
  - "bolt://"
  - entity nodes
  - entity count
  - turn nodes
  - knowledge graph state
  - graph state
  - query the graph
  - graph query
  - graph directly
  - connect to neo4j
  - neo4j query
  - query neo4j
  - discusses relationship
  - most recently created
---

# neo4j-direct — Run Cypher queries against the live knowledge graph

**Status:** Primary path (FRE-327). Use when the task requires reading or inspecting
Neo4j graph state directly — entity counts, relationships, schema, recent nodes.

**Category:** `system_read` · **Risk:** none (read-only queries) · **Approval:** auto-approved

> **Do NOT use:** the deprecated `/db/data/transaction/commit` HTTP endpoint — it was removed
> in Neo4j 5.x and always returns 404 or 401.
>
> **Do NOT use:** `run_python` — the sandbox image does not have the `neo4j` driver installed.
> Use `bash python3 -c "..."` instead; the gateway container has `neo4j>=5.15.0`.

---

## Env vars (always use these — never hardcode)

| Variable | Default (dev) | Purpose |
|----------|--------------|---------|
| `AGENT_NEO4J_URI` | `bolt://neo4j:7687` | Bolt connection URI (Docker DNS inside network) |
| `AGENT_NEO4J_USER` | `neo4j` | Username |
| `AGENT_NEO4J_PASSWORD` | *(see .env)* | Password |

---

## Primary path — bash python3 (1 tool call)

```bash
python3 - <<'EOF'
from neo4j import GraphDatabase
import os

uri  = os.environ.get("AGENT_NEO4J_URI", "bolt://neo4j:7687")
user = os.environ.get("AGENT_NEO4J_USER", "neo4j")
pw   = os.environ.get("AGENT_NEO4J_PASSWORD", "")

driver = GraphDatabase.driver(uri, auth=(user, pw))
with driver.session() as s:
    entities      = s.run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"]
    turns         = s.run("MATCH (t:Turn) RETURN count(t) AS n").single()["n"]
    discusses     = s.run("MATCH ()-[r:DISCUSSES]->() RETURN count(r) AS n").single()["n"]
    recent = [
        dict(r)
        for r in s.run(
            "MATCH (e:Entity) "
            "RETURN e.name AS name, e.created_at AS created "
            "ORDER BY e.created_at DESC LIMIT 5"
        )
    ]

driver.close()

print(f"Entity nodes:        {entities}")
print(f"Turn nodes:          {turns}")
print(f"DISCUSSES relations: {discusses}")
print("5 most recent entities:")
for row in recent:
    print(f"  {row['name']}  ({row['created']})")
EOF
```

---

## Common diagnostic queries

Adapt the script above by replacing the query lines:

```python
# All node labels and counts
s.run("CALL db.labels() YIELD label RETURN label").data()
s.run("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY cnt DESC").data()

# All relationship types and counts
s.run("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType").data()

# Entities by type
s.run("MATCH (e:Entity) RETURN e.entity_type AS type, count(e) AS n ORDER BY n DESC").data()

# Recent turns (conversation history)
s.run("MATCH (t:Turn) RETURN t.session_id, t.created_at ORDER BY t.created_at DESC LIMIT 10").data()

# Specific entity search
s.run("MATCH (e:Entity) WHERE toLower(e.name) CONTAINS $q RETURN e.name, e.entity_type LIMIT 10",
      q="memory").data()

# Entities with most relationships
s.run(
    "MATCH (e:Entity)-[r]-() "
    "RETURN e.name, count(r) AS degree "
    "ORDER BY degree DESC LIMIT 10"
).data()
```

---

## Alternative — HTTP Cypher API (Neo4j 5.x)

Use only if the Python driver is unavailable. Requires Basic auth:

```bash
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -u "${AGENT_NEO4J_USER}:${AGENT_NEO4J_PASSWORD}" \
  "http://neo4j:7474/db/neo4j/tx/commit" \
  -d '{"statements":[{"statement":"MATCH (e:Entity) RETURN count(e) AS n"}]}'
```

---

## Stop rules

- If `AGENT_NEO4J_PASSWORD` is empty, the driver will refuse to connect — check `.env` or `docker-compose.yml` for the actual password set on the neo4j container.
- If `bolt://neo4j:7687` is unreachable, the gateway is not on the Docker network — try `bolt://localhost:7687` only when running outside the container stack.
- Do not write to the graph during diagnostic queries — use `MATCH` / `RETURN` only.
