# SKILL: Seshat Reverse Delegation

> **ADR:** `docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md` (D5)

Reverse delegation allows an external agent (Claude Code, Codex) to hand tasks back to Seshat — creating Linear issues, requesting decomposition, or delegating knowledge queries.

---

## Works Now

**Nothing.** The gateway has no `/delegate` route yet (FRE-265 scope). The MCP server returns stubs.

For ad-hoc delegation-like operations from an external agent, use:

```bash
# Create a Linear issue directly
uv run agent "Create a Linear issue: <title>"

# Or use the native Linear tool via the chat API
```

---

## 🚫 Planned — endpoint not implemented (do not call)

The commands below describe the future `/delegate` API. They will return 404 today.
**Do not use them** — calling a non-existent endpoint trains the model to fabricate responses.

<details>
<summary>Future API (Phase C/E)</summary>

**Delegate a task back to Seshat:**

```bash
# ⚠️ 404 TODAY — Future Phase C/E only
curl -X POST \
  -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Create a Linear issue: Refactor memory query path for performance",
    "type": "linear_issue",
    "context": {
      "discovered_during": "memory module code review",
      "urgency": "medium"
    }
  }' \
  "$SESHAT_API_URL/api/v1/delegate"
```

**Check delegation status:**

```bash
# ⚠️ 404 TODAY
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/api/v1/delegate/{delegation_id}/status"
```

</details>
