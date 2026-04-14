# SKILL: Seshat Sessions & Conversation History

> **Tier:** 2 — CLI tool + HTTP API  
> **Immediate CLI:** `uv run agent memory sessions`  
> **Gateway API:** `$SESHAT_API_URL/sessions` (Phase C+)  
> **Auth:** None (CLI) / `SESHAT_API_TOKEN` (Gateway)  
> **ADR:** `docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md`

---

## What This Skill Does

Query conversation history and session context. Sessions represent distinct conversation threads with their own turn history, entities, and dominant topics. Use this to understand prior discussions, retrieve context from past sessions, and see how concepts have evolved over time.

---

## When to Use

- You need context from a previous conversation: "What did we discuss about memory consolidation last week?"
- You want to see all sessions grouped by topic: "List recent sessions about delegation"
- You need message history from a specific session
- You're looking for when a topic was first introduced

---

## Commands

### Immediate CLI (Works Now)

#### List recent sessions with topics

```bash
uv run agent memory sessions --last 10
```

Output:
```
Recent Sessions:
  Session #5 (2026-04-14 06:30 UTC) — 12 turns
    Topics: memory_consolidation, entity_extraction, knowledge_graph
    Created: 2026-04-14T06:30:00Z

  Session #4 (2026-04-13 15:45 UTC) — 8 turns
    Topics: delegation_patterns, task_decomposition
    Created: 2026-04-13T15:45:00Z
```

Options:
- `--last N` – Show last N sessions (default: 10)
- `--json` – Output as JSON

#### Export session data as JSON

```bash
uv run agent memory sessions --last 5 --json > recent_sessions.json
```

---

### Gateway-Ready API (Available After Phase C Deployment)

> **Note:** Phase C deploys the Seshat API Gateway. Until then, use CLI commands above.

#### Set up authentication

```bash
export SESHAT_API_TOKEN="<your-api-token>"
export SESHAT_API_URL="http://localhost:9000"  # local or https://seshat.example.com after Phase C
```

#### List recent sessions

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/sessions?limit=10"
```

Response:
```json
{
  "sessions": [
    {
      "id": "sess-1001",
      "created_at": "2026-04-14T06:30:00Z",
      "turn_count": 12,
      "topics": ["memory_consolidation", "entity_extraction"],
      "messages": 24,
      "entities_mentioned": 18
    }
  ]
}
```

#### Get messages from a specific session

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/sessions/{session_id}/messages?limit=20"
```

Response:
```json
{
  "session_id": "sess-1001",
  "messages": [
    {
      "turn": 1,
      "role": "user",
      "content": "How does memory consolidation work?",
      "timestamp": "2026-04-14T06:30:05Z"
    },
    {
      "turn": 2,
      "role": "assistant",
      "content": "Memory consolidation is the process...",
      "timestamp": "2026-04-14T06:30:15Z"
    }
  ]
}
```

#### Query sessions by topic

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/sessions/search?topic=delegation&limit=5"
```

---

## Authentication

**CLI (Immediate):**
No authentication required.

**Gateway API (Phase C+):**
```bash
export SESHAT_API_TOKEN="<your-api-token>"

# Verify access
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/sessions?limit=1"
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `neo4j_connection_failed` | Neo4j service down | Run: `./scripts/init-services.sh` |
| `401 Unauthorized` | Missing `SESHAT_API_TOKEN` | Export: `export SESHAT_API_TOKEN="..."` |
| `empty results` | No sessions in time range | Check `--last` parameter; may need to increase |
| `Invalid session ID` | Session was archived or doesn't exist | List sessions first: `uv run agent memory sessions --json` |

---

## Notes

- Sessions are immutable once created; conversation history is append-only
- Session topics are extracted via entity recognition from all turns
- Message limit defaults to 20; use `?limit=N` to fetch more
- Sessions older than 90 days may be archived (stored separately, slower queries)
