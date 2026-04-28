# SKILL: Seshat Sessions & Conversation History

> **Auth:** `SESHAT_API_TOKEN` (Gateway API)
> **ADR:** `docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md`

Query conversation history and session context.

---

## Works Now

### CLI (no auth)

```bash
uv run agent memory sessions --last 10
uv run agent memory sessions --last 5 --json > recent_sessions.json
```

### Gateway API

```bash
export SESHAT_API_TOKEN="<your-api-token>"
export SESHAT_API_URL="http://localhost:9000/api/v1"
```

**List recent sessions (GET /sessions)**

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/sessions?limit=10"
```

Response — **bare list** (not a wrapper object):

```json
[
  {
    "id": "sess-1001",
    "created_at": "2026-04-28T06:30:00Z",
    "title": "Memory consolidation discussion",
    "turn_count": 12
  }
]
```

**Get a single session (GET /sessions/{session_id})**

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/sessions/{session_id}"
```

**Get messages from a session (GET /sessions/{session_id}/messages)**

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/sessions/{session_id}/messages?limit=20"
```

Response — **bare list** (not a wrapper object):

```json
[
  {
    "role": "user",
    "content": "How does memory consolidation work?",
    "created_at": "2026-04-28T06:30:05Z"
  },
  {
    "role": "assistant",
    "content": "Memory consolidation is the process...",
    "created_at": "2026-04-28T06:30:15Z"
  }
]
```

---

## 🚫 Planned — not implemented (do not call)

- `GET /sessions/search?topic=delegation` — topic-search endpoint (404)
- `GET /sessions?topics=...` — topic filter on list endpoint (404)

For topic-based queries, use Kibana (`agent-logs-*`, filter by `session_id`) or the CLI.

---

## Error handling

| Error | Cause | Fix |
|-------|-------|-----|
| `401 Unauthorized` | Bad or missing token | `export SESHAT_API_TOKEN="..."` |
| `404 Not Found` | Session archived or wrong ID | List sessions first to get valid IDs |
| `422 Unprocessable` | Invalid session_id format | Must be a valid UUID |
