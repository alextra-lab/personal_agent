# SKILL: Seshat Reverse Delegation

> **Tier:** 2 — CLI tool + HTTP API  
> **Immediate CLI:** Not available yet  
> **Gateway API:** `$SESHAT_API_URL/delegate` (Phase C+)  
> **Auth:** `SESHAT_API_TOKEN` environment variable  
> **ADR:** `docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md` (D5: Reverse Delegation)

---

## What This Skill Does

Delegate tasks back to Seshat from external agent environments (Claude Code, Codex, Cursor). This is reverse delegation (external agent → Seshat) that enables agents working on delegated tasks to hand off sub-tasks, create Linear issues, or request Seshat's orchestration capabilities without leaving their environment.

**Status:** This skill requires **Phase C (API Gateway)** and **Phase E (MCP Server)** deployment. Currently, reverse delegation is not available via CLI.

---

## When to Use

- You're in Claude Code working on a delegated task and need to create a Linear issue for future work
- You want to file a bug discovery back to Seshat's issue tracker
- You need Seshat to decompose a sub-task and return results
- You're done with part of a task and want to delegate the next phase back to Seshat

**Currently unavailable** — Use the main Seshat CLI (`uv run agent ...`) or wait for Phase C/E deployment.

---

## Commands

### Phase C+ Only (API Gateway Deployed)

#### Set up authentication

```bash
export SESHAT_API_TOKEN="<your-api-token>"
export SESHAT_API_URL="http://localhost:9000"  # local or https://seshat.example.com after Phase C
```

#### Delegate a task back to Seshat

```bash
curl -X POST \
  -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Create a Linear issue: Refactor memory query path for performance",
    "type": "linear_issue",
    "context": {
      "discovered_during": "memory module code review",
      "impact": "Improves query latency by 20%",
      "urgency": "medium"
    },
    "metadata": {
      "source_delegation_id": "deleg-5000",
      "agent_name": "Claude Code"
    }
  }' \
  "$SESHAT_API_URL/delegate"
```

Response:
```json
{
  "delegation_id": "deleg-5001",
  "task": "Create a Linear issue: Refactor memory query path for performance",
  "status": "accepted",
  "created_at": "2026-04-14T08:35:22Z",
  "issue_id": "FRE-256"
}
```

#### Delegate a decomposition request (break task into sub-tasks)

```bash
curl -X POST \
  -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Implement Neo4j connection pooling with failover support",
    "type": "decomposition",
    "context": {
      "original_delegation_id": "deleg-5000",
      "reason": "Task larger than single agent capability",
      "constraints": "Must maintain backward compatibility"
    }
  }' \
  "$SESHAT_API_URL/delegate"
```

Response:
```json
{
  "delegation_id": "deleg-5002",
  "status": "decomposed",
  "subtasks": [
    {
      "id": "sub-1",
      "title": "Design connection pool schema",
      "estimated_effort": "2h"
    },
    {
      "id": "sub-2",
      "title": "Implement pooling logic",
      "estimated_effort": "4h"
    }
  ]
}
```

#### Delegate a knowledge query

```bash
curl -X POST \
  -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Search knowledge graph: What patterns exist for cache invalidation?",
    "type": "knowledge_query",
    "query": "cache invalidation patterns"
  }' \
  "$SESHAT_API_URL/delegate"
```

Response:
```json
{
  "delegation_id": "deleg-5003",
  "status": "completed",
  "results": {
    "entities": [
      {
        "name": "ttl_invalidation",
        "type": "Pattern",
        "description": "Time-based cache expiration"
      }
    ]
  }
}
```

#### Check delegation status

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/delegate/{delegation_id}/status"
```

Response:
```json
{
  "delegation_id": "deleg-5001",
  "status": "completed",
  "result": {
    "issue_id": "FRE-256",
    "issue_url": "https://linear.app/frenchforest/issue/FRE-256"
  },
  "completed_at": "2026-04-14T08:36:00Z"
}
```

---

## Authentication

```bash
export SESHAT_API_TOKEN="<your-api-token>"
export SESHAT_API_URL="http://localhost:9000"  # or https://seshat.example.com after Phase C

# Verify delegation API is available
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/delegate/status"
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `502 Bad Gateway` | Seshat API unavailable or Phase C not deployed | Check Phase C deployment status |
| `401 Unauthorized` | Invalid or missing `SESHAT_API_TOKEN` | Export valid token: `export SESHAT_API_TOKEN="..."` |
| `400 Bad Request` | Missing required fields in delegation JSON | Verify `task`, `type`, and `context` are present |
| `delegation_timeout` | Task took too long to process | Check task complexity; consider breaking into smaller delegations |
| `rate_limit_exceeded` | Too many delegations in short time | Wait before retrying; check API quota |

---

## Notes

- **Requires Phase C & Phase E** — Reverse delegation is not available until both phases are deployed
- **Delegation chaining:** External agents can delegate back to Seshat, which can delegate to other agents (multi-hop)
- **Metadata tracking:** All delegations include source agent name and parent delegation ID for audit trails
- **Async by default:** Delegations are asynchronous; use `/status` to poll for results
- **Linear integration:** Task type `linear_issue` automatically creates issues in FrenchForest Linear team
- **Timeout:** Delegations timeout after 5 minutes; for longer tasks, break into smaller chunks
