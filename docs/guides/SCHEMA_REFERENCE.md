# Seshat Schema Reference

> **Last updated**: 2026-10-30
> **Applies to**: `src/personal_agent/service/app.py`, `src/personal_agent/transport/`, `config/`, `docker/postgres/init.sql`

Formal definitions for the schema boundaries in Seshat: the API endpoints, the AG-UI wire events, the configuration YAML files and the PostgreSQL database. Each section names its source files.

---

## Table of Contents

1. [API Endpoint Schemas](#1-api-endpoint-schemas)
   - [POST /chat/stream](#post-chatstream)
   - [WS /ws/{session_id}](#ws-wssession_id)
   - [POST /chat (synchronous, CLI)](#post-chat-synchronous-cli)
   - [Session CRUD](#session-crud)
   - [GET /health](#get-health)
2. [AG-UI Wire Event Schema](#2-ag-ui-wire-event-schema)
3. [Configuration YAML Schemas](#3-configuration-yaml-schemas)
   - [Model catalog (config/models.yaml)](#model-catalog-configmodelsyaml)
   - [Role bindings (config/model_roles.yaml)](#role-bindings-configmodel_rolesyaml)
4. [PostgreSQL Schema](#4-postgresql-schema)
5. [Infrastructure as code](#5-infrastructure-as-code)

---

## 1. API Endpoint Schemas

Base URL: `http(s)://<host>`. Caddy reverse-proxies `/api/*`, `/chat`, `/chat/stream`, `/ws/*`, `/docs` and `/openapi.json` to the gateway on port 9001. Source: `config/cloud-sim/Caddyfile` (the `routing` snippet).

### `POST /chat/stream`

**Content-Type**: `application/x-www-form-urlencoded`

Fire-and-forget endpoint for the PWA. It returns at once. The events arrive on `WS /ws/{session_id}`.

#### Request fields (form-encoded)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `message` | string | yes | none | The text of the user message |
| `session_id` | string | yes | none | The target session |
| `primary_selection` | string | no | none | A deployment key from `config/models.yaml`. The server uses it only to set up a new session. |
| `seshat_effort` | `fast` \| `standard` \| `deep` | no | none | The effort level of the turn |
| `client_msg_id` | string | no | none | A client-generated message identifier |
| `attachments` | JSON string | no | none | The attachments of the message |

Source: `src/personal_agent/service/app.py`, function `chat_stream_endpoint`. The type `EffortLevel` is in `src/personal_agent/config/seshat_effort.py`.

#### Response (200 OK)

```json
{
  "session_id": "<id>",
  "status": "streaming",
  "primary_selection": "<deployment key>",
  "selection_provenance": "<how the selection was chosen>",
  "seshat_effort": "<level>",
  "effort_provenance": "<how the level was chosen>"
}
```

The response has no `trace_id`. The `trace_id` of the turn arrives in the `DONE` event and in other events.

#### Errors

| Code | Condition |
|------|-----------|
| `503` | A budget cap stopped the turn (`error: "budget_denied"`) |

Source: `seshat-pwa/src/lib/agui-client.ts`, function `sendChatMessage`.

There is no `profile` field. The system has no execution profiles.

---

### `WS /ws/{session_id}`

The WebSocket carries all AG-UI events. The client first mints a ticket with `POST /api/ws-ticket`, and then connects to `/ws/{session_id}?ticket=<ticket>`. The first client message must be `CONNECT`.

The messages, the close codes and the replay rules are in [AG-UI Streaming Reference](AGUI_STREAMING_REFERENCE.md). Source: `src/personal_agent/transport/agui/ws_endpoint.py`, `service/app.py` (function `mint_ws_ticket_endpoint`).

#### `POST /api/ws-ticket`

**Request body** (`application/json`): `{"session_id": "<UUID>"}`

**Response** (200): `{"ticket": "<ticket>", "expires_in": 30}`

| Code | Condition |
|------|-----------|
| `422` | `session_id` is not a valid UUID |
| `404` | The session does not exist for this user |

---

### `POST /chat` (synchronous, CLI)

**Content-Type**: query parameters

The CLI command `uv run agent chat "<message>"` calls this endpoint. The endpoint runs the full pipeline and returns the reply in the response. Source: `src/personal_agent/ui/service_cli.py`, `service/app.py`, function `chat`.

#### Request parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message` | string | yes | none | The text of the user message |
| `session_id` | string | no | none | An existing session UUID |
| `model` | string | no | none | A deployment key for this turn |
| `channel` | string | no | `CHAT` | The client channel |
| `seshat_effort` | `fast` \| `standard` \| `deep` | no | none | The effort level of the turn |

#### Response (200 OK)

```json
{
  "session_id": "<id>",
  "response": "The assistant's reply text",
  "trace_id": "<trace id>",
  "primary_selection": "<deployment key>",
  "seshat_effort": "<level>"
}
```

---

### Session CRUD

Source: `service/app.py` (routes under `/sessions`), `src/personal_agent/service/models.py` (classes `SessionCreate`, `SessionUpdate`, `SessionResponse`), `src/personal_agent/gateway/session_api.py` (routes under `/api/v1/sessions`).

#### `POST /sessions`

Create a new session. `POST /chat/stream` also creates a session for a new `session_id`.

**Request body** (`application/json`):

```json
{
  "channel": "pwa",
  "mode": "NORMAL",
  "metadata": {},
  "seshat_effort": "standard"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `channel` | string \| null | no | `null` | The client identifier, such as `pwa` or `CLI` |
| `mode` | string | no | `"NORMAL"` | The session mode |
| `metadata` | object | no | `{}` | Arbitrary client metadata |
| `seshat_effort` | `fast` \| `standard` \| `deep` \| null | no | `null` | The stored effort level. `null` stores the default level from `config/seshat_effort.yaml`. |

**Response** (`SessionResponse`, 200):

```json
{
  "session_id": "<UUID>",
  "created_at": "2026-10-30T12:00:00Z",
  "last_active_at": "2026-10-30T12:00:00Z",
  "mode": "NORMAL",
  "channel": "pwa",
  "metadata": {},
  "messages": [],
  "primary_model_at_creation": "<deployment key>",
  "model_config_path": "<path>",
  "purged_at": null
}
```

#### `GET /sessions/{session_id}`

Return the session by UUID. The response has the shape of `SessionResponse`.

#### `PATCH /sessions/{session_id}`

Update session fields. All fields are optional. The server updates only the fields that the request contains.

```json
{
  "mode": "NORMAL",
  "channel": "cli",
  "metadata": {"key": "value"},
  "messages": [{"role": "user", "content": "Hello", "timestamp": "..."}]
}
```

#### `GET /sessions`

Return a list of sessions (`list[SessionResponse]`). This is the local service path.

#### `GET /api/v1/sessions`

Return the recent sessions, ordered by `last_active_at` in descending order. The call needs the `sessions:read` scope and a Cloudflare Access identity (the header `cf-access-authenticated-user-email`, or a verified `Cf-Access-Jwt-Assertion` if `gateway_auth_enabled` is on). A request without an identity gets 401. Source: `gateway/session_api.py`, `service/auth.py`.

**Query parameters**: `limit` (default 20)

**Response** (200):

```json
[
  {
    "session_id": "<UUID>",
    "created_at": "2026-10-30T10:00:00Z",
    "last_active_at": "2026-10-30T10:05:00Z",
    "mode": "NORMAL",
    "channel": "CHAT",
    "message_count": 6,
    "turn_count": 3,
    "title": "Hello, my name is Boris",
    "session_label": null,
    "session_digest": null
  }
]
```

The server derives `title` from the first user message, cut to 60 characters, with `…` added if it was cut. The fields `session_label` and `session_digest` come from the Neo4j session digest. They are `null` when no digest exists.

#### `GET /api/v1/sessions/{session_id}/messages`

Return the last `limit` messages of a session in chronological order. The call needs the `sessions:read` scope and the same identity as the list call.

**Query parameters**: `limit` (default 50)

The call returns 404 if the session does not exist. The message element schema is in section 4.

#### Other session routes

| Route | Purpose |
|-------|---------|
| `GET /api/v1/sessions/{session_id}` | Return one session summary |
| `GET /api/v1/sessions/{session_id}/config` | Return the model and effort configuration of the session |
| `PATCH /api/v1/sessions/{session_id}/selection` | Change the model selection of the session |
| `PATCH /api/v1/sessions/{session_id}/effort` | Change the effort level of the session |
| `GET /api/v1/config` | Return the configuration that does not depend on a session |

Source: `src/personal_agent/gateway/session_api.py`.

---

### `GET /health`

Health check endpoint. The Docker Compose health check calls it.

**Response** (200 OK):

```json
{
  "status": "healthy",
  "build_fingerprint": "<fingerprint or null>",
  "components": {
    "database": "connected",
    "elasticsearch": "connected",
    "neo4j": "connected",
    "second_brain": "running",
    "mcp_gateway": "disconnected"
  }
}
```

The value of each component is a state string. Source: `service/app.py`, function `health_check`.

---

## 2. AG-UI Wire Event Schema

The event envelope, the 12 event types and the control messages are defined once, in [AG-UI Streaming Reference](AGUI_STREAMING_REFERENCE.md). This guide does not repeat them.

Short summary. Source: `src/personal_agent/transport/agui/adapter.py`, function `to_agui_event`.

- Most events have this envelope: `{"type": "<TYPE>", "data": {...}, "session_id": "<id>", "seq": <integer or null>}`.
- The types are `TEXT_DELTA`, `TOOL_CALL_START`, `TOOL_CALL_END`, `PHASE_START`, `PHASE_END`, `STATE_DELTA`, `INTERRUPT`, `tool_approval_request`, `CONSTRAINT_PAUSE`, `CONSTRAINT_RESOLVED`, `CANCELLED` and `RUN_ERROR`.
- The control messages are `DONE`, `PONG`, `REPLAY_COMPLETE` and `REPLAY_GAP`.
- The Python event classes are frozen dataclasses in `src/personal_agent/transport/events.py`.

---

## 3. Configuration YAML Schemas

The model configuration has two files. `config/models.yaml` is the catalog. `config/model_roles.yaml` binds each role to one deployment. The system has no execution profiles, and there is no `config/profiles/` directory. The Pydantic classes are in `src/personal_agent/llm_client/models.py`.

### Model catalog (`config/models.yaml`)

Top-level keys:

| Key | Type | Description |
|-----|------|-------------|
| `providers` | map[string, ProviderDefinition] | One entry for each backend |
| `models` | map[string, ModelDefinition] | One entry for each deployment. The key is the deployment key. |

Source: `llm_client/models.py`, class `ModelConfig`.

#### `providers` entry (`ProviderDefinition`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `base_url` | string \| null | no | The base URL. SDK-dispatched providers have none. |
| `auth_env` | string \| null | no | The name of the `AppConfig` field that holds the credential |
| `placement` | `local` \| `cloud` | yes | Where the backend runs |
| `max_concurrency` | int | yes | The total in-flight cap across the deployments of the provider |
| `summary` | string | no | A one-line description |
| `dialect` | string \| null | no | The wire vocabulary of the provider. The values are the members of `Dialect`. |

The providers in `config/models.yaml` are `slm_local`, `ovh`, `ovhcloud`, `scaleway`, `openai`, `anthropic` and `voyage`. The provider `slm_local` is the local llama.cpp server. Its address comes from `AGENT_SLM_BASE_URL`.

#### `models` entry (`ModelDefinition`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | The model identifier at the provider |
| `provider` | string | no | A key in `providers` |
| `kind` | `llm` \| `embedding` \| `reranker` | no | The kind of model |
| `context_length` | int | yes | The maximum context window in tokens |
| `max_concurrency` | int | yes | The maximum number of concurrent requests |
| `min_concurrency` | int | no | The floor for adaptive concurrency |
| `default_timeout` | int | yes | The request timeout in seconds |
| `max_timeout` | int | no | A ceiling on the timeout that any role resolves to |
| `max_tokens` | int | no | The maximum number of output tokens. Cloud models are billed per token, so set the value deliberately. |
| `endpoint` | string | no | A base URL override. The default is `settings.slm_base_url`. |
| `quantization` | string | no | The quantization label of a local model |
| `dialect` | string | no | An override of the provider dialect |
| `modes` | map[string, ModeSpec] | yes for `kind: llm` | The named sampler and thinking presets |
| `default_mode` | string | yes if `modes` is set | The mode that a caller gets when it names none. The loader rejects `modes` without it. |
| `supports_function_calling` | bool | no | Whether the model supports tool calls |
| `tool_calling_strategy` | `native` \| `prompt` \| `disabled` | no | How the client sends tool calls |
| `parallel_tool_calls` | bool | no | Whether the model can return several tool calls in one turn |

Source: `llm_client/models.py`, classes `ModelDefinition`, `ProviderDefinition`, `ToolCallingStrategy` and `ModelKind`. The header comment of `config/models.yaml` describes each field.

**Sampler and thinking values (ADR-0145 D3a).** The fields `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `repeat_penalty`, `reasoning_effort`, `effort` and the thinking lever are fields of `ModeSpec`. A deployment declares them inside `modes:`, in the vocabulary of its resolved `dialect`. A mode is the only declarative home for these values. A call site can pass one to `respond()` as an override. A field that the dialect does not accept raises `DialectParameterRejected` before dispatch.

**Example entry** (the values are illustrative):

```yaml
models:
  example-deployment:
    id: "<model id at the provider>"
    provider: "anthropic"
    kind: llm
    context_length: 200000
    max_tokens: 8192
    max_concurrency: 5
    default_timeout: 120
    modes:
      default: { }
    default_mode: default
```

---

### Role bindings (`config/model_roles.yaml`)

The file has a `bindings:` map. Each key is a role. Each value is a `RoleBinding`.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `deployment` | string | yes | A key in `models:` of `config/models.yaml`, or `inherit` (the `sub_agent` role uses it) |
| `open` | bool | no (default `false`) | If `true`, the user can select the deployment for this role. A role without `open` is pinned. |
| `max_tokens` | int | no | A per-use output cap |
| `mode` | string | no | The named mode that this role asks for |
| `default_timeout` | int | no | A per-use timeout |
| `priority` | `InferencePriority` | no | The inference priority of the role |

Example. The role `primary` is open. The role `entity_extraction` is pinned.

```yaml
bindings:
  primary:           { deployment: <deployment key>, open: true }
  entity_extraction: { deployment: <deployment key> }
```

Source: `llm_client/models.py`, class `RoleBinding`; `config/model_roles.yaml`. The role names are the keys of `bindings:` in that file.

---

## 4. PostgreSQL Schema

Database: `personal_agent`
Connection: `AGENT_DATABASE_URL` (the restricted `seshat_app` role). Source: `src/personal_agent/config/settings.py`, field `database_url`.

The schema is in `docker/postgres/init.sql`. Changes for an existing database are in `docker/postgres/migrations/`. The project has no Alembic. Run the migrations as the `agent` superuser with `AGENT_DATABASE_ADMIN_URL`.

The `public` schema has 18 tables, and the `sysgraph` schema has the tables of the self-improvement store.

| Table | Purpose |
|-------|---------|
| `sessions` | The conversations |
| `metrics` | Time-series metrics |
| `captains_log_captures` | Captain's Log captures |
| `captains_log_reflections` | Captain's Log reflections |
| `api_costs` | The cost of each paid API call |
| `route_traces` | One row for each turn: the decision of the gateway and what the harness did |
| `budget_policies`, `budget_counters`, `budget_reservations` | The budget gate |
| `consolidation_attempts` | The memory consolidation attempts |
| `kg_stats` | Knowledge graph statistics |
| `grounding_compliance_observations`, `grounding_enforcement_state` | The grounding checks |
| `users` | The user identities |
| `artifacts` | The artifact metadata |
| `session_events` | The stored AG-UI events for replay |
| `user_constraint_preferences` | The saved answers to constraint pauses |
| `session_model_selections` | The model selection of each session and role |

Source: `docker/postgres/init.sql` (`CREATE TABLE` statements).

### `sessions` table

| Column | PostgreSQL type | Nullable | Default | Description |
|--------|----------------|----------|---------|-------------|
| `session_id` | `uuid` | no | `gen_random_uuid()` | Primary key |
| `created_at` | `timestamptz` | no | `NOW()` | Session creation time |
| `last_active_at` | `timestamptz` | no | `NOW()` | Last activity time |
| `mode` | `varchar(20)` | no | `'NORMAL'` | Session mode |
| `channel` | `varchar(50)` | yes | `NULL` | Client identifier |
| `metadata` | `jsonb` | yes | `'{}'` | Arbitrary client metadata |
| `messages` | `jsonb` | yes | `'[]'` | Array of `{role, content, timestamp, metadata}` objects |
| `user_id` | `uuid` | no | none | Session owner. Foreign key to `users(user_id)`. Index `ix_sessions_user_id`. |
| `primary_model_at_creation` | `varchar(120)` | yes | `NULL` | Model at session creation (ADR-0074) |
| `model_config_path` | `varchar(255)` | yes | `NULL` | Model configuration path at session creation (ADR-0074) |
| `execution_profile` | `varchar(50)` | no | `'local'` | A leftover column. The application does not read it. |
| `purged_at` | `timestamptz` | yes | `NULL` | Retention tombstone. The retention sweep sets it and clears `messages`. A resumed session clears it. (ADR-0098) |
| `last_event_seq` | `integer` | no | `0` | The allocator for `session_events.seq` (ADR-0075) |
| `revealed_tools` | `text[]` | no | `'{}'` | The skill tools that `read_skill` revealed in this session (ADR-0156). Only the server writes it. |
| `seshat_effort` | `varchar(16)` | no | `'standard'` | The effort level of the session. A check constraint limits it to `fast`, `standard` and `deep` (ADR-0157). |

Source: `docker/postgres/init.sql`, `src/personal_agent/service/app.py` (the code comment on `execution_profile`).

#### `messages` JSONB element schema

```json
{
  "role": "user | assistant | system | tool",
  "content": "string",
  "timestamp": "ISO 8601 datetime (UTC)",
  "trace_id": "UUID string - identifies the request turn in traces and Captain's Log",
  "metadata": {
    "source": "service.app",
    "model": "<deployment id>"
  }
}
```

Every message that the service persisted after FRE-235 (2026-04-26) carries `trace_id`, `timestamp` and `metadata.source`. These fields link `session_id → trace_id → request trace → Captain's Log reflection`. The `model` key is present on assistant messages from the cloud gateway path. Older messages can omit these fields.

---

### `session_events` table

The stored AG-UI events. The WebSocket replays them after a reconnect. A cleanup task deletes rows older than `ws_event_ttl_hours` (24 hours).

| Column | PostgreSQL type | Nullable | Description |
|--------|----------------|----------|-------------|
| `id` | `bigserial` | no | Primary key |
| `session_id` | `uuid` | no | Foreign key to `sessions(session_id)` |
| `seq` | `integer` | no | The sequence number. `(session_id, seq)` is unique. |
| `event_type` | `text` | no | The event type |
| `payload` | `jsonb` | no | The wire payload |
| `created_at` | `timestamptz` | no | Storage time |

Source: `docker/postgres/init.sql`, `src/personal_agent/transport/agui/ws_endpoint.py` (function `run_event_cleanup`).

---

### `users` table

| Column | PostgreSQL type | Nullable | Description |
|--------|----------------|----------|-------------|
| `user_id` | `uuid` | no | Primary key |
| `email` | `text` | no | Unique |
| `display_name` | `text` | yes | The display name |
| `created_at` | `timestamptz` | no | Creation time |

Source: `docker/postgres/init.sql` (ADR-0064).

---

### `metrics` table

| Column | PostgreSQL type | Nullable | Default | Description |
|--------|----------------|----------|---------|-------------|
| `id` | `bigserial` | no | auto-increment | Primary key |
| `timestamp` | `timestamptz` | no | `NOW()` | Metric capture time |
| `trace_id` | `uuid` | yes | `NULL` | Request trace identifier |
| `metric_name` | `varchar(100)` | no | none | Metric name |
| `metric_value` | `double precision` | no | none | Numeric metric value |
| `unit` | `varchar(20)` | yes | `NULL` | Unit of measure |
| `tags` | `jsonb` | yes | `'{}'` | Arbitrary key-value tags |

Source: `docker/postgres/init.sql`.

---

### `api_costs` table (cost tracking)

`CostTrackerService` writes one row for each paid API call.

| Column | PostgreSQL type | Nullable | Description |
|--------|----------------|----------|-------------|
| `id` | `bigserial` | no | Primary key |
| `timestamp` | `timestamptz` | no | Call time |
| `provider` | `varchar(50)` | no | The provider, such as `anthropic` or `openai` |
| `model` | `varchar(100)` | no | The model identifier |
| `input_tokens` | `integer` | no | Prompt tokens |
| `output_tokens` | `integer` | no | Completion tokens |
| `cost_usd` | `numeric(18,12)` | no | Computed cost in USD |
| `cache_read_input_tokens` | `integer` | yes | The Anthropic cache-read tier |
| `cache_creation_input_tokens` | `integer` | yes | The Anthropic cache-creation tier |
| `trace_id` | `uuid` | no | The request trace |
| `session_id` | `uuid` | yes | The session |
| `purpose` | `varchar(50)` | yes | The reason for the call, such as `user_request` |
| `latency_ms` | `integer` | yes | Round-trip latency |

Source: `docker/postgres/init.sql`. The table `api_calls` does not exist.

---

## 5. Infrastructure as code

The Terraform that manages the host firewall is not in this repository. It moved to a private companion repository on 2026-05-01. This guide does not describe the Terraform variables or rules.

For the cloud deployment of this repository, see [Cloud Deployment](CLOUD_DEPLOYMENT.md). The compose file is `docker-compose.cloud.yml`.
