# ADR-0076: Adaptive Constraint Governance Protocol

**Status:** Proposed (revised 2026-05-27, pass 2)
**Date:** 2026-05-27
**Issue:** FRE-389
**Supersedes:** —
**Related:** ADR-0075 (WebSocket transport), ADR-0046 (AG-UI transport), ADR-0033 (model taxonomy), ADR-0065 (cost gate)
**Depends on:** ADR-0075 Phase 1 complete
**Concurrent:** FRE-392 (WS duplicate message idempotency) should ship before or with this ADR

## Context

A live agent run on 2026-05-27 demonstrated a class of failure that is currently invisible to the user:

The LLM was asked to write a large artifact. `claude_sonnet.max_tokens=8192` caused the response to truncate mid-JSON; the required `content` parameter was never included; the executor detected the missing param and retried; the model could never succeed within the budget constraint regardless of retries. The loop ran 3+ times at ~$0.13/call before `force_synthesis_from_limit` fired and produced a degraded response. The user saw a slow, expensive, wrong result with no indication of what happened or any opportunity to intervene.

**Root cause vs. symptom:** The specific artifact truncation loop is a symptom of static `max_tokens` assignment — FRE-391 addresses this upstream by introducing dynamic per-tool/per-task output budgets. The immediate fix (`claude_sonnet.max_tokens` bumped to 32768) is already deployed. Constraint governance remains valuable independently: it gives the user visibility and agency over tool iteration limits, context compression, and future timeout constraints. The motivating example is fixed upstream, but the design pattern of silent, unilateral constraint firing is the real problem this ADR solves.

This is not an isolated incident. It is a symptom of a design pattern: constraints in the harness are **static, hard-coded, and silent**. They fire without user visibility and without giving the user any agency.

### Current silent constraint sites

| Site | File | Lines | What fires silently |
|---|---|---|---|
| `force_synthesis_from_limit` | `orchestrator/executor.py` | SET: ~L2414, READ: ~L1691 | Max tool iterations hit → message injected into LLM context forcing synthesis. User not consulted. |
| Hard sync compression | `orchestrator/executor.py` | ~L1429 | 85% context threshold → synchronous in-place compression before next LLM call. User not informed. |
| Soft async compression | `orchestrator/compression_manager.py` | ~L91 | Async fire-and-forget compression after LLM reply. User not informed. |
| Stage 7 context truncation | `orchestrator/context_window.py` | ~L54 (called from executor.py ~L1198) | Static budget truncation — drops old middle context to fit window. User not informed. |
| `tool_budget_warning_injected` | `orchestrator/executor.py` | ~L1711 | Warning injected into LLM context at max-2 remaining — user still not informed |

**Which constraints get user-facing pauses (this ADR):**
- **`tool_iteration_limit`** (`force_synthesis_from_limit`) — YES, full `CONSTRAINT_PAUSE` with decision card
- **`context_compression`** (hard sync compression at `executor.py:1429`) — YES, full `CONSTRAINT_PAUSE`
- **Soft async compression** (`compression_manager.py:91`) — NO, remains silent. It's a background optimization, not a user-visible decision point.
- **Stage 7 context truncation** (`context_window.py:54`) — NO, remains silent. It's a safety net that runs before every LLM call; pausing here would block every call.

Constraints the user has no visibility into whatsoever:
- Current tool iteration count vs. limit
- Current context token usage vs. window
- Estimated cost of the current turn
- Which constraints have fired this turn

## Decision

Replace silent hard limits with **user-visible, user-controllable pause points** and add **persistent turn telemetry** so the user always knows where the harness stands. Three new UI surfaces:

1. **Send → Stop button** — user can cancel the agent mid-turn at any time (like Claude Code's Escape key)
2. **Turn status bar** — persistent bar below the input area showing context, tool count, and cost
3. **DecisionCard** — inline card in the message stream when a constraint is about to fire

A `user_constraint_preferences` table stores standing preferences so repeat decisions are applied automatically.

### Design principle: safe defaults, not silent defaults

The system currently has silent defaults — constraints fire and the harness makes a unilateral decision. The new default is `always_pause`: the user is asked every time until they set a preference. This is the right default for a research system where the user is actively building intuition about the harness's behavior.

### New event types

Added to `transport/events.py` and `InternalEvent` union:

```python
@dataclass(frozen=True)
class ConstraintPauseEvent:
    """Harness constraint about to fire — pause and request user decision."""
    request_id: str
    session_id: str
    trace_id: str
    constraint: Literal[
        "tool_iteration_limit",
        "context_compression",
    ]
    context: str
    options: Sequence[str]
    default_option: str
    expires_at: str  # ISO-8601 UTC

@dataclass(frozen=True)
class ConstraintResolvedEvent:
    """Constraint pause has been resolved — decision applied.

    Only emitted when a CONSTRAINT_PAUSE was sent (i.e., request_id is always set).
    The preference_applied path does NOT emit this event — it logs
    constraint_preference_applied via structlog instead, since there was
    no pause to resolve and no request_id to reference.
    """
    request_id: str
    session_id: str
    constraint: str
    action_id: str          # stable action identifier (see Action ID registry)
    resolution: Literal["user_choice", "timeout_default", "connection_lost", "user_cancel"]

@dataclass(frozen=True)
class CancelledEvent:
    """Turn cancelled by user via Stop button."""
    session_id: str
    trace_id: str
    reason: str  # "user_cancel"
```

**Phase 2 extension:** `timeout_expiring` will be added to the `ConstraintPauseEvent.constraint` Literal when the per-turn timeout heartbeat is implemented.

### Waiter lifecycle: non-durable (Option B)

Constraint waiters are **connection-scoped, not session-scoped**. They do NOT survive WebSocket disconnect. This matches the existing `_cancel_all_waiters()` pattern in `ws_endpoint.py:167-179` and Claude Code's Escape-key behavior: cancel means cancel, the next turn starts fresh.

**On WS disconnect:** `_cancel_all_waiters()` resolves all pending constraint waiters with `connection_lost`. The executor receives `connection_lost`, applies the `default_option`, and emits `ConstraintResolvedEvent(resolution="connection_lost")` — persisted to `session_events`.

**On reconnect replay:** The PWA always sees both `CONSTRAINT_PAUSE` and its matching `CONSTRAINT_RESOLVED` (because disconnect immediately resolves). The card always renders collapsed. There is no scenario where a replayed `CONSTRAINT_PAUSE` has no matching resolution — that would require the waiter to survive disconnect, which it does not.

**On `USER_CANCEL`:** The executor resolves all pending constraint waiters with `user_cancel` BEFORE emitting `CancelledEvent`. Sequence: resolve constraint waiters → emit `ConstraintResolvedEvent(resolution="user_cancel")` for each → emit `CancelledEvent` → force synthesis.

Both `ConstraintPauseEvent` and `ConstraintResolvedEvent` are persisted to `session_events` (Postgres). On reconnect replay, the PWA sees the pause AND its resolution — the card always renders in its collapsed state.

### Action ID registry and stable identifiers

Each constraint's options have a **stable `action_id`** (snake_case identifier) separate from the display label shown in the DecisionCard. The preference table stores `action_id`, not display strings — so renaming a button label never invalidates stored preferences.

```python
CONSTRAINT_OPTIONS: dict[str, list[ConstraintOption]] = {
    "tool_iteration_limit": [
        ConstraintOption(action_id="continue_10", label="Continue (10 more)"),
        ConstraintOption(action_id="finish_now", label="Finish now"),
    ],
    "context_compression": [
        ConstraintOption(action_id="compress_continue", label="Compress and continue"),
        ConstraintOption(action_id="stop_here", label="Stop here instead"),
    ],
}
```

The `ConstraintPauseEvent.options` field carries `action_id` values (not labels). The PWA maps `action_id` → display label via a local lookup table. The `ConstraintDecision.decision` field carries the `action_id`.

### Waiter metadata registry

When a constraint waiter is registered, its metadata is stored alongside the `asyncio.Event`:

```python
@dataclass
class WaiterMetadata:
    constraint: str
    options: Sequence[str]      # valid action_id values
    default_option: str         # action_id of the default
    created_at: float           # monotonic time
```

Stored in `conn.waiter_metadata: dict[str, WaiterMetadata]`. The WS receiver validates incoming `CONSTRAINT_DECISION.decision` against `waiter_metadata[request_id].options` — unknown action IDs are logged and treated as the `default_option`.

### New decision type: `ConstraintDecision`

Added to `transport/agui/ws_endpoint.py` alongside `ApprovalDecision`:

```python
@dataclass(frozen=True)
class ConstraintDecision:
    """Result of a constraint pause round-trip."""
    decision: str       # action_id, validated against waiter metadata options
    remember: bool      # True if user checked "Remember this choice"
    request_id: str
```

The WS receiver routes `CONSTRAINT_DECISION` messages to this type. Validation uses the `WaiterMetadata` registry (see above).

This is separate from `ApprovalDecision` (typed as `Literal["approve", "deny", "timeout", "connection_lost"]`) because constraint decisions use open-ended `action_id` values that vary per constraint type.

### Round-trip via WebSocket (ADR-0075)

```
executor approaches limit
    → check user_constraint_preferences for this (user_id, constraint)
    → if preference is set and != 'always_pause':
        apply preferred_action silently
        log constraint_preference_applied (structlog only, no event persisted)
        continue
    → else:
        1. register asyncio.Event waiter for request_id  ← BEFORE pushing event
        2. push ConstraintPauseEvent to queue + Postgres
        3. await event (with expires_at timeout → default_option fires)
    → WS sender delivers CONSTRAINT_PAUSE to PWA
    → user sees DecisionCard, picks option
    → PWA sends {type: "CONSTRAINT_DECISION", request_id, decision, remember}
    → WS receiver validates decision against options, routes to registered event
    → executor reads decision, adjusts limit or stops
    → emit ConstraintResolvedEvent(resolution="user_choice") to queue + Postgres
    → if remember=true: upsert user_constraint_preferences
```

**Critical ordering:** The waiter `asyncio.Event` MUST be registered BEFORE the `ConstraintPauseEvent` is pushed to the queue. The existing `transport.py:209-232` has the reverse ordering (event pushed at L209, waiter registered at L232) — this race must be fixed atomically with this ADR. If the PWA responds before registration, `_resolve_waiter()` drops the decision silently. Fix: refactor `_request_tool_approval` in `transport.py` to register-then-push as well.

No Future registry, no separate POST endpoint. The WS connection is the decision channel.

### User cancel (Stop button)

New client → server message: `{"type": "USER_CANCEL"}`

The PWA's Send button transforms into a Stop button while the agent is streaming (same pattern as Claude Code). Tapping Stop sends `USER_CANCEL` via WS. The WS receiver sets a cancel flag on the connection state. The executor checks this flag between tool iterations (same checkpoint as `force_synthesis_from_limit`). When set:

1. Resolve all pending constraint waiters with `resolution="user_cancel"` — emit `ConstraintResolvedEvent` for each
2. Cancel the current tool execution if possible (best-effort)
3. Force synthesis from results gathered so far
4. Emit `CancelledEvent` (persisted to `session_events`)
5. PWA renders a "Stopped by user" pill in the message stream

**Interaction with pending constraint pause:** If the user taps Stop while a `CONSTRAINT_PAUSE` is pending (DecisionCard visible), the Stop takes precedence. The constraint waiter resolves with `user_cancel`, the DecisionCard collapses to "Stopped by user", and the executor synthesizes immediately. No orphaned waiters.

This is independent of constraint pauses — the user can stop at ANY time, not just when a constraint fires.

### Executor integration

**`force_synthesis_from_limit` (executor.py ~L2414):**

Replace the unconditional `ctx.force_synthesis_from_limit = True` with:

```python
action_id = await self._maybe_pause_for_constraint(
    session_id=session_id,
    trace_id=trace_id,
    user_id=user_id,
    constraint="tool_iteration_limit",
    context=f"Reached {iteration_count} tool calls on this turn.",
)
if action_id == "continue_10":
    self._extend_tool_limit(session_id, extra=10)
else:
    self._force_synthesis(session_id)
```

Options and defaults are defined in `CONSTRAINT_OPTIONS["tool_iteration_limit"]` (see Action ID registry).

**Hard sync compression (executor.py ~L1429):**

Before the `compress_in_place()` call:

```python
action_id = await self._maybe_pause_for_constraint(
    constraint="context_compression",
    context=f"Context is at {pct:.0f}% of the window ({tokens:,} / {max_tokens:,} tokens). Compressing will summarise older turns.",
)
if action_id == "stop_here":
    self._force_synthesis(session_id)
    return
```

**Soft async compression and Stage 7 truncation:** Unchanged. These remain silent safety nets.

**`_maybe_pause_for_constraint()` method:**

```python
async def _maybe_pause_for_constraint(
    self,
    *,
    session_id: str,
    trace_id: str,
    user_id: UUID,
    constraint: Literal["tool_iteration_limit", "context_compression"],
    context: str,
    timeout_seconds: float = 60.0,
) -> str:
    """Pause and ask the user, or apply stored preference. Returns action_id."""
    spec = CONSTRAINT_OPTIONS[constraint]
    option_ids = [o.action_id for o in spec]
    default_id = spec[-1].action_id  # last option is the safe default

    # 1. Check stored preference
    pref = await self._load_constraint_preference(user_id, constraint)
    if pref and pref != "always_pause":
        log.info("constraint_preference_applied", constraint=constraint,
                 preferred_action=pref, trace_id=trace_id, session_id=session_id)
        # No request_id — preference bypasses the pause entirely.
        # No ConstraintResolvedEvent persisted (no pause to resolve).
        # Telemetry-only: the structlog event is the record.
        return pref

    # 2. Register waiter BEFORE pushing event (prevents race)
    request_id = str(uuid4())
    log.info("constraint_pause_emitted", constraint=constraint, request_id=request_id,
             trace_id=trace_id, session_id=session_id)

    decision_payload = await register_and_push_constraint(
        session_id=session_id,
        request_id=request_id,
        event=ConstraintPauseEvent(
            request_id=request_id,
            session_id=session_id,
            trace_id=trace_id,
            constraint=constraint,
            context=context,
            options=option_ids,
            default_option=default_id,
            expires_at=...,
        ),
        metadata=WaiterMetadata(
            constraint=constraint,
            options=option_ids,
            default_option=default_id,
            created_at=monotonic(),
        ),
        timeout_seconds=timeout_seconds,
    )

    action_id = decision_payload.get("decision", default_id)
    resolution = decision_payload.get("resolution", "user_choice")
    remember = decision_payload.get("remember", False)

    log.info("constraint_decision_received", constraint=constraint,
             action_id=action_id, resolution=resolution,
             trace_id=trace_id, session_id=session_id)

    # 3. Emit resolution (persisted to session_events for replay)
    await self._emit_constraint_resolved(
        request_id=request_id, constraint=constraint,
        action_id=action_id, resolution=resolution,
        session_id=session_id, trace_id=trace_id,
    )

    # 4. Save preference if requested (any action, including defaults)
    if remember:
        await self._save_constraint_preference(
            user_id, constraint, action_id, session_id=session_id,
        )

    return action_id
```

**Key design choices in this method:**
- **Preference-applied path does NOT emit `ConstraintResolvedEvent`** — there was no `CONSTRAINT_PAUSE` to resolve. The structlog `constraint_preference_applied` event is the telemetry record. This avoids the `request_id: None` type mismatch.
- **`remember` saves any action**, including the default. Users who want "Finish now" every time can persist that.
- **`register_and_push_constraint` takes `WaiterMetadata`** — stored in `conn.waiter_metadata[request_id]` for decision validation in the WS receiver.
- **`_save_constraint_preference` takes `session_id`** — populates `source_session_id` in the preference table for audit trail.

**`register_and_push_constraint` lifecycle:**
- When no active WS connection exists: registers waiter → waiter immediately resolves with `connection_lost` (existing `register_waiter` behavior at `ws_endpoint.py:101`). The `ConstraintPauseEvent` is NOT persisted to `session_events` (no point replaying an event the user never saw). Only the structlog `constraint_no_ws_default_applied` event is the record.
- When WS is active: registers waiter + metadata → pushes event to queue + Postgres → awaits resolution.

**WaiterMetadata cleanup:** Metadata entries are cleaned up alongside their waiters — `_cancel_all_waiters()` clears `conn.waiter_metadata` alongside `conn.waiters` and `conn.waiter_timeouts`. Individual resolution also removes the metadata entry for the resolved `request_id`.

### No-active-WS fallback

When no WebSocket connection is active for the session (headless/API-only usage, or the user closed the PWA), `register_waiter()` returns `connection_lost` immediately (`ws_endpoint.py:101`). In this case, `_maybe_pause_for_constraint()` applies the `default_option` without pausing:

```python
if resolution == "connection_lost":
    log.info("constraint_no_ws_default_applied", constraint=constraint,
             default=default_option, trace_id=trace_id, session_id=session_id)
    return default_option
```

This preserves the current silent behavior for headless usage while giving connected users full agency.

### User preferences

New table (added to `docker/postgres/migrations/`):

```sql
CREATE TABLE user_constraint_preferences (
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    constraint_name  TEXT NOT NULL,
    preferred_action TEXT NOT NULL,  -- stable action_id (e.g. 'continue_10') or 'always_pause'
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_session_id UUID,         -- session where the preference was set
    PRIMARY KEY (user_id, constraint_name)
);
```

- No row = `always_pause` (default). `always_pause` as an explicit value means the user chose to be asked every time.
- `preferred_action` stores the stable `action_id` (e.g., `"continue_10"`, `"compress_continue"`), not the display label. Button label renames never invalidate stored preferences.
- The API validates that `preferred_action` is either `"always_pause"` or a valid `action_id` from `CONSTRAINT_OPTIONS[constraint_name]`.
- `source_session_id` provides audit trail for when a silent preference started being applied.
- Preferences are loaded once per turn from Postgres (small table, negligible overhead).
- The `DecisionCard` "Remember this choice" toggle sends `remember: true` alongside the decision. The executor upserts the preference with the chosen `action_id`.

Preference management endpoint: `PUT /api/v1/preferences/constraint` (body: `{constraint_name, preferred_action}`).

### Wire protocol additions

**Server → client (new event types):**
```json
{"type": "CONSTRAINT_PAUSE",    "seq": 47, "request_id": "abc-123", "data": {"constraint": "tool_iteration_limit", "context": "Reached 25 tool calls on this turn.", "options": ["continue_10", "finish_now"], "default_option": "finish_now", "expires_at": "2026-05-27T12:01:00Z"}}
{"type": "CONSTRAINT_RESOLVED", "seq": 48, "request_id": "abc-123", "data": {"constraint": "tool_iteration_limit", "action_id": "continue_10", "resolution": "user_choice"}}
{"type": "CANCELLED",           "seq": 49, "data": {"reason": "user_cancel"}}
{"type": "STATE_DELTA",         "seq": 50, "data": {"key": "turn_status", "value": {"context_tokens": 34000, "context_max": 128000, "tool_iteration": 12, "tool_iteration_max": 25, "turn_cost_usd": 0.42}}}
```

**Client → server (new message types):**
```json
{"type": "CONSTRAINT_DECISION",  "request_id": "abc-123", "decision": "continue_10", "remember": true}
{"type": "USER_CANCEL"}
```

`CONSTRAINT_DECISION` is already routed in `ws_endpoint.py:499` (shipped with ADR-0075). `USER_CANCEL` is a new message type added to the receiver match.

**Validation:** On receiving `CONSTRAINT_DECISION`, the WS receiver looks up `conn.waiter_metadata[request_id]`. If the `decision` value is not in `metadata.options`, log a warning and substitute `metadata.default_option`. If `request_id` has no metadata (already resolved or unknown), silently drop (existing `_resolve_waiter` behavior).

**Ordering guarantee:** `CONSTRAINT_RESOLVED` is always persisted after its matching `CONSTRAINT_PAUSE` (both use the same Postgres `session_events` seq). On replay, the PWA processes events in seq order, so it always sees the pause before the resolution. If `CONSTRAINT_RESOLVED` arrives without a preceding `CONSTRAINT_PAUSE` (possible if the pause was outside the replay window), the PWA ignores it.

### PWA UI surfaces

**Surface 1: Send → Stop button**

The existing Send button in the input area transforms into a Stop button while the agent is streaming. Implementation in `StreamingChat.tsx`:
- While `isStreaming`: render Stop icon (square) with the same button style
- Tapping Stop calls `sendWSMessage({type: "USER_CANCEL"})`
- On receiving `CANCELLED` event: render a "Stopped by user" pill in the message stream

**Surface 2: `TurnStatusBar` component**

**Location:** `seshat-pwa/src/components/TurnStatusBar.tsx` (replaces `ContextBudgetMeter.tsx`)

Persistent bar below the input area, visible only during active streaming. Shows:
- Context window: `ctx: 34K/128K ████░░ 27%` — amber at 70%, red at 85%
- Tool iterations: `tools: 12/25` — amber at max-2
- Turn cost: `$0.42`

Fed by `STATE_DELTA` events with key `"turn_status"`. The existing `useSSEStream.ts:148` handler (currently checks `key === 'context_window'`) is updated to also handle `key === 'turn_status'` with the object payload.

If the status bar proves too intrusive on mobile, degrade to a per-turn header above the agent's response (same data, different position).

**Surface 3: `DecisionCard` component**

**Location:** `seshat-pwa/src/components/DecisionCard.tsx`

Inline chat bubble (not a modal). Renders when a `CONSTRAINT_PAUSE` WS event arrives. Distinct from `ApprovalModal` (full-screen, blocking, for high-risk tool calls). Design precedent: `BudgetDeniedCard.tsx` (inline bubble styling) + Claude Code's `AskUserQuestion` panels.

**Behaviour:**
- Renders option buttons from `CONSTRAINT_PAUSE.options` (action IDs mapped to display labels via local lookup table)
- Tapping sends `{type: "CONSTRAINT_DECISION", request_id, decision: action_id, remember}`
- After selection: card collapses to a one-line pill ("▶ Continued — 10 more tool calls") and is no longer interactive
- On receiving `CONSTRAINT_RESOLVED` for the same `request_id`: collapse the card with the resolution label (handles reconnect replay, timeout, disconnect, and cancel)
- Countdown progress bar from `expires_at` — shows time remaining before default fires
- "Remember this choice" toggle (off by default) — if checked, sets `remember: true`
- **Reconnect:** Since constraint waiters are non-durable (Option B), disconnect always emits `CONSTRAINT_RESOLVED(connection_lost)`. On replay, the card always renders collapsed. There is no "reconnect to a pending card" scenario.

### Turn status emissions

```python
# In executor.py, emit after each LLM call and after each tool execution:
await transport.send_state(
    key="turn_status",
    value={
        "context_tokens": estimate_messages_tokens(ctx.messages),
        "context_max": settings.context_window_max_tokens,
        "tool_iteration": ctx.tool_iteration_count,
        "tool_iteration_max": _resolve_max_iterations(ctx),
        "turn_cost_usd": ctx.turn_cost_usd,
    },
    session_id=session_id,
)
```

Backward compatibility: the existing `ContextBudgetMeter.tsx` handler checks `key === 'context_window'` with a bare number. The new `TurnStatusBar` handler checks `key === 'turn_status'` with the object payload. Both can coexist during the transition; `ContextBudgetMeter` is removed once `TurnStatusBar` ships.

### Telemetry

All constraint governance events are logged via structlog with `trace_id` and `session_id`:

| Event | When | Key fields |
|---|---|---|
| `constraint_pause_emitted` | Constraint about to fire, pause event sent | `constraint`, `request_id`, `options`, `trace_id`, `session_id` |
| `constraint_decision_received` | User responded or timeout/disconnect resolved | `constraint`, `action_id`, `resolution`, `request_id`, `trace_id`, `session_id` |
| `constraint_resolved_emitted` | `ConstraintResolvedEvent` persisted to `session_events` | `constraint`, `action_id`, `resolution`, `request_id`, `trace_id`, `session_id` |
| `constraint_timeout_applied` | `expires_at` elapsed with no response | `constraint`, `default_option`, `request_id`, `trace_id`, `session_id` |
| `constraint_preference_applied` | Stored preference skipped the pause | `constraint`, `preferred_action`, `trace_id`, `session_id` |
| `constraint_no_ws_default_applied` | No WS connection, default applied silently | `constraint`, `default_option`, `trace_id`, `session_id` |
| `user_cancel_received` | User tapped Stop button | `trace_id`, `session_id`, `pending_constraint_request_ids` |

## Consequences

**Positive:**
- User is never surprised by forced synthesis — they choose when to stop
- User can cancel at any time via Stop button (not just when constraints fire)
- User builds intuition about the harness's actual behavior via persistent status bar
- Preferences table means repeat decisions are frictionless; users who always want to continue don't get interrupted
- `CONSTRAINT_RESOLVED` event makes reconnect replay deterministic — no stale interactive cards
- Headless/API usage is unaffected (no-WS fallback applies defaults silently)

**Negative:**
- Constraint pauses add latency (user must respond before the executor can continue); mitigated by the `expires_at` timeout applying the default automatically
- Requires ADR-0075 (WS transport) to be shipped first
- Three new PWA components (`TurnStatusBar`, `DecisionCard`, Stop button state) — net increase in UI surface
- FRE-392 (WS duplicate delivery) should be fixed first or concurrently to prevent duplicate constraint decisions on reconnect

**Neutral:**
- `force_synthesis_from_limit` still exists as the fallback when the user picks "Finish now" or the timeout fires — the harness mechanism is unchanged, only when it fires

## Out of scope for this ADR

- **Adaptive `max_tokens`** (FRE-391): context-aware per-call output budget — addresses the root cause of the motivating artifact truncation example. Separate ADR.
- **Timeout expiring (`timeout_expiring` constraint)**: requires a per-turn timeout heartbeat — Phase 2
- **Settings UI** for constraint preferences: follows after the backend table and API endpoint exist
- **Tool approval integration**: tool approvals already use a round-trip pattern (ADR-0075); no change to approval semantics needed
- **Eval transport coverage** (FRE-390): the eval harness uses `POST /chat` (synchronous), not the WS transport. A separate end-to-end transport eval is needed to cover constraint pause round-trips.

## Files changed

**Add:**
- `transport/events.py` — `ConstraintPauseEvent`, `ConstraintResolvedEvent`, `CancelledEvent` added to `InternalEvent` union
- `transport/agui/adapter.py` — `to_agui_event()` cases for `CONSTRAINT_PAUSE`, `CONSTRAINT_RESOLVED`, `CANCELLED`
- `seshat-pwa/src/components/DecisionCard.tsx` — inline constraint decision card
- `seshat-pwa/src/components/TurnStatusBar.tsx` — persistent turn metrics bar (replaces `ContextBudgetMeter.tsx`)
- `docker/postgres/migrations/0006_constraint_preferences.sql` — `user_constraint_preferences` table

**Modify:**
- `orchestrator/executor.py` — `_maybe_pause_for_constraint()` method; replace unconditional `force_synthesis_from_limit` at ~L2414; add pause before hard compression at ~L1429; add `USER_CANCEL` flag check between tool iterations; emit `turn_status` STATE_DELTA after each LLM call and tool execution
- `transport/agui/transport.py` — fix waiter registration ordering (register BEFORE push) in `_request_tool_approval` and new `_request_constraint_decision`; add `send_cancel()` method
- `transport/agui/ws_endpoint.py` — add `ConstraintDecision` dataclass; add `USER_CANCEL` to receiver match; validate `CONSTRAINT_DECISION.decision` against originating event options
- `service/app.py` — mount `PUT /api/v1/preferences/constraint` endpoint
- `seshat-pwa/src/hooks/useSSEStream.ts` — add `turn_status` key handler for `STATE_DELTA`; add `CONSTRAINT_PAUSE`, `CONSTRAINT_RESOLVED`, `CANCELLED` event handlers
- `seshat-pwa/src/components/StreamingChat.tsx` — Send→Stop button state; render `DecisionCard` and `TurnStatusBar`

**Remove:**
- `seshat-pwa/src/components/ContextBudgetMeter.tsx` — replaced by `TurnStatusBar`

## Implementation phases

**Phase 1 (this ADR):**
- `ConstraintPauseEvent` + `ConstraintResolvedEvent` + `CancelledEvent` types + adapter mappings
- `ConstraintDecision` dataclass in `ws_endpoint.py`
- Fix waiter registration race (register-before-push) in `transport.py`
- `_maybe_pause_for_constraint()` method in executor
- Wire into `force_synthesis_from_limit` and hard sync compression trigger
- `USER_CANCEL` WS message + executor cancel flag + `CancelledEvent`
- `user_constraint_preferences` table + `PUT /api/v1/preferences/constraint`
- "Remember this choice" toggle wired to preference save
- `DecisionCard` PWA component with reconnect replay awareness (via `CONSTRAINT_RESOLVED`)
- `TurnStatusBar` PWA component (replaces `ContextBudgetMeter`)
- Send → Stop button transformation
- `STATE_DELTA` `turn_status` emissions from executor
- At least one integration test: open WS → trigger constraint pause → send decision → verify executor continuation (reference: FRE-390 broader coverage gap)

**Phase 2 (future ADR):**
- `timeout_expiring` constraint type with LLM-call heartbeat (extends `ConstraintPauseEvent.constraint` Literal)

## Acceptance criteria

| # | Criterion | Verification |
|---|---|---|
| 1 | Constraint pause emitted | Send a message that triggers 25+ tool calls (e.g., broad research query); verify `CONSTRAINT_PAUSE` WS event with `constraint=tool_iteration_limit` arrives in browser devtools |
| 2 | DecisionCard renders | `CONSTRAINT_PAUSE` event renders inline card with option buttons (mapped from `action_id` to display label), countdown bar, and "Remember this choice" toggle |
| 3 | "Continue" resumes executor | Pick "Continue (10 more)" (`continue_10`); verify executor proceeds past original 25 limit; verify at least one additional tool call executes |
| 4 | "Finish now" forces synthesis | Pick "Finish now" (`finish_now`); executor produces synthesis response immediately; no further tool calls |
| 5 | Default fires on timeout | Do not respond to `CONSTRAINT_PAUSE`; wait for `expires_at`; verify `CONSTRAINT_RESOLVED` with `resolution=timeout_default` and `action_id=finish_now` in `session_events` |
| 6 | Preference stores | Check "Remember this choice" + pick any option (including default); verify `user_constraint_preferences` row with matching `action_id` and `source_session_id` |
| 7 | Preference applies | Set `continue_10` preference for `tool_iteration_limit` via AC-6; trigger limit again; no `CONSTRAINT_PAUSE` event emitted; `constraint_preference_applied` in structlog |
| 8 | Status bar live | `TurnStatusBar` shows non-zero context tokens, tool count, and cost during an active turn; disappears when turn completes |
| 9 | Status bar color thresholds | Tool count turns amber when `tool_iteration >= tool_iteration_max - 2`; context turns amber at 70%, red at 85% |
| 10 | Stop button cancels | Tap Stop between tool iterations; verify all pending constraint waiters resolved with `user_cancel`; executor synthesizes from available results; "Stopped by user" pill in stream; `CONSTRAINT_RESOLVED(user_cancel)` + `CANCELLED` in `session_events` |
| 11 | Stop during constraint pause | Tap Stop while DecisionCard is showing; card collapses to "Stopped by user"; executor synthesizes; `CONSTRAINT_RESOLVED(user_cancel)` emitted before `CANCELLED` |
| 12 | Reconnect replay — always resolved | Disconnect during active turn (constraint was pending OR resolved); reconnect; DecisionCard always renders collapsed (non-durable waiters: disconnect emits `CONSTRAINT_RESOLVED(connection_lost)`) |
| 13 | No-WS fallback | Trigger constraint via `POST /chat` (no active WS); default applied silently; `constraint_no_ws_default_applied` in structlog; no `CONSTRAINT_PAUSE` in `session_events` |
| 14 | Waiter race fixed | Automated test: push `CONSTRAINT_PAUSE` and respond with `CONSTRAINT_DECISION` within same event loop tick; verify decision captured (not dropped as unknown waiter) |
| 15 | Duplicate decision idempotent | Send same `CONSTRAINT_DECISION` twice for same `request_id`; second is silently dropped; executor acts exactly once; single `CONSTRAINT_RESOLVED` in `session_events` |
| 16 | Compression pause | Fill context to 85%+ (send very long messages); trigger hard sync compression path; verify `CONSTRAINT_PAUSE` with `constraint=context_compression` arrives |
| 17 | Action ID validation | Send `CONSTRAINT_DECISION` with invalid `action_id`; verify warning logged and `default_option` applied |
