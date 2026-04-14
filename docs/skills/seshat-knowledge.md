# SKILL: Seshat Knowledge Graph

> **Tier:** 2 — CLI tool + HTTP API  
> **Immediate CLI:** `uv run agent memory search "query"`  
> **Gateway API:** `$SESHAT_API_URL/knowledge/search` (Phase C+)  
> **Auth:** `SESHAT_API_TOKEN` environment variable  
> **ADR:** `docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md`

---

## What This Skill Does

Query and manage Seshat's knowledge graph—the persistent semantic memory that captures entities (concepts, patterns, decisions), their relationships, and metadata. Use this to understand architectural context, past decisions, entity definitions, and connections before implementing features or refactoring code.

---

## When to Use

- You need to understand an entity or concept: "What does Seshat know about the memory promotion pipeline?"
- You want to find related entities: "Find all entities related to 'delegation'"
- You need to store a new fact or decision for future use
- **Prefer** `uv run agent memory entities` instead when you want a quick overview of all entities

---

## Commands

### Immediate CLI (Works Now)

#### Search entities and relationships

```bash
uv run agent memory search "query string"
```

Example:
```bash
uv run agent memory search "memory promotion"
```

Output:
```
Entities matching "memory promotion":
  - EntityName (type: Process)
    Relationships: promotes_to → semantic_memory, triggered_by → consolidation_scheduler
  - Related turns: [Turn 42, Turn 156, Turn 289]
```

#### List all entities (frequency sorted)

```bash
uv run agent memory entities --limit 20 --sort mentions
```

Options:
- `--limit N` – Show top N entities (default: 30)
- `--type TEXT` – Filter by entity type (e.g., `--type Process`, repeatable)
- `--days N` – Only entities seen in last N days (default: 90)
- `--json` – Output as JSON

#### List entities by type

```bash
uv run agent memory entities --type "Decision" --type "Pattern" --limit 10
```

#### Export as JSON for processing

```bash
uv run agent memory entities --json > entities.json
```

---

### Gateway-Ready API (Available After Phase C Deployment)

> **Note:** Phase C deploys the Seshat API Gateway. Until then, use CLI commands above.

#### Search knowledge graph via HTTP

```bash
export SESHAT_API_TOKEN="<your-api-token>"
export SESHAT_API_URL="http://localhost:9000"  # local or https://seshat.example.com after Phase C

# Search entities
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/knowledge/search?q=memory+promotion&limit=5"
```

Response:
```json
{
  "entities": [
    {
      "id": "ent-123",
      "name": "memory_promotion_pipeline",
      "type": "Process",
      "description": "Episodic → semantic conversion flow",
      "relationships": [
        {"type": "triggered_by", "target": "ent-456"}
      ]
    }
  ],
  "turns": [42, 156, 289]
}
```

#### Get entity details

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/knowledge/entities/{entity_id}"
```

#### Create or update a fact

```bash
curl -X POST \
  -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "entity": "delegation_pattern",
    "type": "Pattern",
    "description": "Tasks decomposed when context exceeds 20K tokens",
    "metadata": {
      "discovered_date": "2026-04-14",
      "source": "Claude Code session"
    }
  }' \
  "$SESHAT_API_URL/knowledge/entities"
```

---

## Authentication

**CLI (Immediate):**
No authentication required for local memory queries.

**Gateway API (Phase C+):**
```bash
export SESHAT_API_TOKEN="<your-api-token>"
export SESHAT_API_URL="http://localhost:9000"  # or https://seshat.example.com

# Verify connection
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/health"
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `neo4j_connection_failed` | Neo4j not running (Docker down) | Start services: `./scripts/init-services.sh` |
| `401 Unauthorized` | Missing or invalid `SESHAT_API_TOKEN` | Check token and export: `export SESHAT_API_TOKEN="..."` |
| `404 Not Found` | Entity ID doesn't exist | Use search first: `uv run agent memory search "name"` to get IDs |
| `Empty results` | No entities match query | Try broader search term or list all: `uv run agent memory entities` |

---

## Notes

- Search is full-text fuzzy matching across entity names and descriptions
- Entities are extracted from conversation turns via qwen3-8b entity recognition
- Knowledge graph is persistent across sessions (stored in Neo4j)
- For real-time integration, use the Gateway API after Phase C is deployed
