# Usage Guide

This guide shows how to use the Personal Agent for common tasks. Each fact names the file that states it. Short names in the source notes mean these files: `settings.py` is `src/personal_agent/config/settings.py`, `service_cli.py` is `src/personal_agent/ui/service_cli.py`, and `models.py` is `src/personal_agent/service/models.py`.

## Table of Contents

- [Quick Start](#quick-start)
- [Basic Usage](#basic-usage)
- [API Endpoints](#api-endpoints)
- [Configuration](#configuration)
- [Common Tasks](#common-tasks)
- [Troubleshooting](#troubleshooting)
- [Advanced Usage](#advanced-usage)

## Quick Start

### 1. Start the infrastructure

```bash
make up
```

This starts the services in `docker-compose.yml`: PostgreSQL, Elasticsearch, Neo4j, SearXNG, Tempo, the OpenTelemetry collector, Grafana and Redis. In the `local` environment, the target also runs `scripts/init-services.sh`. Source: `Makefile` (targets `up` and `help`), `docker-compose.yml`.

### 2. Start the local model server

The local model runs on llama.cpp. The server is not part of this repository, and it does not run on the VPS. Source: `config/models.yaml` (provider `slm_local`), `CLAUDE.md` (request flow).

Set the origin of the server in `.env`:

```bash
AGENT_SLM_BASE_URL=<origin of the llama.cpp server>
```

Use an origin with no `/v1` suffix. Source: `src/personal_agent/config/settings.py`, field `slm_base_url`.

### 3. Start the Personal Agent service

```bash
make dev
```

The target runs `uvicorn` with hot reload on port 9000. Source: `Makefile`, target `dev`.

### 4. Verify the services

```bash
# Check the Personal Agent
make health
curl http://localhost:9000/health

# Check the local model server
curl "$AGENT_SLM_BASE_URL/health"

# View the API docs
open http://localhost:9000/docs
```

`/health` returns `{"status": "healthy", "build_fingerprint": ..., "components": {...}}`. The components are `database`, `elasticsearch`, `neo4j`, `second_brain` and `mcp_gateway`. Source: `src/personal_agent/service/app.py`, function `health_check`.

The health URL of the model server is the origin of `AGENT_SLM_BASE_URL` plus `/health`. The setting `AGENT_SLM_HEALTH_URL` overrides it. Source: `settings.py`, property `resolved_slm_health_url`.

## Basic Usage

### Conversation-first CLI

Use the `agent` command. The entry point is `agent = personal_agent.ui.service_cli:app`. Source: `pyproject.toml`, `src/personal_agent/ui/service_cli.py`.

```bash
# First message (creates and stores a session automatically)
uv run agent chat "Hello, can you help me plan my day?"

# Follow-up in the same conversation (reuses the stored session)
uv run agent chat "Now turn that into a 3-item checklist"
```

A message without the `chat` subcommand does not work. The CLI reads the message as an unknown subcommand and exits with an error. Source: `service_cli.py`, function `main` and function `chat`.

### Start a new conversation

```bash
uv run agent chat "Let's start a new topic" --new
```

### Choose the model for one turn

```bash
uv run agent chat "Summarize this" --model claude_sonnet
```

The value is a deployment key from `config/models.yaml`. If you omit `--model`, the turn uses the selection of the session. A new session uses the default of the `primary` role in `config/model_roles.yaml`. Source: `service_cli.py`, function `chat`, `config/model_roles.yaml`.

### Inspect or rotate the session id

```bash
uv run agent session
uv run agent session new
```

Source: `service_cli.py`, functions `session_show` and `session_new`.

### Memory commands

```bash
uv run agent memory --help
```

Source: `service_cli.py` (the `memory` sub-application).

### Service URL

The CLI reads the setting `service_url`. The environment variable is `SERVICE_URL`, not `AGENT_SERVICE_URL`, because the field declares the alias `SERVICE_URL`. The default is `http://localhost:9000`. Source: `settings.py`, field `service_url`.

```bash
export SERVICE_URL=http://localhost:9000
```

### Create a session

```bash
curl -X POST http://localhost:9000/sessions \
  -H "Content-Type: application/json" \
  -d '{"channel": "CLI", "mode": "NORMAL"}'
```

The request body has the fields `channel` (optional string), `mode` (default `NORMAL`), `metadata` and `seshat_effort`. Source: `src/personal_agent/service/models.py`, class `SessionCreate`.

Response (abridged):
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "channel": "CLI",
  "mode": "NORMAL",
  "created_at": "2026-01-26T12:00:00Z",
  "last_active_at": "2026-01-26T12:00:00Z",
  "metadata": {},
  "messages": []
}
```

The full response also has `primary_model_at_creation`, `model_config_path` and `purged_at`. Source: `models.py`, class `SessionResponse`.

### Send a chat message

```bash
curl -X POST "http://localhost:9000/chat?message=Hello&session_id=YOUR_SESSION_ID"
```

Response:
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "response": "Hello! How can I help you today?",
  "trace_id": "abc123",
  "primary_selection": "qwen3.8-flash-next",
  "seshat_effort": "standard"
}
```

The response has no `usage` field. The value of `primary_selection` is the deployment key that ran the turn. The value of `seshat_effort` is `fast`, `standard` or `deep`. The values shown are examples. Source: `service/app.py`, function `chat`, `src/personal_agent/config/seshat_effort.py` (type `EffortLevel`).

The `/chat` endpoint also accepts the query parameters `model`, `channel` and `seshat_effort`. Source: `service/app.py`, function `chat`.

### List sessions

```bash
curl http://localhost:9000/api/v1/sessions \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H "cf-access-authenticated-user-email: $OWNER_EMAIL"
```

The `/api/v1/sessions` routes need two things: a bearer token with the `sessions:read` scope, and a Cloudflare Access identity. If `AGENT_GATEWAY_AUTH_ENABLED` is off, send the header `cf-access-authenticated-user-email`. If it is on, the request must carry a verified `Cf-Access-Jwt-Assertion`. A request without an identity gets 401. Source: `src/personal_agent/gateway/session_api.py` (module docstring), `src/personal_agent/service/auth.py`, function `resolve_request_email`.

Returns the 20 most recent sessions, ordered by last activity. Use `?limit=N` to change the number. Each entry has `session_id`, `created_at`, `last_active_at`, `mode`, `channel`, `message_count`, `turn_count`, `title` (the first user message, cut to 60 characters, with `…` added if it was cut), `session_label` and `session_digest`. Source: `src/personal_agent/gateway/session_api.py`, functions `list_sessions` and `_session_to_dict`.

### Resume a session

The PWA does this automatically with the `/c/{session_id}` URL. Use these calls for the same result through the API:

```bash
# Fetch the history of an existing session
curl "http://localhost:9000/api/v1/sessions/YOUR_SESSION_ID/messages?limit=200" \
  -H "Authorization: Bearer $GATEWAY_TOKEN"

# Continue the conversation with the same session_id
curl -X POST http://localhost:9000/chat \
  -G --data-urlencode "message=What did we discuss earlier?" \
  --data-urlencode "session_id=YOUR_SESSION_ID"
```

The backend loads the prior message history before it calls the model. Conversation memory stays across browser reloads, devices and sessions.

## API Endpoints

Source for this section: `src/personal_agent/service/app.py` and `src/personal_agent/gateway/session_api.py`.

### Health check

```bash
GET /health
```

Returns the service health. See "Verify the services" for the response.

### Create session

```bash
POST /sessions
Content-Type: application/json

{
  "channel": "CLI",
  "mode": "NORMAL"
}
```

### Chat

```bash
POST /chat?message=<message>&session_id=<session_id>
```

Sends a message to the agent and waits for the response.

### Chat stream (PWA)

```bash
POST /chat/stream
Content-Type: application/x-www-form-urlencoded

message=<message>&session_id=<session_id>
```

The form fields are `message`, `session_id`, `primary_selection`, `seshat_effort`, `client_msg_id` and `attachments`. The call returns at once with `{"session_id", "status", "primary_selection", "selection_provenance", "seshat_effort", "effort_provenance"}`. The reply arrives as events on the WebSocket `/ws/{session_id}`. See [AG-UI Streaming Reference](AGUI_STREAMING_REFERENCE.md).

### List sessions

```bash
GET /api/v1/sessions?limit=20
Authorization: Bearer <token>
```

### Get session messages

```bash
GET /api/v1/sessions/{session_id}/messages?limit=200
Authorization: Bearer <token>
```

Returns the last `limit` messages (default 50) in chronological order. Each message has `role`, `content`, `timestamp`, `trace_id` and `metadata.source`. The call returns 404 if the session does not exist.

### Change the model or the effort of a session

```bash
PATCH /api/v1/sessions/{session_id}/selection
PATCH /api/v1/sessions/{session_id}/effort
```

### Get session

```bash
GET /sessions/{session_id}
```

Returns the session details and the history (local service path).

## Configuration

See the [Configuration Guide](CONFIGURATION.md) for the full description. Every setting uses the prefix `AGENT_`, except the aliases that `settings.py` declares.

### Environment variables

```bash
# Local model server (no default; an origin without /v1)
AGENT_SLM_BASE_URL=<origin of the llama.cpp server>

# Model calls
AGENT_LLM_TIMEOUT_SECONDS=120
AGENT_LLM_MAX_RETRIES=3

# Service
AGENT_SERVICE_PORT=9000

# Database
AGENT_DATABASE_URL=postgresql+asyncpg://<user>:<password>@localhost:5432/personal_agent

# Elasticsearch
AGENT_ELASTICSEARCH_URL=http://localhost:9200

# MCP gateway (optional)
AGENT_MCP_GATEWAY_ENABLED=false
AGENT_MCP_GATEWAY_COMMAND='["docker", "mcp", "gateway", "run"]'
```

Source: `settings.py`, fields `slm_base_url`, `llm_timeout_seconds`, `llm_max_retries`, `service_port`, `database_url`, `elasticsearch_url`, `mcp_gateway_enabled` and `mcp_gateway_command`.

### Owner Identity Setup (FRE-213 / ADR-0052)

Set your display name so the agent greets you by name and never burns tool calls trying to infer who you are:

```bash
# Add to /opt/seshat/.env (VPS) or .env (local dev)
AGENT_OWNER_NAME=Alex
AGENT_AGENT_ID=seshat-local   # default — only change for multiple deployments
```

On the next restart (`make restart SERVICE=seshat-gateway`), the service will:

1. Resolve your `user_id` from the `users` table (keyed on `AGENT_OWNER_EMAIL`)
2. `MERGE` a `:Person {is_owner: true, name: "Alex", user_id: …}` node in Neo4j
3. Inject `## Operator\nYou are assisting Alex.` into every system prompt

All other CF Access users are provisioned automatically on their first authenticated request — their `:Person` node is created with their email local-part as a temporary display name until `users.display_name` is populated.

**Verification** after restart:

```bash
# Ask the agent
uv run agent chat "who am I?" --new
# Expect: "You're Alex" — zero tool iterations in the trace

# Check the graph
docker compose exec neo4j cypher-shell -u neo4j -p "$AGENT_NEO4J_PASSWORD" \
  'MATCH (a:Agent {id: "seshat-local"})-[:OPERATED_BY]->(p) RETURN p.name, p.is_owner, p.user_id'
```

### Identity Setup — Cloudflare Access (multi-user / FRE-268)

If the service runs behind Cloudflare Access (recommended for any shared deployment), add the following to `.env`:

```bash
# ── Inbound Identity (Cloudflare Access) ─────────────────────────────────────
# CRITICAL: AGENT_OWNER_EMAIL must match the deployment owner's CF Access email
# so that CLI paths and CF Access paths resolve to the same user_id in Postgres.
AGENT_OWNER_EMAIL=your-cf-access-email@example.com

# Optional: enable JWT verification of Cf-Access-Jwt-Assertion (defense-in-depth)
# CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
# CF_ACCESS_AUD=<application-audience-tag-from-cf-dashboard>
```

Before starting the service for the first time after upgrading, run the one-time migration:

```bash
uv run python scripts/migrate_fre268_add_user_identity.py
```

This creates the `users` table, adds `user_id` to `sessions`, and backfills all existing sessions to the deployment owner. The script is idempotent — safe to re-run.

**Without Cloudflare Access** (local dev / CLI): set `AGENT_GATEWAY_AUTH_ENABLED=false` (default) and `AGENT_OWNER_EMAIL`. The CLI will resolve to the owner identity automatically. Session isolation is enforced in production via the CF Access email header.

See [ADR-0064](../architecture_decisions/ADR-0064-inbound-user-identity-cloudflare-access.md) for the full decision record.

### Memory Visibility Setup (FRE-229)

Memory nodes in Neo4j now carry a `visibility` property: `public` (world/shared knowledge), `group` (all CF Access users — the household), or `private:<user_id>` (reserved, not yet assigned). New writes from authenticated sessions default to `group`; CLI/unauthenticated writes default to `public`.

**One-time Neo4j backfill** — tag all existing nodes `public` to match their original behaviour:

```bash
uv run python scripts/migrate_fre229_visibility_backfill.py
```

Run this after deploying the FRE-229 build. The service has an IS NULL grace clause so existing data remains readable until the backfill runs, but running it promptly makes the data model canonical. The script is idempotent.

**Adding a household member** — add their email to the Cloudflare Access email policy. No admin console change needed inside the agent; group membership = CF Access policy.

**Visibility at query time** is applied automatically by the chokepoint filter in `memory/service.py`. Callers never need to filter manually — the `user_id` + `authenticated` flags flow from the request through the gateway pipeline to every Cypher query.

### Governance configuration

Edit the files in `config/governance/` to change the policies:

- **modes.yaml**: the operating modes and the transition rules
- **tools.yaml**: the tool permissions and risk levels
- **permissions.yaml**: the permission rules (read by the tool executor, `src/personal_agent/tools/executor.py`)

Source: `config/governance/`, `src/personal_agent/config/governance_loader.py` (modes and tools).

## Common Tasks

### Ask a question that needs a tool

```bash
curl -X POST "http://localhost:9000/chat" \
  -G --data-urlencode "message=Search the web for python async libraries" \
  --data-urlencode "session_id=YOUR_SESSION_ID"
```

The agent chooses the tools. Tier 1 tools such as `search_memory`, `recall_personal_history` and `web_search` are registered in `src/personal_agent/tools/__init__.py` (function `register_mvp_tools`). The tool policy is in `config/governance/tools.yaml`.

### Memory queries

The memory graph is off by default. Set `AGENT_ENABLE_MEMORY_GRAPH=true` to enable it. Source: `settings.py`, field `enable_memory_graph`.

```bash
curl -X POST "http://localhost:9000/chat" \
  -G --data-urlencode "message=What topics have we discussed about Python?" \
  --data-urlencode "session_id=YOUR_SESSION_ID"
```

### MCP tools

If the MCP gateway is enabled, the agent can use containerized MCP tools:

```bash
# Enable in .env
AGENT_MCP_GATEWAY_ENABLED=true

# Restart the service, then ask for an MCP tool
```

Tier 3 (MCP) tools need an ADR. See `docs/reference/TOOL_INTEGRATION_GUIDE.md` and [MCP Integration Quick Start](MCP_INTEGRATION_QUICK_START.md).

## Troubleshooting

### Service does not start

```bash
# Check if the port is in use
lsof -i :9000

# Check the Docker services
make ps

# View the logs
make logs SERVICE=postgres
make logs SERVICE=elasticsearch
```

Source: `Makefile` (targets `ps` and `logs`).

### Database connection issues

```bash
# Open a PostgreSQL shell
make shell SERVICE=postgres
```

Run `\dt` in the shell to list the tables. The schema is in `docker/postgres/init.sql`. Source: `Makefile` (target `shell`).

### The model server does not respond

```bash
# Check the model server health
curl "$AGENT_SLM_BASE_URL/health"
```

If the command fails, check that `AGENT_SLM_BASE_URL` is set and that the llama.cpp server runs on the Mac. The model server logs are on the Mac, not on this host. Source: `settings.py`, property `resolved_slm_health_url`.

### MCP gateway issues

**Issue**: MCP tools are not available.

**Solutions**:
1. Verify that Docker runs: `docker ps`
2. Check the setting: `AGENT_MCP_GATEWAY_ENABLED=true` in `.env`
3. Check the logs for initialization errors
4. Check the `mcp_gateway` component in the `/health` response

**Known limitations**:
- MCP integration needs async execution. The service handles this automatically.
- Some async cleanup warnings can appear in the logs. They are harmless.
- Docker must run for the MCP gateway to work.

### Memory service issues

**Issue**: Memory queries return empty results.

**Solutions**:
1. Verify that Neo4j runs: `make ps SERVICE=neo4j`
2. Check the `neo4j` component in the `/health` response
3. Verify that the memory graph is on: `grep AGENT_ENABLE_MEMORY_GRAPH .env`
4. Check the memory service logs

## Advanced Usage

### Custom tools

A tool has a `ToolDefinition` and an async executor that returns a `ToolResult`. Register both in `register_mvp_tools`, then add the policy in `config/governance/tools.yaml`.

```python
from personal_agent.tools.types import ToolResult

async def my_tool_executor(arg1: str, arg2: int) -> ToolResult:
    # Your tool logic
    return ToolResult(
        tool_name="my_tool",
        success=True,
        output="Result",
        latency_ms=0.0,
    )
```

Source: `src/personal_agent/tools/types.py` (classes `ToolDefinition` and `ToolResult`), `src/personal_agent/tools/__init__.py`. The tool tiers are in `docs/reference/TOOL_INTEGRATION_GUIDE.md`.

### Models and roles

`config/models.yaml` is the catalog. Each key under `models:` is a deployment. The `providers:` section declares the backends, such as `slm_local` for the local llama.cpp server. A role is a job. `config/model_roles.yaml` binds each role to one deployment.

```yaml
# config/model_roles.yaml
bindings:
  primary: { deployment: <deployment key>, open: true }
```

An `open: true` role can be selected by the user. There is no router model. Source: `config/models.yaml`, `config/model_roles.yaml`.

### Observability

- **Logs**: structured JSON logs in `telemetry/logs/`, and Elasticsearch
- **Metrics and events**: Elasticsearch (`agent-logs-*`)
- **Traces**: Tempo, with `trace_id` correlation
- **Grafana**: `http://localhost:3000` with the local compose file. The cloud stack publishes Grafana on `127.0.0.1` at the host port `GRAFANA_HOST_PORT` (default 3001), and the PWA uses port 3000.

Source: `docker-compose.yml`, `docker-compose.cloud.yml`, `config/otel/`.

## Next Steps

- See the [Configuration Guide](CONFIGURATION.md) for the detailed configuration
- See the [Architecture Documentation](../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md) for the system design
- See [Contributing](../../README.md#contributing) for the development setup
