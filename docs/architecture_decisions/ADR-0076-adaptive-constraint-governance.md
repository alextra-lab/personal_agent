# ADR-0076: Adaptive Constraint Governance Protocol

**Status:** Proposed (revised 2026-05-27)
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
    """Constraint pause has been resolved — decision applied."""
    request_id: str
    session_id: str
    constraint: str
    decision: str
    resolution: Literal["user_choice", "timeout_default", "connection_lost", "user_cancel", "preference_applied"]

@dataclass(frozen=True)
class CancelledEvent:
    """Turn cancelled by user via Stop button."""
    session_id: str
    trace_id: str
    reason: str  # "user_cancel"
```

**Phase 2 extension:** `timeout_expiring` will be added to the `ConstraintPauseEvent.constraint` Literal when the per-turn timeout heartbeat is implemented.

Both `ConstraintPauseEvent` and `ConstraintResolvedEvent` are persisted to `session_events` (Postgres). On reconnect replay, the PWA sees the pause AND its resolution — if a `CONSTRAINT_RESOLVED` follows a `CONSTRAINT_PAUSE` with the same `request_id`, the card renders in its collapsed/resolved state. If no resolution exists, the card renders as interactive (the waiter is still pending server-side).

### New decision type: `ConstraintDecision`

Added to `transport/agui/ws_endpoint.py` alongside `ApprovalDecision`:

```python
@dataclass(frozen=True)
class ConstraintDecision:
    """Result of a constraint pause round-trip."""
    decision: str       # free-form, validated against originating event's options list
    remember: bool      # True if user checked "Remember this choice"
    request_id: str
```

The WS receiver routes `CONSTRAINT_DECISION` messages to this type. The `decision` field is validated against the `options` list from the originating `ConstraintPauseEvent` — unknown values are logged and treated as the `default_option`.

This is separate from `ApprovalDecision` (typed as `Literal["approve", "deny", "timeout", "connection_lost"]`) because constraint decisions are open-ended strings that vary per constraint type.

### Round-trip via WebSocket (ADR-0075)

```
executor approaches limit
    → check user_constraint_preferences for this (user_id, constraint)
    → if preference is set and != 'always_pause':
        apply preferred_action silently
        emit ConstraintResolvedEvent(resolution="preference_applied")
        log constraint_preference_applied
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

The PWA's Send button transforms into a Stop button while the agent is streaming (same pattern as Claude Code). Tapping Stop sends `USER_CANCEL` via WS. The executor checks for a cancel flag between tool iterations (same checkpoint as `force_synthesis_from_limit`). When set:

1. Cancel the current tool execution if possible (best-effort)
2. Force synthesis from results gathered so far
3. Emit `CancelledEvent` (persisted to `session_events`)
4. PWA renders a "Stopped by user" pill in the message stream

This is independent of constraint pauses — the user can stop at ANY time, not just when a constraint fires.

### Executor integration

**`force_synthesis_from_limit` (executor.py ~L2414):**

Replace the unconditional `ctx.force_synthesis_from_limit = True` with:

```python
decision = await self._maybe_pause_for_constraint(
    session_id=session_id,
    trace_id=trace_id,
    user_id=user_id,
    constraint="tool_iteration_limit",
    context=f"Reached {iteration_count} tool calls on this turn.",
    options=["Continue (10 more)", "Finish now"],
    default_option="Finish now",
)
if decision == "Continue (10 more)":
    self._extend_tool_limit(session_id, extra=10)
else:
    self._force_synthesis(session_id)
```

**Hard sync compression (executor.py ~L1429):**

Before the `compress_in_place()` call:

```python
decision = await self._maybe_pause_for_constraint(
    constraint="context_compression",
    context=f"Context is at {pct:.0f}% of the window ({tokens:,} / {max_tokens:,} tokens). Compressing will summarise older turns.",
    options=["Compress and continue", "Stop here instead"],
    default_option="Compress and continue",
)
if decision == "Stop here instead":
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
    options: Sequence[str],
    default_option: str,
    timeout_seconds: float = 60.0,
) -> str:
    """Pause and ask the user, or apply stored preference. Returns chosen option."""
    pref = await self._load_constraint_preference(user_id, constraint)
    if pref and pref != "always_pause":
        log.info("constraint_preference_applied", constraint=constraint,
                 preferred_action=pref, trace_id=trace_id, session_id=session_id)
        await self._emit_constraint_resolved(request_id=None, constraint=constraint,
                                              decision=pref, resolution="preference_applied",
                                              session_id=session_id)
        return pref

    request_id = str(uuid4())
    log.info("constraint_pause_emitted", constraint=constraint, request_id=request_id,
             trace_id=trace_id, session_id=session_id)

    # 1. Register waiter BEFORE pushing event (prevents race)
    # 2. Push ConstraintPauseEvent
    # 3. Await with timeout
    decision_payload = await register_and_push_constraint(
        session_id=session_id,
        request_id=request_id,
        event=ConstraintPauseEvent(...),
        timeout_seconds=timeout_seconds,
        default_option=default_option,
    )

    decision = decision_payload.get("decision", default_option)
    resolution = decision_payload.get("resolution", "user_choice")
    remember = decision_payload.get("remember", False)

    log.info("constraint_decision_received", constraint=constraint, decision=decision,
             resolution=resolution, trace_id=trace_id, session_id=session_id)

    await self._emit_constraint_resolved(request_id=request_id, constraint=constraint,
                                          decision=decision, resolution=resolution,
                                          session_id=session_id)

    if remember and decision != default_option:
        await self._save_constraint_preference(user_id, constraint, decision)

    return decision
```

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
    preferred_action TEXT NOT NULL,  -- exact option string or 'always_pause'
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_session_id UUID,         -- session where the preference was set
    PRIMARY KEY (user_id, constraint_name)
);
```

- No row = `always_pause` (default). `always_pause` as an explicit value means the user chose to be asked every time.
- `preferred_action` stores the exact option string (e.g., `"Continue (10 more)"`, `"Compress and continue"`). The API validates that the action matches one of the constraint's known options.
- `source_session_id` provides audit trail for when a silent preference started being applied.
- Preferences are loaded once per turn from Postgres (small table, negligible overhead).
- The `DecisionCard` "Remember this choice" toggle sends `remember: true` alongside the decision. The executor upserts the preference.

Preference management endpoint: `PUT /api/v1/preferences/constraint` (body: `{constraint_name, preferred_action}`).

### Wire protocol additions

**Server → client (new event types):**
```json
{"type": "CONSTRAINT_PAUSE",    "seq": 47, "request_id": "...", "data": {"constraint": "tool_iteration_limit", "context": "...", "options": [...], "default_option": "...", "expires_at": "..."}}
{"type": "CONSTRAINT_RESOLVED", "seq": 48, "request_id": "...", "data": {"constraint": "...", "decision": "...", "resolution": "user_choice"}}
{"type": "CANCELLED",           "seq": 49, "data": {"reason": "user_cancel"}}
{"type": "STATE_DELTA",         "seq": 50, "data": {"key": "turn_status", "value": {"context_tokens": 34000, "context_max": 128000, "tool_iteration": 12, "tool_iteration_max": 25, "turn_cost_usd": 0.42}}}
```

**Client → server (new message types):**
```json
{"type": "CONSTRAINT_DECISION",  "request_id": "...", "decision": "Continue (10 more)", "remember": true}
{"type": "USER_CANCEL"}
```

`CONSTRAINT_DECISION` is already routed in `ws_endpoint.py:499` (shipped with ADR-0075). `USER_CANCEL` is a new message type added to the receiver match.

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
- Buttons for each option; tapping sends `{type: "CONSTRAINT_DECISION", request_id, decision, remember}`
- After selection: card collapses to a one-line pill ("▶ Continued — 10 more tool calls") and is no longer interactive
- On receiving `CONSTRAINT_RESOLVED` for the same `request_id`: collapse the card (handles reconnect replay)
- Countdown progress bar from `expires_at` — shows time remaining before default fires
- "Remember this choice" toggle (off by default) — if checked, sets `remember: true`
- If the WS disconnects while waiting: on reconnect, if a `CONSTRAINT_RESOLVED` event follows in the replay, card renders collapsed. If no resolution exists, card renders interactive (waiter is still pending server-side).

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
| `constraint_pause_emitted` | Constraint about to fire, pause event sent | `constraint`, `request_id`, `options` |
| `constraint_decision_received` | User responded or timeout/disconnect resolved | `constraint`, `decision`, `resolution` |
| `constraint_timeout_applied` | `expires_at` elapsed with no response | `constraint`, `default_option` |
| `constraint_preference_applied` | Stored preference skipped the pause | `constraint`, `preferred_action` |
| `constraint_no_ws_default_applied` | No WS connection, default applied silently | `constraint`, `default_option` |
| `user_cancel_received` | User tapped Stop button | — |

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
| 1 | Constraint pause emitted | Run a task that hits 25 tool calls; verify `CONSTRAINT_PAUSE` WS event arrives in browser devtools |
| 2 | DecisionCard renders | `CONSTRAINT_PAUSE` event renders inline card with countdown and "Remember" toggle |
| 3 | "Continue" resumes executor | Pick "Continue (10 more)"; executor proceeds past original limit |
| 4 | "Finish now" forces synthesis | Pick "Finish now"; executor produces synthesis response immediately |
| 5 | Default fires on timeout | Do not respond to `CONSTRAINT_PAUSE`; verify `default_option` applied after `expires_at`; `CONSTRAINT_RESOLVED` with `resolution=timeout_default` appears |
| 6 | Preference stores | Check "Remember this choice" + pick "Continue"; verify `user_constraint_preferences` row created |
| 7 | Preference applies | Set preference for `tool_iteration_limit`; next limit hit fires no pause event; `constraint_preference_applied` in logs |
| 8 | Status bar live | `TurnStatusBar` shows non-zero context tokens, tool count, and cost during a turn |
| 9 | Status bar color thresholds | Tool count turns amber at max-2; context turns amber at 70%, red at 85% |
| 10 | Stop button cancels | Tap Stop mid-turn; executor synthesizes from available results; "Stopped by user" pill in stream |
| 11 | Reconnect replay — resolved | Disconnect after constraint resolved; reconnect; card renders collapsed (not interactive) |
| 12 | Reconnect replay — pending | Disconnect while constraint pending; reconnect; card renders interactive; decision goes through |
| 13 | No-WS fallback | Trigger constraint with no active WS; default applied silently; `constraint_no_ws_default_applied` in logs |
| 14 | Waiter race fixed | Rapid PWA response to `CONSTRAINT_PAUSE` (< 10ms): decision is captured, not dropped |
| 15 | Duplicate decision idempotent | Send same `CONSTRAINT_DECISION` twice (reconnect scenario); executor acts once |
| 16 | Compression pause | Trigger hard compression (85% context fill); `CONSTRAINT_PAUSE` with `context_compression` arrives |
