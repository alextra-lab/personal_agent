# Plan: Adaptive Harness Governance + Durable Real-Time Channel

## Context

A live agent run exposed three related problems:

1. **Truncation loop** — `claude_sonnet.max_tokens=8192` caused the LLM to truncate mid-JSON when writing large artifact content. The `content` param was never emitted, the executor detected the missing required param and retried, and the model could never succeed within budget. Cycled 3+ times at ~$0.13/call. User never knew it was happening.

2. **Silent static constraints** — all harness constraints (max_tokens, tool iteration limits, timeouts, context compression threshold) are hard-coded, fire silently, and give the user no agency. The cost gate TTL (`RESERVATION_TTL_SECONDS = 90`) is miscalibrated against the actual timeout (now 180s), causing `litellm_commit_failed` on every Sonnet call.

3. **SSE reconnect loss** — when the user switches screens on iPhone, the SSE connection drops. The background task completes and pushes its result to the in-memory `asyncio.Queue`, but `cleanup_session()` destroys the queue when the SSE generator exits. Response is lost.

The `models.yaml` fix (max_tokens 8192→32768, timeout 60→180) is already applied.

**Root cause of problem 3 is deeper than reconnect:** SSE is fundamentally a unidirectional protocol. Every interactive feature (tool approvals, HITL interrupts, and the planned constraint pauses) requires a separate POST endpoint + an in-process `asyncio.Future` registry as a workaround. The system has outgrown SSE.

---

## Immediate Fixes (no ADR required)

### Fix 1: Cost gate TTL miscalibration

`src/personal_agent/cost_gate/gate.py:54`:
```python
RESERVATION_TTL_SECONDS = 180  # was 90, comment said "calibrated for Sonnet default_timeout: 60"
```

### Fix 2: Docker rebuild

`models.yaml` is COPY'd at build time:
```bash
make rebuild SERVICE=seshat-gateway
```

---

## Transport Architecture Analysis: SSE vs WebSockets

### Why SSE has become a workaround pattern

The current SSE architecture handles interaction via two workarounds:

| Scenario | How it works today | Problem |
|---|---|---|
| Tool approval | SSE push → separate `POST /approval/{request_id}` → resolve asyncio.Future | Future registry fragility; timed-out Future can hang executor |
| HITL interrupt | SSE push → separate `POST /stream/{session_id}/resume` | Not fully implemented; `send_interrupt()` returns None (FRE-209 deferred) |
| Constraint pause (planned) | Would need another POST endpoint + another Future registry | Pattern doesn't scale |
| Reconnect | In-memory queue destroyed on disconnect | No replay mechanism |

`protocols.py:4` already anticipates this: "Future implementations may target WebSocket, SSE, AG-UI, or other transports."

### WebSocket analysis against stated priorities

**Quality:**
- WS is the industry standard for bidirectional real-time agent chat — Slack, Discord, Copilot, all agent UIs use WS
- Eliminates the `approval_waiter.py` asyncio.Future pattern: instead of registering a Future and waiting for a POST to resolve it, the WS handler routes incoming `{type: "DECISION", ...}` messages directly to the waiting executor coroutine
- Message IDs + sequence numbers enable proper reconnect replay
- iOS PWA: native WebSocket API, no library needed, reliable on all iOS versions

**Stability:**
- Fewer moving parts: one connection replaces SSE stream + multiple POST endpoints + Future registry
- `approval_waiter.py` Future TTL is a known fragility — a network blip between client and server can orphan a Future and leave the executor blocked. WS closes this gap: the connection state is authoritative.
- WS ping/pong keepalive is standard; `fetchEventSource` keepalive is a comment hack

**Ease of future development:**
- New interactive event type = new message `type` field in the wire format. No new POST endpoint, no new Future registration.
- Constraint pause, tool approval, HITL interrupt, agent asking mid-turn clarifications — all the same WS round-trip pattern.
- `agui-client.ts` is the single PWA touch point for the connection; swapping SSE → WS is bounded to that file + `StreamingChat.tsx`.

**Migration complexity:**
- Server: new `ws_endpoint.py`, updated `AGUITransport`, retire `approval_waiter.py`
- PWA: replace `fetchEventSource` call in `agui-client.ts` with native `WebSocket`, send decisions via WS instead of POST
- Cloudflare Tunnel: native WS proxy support (already used for Neo4j WebSocket split)

**Recommendation: Migrate to WebSockets.** The system is already conceptually bidirectional — WS makes it structurally bidirectional too.

---

## ADR-0075: WebSocket Transport + Durable Channel

**Replaces the SSE transport.** Covers reconnect persistence as part of the migration.

### Wire protocol

Single WS connection per session at `GET /ws/{session_id}`.

**Server → client messages** (same JSON envelope as current SSE events, new `seq` field):
```json
{"type": "TEXT_DELTA", "seq": 42, "data": {"text": "..."}, "session_id": "..."}
{"type": "TOOL_CALL_START", "seq": 43, "data": {"tool_name": "...", "args": {...}}}
{"type": "CONSTRAINT_PAUSE", "seq": 44, "request_id": "...", "data": {...}}
{"type": "DONE", "seq": 45}
```

**Client → server messages** (new capability — not possible with SSE):
```json
{"type": "APPROVAL_DECISION", "request_id": "...", "decision": "approve"}
{"type": "CONSTRAINT_DECISION", "request_id": "...", "decision": "Continue"}
{"type": "PING"}
```

### Reconnect design

On connect, client sends `{type: "CONNECT", last_seq: N}` (N=0 for fresh connect).

Server replays events with `seq > N` from `session_events` Postgres table (write-on-emit, TTL 24h). Final assistant message is already persisted by `_process_chat_stream_background`; intermediate events (tool calls, state updates) added to the buffer as part of this ADR.

**Reconnect flow on iOS screen-switch:**
1. iPhone suspends PWA → WS closes
2. Background task continues, writes events to `session_events` table and in-memory queue
3. User returns → PWA reconnects → sends `{type: "CONNECT", last_seq: N}`
4. Server replays buffered events from Postgres → user sees missed tool calls + response

### Internal architecture (keep asyncio.Queue, add Postgres flush)

```
Background task
    → asyncio.Queue (in-memory fast path)
    → AND writes to session_events (Postgres, durability)
        ↓
WS handler drains queue → sends to client
    ↑
Client → server messages routed by WS handler → executor coroutines
```

The queue decouples the background task from WS connection state. If WS drops mid-run, queue events accumulate and get written to Postgres. On reconnect, replay from Postgres.

### Retiring SSE infrastructure

- Remove: `endpoint.py` SSE `_event_generator`, `cleanup_session()`, `_session_queues` dict
- Remove: `POST /approval/{request_id}` and `POST /stream/{session_id}/resume` REST endpoints
- Remove: `approval_waiter.py` asyncio.Future registry
- Replace: `AGUITransport` methods push to queue (same) but WS handler routes incoming messages
- Keep: `asyncio.Queue` (reused as internal buffer between background task and WS sender)
- Keep: `InternalEvent` frozen dataclasses (wire format unchanged)
- Keep: `adapter.py` `to_agui_event()` (adds `seq` field)

### PWA changes (bounded to `agui-client.ts`)

- Replace `connectToStream` (fetchEventSource) with `connectWebSocket` (native WS)
- `postApprovalDecision` / `resumeInterrupt` → `sendWSMessage({type: "APPROVAL_DECISION", ...})`
- `StreamingChat.tsx`: same event handler interface, WS fires `onmessage` not SSE `onmessage`
- Add reconnect logic: exponential backoff, send `last_seq` on reconnect

### New Postgres table

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
CREATE INDEX ON session_events (session_id, seq);
```

Events expire via a periodic cleanup job (or Postgres partitioning) after 24h.

---

## ADR-0076: Adaptive Constraint Governance Protocol

**Replaces silent `force_synthesis_from_limit` and hard thresholds.** Leverages WS bidirectionality.

### New event type: `ConstraintPauseEvent`

```python
@dataclass(frozen=True)
class ConstraintPauseEvent:
    request_id: str
    session_id: str
    trace_id: str
    constraint: Literal["tool_iteration_limit", "context_compression", "timeout_expiring"]
    context: str          # "Reached 25 tool calls on this turn. Continue or finish?"
    options: Sequence[str]
    default_option: str   # fires after timeout if user doesn't respond
    expires_at: str       # ISO-8601
```

### Round-trip (WS native, no Future registry)

```
executor hits limit
    → push ConstraintPauseEvent to queue
    → asyncio.Event registered for request_id
    → WS handler sends to client
    → client renders DecisionCard
    → user picks option
    → PWA sends {type: "CONSTRAINT_DECISION", request_id: "...", decision: "Continue"}
    → WS handler sets asyncio.Event, stores decision
    → executor resumes with decision
```

### Executor integration points

| File | Line (approx) | Change |
|---|---|---|
| `executor.py` | ~2414 `force_synthesis_from_limit` | Before forcing synthesis, push `ConstraintPauseEvent(constraint="tool_iteration_limit")` and await |
| `context_window.py` | Compression trigger | Before compressing, push `ConstraintPauseEvent(constraint="context_compression")` |

### User preferences (Postgres)

```sql
CREATE TABLE user_constraint_preferences (
    user_id    UUID NOT NULL REFERENCES users(id),
    constraint TEXT NOT NULL,
    behavior   TEXT NOT NULL CHECK (behavior IN ('always_pause', 'always_continue', 'always_stop')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, constraint)
);
```

Default: all constraints → `always_pause`. If preference is set, executor applies it without emitting a WS event.

### PWA: `DecisionCard` component

Inline chat bubble, not a modal. Renders on `CONSTRAINT_PAUSE` WS event. Buttons for each option; press sends WS message, card collapses to a pill showing choice made. Countdown progress bar for `expires_at`. Distinct from `ApprovalModal` (full-screen for high-risk tool calls).

---

## Deliverables

1. `gate.py` fix → commit to main (1 line, immediate)
2. `make rebuild SERVICE=seshat-gateway` (deploy models.yaml fix)
3. **ADR-0075** → `docs/architecture_decisions/ADR-0075-websocket-transport.md`
4. **ADR-0076** → `docs/architecture_decisions/ADR-0076-adaptive-constraint-governance.md`
5. Linear issues: one per ADR, state `Needs Approval`, label `Tier-1:Opus`

## ADR authoring order

ADR-0075 (WS transport) first — ADR-0076 depends on it (constraint pause round-trips are WS messages).

## Verification after implementation

- **ADR-0075**: switch screens on iPhone mid-turn; reconnect; verify missed events replay and response appears
- **ADR-0075**: `litellm_commit_failed` no longer appears in logs after gate.py fix + rebuild
- **ADR-0076**: trigger tool-loop-limit in a long chain; verify `CONSTRAINT_PAUSE` WS message arrives and `DecisionCard` renders; pick "Continue"; verify executor resumes
