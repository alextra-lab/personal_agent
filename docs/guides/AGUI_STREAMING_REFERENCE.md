# AG-UI Streaming Reference

> **Last updated**: 2026-10-30
> **Protocol**: AG-UI over WebSocket (ADR-0075, which replaced the SSE transport of ADR-0046)
> **Applies to**: PWA client, `src/personal_agent/service/app.py`, `src/personal_agent/transport/`

This reference documents the AG-UI streaming protocol as implemented in Seshat: the wire format, the endpoint contracts, the event types and the client integration. Each fact names its source file.

---

## Overview

AG-UI is the streaming protocol between the Seshat PWA and the backend. It uses one fire-and-forget HTTP call and one WebSocket.

```
Client                                    Backend
  │                                          │
  │── POST /api/ws-ticket ──────────────────►│  (mint a single-use ticket)
  │◄─ {"ticket": "...", "expires_in": 30} ───│
  │── WS /ws/{session_id}?ticket=... ───────►│  (upgrade)
  │── {"type":"CONNECT","last_seq":0} ──────►│  (first message)
  │                                          │
  │── POST /chat/stream ────────────────────►│
  │◄─ 200 {"status":"streaming",...} ────────│  (returns at once)
  │                                          │  [background task runs]
  │◄─ {"type":"PHASE_START",...,"seq":1} ────│
  │◄─ {"type":"TEXT_DELTA",...,"seq":2} ─────│  [events arrive]
  │◄─ {"type":"DONE","trace_id":"..."} ──────│  [turn complete]
```

The client opens two independent channels:

1. `POST /chat/stream` submits the message and returns at once.
2. `WS /ws/{session_id}` carries the events in both directions.

The `DONE` event ends a turn. After the server sends `DONE`, the sender task ends, and the handler closes the socket. The PWA client then reconnects with backoff, and it sends `CONNECT` with its stored `last_seq`.

Source: `src/personal_agent/transport/agui/ws_endpoint.py` (module docstring and function `ws_session`), `src/personal_agent/service/app.py` (functions `chat_stream_endpoint` and `mint_ws_ticket_endpoint`).

The SSE routes `GET /stream/{session_id}` and `POST /stream/{session_id}/resume` do not exist. The module `transport/agui/endpoint.py` does not exist either.

---

## Endpoints

### `POST /api/ws-ticket`

Mints a single-use ticket for the WebSocket handshake. The ticket keeps the bearer token out of the URL.

**Content-Type**: `application/json`

```json
{"session_id": "<UUID>"}
```

**Response** (200):
```json
{"ticket": "...", "expires_in": 30}
```

The default life of a ticket is 30 seconds (`ws_ticket_ttl_seconds`).

**Errors**:
- `422` - `session_id` is not a valid UUID.
- `404` - the session does not exist for this user.

Source: `service/app.py`, function `mint_ws_ticket_endpoint`. `src/personal_agent/config/settings.py`, field `ws_ticket_ttl_seconds`.

---

### `POST /chat/stream`

**Content-Type**: `application/x-www-form-urlencoded`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | yes | The text of the user message |
| `session_id` | string | yes | The target session |
| `primary_selection` | string | no | A deployment key. The server uses it only to set up a new session. For an existing session, the stored selection wins. |
| `seshat_effort` | `fast`, `standard` or `deep` | no | The effort level of the turn |
| `client_msg_id` | string | no | A client-generated message identifier |
| `attachments` | JSON string | no | The attachments of the message |

**Response** (200):
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

**Behavior**: The call returns at once. The full pipeline (gateway, orchestrator, model) runs in a background task. Each event is stored in Postgres and then pushed to the WebSocket.

**Errors**: A `503` response with `error: "budget_denied"` means that a budget cap stopped the turn.

Source: `service/app.py`, function `chat_stream_endpoint`. `seshat-pwa/src/lib/agui-client.ts`, function `sendChatMessage`.

There is no `profile` field. The system has no execution profiles.

---

### `WS /ws/{session_id}`

The handshake has these steps:

1. **Auth before accept.** If `gateway_auth_enabled` is on, the client must pass `?ticket=<ticket>`. The ticket is valid for this session only, and it works once. If auth is off, the server uses the owner identity.
2. **Origin check.** The `Origin` header must be in `allowed_ws_origins`. A missing origin is accepted only when auth is off.
3. **Ownership check.** The session must exist for the user.
4. **Eviction.** One socket is active per session. A new connection closes the old one with code `4001` ("Superseded").
5. **CONNECT.** The client must send a `CONNECT` message within 10 seconds. The server closes the socket with code `1008` if it does not.

Source: `ws_endpoint.py`, functions `_authenticate_ws`, `_validate_origin`, `ws_session` and `_receive_connect`.

**Close codes**

| Code | Meaning |
|------|---------|
| `1001` | No inbound message within `ws_ping_timeout_seconds` (default 60 s) |
| `1008` | Unauthorized, origin not allowed, session not found, CONNECT missing, message too large, or rate limit exceeded |
| `4001` | Superseded by a newer connection for the same session |
| `4002` | The turn closed (the turn deadline) |

**Limits** (defaults from `settings.py`): the maximum inbound message size is 8192 characters (`ws_max_message_size`). The inbound rate limit is 20 messages per second (`ws_rate_limit_per_second`). The server keeps events for 24 hours (`ws_event_ttl_hours`).

Source: `ws_endpoint.py` (constants `WS_CLOSE_SUPERSEDED` and `WS_CLOSE_TURN_DEADLINE`, function `_receiver`), `settings.py` (fields `ws_ping_timeout_seconds`, `ws_max_message_size`, `ws_rate_limit_per_second`, `ws_event_ttl_hours`).

---

## Messages from the client

Every message is a JSON text frame with a `type` field.

| `type` | Fields | Purpose |
|--------|--------|---------|
| `CONNECT` | `last_seq` (integer, default 0), `attach` (boolean) | The first message. `last_seq` is the replay watermark. `attach: true` is for a page that holds nothing in memory. |
| `PING` | none | Heartbeat. The server answers with `PONG`. |
| `APPROVAL_DECISION` or `INTERRUPT_RESPONSE` | `request_id`, `decision` | The answer to a tool approval request. |
| `CONSTRAINT_DECISION` | `request_id`, `decision`, `remember` (boolean) | The answer to a `CONSTRAINT_PAUSE`. An unknown `decision` is replaced by the default option. |
| `USER_CANCEL` | none | Stops the running turn. |

The server ignores a message with an unknown `type`.

Source: `ws_endpoint.py`, functions `_receiver`, `_receive_connect`, `_resolve_waiter` and `_resolve_constraint_decision`.

---

## Messages from the server

Event messages have this shape: `{"type": "<TYPE>", "data": {...}, "session_id": "...", "seq": <integer or null>}`. The field `seq` is the Postgres sequence number of the stored event. Control messages have `"seq": null`. Five message types do not use the `data` envelope: `tool_approval_request`, `DONE`, `PONG`, `REPLAY_COMPLETE` and `REPLAY_GAP`. `tool_approval_request` also has no `session_id`. `DONE` and the control messages have no `session_id`. See the notes in the table.

| `type` | `data` | When |
|--------|--------|------|
| `TEXT_DELTA` | `{"text"}` | The text of the reply |
| `TOOL_CALL_START` | `{"tool_name", "args"}` and an optional `tool_call_id` | A tool starts |
| `TOOL_CALL_END` | `{"tool_name", "result"}` and an optional `tool_call_id` | A tool ends. `result` is a summary. |
| `PHASE_START` | `{"phase", "phase_id", "started_at", "detail", "parent_id"}` | A phase of the turn starts |
| `PHASE_END` | `{"phase", "phase_id", "parent_id", "ok", "ended_at"}` | A phase ends |
| `STATE_DELTA` | `{"key", "value"}` | A state update, such as `turn_status` or `session_effort` |
| `INTERRUPT` | `{"context", "options"}` | A human choice is needed |
| `tool_approval_request` | none. The fields `request_id`, `trace_id`, `tool`, `args`, `risk_level`, `reason`, `expires_at`, `worker` and `session_option` are at the top level. | A tool needs approval. The type is lower case. |
| `CONSTRAINT_PAUSE` | `{"constraint", "context", "options", "default_option", "expires_at"}`. The fields `request_id` and `trace_id` are at the top level. | The turn waits for a choice |
| `CONSTRAINT_RESOLVED` | `{"constraint", "action_id", "resolution"}`. The field `request_id` is at the top level. | The choice was made |
| `CANCELLED` | `{"reason"}` | The turn was cancelled |
| `RUN_ERROR` | `{"category", "reason", "next_step", "actions", "partial"}` | The turn failed |
| `DONE` | none. The field `trace_id` is at the top level. | The turn is complete |
| `PONG` | none | The answer to `PING` |
| `REPLAY_COMPLETE` | none. The fields `seq` and, after an attach, `last_seq` and `turn_in_flight` are at the top level. | The replay is complete |
| `REPLAY_GAP` | none. The field `oldest_available_seq` is at the top level. | The server no longer holds events after the client watermark |

The `phase` value is one of `planning`, `synthesis`, `artifact_build`, `expansion`, `sub_agent` or `waiting_for_choice` (lower case).

Source: `src/personal_agent/transport/agui/adapter.py` (function `to_agui_event`), `src/personal_agent/transport/events.py` (class `Phase`), `ws_endpoint.py` (functions `_sender` and `_send_attach_replay`), `src/personal_agent/transport/agui/transport.py` (function `emit_done`).

### `TEXT_DELTA`

```json
{
  "type": "TEXT_DELTA",
  "data": {"text": "Hello! How can I help you today?"},
  "session_id": "...",
  "seq": 2
}
```

**Current behavior**: The reply arrives whole in one `TEXT_DELTA` event. The server does not stream tokens to the client. Source: `service/app.py`, function `_process_chat_stream_background` (the comment "the reply goes out whole").

### `TOOL_CALL_START` and `TOOL_CALL_END`

```json
{"type": "TOOL_CALL_START", "data": {"tool_name": "search_memory", "args": {"query": "..."}}, "session_id": "...", "seq": 3}
{"type": "TOOL_CALL_END", "data": {"tool_name": "search_memory", "result": "Found 3 relevant memories"}, "session_id": "...", "seq": 4}
```

### `STATE_DELTA`

```json
{"type": "STATE_DELTA", "data": {"key": "session_effort", "value": {"seshat_effort": "standard", "effort_provenance": "..."}}, "session_id": "...", "seq": null}
```

The keys that the server sends include `turn_status` and `session_effort`. After an attach, the server always sends `session_effort`. It sends the `turn_status` snapshot only when one exists. Both have `"seq": null`.

### `DONE`

```json
{"type": "DONE", "trace_id": "..."}
```

The turn is complete. The live `DONE` message has `"seq": null`. The stored `DONE` event has a `seq`, and a replay sends it with that `seq`. After the live `DONE`, the server closes the socket. The client must reconnect for the next turn. Source: `ws_endpoint.py`, functions `_sender` and `ws_session` (the `finally` block); `seshat-pwa/src/lib/agui-client.ts`, handler `ws.onclose`.

---

## Reconnect and replay

The server stores every event in the Postgres table `session_events` before it pushes the event. The client keeps the highest contiguous `seq` that it handled (`last_seq`).

- **Reconnect.** The client sends `CONNECT` with `last_seq`. If `last_seq` is greater than 0, the server replays all stored events after it, and then sends `REPLAY_COMPLETE`.
- **Gap.** If the server swept events after `last_seq`, it sends `REPLAY_GAP` with `oldest_available_seq` before the replay. The client must then fetch the session history again.
- **Attach.** A page that holds nothing in memory (load, reload, session switch) sends `CONNECT` with `attach: true`. The server sends the `turn_status` snapshot (only when one exists), the `session_effort` state, the events of the turn that is in flight, and `REPLAY_COMPLETE` with `last_seq` and `turn_in_flight`.
- **Keepalive.** The client sends `PING` every 25 seconds. The server closes a socket that is silent for `ws_ping_timeout_seconds`.

Source: `ws_endpoint.py` (functions `_sender` and `_send_attach_replay`), `src/personal_agent/transport/agui/event_buffer.py` (class `SessionEventBuffer`), `seshat-pwa/src/lib/agui-client.ts` (function `connectWebSocket`).

---

## Internal Event Types

The backend defines 12 event classes in `src/personal_agent/transport/events.py`. Each class is a frozen dataclass.

| Class | Wire `type` |
|-------|-------------|
| `TextDeltaEvent` | `TEXT_DELTA` |
| `ToolStartEvent` | `TOOL_CALL_START` |
| `ToolEndEvent` | `TOOL_CALL_END` |
| `PhaseStartEvent` | `PHASE_START` |
| `PhaseEndEvent` | `PHASE_END` |
| `StateUpdateEvent` | `STATE_DELTA` |
| `InterruptEvent` | `INTERRUPT` |
| `ToolApprovalRequestEvent` | `tool_approval_request` |
| `ConstraintPauseEvent` | `CONSTRAINT_PAUSE` |
| `ConstraintResolvedEvent` | `CONSTRAINT_RESOLVED` |
| `CancelledEvent` | `CANCELLED` |
| `ClassifiedErrorEvent` | `RUN_ERROR` |

The function `to_agui_event` in `transport/agui/adapter.py` converts an event to the wire envelope and adds `seq`. The module `transport/agui/transport.py` holds the `emit_*` functions that persist and push the events.

---

## Session Queue Lifecycle

Each session has one in-memory queue and one connection.

1. A producer calls `_persist_and_enqueue`. The function stores the event in Postgres, and then puts it on the session queue. The order of the commits is the order of the queue.
2. The sender task of the connection drains the queue and sends each item to the socket.
3. `emit_done` stores the `DONE` event and puts a sentinel on the queue. The sender turns the sentinel into the `DONE` message.
4. The queue is in memory only. After a gateway restart, the client reconnects and replays from Postgres.

Source: `transport/agui/transport.py` (functions `_persist_and_enqueue`, `_push_event` and `emit_done`), `ws_endpoint.py` (function `_sender`, constant `DONE_SENTINEL_TYPE`).

---

## Client Integration (TypeScript)

From `seshat-pwa/src/lib/agui-client.ts`:

### Sending a message

```typescript
await sendChatMessage({
  message: "Hello Seshat",
  sessionId: generateUUID(),  // use the uuid.ts polyfill for Safari compatibility
  primarySelection: "<deployment key>",  // optional; sets up a new session
  seshatEffort: "standard",              // optional: "fast", "standard" or "deep"
});
```

### Connecting to the WebSocket

```typescript
const conn = connectWebSocket(
  sessionId,
  (event: AGUIEvent) => {
    switch (event.type) {
      case "TEXT_DELTA":
        buffer += event.data.text;
        updateUI(buffer);
        break;
      case "TOOL_CALL_START":
        showToolSpinner(event.data.tool_name);
        break;
      case "TOOL_CALL_END":
        hideToolSpinner(event.data.tool_name);
        break;
      case "STATE_DELTA":
        applyState(event.data.key, event.data.value);
        break;
      case "DONE":
        finishTurn();  // the server closes the socket; the client reconnects
        break;
    }
  },
  (error) => reportError(error),
);
```

The function `connectWebSocket` mints a fresh ticket for each connection attempt. It sends `CONNECT` with the stored `last_seq`, sends `PING` every 25 seconds, and reconnects with exponential backoff and jitter (1 s to 30 s). It stores `last_seq` in `localStorage`. The function returns an object with `close()` and `send()`.

### UUID generation (Safari polyfill)

Safari on plain HTTP (private IP) rejects `crypto.randomUUID()`. Use the helper in `seshat-pwa/src/lib/uuid.ts`:

```typescript
export function generateUUID(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    try { return crypto.randomUUID(); } catch {}
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}
```

---

## Background Task Pipeline

`_process_chat_stream_background` in `service/app.py` runs as a background task:

```
resolve the session selection and effort
  │
  ▼
run_gateway_pipeline(...)     ← intent, decomposition, memory context
  │
  ▼
orchestrator.handle_user_request(...)   ← model calls through LiteLLMClient
  │
  ▼
_push_event(TextDeltaEvent(text=reply))   ← the whole reply, stored in Postgres, then queued
  │
  ▼
append the assistant message to the session
  │
  ▼
emit_done(session_id, trace_id)           ← in a finally block, also on error
```

The `finally` block sends `DONE`, so the client always sees the end of the turn.

Source: `service/app.py`, function `_process_chat_stream_background` (calls at `run_gateway_pipeline`, `orchestrator.handle_user_request`, `_push_event` and `emit_done`).

---

## Caddy Routing

The Caddyfile (`config/cloud-sim/Caddyfile`) must list the gateway paths explicitly:

```caddyfile
@backend path /api/* /chat /chat/stream /ws/* /docs /docs/* /openapi.json /redoc
handle @backend {
    reverse_proxy seshat-gateway:9001
}
```

A `path` matcher compares exactly when the path has no wildcard. `/chat` does not match `/chat/stream`. `/ws/*` carries the WebSocket upgrade.

Source: `config/cloud-sim/Caddyfile` (the `routing` snippet).

---

## Known Limitations

| Limitation | Status |
|------------|--------|
| The whole reply arrives in one `TEXT_DELTA`. The server does not stream tokens to the client. | Current. Source: `service/app.py`. |
| The session queue is in memory. A gateway restart loses queued items. | The client replays from Postgres with `last_seq`. The server keeps events for 24 hours. |
| One active socket per session. | A second connection evicts the first with code `4001`. |
| The server closes the socket after each `DONE`. | The client reconnects for the next turn. |
