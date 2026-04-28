# SKILL: Seshat Knowledge Graph

> **Tier:** 2 — CLI tool + HTTP API
> **Auth:** `SESHAT_API_TOKEN` environment variable (Gateway API)
> **ADR:** `docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md`

Query and manage the persistent knowledge graph — entities, relationships, and metadata captured from conversation turns.

---

## Works Now

### CLI (no auth required)

```bash
uv run agent memory search "query string"
uv run agent memory entities --limit 20 --sort mentions
uv run agent memory entities --type "Decision" --limit 10
uv run agent memory entities --json > entities.json
```

### Gateway API

The gateway is mounted at `/api/v1` when the service is running. Set the base URL once:

```bash
export SESHAT_API_TOKEN="<your-api-token>"
export SESHAT_API_URL="http://localhost:9000/api/v1"
```

**Search entities (GET /knowledge/search)**

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/knowledge/search?q=memory+promotion&limit=5"
```

Response — **bare list** (not a wrapper object):

```json
[
  {
    "id": "ent-123",
    "name": "memory_promotion_pipeline",
    "type": "Process",
    "metadata": {"source": "session-42"}
  }
]
```

**Get entity by ID (GET /knowledge/entities/{entity_id})**

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/knowledge/entities/{entity_id}"
```

**Store a new entity (POST /knowledge/entities)**

Required fields: `entity` (name), `entity_type`. Optional: `metadata` (free-form dict).

```bash
curl -X POST \
  -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "entity": "delegation_pattern",
    "entity_type": "Pattern",
    "metadata": {
      "discovered_date": "2026-04-28",
      "source": "Claude Code session"
    }
  }' \
  "$SESHAT_API_URL/knowledge/entities"
```

> **Common mistake:** do NOT send `"type"` or `"description"` — the request model uses `entity_type` and `metadata` only. A `description` field is not accepted and will be silently ignored (Pydantic ignores extra fields by default).

**Get entity relationships (GET /knowledge/entities/{entity_id}/relationships)**

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/knowledge/entities/{entity_id}/relationships"
```

---

## Planned — not yet implemented

- `/knowledge/graph` — full graph export
- Topic-graph traversal endpoints
- Relationship write API

---

## Error handling

| Error | Cause | Fix |
|-------|-------|-----|
| `neo4j_connection_failed` | Neo4j not running | `./scripts/init-services.sh` |
| `401 Unauthorized` | Bad or missing token | `export SESHAT_API_TOKEN="..."` |
| `404 Not Found` | Entity ID doesn't exist | Search first to get IDs |
