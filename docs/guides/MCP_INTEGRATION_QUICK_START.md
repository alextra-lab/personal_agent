# MCP Tools — Quick Start

**Audience:** Operators and developers using MCP-backed tools *today*  
**Prerequisites:** Docker (for MCP Gateway), `uv` environment, config per ADR-0011

---

## What you get

The agent can call tools exposed by the **MCP Gateway** (containerized MCP servers) with the same governance and tracing as native tools. ADR: `docs/architecture_decisions/ADR-0011-mcp-gateway-integration.md`.

---

## 1. Configuration

- **Unified settings:** Use `personal_agent.config.settings` — MCP-related fields follow ADR-0007 (no raw `os.environ` in code).
- **Typical env (example):** `MCP_GATEWAY_COMMAND` as a JSON list for the gateway CLI, e.g. `["docker", "mcp", "gateway", "run", ...]` — see `config/` templates and `.env.example` if present.
- **Governance:** Tool allowlists and risk levels live under `config/governance/` (see ADR-0005 patterns).

---

## 2. Run the gateway

1. Ensure Docker is running and the MCP Gateway image/command you use is available.
2. Start (or attach) the gateway process defined in your `MCP_GATEWAY_COMMAND`.
3. Confirm the agent process can reach the gateway socket/stdio as configured.

---

## 3. Verify tools

- On startup, the MCP adapter should **discover** tools and register them with the tool registry.
- Check structured logs for discovery and execution events (include `trace_id` when debugging a request).
- For a minimal sanity check, invoke a **read-only** MCP tool from a dev session and confirm the result and governance audit trail.

---

## 4. Troubleshooting

| Symptom | Check |
|--------|--------|
| No MCP tools listed | Gateway running? `MCP_GATEWAY_COMMAND` valid JSON list? Network/socket path? |
| Tool denied | `config/governance/tools.yaml` and current mode |
| Async/timeouts | MCP client timeouts vs. model timeouts in `config/models.yaml` |

---

## 5. Deeper docs

- **ADR-0011** — Architecture and security model  
- **Completed implementation plan:** `docs/plans/completed/MCP_GATEWAY_IMPLEMENTATION_PLAN_v2.md` (historical step-by-step; behavior may have evolved — prefer code + ADR for truth)

---

**Last updated:** 2026-03-30
