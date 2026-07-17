# Implementation Plan: FRE-388 — WebSocket Transport + Durable Channel

**ADR:** [ADR-0075](../../architecture_decisions/ADR-0075-websocket-transport.md)
**Linear:** FRE-388 (Approved, Tier-1:Opus)
**Branch:** `fre-388-websocket-transport`

---

## Context

The current SSE transport layer (ADR-0046) is fundamentally unidirectional. Every interactive feature requires a workaround: tool approvals use a separate `POST /approval/{request_id}` endpoint bridged by an in-memory `asyncio.Future` registry (`approval_waiter.py`); HITL interrupts are unimplementable (`send_interrupt()` returns `None`); and iOS PWA screen-switches kill the SSE connection — `cleanup_session()` destroys the queue and the response is lost.

ADR-0075 replaces SSE with a single bidirectional WebSocket connection per session. Events are durably buffered in a Postgres `session_events` table with sequence numbers, enabling reconnect replay. The `approval_waiter.py` Future registry is retired — decision round-trips flow over the same WS connection.

This unblocks FRE-389 (ADR-0076 constraint governance), which needs arbitrary bidirectional event types without adding new POST endpoints.

---

## Implementation Steps

### Step 1: Postgres schema — `session_events` table

**Create:** `docker/postgres/migrations/0005_websocket_session_events.sql`
**Modify:** `docker/postgres/init.sql` — add `session_events` table + sequence

```sql
CREATE SEQUENCE IF NOT EXISTS session_events_seq;

CREATE TABLE IF NOT EXISTS session_events (
    id           BIGSERIAL PRIMARY KEY,
    session_id   UUID NOT NULL REFERENCES sessions(session_id),
    seq          INTEGER NOT NULL DEFAULT nextval('session_events_seq'),
    event_type   TEXT NOT NULL,
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_session_events_replay
    ON session_events (session_id, seq);
```

Follow existing migration pattern (idempotent DDL, header comment with ADR/FRE reference, `BEGIN`/`COMMIT` wrapper).

**Add ORM model:** `src/personal_agent/service/models.py` — `SessionEventModel(Base)` with columns matching the SQL schema.

---

### Step 2: Event buffer — Postgres persistence + replay

**Create:** `src/personal_agent/transport/agui/event_buffer.py`

Class `SessionEventBuffer`:
- `async append(session_id: UUID, event_type: str, payload: dict) -> int` — INSERT into `session_events`, return assigned `seq`
- `async replay(session_id: UUID, after_seq: int) -> list[dict]` — `SELECT payload, seq FROM session_events WHERE session_id = ? AND seq > ? ORDER BY seq`
- `async oldest_available_seq(session_id: UUID) -> int | None` — for REPLAY_GAP detection
- `async cleanup_expired(ttl_hours: int = 24) -> int` — `DELETE FROM session_events WHERE created_at < now() - interval`
- Uses `AsyncSessionLocal` from `service.database` (same pattern as `SessionRepository`)

No user_id scoping on this table — session ownership is validated at the WS handshake; the event buffer trusts the session_id it receives from the authenticated handler.

---

### Step 3: Config — WS-specific settings

**Modify:** `src/personal_agent/config/settings.py`

Add fields:
- `allowed_ws_origins: list[str]` — default `["https://seshat.example.com", "http://localhost:3000"]`
- `ws_ping_timeout_seconds: int = 60` — server closes socket if no inbound message within this window
- `ws_max_message_size: int = 8192` — reject inbound messages larger than 8 KB
- `ws_rate_limit_per_second: int = 20` — inbound message rate cap
- `ws_event_queue_size: int = 500` — bounded asyncio.Queue size
- `ws_event_ttl_hours: int = 24` — session_events cleanup window

All with `AGENT_` env prefix per existing convention.

---

### Step 4: WS authentication — ticket-based handshake

Browsers don't support custom headers on WebSocket connections. Passing the real auth token as a query parameter leaks it to proxy logs, error telemetry, and browser dev tools. Instead, use a **short-lived single-use WS ticket** minted over HTTPS.

**Flow:**
1. PWA calls `POST /api/ws-ticket` with the normal `Authorization: Bearer` header
2. Server validates the bearer token (same as `get_request_user`), mints a ticket scoped to `(user_id, session_id)`, stores it in an in-memory dict with 30s TTL
3. PWA opens `wss://host/ws/{session_id}?ticket=<ticket>`
4. WS endpoint validates the ticket, marks it as used (single-use), extracts the authenticated user

**Create:** `src/personal_agent/service/ws_ticket.py`

```python
@dataclass(frozen=True)
class WSTicket:
    user_id: UUID
    session_id: UUID
    email: str
    display_name: str | None
    expires_at: float  # time.monotonic() deadline
    
_pending_tickets: dict[str, WSTicket] = {}  # ticket_id → WSTicket

def mint_ws_ticket(user: RequestUser, session_id: UUID, ttl_seconds: int = 30) -> str:
    """Mint a single-use WS ticket. Returns the ticket string."""
    
def consume_ws_ticket(ticket_id: str, session_id: UUID) -> RequestUser | None:
    """Validate and consume a ticket. Returns None if expired/used/mismatched."""
```

Ticket ID: `secrets.token_urlsafe(32)` — cryptographically random, 43 chars.

**Modify:** `src/personal_agent/service/app.py` (or a new `ws_ticket_router`)

Add endpoint:
```
POST /api/ws-ticket
Body: {"session_id": "..."}
Auth: Depends(get_request_user)
Response: {"ticket": "...", "expires_in": 30}
```
Validates session ownership before minting.

**Modify:** WS endpoint (Step 5) — extract ticket from `websocket.query_params["ticket"]`, call `consume_ws_ticket()`. On failure: close with 1008 before accept.

**Dev fallback:** When `gateway_auth_enabled=False`, the WS endpoint falls back to reading `Cf-Access-Authenticated-User-Email` header (same as existing dev mode) — no ticket needed in local dev.

---

### Step 5: WS endpoint — connection handler

**Create:** `src/personal_agent/transport/agui/ws_endpoint.py`

**Router:** `ws_router = APIRouter()`

**Route:** `GET /ws/{session_id}` — WebSocket endpoint

**Connection lifecycle:**
1. **Auth before accept** — call `get_ws_user(websocket)`, validate Origin header against `allowed_ws_origins`, verify session ownership via `SessionRepository.get(session_id, user_id=user.user_id)`. On any failure: `await websocket.close(code=1008)` before accept.
2. **Multi-connection eviction** — check `_active_connections[session_id]`; if exists, close old socket with code 4001, cancel its waiters.
3. **Accept** — `await websocket.accept()`
4. **Spawn sender + receiver tasks** as `asyncio.Task`; wait for either to complete; cancel the other.

**Sender task:**
- Receive `CONNECT` message from client → extract `last_seq`
- Snapshot current queue contents into local list
- Replay from Postgres via `event_buffer.replay(session_id, after_seq=last_seq)`
- Deduplicate local list against replayed seq values
- Send remaining local-list items
- Switch to normal queue-drain loop: `event = await queue.get()` → `ws.send_text(json.dumps(event))`
- Catch `RuntimeError` (Starlette: send on closed socket) → break

**Receiver task:**
- Loop: `msg = await websocket.receive_text()` → parse JSON
- Rate limiting: sliding window counter, close with 1008 if exceeded
- Message size check against `ws_max_message_size`
- Route by `msg["type"]`:
  - `PING` → enqueue `{"type": "PONG"}` to outbound queue
  - `APPROVAL_DECISION` / `CONSTRAINT_DECISION` / `INTERRUPT_RESPONSE` → resolve waiter
- Inactivity timeout: if no message within `ws_ping_timeout_seconds`, close with 1001
- Catch `WebSocketDisconnect` → trigger cleanup

**Decision waiter registry:**
- Per-connection dict: `_waiters: dict[str, asyncio.Event]` + `_waiter_payloads: dict[str, dict]`
- `register_waiter(request_id, expires_at, default_option)` → creates Event + schedules timeout
- `resolve_waiter(request_id, payload)` → sets event + stores payload
- On disconnect: resolve all pending waiters with `{"decision": "connection_lost"}`

**Connection registry:**
- Module-level `_active_connections: dict[str, WebSocket]` — one entry per session
- Cleaned up in `finally` block of handler

**Cleanup task:**
- Registered as FastAPI lifespan background task (hourly)
- Calls `event_buffer.cleanup_expired()`

---

### Step 6: Transport modification — dual write (queue + Postgres)

**Modify:** `src/personal_agent/transport/agui/transport.py`

- Replace `from .endpoint import get_event_queue` with a new function that returns a **bounded** `asyncio.Queue(maxsize=500)`
- Add `SessionEventBuffer` integration: every event pushed to the queue is also appended to Postgres via `event_buffer.append()`, which returns the assigned `seq`
- The `seq` is attached to the event dict before queueing
- On `QueueFull`: log warning, skip queue put (event is durable in Postgres; client will get it on reconnect)
- `request_tool_approval()` refactored: instead of `approval_waiter.register/wait`, it calls `ws_endpoint.register_waiter()` on the active connection and awaits the `asyncio.Event`

Key changes:
- Import `SessionEventBuffer` and `get_active_connection` from `ws_endpoint`
- `_get_or_create_queue()` replaces `get_event_queue()` — bounded, per-session, stored in module-level dict
- `_push_event()` helper: serialize → append to Postgres → attach seq → put on queue (or log warning on full)

---

### Step 7: Adapter modification — add `seq` field

**Modify:** `src/personal_agent/transport/agui/adapter.py`

- `to_agui_event()` signature: add `seq: int | None = None` parameter
- All returned dicts include `"seq": seq` when seq is not None
- `serialize_event()` updated to accept and pass through `seq`
- PONG and REPLAY_GAP events have `"seq": null` per ADR

---

### Step 8: Route mounting — swap SSE for WS

**Modify:** `src/personal_agent/service/app.py`

- Replace `from personal_agent.transport.agui.endpoint import router as transport_router` with `from personal_agent.transport.agui.ws_endpoint import ws_router as transport_router`
- Remove `from personal_agent.transport.agui.endpoint import get_event_queue` (line 45) — replace with the new queue accessor from `transport.py`
- `_process_chat_stream_background` still pushes to the asyncio.Queue via the transport layer — same pattern, just bounded now
- Register the hourly cleanup task in the FastAPI `lifespan` context manager

---

### Step 9: Remove SSE infrastructure

**Remove:** `src/personal_agent/transport/agui/endpoint.py` — entire SSE endpoint + approval POST route
**Remove:** `src/personal_agent/transport/agui/approval_waiter.py` — Future registry

**Modify:** `src/personal_agent/transport/agui/__init__.py` — update docstring from "SSE" to "WebSocket"

**Modify:** `src/personal_agent/transport/agui/transport.py` — remove all imports from `approval_waiter` and `endpoint`

---

### Step 10: PWA — WebSocket client

**Modify:** `seshat-pwa/src/lib/agui-client.ts`

Replace `connectToStream()` with `connectWebSocket()`:
- Uses native `WebSocket` API (no library needed)
- **Auth:** before opening the socket, call `POST /api/ws-ticket` with the normal bearer token to mint a short-lived single-use ticket. Then open `wss://host/ws/${sessionId}?ticket=${ticket}`. Each reconnect mints a fresh ticket.
- On open: send `{"type": "CONNECT", "last_seq": N}` where N is from `localStorage`
- On message: parse JSON → call `onEvent(parsed)` → persist `parsed.seq` to localStorage
- On close: if code !== 4001, trigger reconnect loop
- On error: trigger reconnect loop

Add `async function getWSTicket(sessionId: string): Promise<string>`:
- `POST ${SESHAT_API}/api/ws-ticket` with `Authorization: Bearer` header and `{"session_id": sessionId}` body
- Returns the `ticket` string from the response
- In local dev (no `GATEWAY_TOKEN`): returns empty string (ticket not required)

Replace `postApprovalDecision()` with `sendWSMessage()`:
- Sends `{"type": "APPROVAL_DECISION", "request_id": ..., "decision": ..., "reason": ...}` over WS

Replace `resumeInterrupt()` with `sendWSMessage()`:
- Sends `{"type": "INTERRUPT_RESPONSE", "request_id": ..., "choice": ...}` over WS

Add PING heartbeat: `setInterval(() => ws.send('{"type":"PING"}'), 25000)`

Remove `@microsoft/fetch-event-source` from `package.json`.

---

### Step 11: PWA — reconnect + replay logic

**Modify:** `seshat-pwa/src/hooks/useSSEStream.ts` (rename to `useWSStream.ts` or keep name)

- Add `lastSeqRef = useRef<number>(0)` — tracks highest received seq
- On every event with `seq`: update ref + write to `localStorage` key `seshat_last_seq_{sessionId}`
- Add `pagehide` / `visibilitychange` listeners: persist `lastSeqRef.current` to localStorage
- On `visibilitychange` to `visible`: reconnect with stored `last_seq`

Reconnect loop:
- Exponential backoff: 1s, 2s, 4s, ... capped at 30s
- Jitter: random 0–500ms
- On code 4001 (superseded): do not reconnect
- On successful connect: reset backoff

Handle `REPLAY_GAP` event:
- Fall back to `getSessionMessages()` API for full conversation history
- Reset message state from API response

Handle `PONG`: no-op (confirms server liveness)

---

### Step 12: PWA — component updates

**Modify:** `seshat-pwa/src/components/StreamingChat.tsx`
- Remove `resumeInterrupt()` import from `agui-client`
- `handleInterruptChoice()`: call hook method that sends via WS instead of separate POST
- No other structural changes — event handling is in the hook

**Modify:** `seshat-pwa/src/lib/types.ts`
- Add `seq: number | null` to `AGUIEvent` interface
- Add `REPLAY_GAP` and `PONG` to `AGUIEventType` union
- Add `CONNECT`, `APPROVAL_DECISION`, `CONSTRAINT_DECISION`, `INTERRUPT_RESPONSE`, `PING` as client message types

**Modify:** `seshat-pwa/package.json`
- Remove `@microsoft/fetch-event-source` dependency

---

### Step 13: Tests

**Create:** `tests/personal_agent/transport/agui/test_event_buffer.py`
- Test append + replay ordering
- Test REPLAY_GAP detection (oldest_available_seq)
- Test cleanup_expired deletes old rows
- Test seq uniqueness per session

**Create:** `tests/personal_agent/transport/agui/test_ws_endpoint.py`
- Test auth rejection before accept (missing token, bad origin, wrong session owner)
- Test multi-connection eviction (4001 on old socket)
- Test reconnect replay (disconnect mid-stream, reconnect with last_seq, verify no gaps)
- Test waiter resolution (APPROVAL_DECISION resolves pending waiter)
- Test waiter cleanup on disconnect (connection_lost synthetic)
- Test rate limiting (>20 msg/s → 1008)
- Test backpressure (queue full → events still in Postgres)
- Use Starlette `TestClient` WebSocket support or `httpx` AsyncClient

**Create:** `tests/personal_agent/transport/agui/test_adapter_seq.py`
- Test `to_agui_event()` includes `seq` when provided
- Test `seq` is null for PONG/REPLAY_GAP

**Modify:** `tests/personal_agent/transport/agui/test_transport.py` (if exists)
- Update for bounded queue + dual-write behavior
- Remove approval_waiter-related tests

**Create:** `tests/personal_agent/service/test_ws_ticket.py`
- Test ticket minting returns cryptographically random 43-char string
- Test ticket consumption (single-use: second consume returns None)
- Test ticket expiry (30s TTL; consuming after expiry returns None)
- Test session_id mismatch rejection (ticket minted for session A, consumed for session B → None)
- Test `POST /api/ws-ticket` endpoint requires auth and valid session ownership

---

## Files Summary

| Action | Path | Purpose |
|--------|------|---------|
| Create | `docker/postgres/migrations/0005_websocket_session_events.sql` | Schema migration |
| Modify | `docker/postgres/init.sql` | Add session_events to canonical schema |
| Modify | `src/personal_agent/service/models.py` | Add `SessionEventModel` |
| Create | `src/personal_agent/transport/agui/event_buffer.py` | Postgres event persistence + replay |
| Create | `src/personal_agent/transport/agui/ws_endpoint.py` | WebSocket connection handler |
| Create | `src/personal_agent/service/ws_ticket.py` | Short-lived single-use WS ticket minting |
| Modify | `src/personal_agent/transport/agui/transport.py` | Bounded queue + dual-write + WS waiters |
| Modify | `src/personal_agent/transport/agui/adapter.py` | Add `seq` field |
| Modify | `src/personal_agent/transport/agui/__init__.py` | Update docstring |
| Remove | `src/personal_agent/transport/agui/endpoint.py` | SSE endpoint retired |
| Remove | `src/personal_agent/transport/agui/approval_waiter.py` | Future registry retired |
| Modify | `src/personal_agent/service/app.py` | Mount WS router, ticket endpoint, cleanup task |
| Modify | `src/personal_agent/config/settings.py` | WS config fields |
| Modify | `seshat-pwa/src/lib/agui-client.ts` | Native WebSocket client |
| Modify | `seshat-pwa/src/hooks/useSSEStream.ts` | Reconnect + replay + seq tracking |
| Modify | `seshat-pwa/src/components/StreamingChat.tsx` | WS interrupt handling |
| Modify | `seshat-pwa/src/lib/types.ts` | New event/message types |
| Modify | `seshat-pwa/package.json` | Remove fetch-event-source |
| Create | `tests/.../test_event_buffer.py` | Event buffer unit tests |
| Create | `tests/.../test_ws_endpoint.py` | WS endpoint tests |
| Create | `tests/.../test_adapter_seq.py` | Adapter seq tests |

---

## Verification

### Pre-merge (local dev)
1. `make test` — all existing tests pass (no regressions)
2. `make mypy` — clean
3. `make ruff-check && make ruff-format` — clean
4. Manual: `wscat -c ws://localhost:9000/ws/{session_id}` connects and receives events after a `/chat/stream` POST
5. Manual: `GET /stream/{session_id}` returns 404 (SSE removed)
6. Manual: approval round-trip via WS (trigger gated tool, send APPROVAL_DECISION, executor continues)
7. Manual: disconnect mid-turn, reconnect with `last_seq`, verify replay
8. PWA: `npm run dev` on localhost:3000, send message, verify streaming works via WS

### Post-deploy (cloud)
1. `make build` — rebuild gateway container
2. PWA connects via `wss://` through Cloudflare Tunnel
3. iOS screen-switch test: start 30s agent run, switch away, return — full response rendered
4. Approval card: trigger gated tool from iPad PWA, approve via card, verify continuation
5. Check `session_events` table has rows with sequential `seq` values
6. Verify hourly cleanup removes >24h events

### Acceptance criteria cross-reference (from ADR-0075)
- SSE endpoint removed → 404
- WS endpoint responds → wscat connects, receives DONE
- WSS enforced externally → PWA connects wss://, tunnel rejects ws://
- Auth before accept → no ticket / expired ticket / reused ticket → socket rejected before accept
- Origin validation → bad Origin → 403
- Session ownership → wrong user → 404
- Multi-connection eviction → second WS closes first with 4001
- Reconnect replay → disconnect mid-turn, reconnect, events arrive
- Replay gap detection → old last_seq → REPLAY_GAP event
- Approval round-trip → card → WS decision → executor continues
- Waiter cleanup on disconnect → connection_lost
- last_seq persistence → kill PWA, reopen, correct replay
- Backpressure → slow client, queue at 500, Postgres still writes
- Postgres event TTL → >24h rows purged
- iOS screen-switch → switch during run, return, full response
- Inbound rate limit → >20 msg/s → 1008
