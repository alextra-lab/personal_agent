# ADR-0076: Adaptive Constraint Governance Protocol

**Status:** Proposed
**Date:** 2026-05-27
**Issue:** FRE-389
**Supersedes:** —
**Related:** ADR-0075 (WebSocket transport), ADR-0046 (AG-UI transport), ADR-0033 (model taxonomy)
**Depends on:** ADR-0075 Phase 1 complete

## Context

A live agent run on 2026-05-27 demonstrated a class of failure that is currently invisible to the user:

The LLM was asked to write a large artifact. `claude_sonnet.max_tokens=8192` caused the response to truncate mid-JSON; the required `content` parameter was never included; the executor detected the missing param and retried; the model could never succeed within the budget constraint regardless of retries. The loop ran 3+ times at ~$0.13/call before `force_synthesis_from_limit` fired and produced a degraded response. The user saw a slow, expensive, wrong result with no indication of what happened or any opportunity to intervene.

This is not an isolated incident. It is a symptom of a design pattern: constraints in the harness are **static, hard-coded, and silent**. They fire without user visibility and without giving the user any agency.

Current silent constraint sites:

| Site | File | What fires silently |
|---|---|---|
| `force_synthesis_from_limit` | `orchestrator/executor.py` ~L2414 | Max tool iterations hit → forced synthesis, user not consulted |
| Context compression | `orchestrator/context_window.py` | 85% threshold → silent compression of conversation history |
| `tool_budget_warning_injected` | `orchestrator/executor.py` ~L1711 | Warning injected into LLM context at max-2 remaining — user still not informed |

Constraints the user has no visibility into whatsoever:
- Current tool iteration count vs. limit
- Current context token usage vs. window
- Which constraints have fired this turn

## Decision

Replace silent hard limits with **user-visible, user-controllable pause points**. When a constraint is about to fire, the harness pauses and presents a `CONSTRAINT_PAUSE` event via the WebSocket transport (ADR-0075). The user sees an inline `DecisionCard` in the chat stream and can choose how to proceed. A `user_constraint_preferences` table stores their standing preference for each constraint so repeat decisions are applied automatically without interruption.

### Design principle: safe defaults, not silent defaults

The system currently has silent defaults — constraints fire and the harness makes a unilateral decision. The new default is `always_pause`: the user is asked every time until they set a preference. This is the right default for a research system where the user is actively building intuition about the harness's behavior.

### New event type: `ConstraintPauseEvent`

Added to `transport/events.py`:

```python
@dataclass(frozen=True)
class ConstraintPauseEvent:
    """Harness constraint about to fire — pause and request user decision.

    Attributes:
        request_id: UUID for the WS decision round-trip.
        session_id: Target session identifier.
        trace_id: Trace context for telemetry correlation.
        constraint: Which constraint is about to fire.
        context: Human-readable description of the situation.
        options: Available choices (first option is the default).
        default_option: The option applied if expires_at passes with no response.
        expires_at: ISO-8601 UTC; default fires after this time.
    """
    request_id: str
    session_id: str
    trace_id: str
    constraint: Literal[
        "tool_iteration_limit",
        "context_compression",
        "timeout_expiring",      # Phase 2
    ]
    context: str
    options: Sequence[str]
    default_option: str
    expires_at: str  # ISO-8601 UTC
```

Added to `InternalEvent` union and `adapter.py:to_agui_event()` → `"CONSTRAINT_PAUSE"` wire type.

### Round-trip via WebSocket (ADR-0075)

```
executor approaches limit
    → check user_constraint_preferences for this (user_id, constraint)
    → if preference is set: apply it silently and continue
    → else: push ConstraintPauseEvent to queue
            register asyncio.Event for request_id
            await event (with expires_at timeout → default_option fires)
    → WS sender delivers CONSTRAINT_PAUSE to PWA
    → user sees DecisionCard, picks option
    → PWA sends {type: "CONSTRAINT_DECISION", request_id: "...", decision: "Continue (10 more)"}
    → WS receiver routes to registered event
    → executor reads decision, adjusts limit or stops
```

No Future registry, no separate POST endpoint. The WS connection is the decision channel.

### Executor integration

**`force_synthesis_from_limit` (executor.py ~L2414):**

Before the current `force_synthesis_from_limit` block:

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
    ...
```

Replace the current unconditional `force_synthesis_from_limit` call with:

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

**Context compression (`context_window.py`):**

Before the compression step:

```python
decision = await self._maybe_pause_for_constraint(
    constraint="context_compression",
    context=f"Context is at {pct:.0f}% of the window ({tokens:,} / {max_tokens:,} tokens). Compressing will summarise older turns.",
    options=["Compress and continue", "Stop here instead"],
    default_option="Compress and continue",
)
```

### User preferences

New table (added to `docker/postgres/migrations/`):

```sql
CREATE TABLE user_constraint_preferences (
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    constraint  TEXT NOT NULL,
    behavior    TEXT NOT NULL CHECK (behavior IN ('always_pause', 'always_continue', 'always_stop')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, constraint)
);
```

No row = `always_pause` (default). Preferences are loaded once per turn from Postgres (small table, negligible overhead). The `DecisionCard` component offers a "Remember this choice" toggle; when checked, the PWA sends a preference-save payload alongside the decision.

Preference management endpoint: `PUT /api/v1/preferences/constraint` (body: `{constraint, behavior}`).

### PWA: `DecisionCard` component

**Location:** `seshat-pwa/src/components/DecisionCard.tsx`

**Design:** Inline chat bubble, not a modal. Renders when a `CONSTRAINT_PAUSE` WS event arrives, inserted into the message stream at the current position. Distinct from `ApprovalModal` (full-screen, blocking, for high-risk tool calls).

**Behaviour:**
- Buttons for each option; tapping sends `{type: "CONSTRAINT_DECISION", request_id, decision}`
- After selection: card collapses to a one-line pill ("▶ Continued — 10 more tool calls") and is no longer interactive
- Countdown progress bar from `expires_at` — shows time remaining before default fires
- "Remember this choice" toggle (off by default) — if checked, also sends preference save
- If the WS disconnects while waiting: card shows "Connection lost — default applied" when reconnected

**UX reference:** Claude Code's `AskUserQuestion` panels that vanish after selection. Not a blocking modal.

### Context budget meter (existing component, now functional)

`ContextBudgetMeter.tsx` already exists but receives no data. Wire it via `STATE_DELTA` events:

```python
# In executor.py, emit after each LLM call:
await transport.send_state({
    "context_tokens": current_tokens,
    "context_max": max_tokens,
    "tool_iteration": current_iteration,
    "tool_iteration_max": effective_limit,
}, session_id=session_id)
```

This is a separate concern from constraint pauses but part of the same "user visibility" theme. Low implementation cost; high user value.

## Consequences

**Positive:**
- User is never surprised by forced synthesis — they choose when to stop
- User builds intuition about the harness's actual behavior (turn lengths, context pressure)
- Preferences table means repeat decisions are frictionless; users who always want to continue don't get interrupted
- `ContextBudgetMeter` finally receives real data — dead component becomes live

**Negative:**
- Constraint pauses add latency (user must respond before the executor can continue); mitigated by the `expires_at` timeout applying the default automatically
- Requires ADR-0075 (WS transport) to be shipped first

**Neutral:**
- `force_synthesis_from_limit` still exists as the fallback when the user picks "Finish now" or the timeout fires — the harness mechanism is unchanged, only when it fires

## Out of scope for this ADR

- **Adaptive `max_tokens`**: context-aware per-call output budget (the truncation loop root cause is fixed by bumping `claude_sonnet.max_tokens` to 32768; dynamic budgeting is a future ADR)
- **Timeout expiring (`timeout_expiring` constraint)**: requires a per-turn timeout heartbeat — Phase 2 gate in this ADR's implementation
- **Settings UI** for constraint preferences: follows after the backend table and API endpoint exist
- **Tool approval integration**: tool approvals already use a round-trip pattern (ADR-0075 migrates them to WS); no change to approval semantics needed

## Implementation phases

**Phase 1 (this ADR):**
- `ConstraintPauseEvent` type + adapter mapping
- `_maybe_pause_for_constraint()` method in executor
- Wire into `force_synthesis_from_limit` and context compression trigger
- `user_constraint_preferences` table + `PUT /api/v1/preferences/constraint`
- `DecisionCard` PWA component
- `STATE_DELTA` emissions for context meter

**Phase 2 (future ADR):**
- `timeout_expiring` constraint type with LLM-call heartbeat
- Preference-save from `DecisionCard` wired to API

## Acceptance criteria

| Criterion | Verification |
|---|---|
| Constraint pause emitted | Run a task that hits 25 tool calls; verify `CONSTRAINT_PAUSE` WS event arrives in browser devtools |
| DecisionCard renders | `CONSTRAINT_PAUSE` event renders inline card with countdown |
| "Continue" resumes executor | Pick "Continue (10 more)"; executor proceeds past original limit |
| "Finish now" forces synthesis | Pick "Finish now"; executor produces synthesis response immediately |
| Default fires on timeout | Send `CONSTRAINT_PAUSE` event, do not respond; verify `default_option` is applied after `expires_at` |
| Preference stores and applies | Set `always_continue` for `tool_iteration_limit`; next limit hit fires no pause event |
| Context meter live | `ContextBudgetMeter` shows non-zero token count during a turn |
