# ADR-0075: WebSocket Transport + Durable Channel

**Status:** Proposed
**Date:** 2026-05-27
**Issue:** TBD (create Linear issue from this ADR)
**Supersedes:** ADR-0046 (AG-UI SSE transport)
**Related:** ADR-0049 (UITransportProtocol), ADR-0076 (adaptive constraint governance)

## Context

The AG-UI transport layer (ADR-0046) was introduced as SSE (Server-Sent Events) — a reasonable choice for a simple unidirectional text stream. The system has since grown three interactive features that SSE cannot handle natively:

1. **Tool approval round-trips** — SSE pushes an event; a separate `POST /approval/{request_id}` carries the decision; an `asyncio.Future` registered in `approval_waiter.py` bridges the two. A network blip between push and POST can orphan the Future, leaving the executor blocked indefinitely until the Future's TTL fires.

2. **HITL interrupts** — `send_interrupt()` in `AGUITransport` returns `None` immediately with a comment "deferred to FRE-209". The feature exists in the protocol but is unimplementable with SSE without adding yet another Future + POST endpoint.

3. **Reconnect loss** — when the user switches screens on an iOS PWA, the SSE connection drops. `cleanup_session()` destroys the in-memory `asyncio.Queue` on disconnect. The background task continues running and puts its result into the (now destroyed) queue. The response is lost.

Each new interactive event type would require its own POST endpoint, its own Future registration pattern, and its own TTL logic. The pattern does not scale and is already fragile.

`protocols.py:4` explicitly anticipates this: "Future implementations may target WebSocket, SSE, AG-UI, or other transports without changing consumer code."

### Why not fix SSE instead?

SSE is structurally unidirectional. The `approval_waiter.py` Future registry is not a bug to be fixed — it is the inherent cost of bidirectional interaction over a one-way protocol. Adding reconnect replay to SSE (via `Last-Event-ID`) requires the same Postgres event buffering work as WebSockets, without getting the bidirectionality benefit.

## Decision

Replace the SSE transport with a WebSocket transport. A single persistent WS connection per session carries all events in both directions, eliminating the need for separate POST endpoints and Future registries.

### Wire protocol

**Connection:** `GET /ws/{session_id}` — upgraded to WebSocket.

On connect, client sends one `CONNECT` message:
```json
{"type": "CONNECT", "last_seq": 0}
```
`last_seq` is the sequence number of the last event the client received. `0` means fresh connection. The server replays all events with `seq > last_seq` from the `session_events` Postgres table before resuming the live stream.

**Server → client message envelope** (all event types add a `seq` field):
```json
{"type": "TEXT_DELTA",      "seq": 42, "data": {"text": "..."}, "session_id": "..."}
{"type": "TOOL_CALL_START", "seq": 43, "data": {"tool_name": "...", "args": {...}}}
{"type": "TOOL_CALL_END",   "seq": 44, "data": {"tool_name": "...", "result": "..."}}
{"type": "STATE_DELTA",     "seq": 45, "data": {"key": "context_tokens", "value": 12400}}
{"type": "INTERRUPT",       "seq": 46, "request_id": "...", "data": {"context": "...", "options": [...]}}
{"type": "CONSTRAINT_PAUSE","seq": 47, "request_id": "...", "data": {...}}
{"type": "DONE",            "seq": 48}
```

**Client → server messages** (new capability):
```json
{"type": "APPROVAL_DECISION",    "request_id": "...", "decision": "approve", "reason": null}
{"type": "CONSTRAINT_DECISION",  "request_id": "...", "decision": "Continue"}
{"type": "INTERRUPT_RESPONSE",   "request_id": "...", "choice": "approve"}
{"type": "PING"}
```

The server responds to `PING` with `{"type": "PONG"}`. The client sends `PING` every 25s to keep the connection alive through Cloudflare Tunnel's idle timeout.

### Internal architecture

The `asyncio.Queue` is retained as an internal buffer decoupling the background task from the WS connection state. Events flow:

```
Background task
    ↓
    push(event) to asyncio.Queue
    AND append to session_events (Postgres) with incrementing seq
        ↓
WS sender coroutine drains queue → ws.send_text(json)
        ↑
WS receiver coroutine reads incoming messages → routes to registered decision waiters
```

If the WS connection drops while the background task is running, queue events accumulate. On reconnect with `last_seq: N`, the server replays `session_events WHERE seq > N` before switching to the live queue. This guarantees no event is lost.

### Decision waiters (replaces approval_waiter.py)

A simple `dict[request_id, asyncio.Event]` registered on the WS handler replaces the Future registry. The receiver coroutine sets the event when the matching `APPROVAL_DECISION` or `CONSTRAINT_DECISION` message arrives. Because the WS connection is the only path for decisions, no TTL mismatch is possible — if the WS closes, the waiter is cancelled with `ConnectionResetError`, the executor handles it, and the connection state drives the cancellation (not a ticking TTL).

```python
# WS handler router (simplified)
match message["type"]:
    case "APPROVAL_DECISION" | "CONSTRAINT_DECISION" | "INTERRUPT_RESPONSE":
        _resolve_waiter(message["request_id"], message)
    case "PING":
        await ws.send_text('{"type": "PONG"}')
```

### Postgres event buffer

New table (added to `docker/postgres/migrations/`):

```sql
CREATE TABLE session_events (
    id           BIGSERIAL PRIMARY KEY,
    session_id   UUID NOT NULL,
    seq          INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);
CREATE INDEX idx_session_events_replay ON session_events (session_id, seq);
```

A background cleanup task (runs every hour) deletes events older than 24 hours. The 24-hour TTL covers the iOS PWA backgrounding case and short-term device sleep; it is not intended as long-term archival (the `session_messages` table already provides durable conversation history).

The `seq` counter is per-session, starting at 1. The server tracks the current seq in a `dict[session_id, int]` in memory, initialised from `MAX(seq) WHERE session_id = ?` on first event write.

### Files changed

**Remove:**
- `transport/agui/approval_waiter.py` — Future registry retired
- `transport/agui/endpoint.py` — SSE `_event_generator`, `cleanup_session()`, `_session_queues`
- Route `POST /agui/approval/{request_id}` (approval endpoint moved to WS)
- Route `POST /stream/{session_id}/resume` (interrupt resume moved to WS)

**Add:**
- `transport/agui/ws_endpoint.py` — WS connection handler, sender coroutine, receiver router, decision waiter registry, reconnect replay from Postgres
- `transport/agui/event_buffer.py` — `SessionEventBuffer`: append to `session_events`, seq counter, replay query

**Modify:**
- `transport/agui/transport.py` — `AGUITransport`: methods unchanged externally (same `UITransportProtocol` contract); internally push to queue, which is drained by WS sender
- `transport/agui/adapter.py` — `to_agui_event()`: add `seq` field to all event envelopes
- `transport/events.py` — add `ConstraintPauseEvent` (required by ADR-0076)
- `service/app.py` — mount `ws_router` instead of `sse_router`; `_process_chat_stream_background` unchanged (still pushes to asyncio.Queue)

**PWA (`seshat-pwa/src/lib/agui-client.ts`):**
- Replace `connectToStream` (fetchEventSource) with `connectWebSocket` (native `WebSocket`)
- Replace `postApprovalDecision` and `resumeInterrupt` with `sendWSMessage()`
- Add reconnect loop: exponential backoff (1s, 2s, 4s, …, cap 30s), send `{type: "CONNECT", last_seq: N}` on each attempt
- Track `last_seq` from received events

**PWA (`seshat-pwa/src/components/StreamingChat.tsx`):**
- Event handler interface unchanged; WS `onmessage` replaces SSE `onmessage`
- Remove `postApprovalDecision` call site; send approval decision via WS

### What does NOT change

- `InternalEvent` frozen dataclasses (`TextDeltaEvent`, `ToolStartEvent`, etc.) — wire format is the same JSON
- `asyncio.Queue` as internal buffer
- `_process_chat_stream_background` logic
- `UITransportProtocol` interface — `AGUITransport` still satisfies it
- All existing event type names (`TEXT_DELTA`, `TOOL_CALL_START`, etc.)

## Consequences

**Positive:**
- Bidirectional by construction — no more SSE + POST workarounds
- `send_interrupt()` becomes fully implementable (FRE-209 unblocked)
- Tool approvals, constraint pauses, HITL interrupts all share the same WS round-trip pattern
- iOS screen-switch reconnect works via `last_seq` replay
- `approval_waiter.py` fragility eliminated — WS connection state drives cancellation

**Negative:**
- WS reconnect logic must be implemented in the PWA (EventSource auto-retries; WS does not)
- `fetchEventSource` library is replaced by native WS API (net reduction in dependency, but a rewrite)
- Cloudflare Tunnel idle timeout (typically 100s) requires the 25s PING keepalive

**Neutral:**
- AG-UI protocol supports both SSE and WS; this is an in-spec migration

## Implementation phases

**Phase 1 (this ADR):** WS endpoint + Postgres event buffer + reconnect replay + PWA WS client
**Phase 2 (ADR-0076):** `CONSTRAINT_PAUSE` event type wired into the executor

## Acceptance criteria

| Criterion | Verification |
|---|---|
| SSE endpoint removed | `GET /stream/{session_id}` returns 404 |
| WS endpoint responds | `wscat -c ws://localhost:9000/ws/{session_id}` connects and receives `DONE` after a chat turn |
| Reconnect replay | Disconnect mid-turn; reconnect with `last_seq: N`; all missed events arrive in order |
| Approval round-trip | Tool approval card in PWA; decision sent via WS; executor receives it and continues |
| `litellm_commit_failed` gone | No expired-reservation errors after gate.py TTL fix + rebuild |
| Postgres event TTL | `session_events` rows older than 24h are purged by cleanup task |
| iOS screen-switch | Switch screens during a 30s agent run; return; full response rendered |
