# ADR-0075: WebSocket Transport + Durable Channel

**Status:** Implemented — FRE-388 shipped (SSE→WS, Postgres replay buffer, ticket auth)
**Date:** 2026-05-27
**Issue:** FRE-388
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

Replace the SSE transport with a WebSocket transport. A single persistent WSS connection per session carries all events in both directions, eliminating the need for separate POST endpoints and Future registries.

### Transport security

WSS (`wss://`) is required for all external connections. The Cloudflare Tunnel terminates TLS at the edge and proxies over plain WS internally to the origin (same pattern as the existing Neo4j WebSocket split via Caddy). The FastAPI app listens on `ws://` locally; all external traffic is `wss://` through the tunnel. Plain `ws://` from external origins never reaches the app — Cloudflare's tunnel ingress only accepts HTTPS, so the unencrypted WebSocket upgrade is impossible from outside. Local dev (`localhost`) is the only context where `ws://` is permissible.

### Authentication and authorization

The WS handshake is an HTTP `GET` with `Upgrade: websocket`. Authentication happens **before** `await websocket.accept()`:

1. **Token extraction** — the bearer token is read from the `Authorization` header (same as the current SSE endpoint). If missing or invalid, the server responds with HTTP 403 before accepting the socket.
2. **Session ownership** — `SessionRepository.get(session_id, user_id=request_user.user_id)` verifies the caller owns the requested session. If not, HTTP 404 (do not confirm existence of other users' sessions).
3. **Origin validation** — the `Origin` header is checked against an allowlist (`AGENT_ALLOWED_WS_ORIGINS`, defaulting to the production PWA hostname and `localhost` for dev). Requests with a missing or non-allowlisted Origin are rejected with HTTP 403 before acceptance. This prevents cross-site WebSocket hijacking (RFC 6455 §10.2).

Only after all three checks pass does the server call `await websocket.accept()`.

**Message-level authorization:** every client → server message that references a `request_id` is validated: the `request_id` must correspond to an active waiter registered against the authenticated session. Messages with unknown or mismatched `request_id` values are logged and silently dropped (no error frame to avoid information leakage).

**Inbound message limits:**
- Maximum message size: 8 KB (client messages are small JSON; anything larger is malformed or adversarial)
- Rate limit: 20 messages/second per connection (burst); exceeding closes the socket with code 1008 (Policy Violation)
- Maximum payload for `reason` fields: 500 characters (truncated silently)

### Wire protocol

**Connection:** `GET /ws/{session_id}` — upgraded to WebSocket after auth (see above).

On successful accept, the client sends one `CONNECT` message:
```json
{"type": "CONNECT", "last_seq": 0}
```
`last_seq` is the sequence number of the last event the client received. `0` means fresh connection. The server replays all events with `seq > last_seq` from the `session_events` Postgres table, then switches to the live queue. See "Replay-to-live handoff" below for atomicity guarantees.

If `last_seq` is older than the 24-hour retention window (i.e. no rows exist for `seq > last_seq`), the server sends a `{"type": "REPLAY_GAP", "oldest_available_seq": N}` message so the PWA can fetch the full conversation via `GET /api/v1/sessions/{id}/messages` instead of relying on partial replay.

**Server → client message envelope** (all event types carry a `seq` field):
```json
{"type": "TEXT_DELTA",      "seq": 42, "data": {"text": "..."}, "session_id": "..."}
{"type": "TOOL_CALL_START", "seq": 43, "data": {"tool_name": "...", "args": {...}}}
{"type": "TOOL_CALL_END",   "seq": 44, "data": {"tool_name": "...", "result": "..."}}
{"type": "STATE_DELTA",     "seq": 45, "data": {"key": "context_tokens", "value": 12400}}
{"type": "INTERRUPT",       "seq": 46, "request_id": "...", "data": {"context": "...", "options": [...]}}
{"type": "CONSTRAINT_PAUSE","seq": 47, "request_id": "...", "data": {...}}
{"type": "DONE",            "seq": 48}
{"type": "PONG",            "seq": null}
{"type": "REPLAY_GAP",      "seq": null, "oldest_available_seq": 15}
```

**Client → server messages** (new capability):
```json
{"type": "APPROVAL_DECISION",    "request_id": "...", "decision": "approve", "reason": null}
{"type": "CONSTRAINT_DECISION",  "request_id": "...", "decision": "Continue"}
{"type": "INTERRUPT_RESPONSE",   "request_id": "...", "choice": "approve"}
{"type": "PING"}
```

**Heartbeat:** The client sends `{"type": "PING"}` every 25s. This is an application-level heartbeat, not RFC 6455 control-frame Ping/Pong, because Cloudflare Tunnel may not transparently proxy control frames. The server responds with `{"type": "PONG"}`. The server also monitors inbound message activity: if no message (including PING) arrives within 60s, the server closes the socket with code 1001 (Going Away) and cleans up the connection state.

### Connection lifecycle and multi-connection policy

**One active connection per session.** If a new WS connection is authenticated for a session that already has an active connection (e.g. iOS reconnect while the old half-open socket hasn't timed out yet), the server:

1. Closes the old socket with code 4001 ("Superseded by new connection")
2. Cancels any waiters registered on the old connection
3. Accepts the new connection as the active one

This prevents split-brain scenarios where two sockets receive events or race on decision approvals. The PWA handles code 4001 by not triggering its reconnect loop (the closure was intentional).

### Internal architecture

The `asyncio.Queue` is retained as an internal buffer decoupling the background task from the WS connection state. Events flow:

```
Background task
    ↓
    push(event) to asyncio.Queue
    AND append to session_events (Postgres) with DB-assigned seq
        ↓
WS sender task drains queue → ws.send_text(json)
        ↑
WS receiver task reads incoming messages → routes to registered decision waiters
```

**Backpressure:** The `asyncio.Queue` is bounded to 500 items. If the queue is full (client socket dead or slow), new events are still written to Postgres (durable path) but the queue put raises `QueueFull` — the background task catches this, logs a warning, and continues. The client will receive these events on reconnect via replay. This prevents unbounded memory growth from a dead socket.

**Single-writer invariant:** All outbound WS I/O goes through a single sender task that drains the queue and calls `ws.send_text()`. The receiver task never calls `ws.send_text()` directly — it enqueues PONG responses into the same outbound queue. This prevents concurrent send races that would corrupt the ASGI connection state.

### Replay-to-live handoff

The transition from Postgres replay to live queue drain must not drop or duplicate events. The handoff works:

1. Before starting replay, the sender task snapshots the queue (drains all currently-queued items into a local list but does not send them yet).
2. Replay from Postgres: `SELECT payload, seq FROM session_events WHERE session_id = ? AND seq > ? ORDER BY seq` — send each event over WS.
3. Deduplicate: the local list from step 1 may contain events already replayed from Postgres (they were written to both paths). Filter the local list to keep only events with `seq > max_replayed_seq`.
4. Send remaining local-list events.
5. Switch to normal queue-drain loop.

The Postgres `seq` is the source of truth for ordering. Events written to Postgres before the replay query executes are covered by the query; events written after are in the queue and sent in step 4/5.

### Decision waiters (replaces approval_waiter.py)

A `dict[str, asyncio.Event]` plus a `dict[str, dict]` (for the decision payload) registered on the WS handler replaces the Future registry. The receiver task sets the event when the matching `APPROVAL_DECISION`, `CONSTRAINT_DECISION`, or `INTERRUPT_RESPONSE` message arrives.

**Lifecycle guarantees:**
- **Timeout:** Each waiter has an `expires_at` timestamp. A background check (piggy-backed on the receiver loop) applies the `default_option` and sets the event if the timeout elapses with no client response.
- **Cancellation on disconnect:** When the WS closes (client disconnect or server-side close), all active waiters for that session are resolved with a synthetic `{"decision": "connection_lost"}` payload. The executor treats this the same as the `default_option`.
- **Duplicate responses:** If the client sends a decision for an already-resolved `request_id`, the message is silently dropped (logged at debug level).
- **Unknown `request_id`:** Logged and dropped (see "Message-level authorization" above).
- **Reconnect during active waiter:** If the client disconnects and reconnects while a `CONSTRAINT_PAUSE` or `INTERRUPT` is pending, the server re-sends the pending event during replay (it's in `session_events`). The client renders the decision card again; the user responds on the new connection.

**Exception model:** Starlette surfaces client disconnects from `websocket.receive_text()` as `WebSocketDisconnect` (not `ConnectionResetError`). The WS handler catches `WebSocketDisconnect` and `WebSocketClose` specifically, triggering waiter cleanup. Send-side failures from `websocket.send_text()` on a closed socket raise `RuntimeError` in Starlette — the sender task catches this and breaks out of its drain loop.

```python
# WS receiver routing (simplified)
match message["type"]:
    case "APPROVAL_DECISION" | "CONSTRAINT_DECISION" | "INTERRUPT_RESPONSE":
        _resolve_waiter(message["request_id"], message)
    case "PING":
        await _outbound_queue.put({"type": "PONG"})
```

### Postgres event buffer

New table (added to `docker/postgres/migrations/`):

```sql
CREATE TABLE session_events (
    id           BIGSERIAL PRIMARY KEY,
    session_id   UUID NOT NULL,
    seq          INTEGER NOT NULL DEFAULT nextval('session_events_seq_seq'),
    event_type   TEXT NOT NULL,
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);
CREATE INDEX idx_session_events_replay ON session_events (session_id, seq);
```

**Sequence generation:** The `seq` counter is generated by a Postgres sequence (`session_events_seq_seq`), not an in-memory counter. This is safe across process restarts and multiple Uvicorn workers. The sequence is global (not per-session), but the `UNIQUE(session_id, seq)` constraint ensures per-session uniqueness and the `ORDER BY seq` in the replay query preserves insertion order. A per-session sequence would require dynamic DDL; the global sequence with per-session uniqueness is simpler and sufficient.

**Note on single-worker deployment:** The current deployment is single-worker Uvicorn behind Cloudflare Tunnel. The Postgres-backed seq is forward-compatible with multi-worker if needed, but the in-memory queue and connection registry are per-process. Multi-worker would require a shared pub-sub layer (e.g. Redis) for cross-process event delivery. This is explicitly out of scope — the single-worker constraint is documented here so a future multi-worker migration knows what to address.

A background cleanup task (runs every hour) deletes events older than 24 hours. The 24-hour TTL covers the iOS PWA backgrounding case and short-term device sleep; it is not intended as long-term archival (the `session_messages` table already provides durable conversation history).

### iOS PWA lifecycle

iOS aggressively suspends PWA processes when the app is backgrounded or the screen locks. The WebSocket will silently die — no `close` event fires reliably. The design assumes this as the normal case, not an edge case.

**Page Visibility integration:** The PWA listens for `pagehide` / `visibilitychange` events:
- On `pagehide` or `visibilitychange` to `hidden`: persist `last_seq` to `localStorage` (key: `seshat_last_seq_{sessionId}`). The socket is not explicitly closed (iOS may or may not deliver the close frame).
- On `pageshow` or `visibilitychange` to `visible`: read `last_seq` from `localStorage`, open a new WS connection with `{type: "CONNECT", last_seq: N}`.

This ensures `last_seq` survives process termination. The `localStorage` write is synchronous and completes before iOS suspends the process.

**Reconnect loop:**
- Exponential backoff: 1s + jitter, 2s + jitter, 4s + jitter, … capped at 30s
- Jitter: random 0–500ms added to each delay to prevent reconnect storms (RFC 6455 §7.2.3)
- On successful connect: reset backoff to 1s
- On code 4001 (superseded): do not reconnect (another tab/connection took over)

### FastAPI / Starlette implementation notes

**Auth before accept:** Starlette allows inspecting headers and query parameters on the `WebSocket` object before calling `accept()`. The auth and origin checks use `websocket.headers` and respond with `await websocket.close(code=1008)` on failure — this sends an HTTP 403 before the upgrade completes.

**Dependency injection:** The existing `get_request_user` dependency is HTTP-specific (reads from `Request`). A `get_ws_user` variant extracts the bearer token from `websocket.headers["authorization"]` and performs the same CF Access JWT validation. Session lookup reuses `SessionRepository` unchanged.

### Files changed

**Remove:**
- `transport/agui/approval_waiter.py` — Future registry retired
- `transport/agui/endpoint.py` — SSE `_event_generator`, `cleanup_session()`, `_session_queues`
- Route `POST /agui/approval/{request_id}` (approval endpoint moved to WS)
- Route `POST /stream/{session_id}/resume` (interrupt resume moved to WS)

**Add:**
- `transport/agui/ws_endpoint.py` — WS connection handler (auth, accept, sender task, receiver task, waiter registry, reconnect replay, multi-connection eviction)
- `transport/agui/event_buffer.py` — `SessionEventBuffer`: append to `session_events` via Postgres sequence, replay query, cleanup task, `REPLAY_GAP` detection

**Modify:**
- `transport/agui/transport.py` — `AGUITransport`: methods unchanged externally (same `UITransportProtocol` contract); internally push to bounded queue + write to `SessionEventBuffer`
- `transport/agui/adapter.py` — `to_agui_event()`: add `seq` field to all event envelopes
- `transport/events.py` — add `ConstraintPauseEvent` (required by ADR-0076)
- `service/app.py` — mount `ws_router` instead of `sse_router`; `_process_chat_stream_background` unchanged (still pushes to asyncio.Queue)

**PWA (`seshat-pwa/src/lib/agui-client.ts`):**
- Replace `connectToStream` (fetchEventSource) with `connectWebSocket` (native `WebSocket`)
- Replace `postApprovalDecision` and `resumeInterrupt` with `sendWSMessage()`
- Add reconnect loop: exponential backoff with jitter, send `{type: "CONNECT", last_seq: N}` on each attempt
- Persist `last_seq` to `localStorage` on every received event and on `pagehide`
- Handle code 4001 (superseded) — do not reconnect

**PWA (`seshat-pwa/src/components/StreamingChat.tsx`):**
- Event handler interface unchanged; WS `onmessage` replaces SSE `onmessage`
- Remove `postApprovalDecision` call site; send approval decision via WS
- Handle `REPLAY_GAP` event: fall back to `getSessionMessages()` API for full history

### What does NOT change

- `InternalEvent` frozen dataclasses (`TextDeltaEvent`, `ToolStartEvent`, etc.) — wire format is the same JSON
- `asyncio.Queue` as internal buffer (now bounded to 500)
- `_process_chat_stream_background` logic
- `UITransportProtocol` interface — `AGUITransport` still satisfies it
- All existing event type names (`TEXT_DELTA`, `TOOL_CALL_START`, etc.)

## Consequences

**Positive:**
- Bidirectional by construction — no more SSE + POST workarounds
- `send_interrupt()` becomes fully implementable (FRE-209 unblocked)
- Tool approvals, constraint pauses, HITL interrupts all share the same WS round-trip pattern
- iOS screen-switch reconnect works via `last_seq` replay from Postgres
- `approval_waiter.py` fragility eliminated — WS connection state drives cancellation
- Postgres-backed seq is safe across restarts and forward-compatible with multi-worker

**Negative:**
- WS reconnect logic must be implemented in the PWA (EventSource auto-retries; WS does not)
- `fetchEventSource` library is replaced by native WS API (net reduction in dependency, but a rewrite)
- Cloudflare Tunnel idle timeout requires the 25s PING keepalive
- Single-worker constraint is now documented and load-bearing (queue + connection registry are per-process)

**Neutral:**
- AG-UI protocol supports both SSE and WS; this is an in-spec migration

## Implementation phases

**Phase 1 (this ADR):** WS endpoint + Postgres event buffer + reconnect replay + PWA WS client
**Phase 2 (ADR-0076):** `CONSTRAINT_PAUSE` event type wired into the executor

## Acceptance criteria

| Criterion | Verification |
|---|---|
| SSE endpoint removed | `GET /stream/{session_id}` returns 404 |
| WS endpoint responds | `wscat -c ws://localhost:9000/ws/{session_id}` connects locally and receives `DONE` after a chat turn |
| WSS enforced externally | Production PWA connects via `wss://`; Cloudflare Tunnel rejects plain `ws://` at ingress |
| Auth before accept | WS connection without valid bearer token receives HTTP 403 (socket never upgraded) |
| Origin validation | WS connection with non-allowlisted `Origin` header receives HTTP 403 |
| Session ownership | WS connection for another user's session receives HTTP 404 |
| Multi-connection eviction | Second WS for same session closes the first with code 4001 |
| Reconnect replay | Disconnect mid-turn; reconnect with `last_seq: N`; all missed events arrive in order |
| Replay gap detection | Reconnect with `last_seq` older than 24h retention; receive `REPLAY_GAP` event |
| Approval round-trip | Tool approval card in PWA; decision sent via WS; executor receives and continues |
| Waiter cleanup on disconnect | WS drops during pending approval; waiter resolves with `connection_lost` |
| `last_seq` persistence | Kill PWA process; reopen; `last_seq` read from `localStorage`; replay is correct |
| Backpressure | Slow/dead client: queue at 500 cap; events still written to Postgres; no OOM |
| Postgres event TTL | `session_events` rows older than 24h are purged by cleanup task |
| iOS screen-switch | Switch screens during a 30s agent run; return; full response rendered |
| Inbound rate limit | >20 msg/s closes socket with 1008 |
