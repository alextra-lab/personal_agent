# Seshat Schema Reference

> **Last updated**: 2026-04-26  
> **Applies to**: service/app.py, transport/agui/, config/, infrastructure/terraform/

Formal definitions for every schema boundary in Seshat: API endpoints, AG-UI wire events, configuration YAML files, the PostgreSQL database, and Terraform variables.

---

## Table of Contents

1. [API Endpoint Schemas](#1-api-endpoint-schemas)
   - [POST /chat/stream](#post-chatstream)
   - [POST /chat (synchronous, CLI)](#post-chat-synchronous-cli)
   - [GET /stream/{session_id}](#get-streamsession_id)
   - [POST /stream/{session_id}/resume](#post-streamsession_idresume)
   - [Session CRUD](#session-crud)
   - [GET /health](#get-health)
2. [AG-UI Wire Event Schema](#2-ag-ui-wire-event-schema)
3. [Configuration YAML Schemas](#3-configuration-yaml-schemas)
   - [Execution Profile (config/profiles/*.yaml)](#execution-profile-configprofilesyaml)
   - [Model Definitions (config/models.yaml)](#model-definitions-configmodelsyaml)
   - [Cloud Model Overrides (config/models.cloud.yaml)](#cloud-model-overrides-configmodelscloudyaml)
4. [PostgreSQL Schema](#4-postgresql-schema)
5. [Terraform Variable Schema](#5-terraform-variable-schema)

---

## 1. API Endpoint Schemas

Base URL: `http(s)://<host>` (Caddy reverse-proxies `/chat*` and `/stream*` to port 9001).

### `POST /chat/stream`

**Content-Type**: `application/x-www-form-urlencoded`

Fire-and-forget streaming endpoint for the PWA. Returns immediately; events arrive via `GET /stream/{session_id}`.

#### Request fields (form-encoded)

| Field | Type | Required | Default | Constraints | Description |
|-------|------|----------|---------|-------------|-------------|
| `message` | string | yes | — | non-empty | User's message text |
| `session_id` | string | yes | — | UUID v4 | Client-generated session identifier |
| `profile` | string | no | `"local"` | `"local"` \| `"cloud"` | Execution profile to activate |

#### Response (200 OK)

```json
{
  "session_id": "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx",
  "trace_id": "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx",
  "status": "streaming"
}
```

`trace_id` identifies this specific request turn in Elasticsearch traces and Captain's Log entries. The PWA can store it on the optimistic message object for future "view trace" affordances.

#### Errors

| Code | Condition |
|------|-----------|
| `422` | `session_id` is not a valid UUID v4 |
| `503` | Anthropic API key not configured (cloud path only) |

---

### `POST /chat` (synchronous, CLI)

**Content-Type**: `application/x-www-form-urlencoded` or query params

Synchronous endpoint used by the CLI (`uv run agent "..."` via `service_client.py`). Runs the full pipeline and returns the response inline.

#### Request parameters (query string or form)

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message` | string | yes | — | User's message text |
| `session_id` | string | no | auto-generated | Existing session UUID. Creates new session if omitted |

#### Response (200 OK)

```json
{
  "response": "The assistant's reply text",
  "session_id": "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx"
}
```

---

### `GET /stream/{session_id}`

**Content-Type**: `text/event-stream`

SSE stream that delivers AG-UI events for the given session. Call immediately after `POST /chat/stream`.

#### Path parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | UUID string | The `session_id` returned from `POST /chat/stream` |

#### Response

A sequence of SSE data lines. Each line is a JSON-encoded AG-UI event (see [Section 2](#2-ag-ui-wire-event-schema)).

```
data: {"type": "TEXT_DELTA", "data": {"text": "Hello"}, "session_id": "..."}

data: {"type": "DONE"}
```

The stream ends when:
- A `DONE` event is sent (normal completion)
- The client disconnects (server detects via `request.is_disconnected()`)

**Keepalive**: `: keepalive` SSE comment every 30 seconds when queue is idle.

---

### `POST /stream/{session_id}/resume`

**Content-Type**: `application/json`

Resumes a paused session after an `INTERRUPT` event. Currently partially wired — `InterruptEvent` is defined but the orchestrator resume path is not yet complete.

#### Request body

```json
{
  "choice": "approve"
}
```

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `choice` | string | One of the strings from `event.data.options` | The human's selection |

---

### Session CRUD

#### `POST /sessions`

Create a new session. The PWA does not need to call this — `POST /chat/stream` creates sessions with the client-provided UUID automatically. This is available for explicit session management.

**Request body** (`application/json`):

```json
{
  "channel": "pwa",
  "mode": "NORMAL",
  "metadata": {}
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `channel` | string \| null | no | `null` | Client identifier (`"pwa"`, `"cli"`, etc.) |
| `mode` | string | no | `"NORMAL"` | Session mode (`"NORMAL"` \| `"DEBUG"`) |
| `metadata` | object | no | `{}` | Arbitrary client metadata |

**Response** (`SessionResponse`, 201):

```json
{
  "session_id": "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx",
  "created_at": "2026-04-16T12:00:00Z",
  "last_active_at": "2026-04-16T12:00:00Z",
  "mode": "NORMAL",
  "channel": "pwa",
  "metadata": {},
  "messages": []
}
```

#### `GET /sessions/{session_id}`

Retrieve session by UUID. Returns `SessionResponse` (same shape as above).

#### `PATCH /sessions/{session_id}`

Update session fields.

**Request body** (`application/json`):

```json
{
  "mode": "DEBUG",
  "channel": "cli",
  "metadata": {"key": "value"},
  "messages": [{"role": "user", "content": "Hello", "timestamp": "..."}]
}
```

All fields are optional; only provided fields are updated.

#### `GET /api/v1/sessions`

List recent sessions, ordered by `last_active_at DESC`. Requires `sessions:read` scope.

**Query parameters**: `limit` (default 20)

**Response** (`list[SessionSummary]`, 200):

```json
[
  {
    "session_id": "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx",
    "created_at": "2026-04-26T10:00:00Z",
    "last_active_at": "2026-04-26T10:05:00Z",
    "mode": "NORMAL",
    "channel": "CHAT",
    "message_count": 6,
    "title": "Hello, my name is Boris"
  }
]
```

`title` is derived server-side from the first user message, truncated to ≤ 60 characters (with `…` suffix). Returns `null` for empty sessions.

#### `GET /api/v1/sessions/{session_id}/messages`

Fetch the full message history for a session. Requires `sessions:read` scope.

**Query parameters**: `limit` (default 50, `0` = all)

**Response** (`list[Message]`, 200): Messages in chronological order. Each message includes all persisted metadata (see PostgreSQL schema §4 for element schema). Returns 404 if session does not exist.

#### `GET /sessions`

*(Legacy — local service path only)* List all sessions. Returns `list[SessionResponse]`.

---

### `GET /health`

Health check endpoint. Used by Docker Compose healthcheck.

**Response** (200 OK):

```json
{"status": "ok"}
```

---

## 2. AG-UI Wire Event Schema

All events share the same envelope:

```json
{
  "type": "<EVENT_TYPE>",
  "data": { ... },
  "session_id": "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx"
}
```

The `DONE` event omits `data` and `session_id`:

```json
{"type": "DONE"}
```

### Event type reference

#### `TEXT_DELTA`

A chunk of text from the LLM. Concatenate chunks to build the full response.

```json
{
  "type": "TEXT_DELTA",
  "data": {
    "text": "string — partial or full LLM output text"
  },
  "session_id": "string"
}
```

> **Current behavior**: Full response arrives as a single `TEXT_DELTA` (non-streaming orchestrator). Planned improvement: token-level streaming via LiteLLM async.

---

#### `TOOL_CALL_START`

A tool invocation has begun.

```json
{
  "type": "TOOL_CALL_START",
  "data": {
    "tool_name": "string — e.g. \"search_memory\"",
    "args": { "key": "value" }
  },
  "session_id": "string"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `tool_name` | string | Registered tool name |
| `args` | object | Tool arguments (varies per tool) |

---

#### `TOOL_CALL_END`

A tool invocation has completed.

```json
{
  "type": "TOOL_CALL_END",
  "data": {
    "tool_name": "string",
    "result": "string — human-readable summary of the tool result"
  },
  "session_id": "string"
}
```

---

#### `STATE_DELTA`

An agent state update.

```json
{
  "type": "STATE_DELTA",
  "data": {
    "key": "string — state key",
    "value": "any — new value"
  },
  "session_id": "string"
}
```

**Known keys**:

| Key | Value type | Range | Description |
|-----|-----------|-------|-------------|
| `context_window` | float | 0.0–1.0 | Fraction of context window consumed |

---

#### `INTERRUPT`

Human approval required. After receiving this event, call `POST /stream/{session_id}/resume`.

```json
{
  "type": "INTERRUPT",
  "data": {
    "context": "string — description of the decision presented to the human",
    "options": ["approve", "reject"]
  },
  "session_id": "string"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `context` | string | Human-readable description of what approval is needed for |
| `options` | string[] | Valid choices (pass one back in `/resume` as `choice`) |

---

#### `DONE`

Stream complete. Close the `EventSource`.

```json
{"type": "DONE"}
```

No `data` or `session_id` fields.

---

### Internal Python event types (`transport/events.py`)

The backend uses frozen dataclasses that the adapter serializes to the wire format above:

```python
@dataclass(frozen=True)
class TextDeltaEvent:
    text: str
    session_id: str

@dataclass(frozen=True)
class ToolStartEvent:
    tool_name: str
    args: Mapping[str, Any]
    session_id: str

@dataclass(frozen=True)
class ToolEndEvent:
    tool_name: str
    result_summary: str      # → wire field "result"
    session_id: str

@dataclass(frozen=True)
class StateUpdateEvent:
    key: str
    value: Any
    session_id: str

@dataclass(frozen=True)
class InterruptEvent:
    context: str
    options: Sequence[str]
    session_id: str
```

`None` pushed to the queue is the `DONE` sentinel.

---

## 3. Configuration YAML Schemas

### Execution Profile (`config/profiles/*.yaml`)

Files: `config/profiles/local.yaml`, `config/profiles/cloud.yaml`.

```yaml
name: string                    # Profile identifier (matches filename stem)
description: string             # Human-readable description

primary_model: string           # Key into models.yaml (e.g. "claude_sonnet")
sub_agent_model: string         # Key into models.yaml (e.g. "claude_haiku")
provider_type: string           # "local" | "cloud"

cost_limit_per_session: float | null  # USD cap per session; null = no limit

delegation:
  allow_cloud_escalation: bool  # Whether to allow escalating to cloud models
  escalation_provider: string | null   # "anthropic" | "openai" | null
  escalation_model: string | null      # Key into models.yaml | null
```

**Example — `cloud.yaml`:**

```yaml
name: cloud
description: "Cloud inference via LiteLLM (Claude Sonnet + Haiku)"
primary_model: claude_sonnet
sub_agent_model: claude_haiku
provider_type: cloud
cost_limit_per_session: 2.00
delegation:
  allow_cloud_escalation: true
  escalation_provider: anthropic
  escalation_model: claude_sonnet
```

**Example — `local.yaml`:**

```yaml
name: local
description: "Local inference via SLM Server (Qwen3.5-35B)"
primary_model: qwen3.5-35b-a3b
sub_agent_model: qwen3-8b
provider_type: local
cost_limit_per_session: null
delegation:
  allow_cloud_escalation: false
  escalation_provider: null
  escalation_model: null
```

---

### Model Definitions (`config/models.yaml`)

Top-level fields:

| Field | Type | Description |
|-------|------|-------------|
| `entity_extraction_role` | string | Model key for entity extraction |
| `captains_log_role` | string | Model key for Captain's Log summarization |
| `insights_role` | string | Model key for insights engine |
| `models` | map[string, ModelDef] | Model definitions keyed by role name |

Each `ModelDef` supports the following fields. Some are local-only; some are cloud-only.

#### Common fields (all models)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Provider model identifier (LM Studio slug or cloud model name) |
| `context_length` | int | yes | Maximum context window in tokens |
| `max_concurrency` | int | yes | Max concurrent in-flight requests |
| `default_timeout` | int | yes | Request timeout in seconds |
| `provider_type` | string | no | `"local"` \| `"managed"` \| `"cloud"` (auto-detected if omitted) |

#### Local model fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `endpoint` | string | no | `settings.llm_base_url` | Base URL override |
| `quantization` | string | no | — | `"8bit"` \| `"4bit"` |
| `temperature` | float | no | — | Sampling temperature |
| `top_p` | float | no | — | Nucleus sampling threshold |
| `top_k` | int | no | — | Top-K sampling (passed via `extra_body`) |
| `presence_penalty` | float | no | — | Presence penalty |
| `max_tokens` | int | no | — | Max output tokens |
| `disable_thinking` | bool | no | `false` | Hard-disable thinking via `chat_template_kwargs` |
| `thinking_budget_tokens` | int | no | — | Cap thinking tokens via `extra_body` |
| `min_concurrency` | int | no | — | Floor for adaptive concurrency |
| `supports_function_calling` | bool | no | `false` | Whether the model supports tool calling |
| `tool_calling_strategy` | string | no | — | `"native"` \| `"prompt"` |

#### Cloud model fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `provider` | string | yes | `"anthropic"` \| `"openai"` \| `"google"` |
| `max_tokens` | int | yes | Max output tokens (billed per token) |
| `provider_type` | string | yes | Must be `"cloud"` |

**Example cloud model entry:**

```yaml
claude_sonnet:
  id: "claude-sonnet-4-6"
  provider: "anthropic"
  provider_type: "cloud"
  context_length: 200000
  max_tokens: 8192
  max_concurrency: 5
  default_timeout: 120
```

---

### Cloud Model Overrides (`config/models.cloud.yaml`)

Optional file that adds or overrides model definitions when running in cloud mode. Merged over `models.yaml` at startup. Same schema as the `models:` map in `models.yaml`.

Exists to keep cloud-only model entries (like `claude_haiku`) separate from the local-inference defaults.

---

## 4. PostgreSQL Schema

Database: `personal_agent`  
Connection: `AGENT_DATABASE_URL` environment variable (Pydantic settings).

### `sessions` table

| Column | PostgreSQL type | Nullable | Default | Description |
|--------|----------------|----------|---------|-------------|
| `session_id` | `uuid` | no | `gen_random_uuid()` | Primary key |
| `created_at` | `timestamptz` | no | — | Session creation time |
| `last_active_at` | `timestamptz` | no | — | Last activity time |
| `mode` | `varchar(20)` | no | `'NORMAL'` | Session mode (`NORMAL` \| `DEBUG`) |
| `channel` | `varchar(50)` | yes | `NULL` | Client identifier (`pwa`, `cli`, etc.) |
| `metadata` | `jsonb` | no | `'{}'` | Arbitrary client metadata |
| `messages` | `jsonb` | no | `'[]'` | Array of `{role, content, timestamp, metadata}` objects |

#### `messages` JSONB element schema

```json
{
  "role": "user | assistant | system | tool",
  "content": "string",
  "timestamp": "ISO 8601 datetime (UTC)",
  "trace_id": "UUID string — identifies the request turn in ES traces and Captain's Log",
  "metadata": {
    "source": "gateway.chat_api | service.app | request_completed_handler",
    "model": "claude-sonnet-4-6"
  }
}
```

Every message persisted after FRE-235 (2026-04-26) carries `trace_id`, `timestamp`, and `metadata.source`, enabling end-to-end correlation: `session_id → trace_id → ES request trace → Captain's Log reflection`. The `model` key is present on assistant messages from the cloud gateway path. Older messages (pre-FRE-235) may omit these fields.

---

### `metrics` table

| Column | PostgreSQL type | Nullable | Default | Description |
|--------|----------------|----------|---------|-------------|
| `id` | `bigint` | no | auto-increment | Primary key |
| `timestamp` | `timestamptz` | no | — | Metric capture time |
| `trace_id` | `uuid` | yes | `NULL` | Request trace identifier |
| `metric_name` | `varchar(100)` | no | — | Metric name (e.g. `"llm_latency_ms"`) |
| `metric_value` | `double precision` | no | — | Numeric metric value |
| `unit` | `varchar(20)` | yes | `NULL` | Unit of measure (e.g. `"ms"`, `"USD"`) |
| `tags` | `jsonb` | no | `'{}'` | Arbitrary key-value tags |

---

### `api_calls` table (cost tracking)

Managed by `CostTrackerService`. Stores LiteLLM cloud API call costs.

| Column | PostgreSQL type | Nullable | Description |
|--------|----------------|----------|-------------|
| `id` | `bigint` | no | Primary key |
| `timestamp` | `timestamptz` | no | Call time |
| `provider` | `varchar(50)` | no | `"anthropic"` \| `"openai"` |
| `model` | `varchar(100)` | no | Model ID (e.g. `"claude-sonnet-4-6"`) |
| `input_tokens` | `int` | no | Prompt tokens |
| `output_tokens` | `int` | no | Completion tokens |
| `cost_usd` | `numeric(10,6)` | no | Computed cost in USD |
| `latency_ms` | `int` | no | Round-trip latency |

---

## 5. Terraform Variable Schema

**Provider**: OVH (`ovh` Terraform provider)  
**State**: `infrastructure/terraform/terraform.tfstate` (local)  
**Credentials file**: `infrastructure/terraform/terraform.tfvars` (gitignored)  
**Template**: `infrastructure/terraform/terraform.tfvars.example`

### Variables (`variables.tf`)

| Variable | Type | Sensitive | Validation | Description |
|----------|------|-----------|------------|-------------|
| `ovh_application_key` | string | yes | — | OVH API application key from `eu.api.ovh.com/createApp` |
| `ovh_application_secret` | string | yes | — | OVH API application secret |
| `ovh_consumer_key` | string | yes | — | OVH consumer key (generated via token request) |
| `vps_ip` | string | yes | Must match `\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}` | VPS public IPv4 address |
| `ssh_port` | number | yes | 1025–65534 | Non-standard SSH port configured on the server |

### `terraform.tfvars` format

```hcl
ovh_application_key    = "xxxxxxxxxxxx"
ovh_application_secret = "xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
ovh_consumer_key       = "xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
vps_ip                 = "1.2.3.4"
ssh_port               = 2222
```

### Firewall rules managed (`main.tf`)

| Sequence | Resource name | Action | Protocol | Port/Option | Purpose |
|----------|---------------|--------|----------|-------------|---------|
| 0 | `allow_established` | permit | tcp | `established` | Return traffic for outbound connections |
| 1 | `allow_ssh` | permit | tcp | `var.ssh_port` | SSH access |
| 2 | `allow_http` | permit | tcp | 80 | HTTP (Caddy redirect) |
| 3 | `allow_https` | permit | tcp | 443 | HTTPS (Caddy TLS termination) |
| 4 | `allow_icmp` | permit | icmp | — | Ping / health diagnostics |
| 19 | `deny_all` | deny | ipv4 | — | Catch-all deny (must be last; OVH max sequence = 19) |

> **Stateless firewall**: The OVH network firewall does not track connection state. Rule 0 (`established`) explicitly permits ACK/RST packets so that TCP replies to outbound connections (e.g. apt, Docker pulls, Cloudflare WARP) are not blocked.

### Outputs (`outputs.tf`)

After `terraform apply`, the following outputs are available via `terraform output`:

| Output | Description |
|--------|-------------|
| `firewall_ip_block` | The `/32` CIDR block the firewall is applied to |
| `vps_ip` | The VPS IP (echoed for confirmation) |

### Credential setup

```bash
# 1. Create an OVH app at https://eu.api.ovh.com/createApp
#    → Save application_key and application_secret

# 2. Request a consumer key with firewall permissions
curl -X POST https://eu.api.ovh.com/1.0/auth/credential \
  -H "X-Ovh-Application: <application_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "accessRules": [
      {"method": "GET",    "path": "/ip/*"},
      {"method": "POST",   "path": "/ip/*"},
      {"method": "PUT",    "path": "/ip/*"},
      {"method": "DELETE", "path": "/ip/*"}
    ],
    "redirection": "https://example.com"
  }'
# → Visit the validation URL, then note the consumer_key from the response

# 3. Create terraform.tfvars from the example template
cp infrastructure/terraform/terraform.tfvars.example infrastructure/terraform/terraform.tfvars
# Edit with your values

# 4. Apply
cd infrastructure/terraform
terraform init
terraform plan
terraform apply
```
