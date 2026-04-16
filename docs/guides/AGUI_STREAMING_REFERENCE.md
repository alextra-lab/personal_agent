# AG-UI Streaming Reference

> **Last updated**: 2026-04-16  
> **Protocol**: AG-UI (ADR-0046)  
> **Applies to**: PWA client, service/app.py, transport/agui/

This reference documents the AG-UI streaming protocol as implemented in Seshat: the wire format, endpoint contracts, event types, and client integration patterns.

---

## Overview

AG-UI is the streaming protocol between the Seshat PWA and the backend. It follows a fire-and-forget + SSE (Server-Sent Events) pattern:

```
Client                                    Backend
  │                                          │
  │── POST /chat/stream ────────────────────►│
  │◄─ 200 {"status":"streaming"} ───────────│  (returns immediately)
  │                                          │  [background task running]
  │── GET /stream/{session_id} ────────────►│
  │◄─ text/event-stream ────────────────────│
  │   data: {"type":"TEXT_DELTA",...}        │  [events stream in]
  │   data: {"type":"DONE"}                  │  [task complete]
  │                                          │
```

The client initiates two independent connections:
1. `POST /chat/stream` — submits the message, gets back immediately
2. `GET /stream/{session_id}` — receives events as they are produced

---

## Endpoints

### `POST /chat/stream`

**Content-Type**: `application/x-www-form-urlencoded`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | yes | User's message text |
| `session_id` | UUID v4 | yes | Client-generated session identifier |
| `profile` | string | no | Execution profile (`"local"` or `"cloud"`, default `"local"`) |

**Response** (200):
```json
{"session_id": "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx", "status": "streaming"}
```

**Errors**:
- `422` — `session_id` is not a valid UUID v4

**Behavior**: Returns immediately. The full pipeline (gateway → orchestrator → LLM) runs in a background asyncio task. Events are pushed to the per-session SSE queue as they complete.

---

### `GET /stream/{session_id}`

**Content-Type**: `text/event-stream`

Streams AG-UI events until:
- A `DONE` event is sent (normal completion)
- The client disconnects (detected via `request.is_disconnected()`)

**Keepalive**: The server sends `: keepalive` SSE comments every 30 seconds when idle (prevents proxy timeout).

**Response format** (SSE):
```
data: {"type": "TEXT_DELTA", "data": {"text": "Hello"}, "session_id": "..."}

data: {"type": "TOOL_CALL_START", "data": {"tool_name": "search"}, "session_id": "..."}

data: {"type": "DONE"}
```

---

### `POST /stream/{session_id}/resume`

Used for Human-in-the-Loop (HITL) interrupts.

**Content-Type**: `application/json`

```json
{"choice": "approve"}
```

---

## Event Types

All events follow the schema: `{"type": "<EVENT_TYPE>", "data": {...}, "session_id": "..."}`.

### `TEXT_DELTA`

A chunk of text from the LLM. The client concatenates chunks to build the full response.

```json
{
  "type": "TEXT_DELTA",
  "data": {"text": "Hello! How can I help you today?"},
  "session_id": "..."
}
```

**Current behavior**: The full response arrives as a single `TEXT_DELTA` event (non-streaming orchestrator). Future improvement: token-level streaming from LiteLLM.

### `TOOL_CALL_START`

A tool has started executing.

```json
{
  "type": "TOOL_CALL_START",
  "data": {"tool_name": "search_memory", "args": {"query": "..."}},
  "session_id": "..."
}
```

### `TOOL_CALL_END`

A tool finished executing.

```json
{
  "type": "TOOL_CALL_END",
  "data": {"tool_name": "search_memory", "result": "Found 3 relevant memories"},
  "session_id": "..."
}
```

### `STATE_DELTA`

Agent state update (e.g., context window usage).

```json
{
  "type": "STATE_DELTA",
  "data": {"key": "context_window", "value": 0.43},
  "session_id": "..."
}
```

`context_window` is a float 0.0–1.0 representing percentage used.

### `INTERRUPT`

Human approval required before proceeding.

```json
{
  "type": "INTERRUPT",
  "data": {
    "context": "The agent wants to delete 3 files. Approve?",
    "options": ["approve", "reject"]
  },
  "session_id": "..."
}
```

After receiving an `INTERRUPT`, the client calls `POST /stream/{session_id}/resume` with the chosen option.

### `DONE`

Stream complete. The `EventSource` should be closed.

```json
{"type": "DONE"}
```

---

## Internal Event Types

Backend event types (Python, in `transport/events.py`):

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
    result_summary: str
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

The `transport/agui/adapter.py` converts these to wire-format JSON. The `transport/agui/endpoint.py` manages per-session queues.

---

## Session Queue Lifecycle

```python
# Create or get queue (idempotent)
queue = get_event_queue(session_id)  # asyncio.Queue[InternalEvent | None]

# Producer (background task) pushes events
await queue.put(TextDeltaEvent(text="Hello", session_id=sid))
await queue.put(None)  # None sentinel → DONE

# Consumer (SSE endpoint) drains queue
event = await asyncio.wait_for(queue.get(), timeout=30.0)
if event is None:
    yield 'data: {"type": "DONE"}\n\n'
    break
yield f"data: {serialize_event(event)}\n\n"

# Cleanup on stream close
cleanup_session(session_id)
```

---

## Client Integration (TypeScript)

From `seshat-pwa/src/lib/agui-client.ts`:

### Sending a message

```typescript
await sendChatMessage({
  message: "Hello Seshat",
  sessionId: generateUUID(),  // use uuid.ts polyfill for Safari compat
  profile: "cloud",           // or "local"
});
```

### Connecting to the SSE stream

```typescript
const conn = connectToStream(
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
        if (event.data.key === "context_window") {
          updateContextMeter(event.data.value);
        }
        break;
      case "INTERRUPT":
        showApprovalDialog(event.data.context, event.data.options);
        break;
      case "DONE":
        conn.close();
        break;
    }
  },
  (error) => conn.close(),
);
```

### UUID generation (Safari polyfill)

Safari on plain HTTP (private IP) rejects `crypto.randomUUID()`. Use:

```typescript
// seshat-pwa/src/lib/uuid.ts
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

`_process_chat_stream_background` in `service/app.py`:

```
set_current_profile(load_profile(profile_name))
  │
  ▼
get_or_create_session(session_uuid)
  │
  ▼
hydrate_messages(prior_messages)
  │
  ▼
append_user_message(db)
  │
  ▼
run_gateway_pipeline(...)     ← intent, decomposition, memory context
  │
  ▼
orchestrator.handle_user_request(...)   ← LLM calls via profile-aware factory
  │
  ▼
queue.put(TextDeltaEvent(text=reply))
  │
  ▼
queue.put(None)               ← always sent, even on error
  │
  ▼
append_assistant_message(db)
```

The `None` sentinel is guaranteed in a `finally` block so the SSE client always closes cleanly.

---

## Caddy Routing

The Caddyfile (`config/cloud-sim/Caddyfile`) must list `/chat/stream` explicitly:

```caddyfile
@backend path /api/* /chat /chat/stream /stream/* /docs /docs/* /openapi.json /redoc
handle @backend {
    reverse_proxy seshat-gateway:9001
}
```

`path` does exact matching for paths without wildcards. `/chat` does not match `/chat/stream`.

---

## Known Limitations

| Limitation | Status | Planned fix |
|------------|--------|-------------|
| Full response arrives as single TEXT_DELTA (no streaming) | Current | Wire LiteLLM async streaming to push tokens as they arrive |
| SSE queue not persisted across gateway restarts | Current | Reconnect after restart; queue is in-memory |
| No authentication on `/stream/{session_id}` | Current | Anyone who knows the UUID can read the stream |
| HITL resume not wired to orchestrator | Partial | `InterruptEvent` defined but orchestrator resume path incomplete |
