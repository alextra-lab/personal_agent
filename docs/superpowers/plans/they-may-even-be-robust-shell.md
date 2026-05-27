# Plan: Revise ADR-0076 — Adaptive Constraint Governance

## Context

ADR-0076 was Codex-reviewed and cross-referenced against FRE-390/391/392 (sibling issues from ADR-0075/0077 work). The review found 5 blocking issues and several significant gaps. The ADR's product direction is correct but the spec has holes that would cause another 8-hotfix cascade like ADR-0075. This plan revises the ADR in-place to fix those issues.

Additionally, FRE-391 and FRE-392 are missing from the MASTER_PLAN Needs Approval table.

## Files to modify

1. **`docs/architecture_decisions/ADR-0076-adaptive-constraint-governance.md`** — the ADR itself
2. **`docs/plans/MASTER_PLAN.md`** — add FRE-391 and FRE-392 to Needs Approval table

## ADR-0076 Revisions

### Fix 1: Waiter registration race condition (Blocking)
**Problem:** `transport.py:209` pushes the event before `ws_endpoint.py:232` registers the waiter. If the PWA responds before registration, `_resolve_waiter()` at `ws_endpoint.py:148` drops the decision silently.
**Fix in ADR:** Add a section under "Round-trip via WebSocket" specifying: register the `asyncio.Event` waiter BEFORE pushing the `ConstraintPauseEvent` to the queue. Reference the existing race in `transport.py:209-232` as a known issue to fix atomically with this ADR. Add to acceptance criteria.

### Fix 2: Decision result type mismatch (Blocking)
**Problem:** `ApprovalDecision` at `ws_endpoint.py:71` is typed `Literal["approve", "deny", "timeout", "connection_lost"]`. ADR-0076 needs constraint-specific values like `"Continue (10 more)"`.
**Fix in ADR:** Define a new `ConstraintDecision` dataclass (parallel to `ApprovalDecision`) with `decision: str` (free-form, validated against the `options` list from the originating event) + `remember: bool` (for the preference toggle). The WS receiver routes `CONSTRAINT_DECISION` messages to this type, not `ApprovalDecision`. Update wire protocol section.

### Fix 3: Compression integration points unnamed (Significant)
**Problem:** ADR says "Context compression (context_window.py)" but there are THREE sites: soft async (`compression_manager.py:91`), hard sync (`executor.py:1429`), and Stage 7 truncation (`context_window.py:54`, called from `executor.py:1198`).
**Fix in ADR:** Enumerate all three. Only the hard sync compression (`executor.py:1429`, fires at 85% threshold) gets a `CONSTRAINT_PAUSE`. Soft async and Stage 7 truncation remain silent (they're safety nets, not decision points). Make this explicit in the ADR.

### Fix 4: `behavior` column too coarse (Blocking)
**Problem:** `always_continue` is ambiguous — "continue by how much?" Options are constraint-specific strings.
**Fix in ADR:** Replace `behavior TEXT` with `preferred_action TEXT` that stores the exact option string (e.g., `"Continue (10 more)"`, `"Compress and continue"`). Add `CHECK (preferred_action IN ('always_pause', ...))` — actually no, keep it open since options vary per constraint. Add a comment that `always_pause` is the magic value meaning "ask every time" (and is the default when no row exists). The API validates that `preferred_action` matches one of the constraint's known options.

### Fix 5: Missing `CONSTRAINT_RESOLVED` event (Blocking)
**Problem:** On reconnect, the PWA replays a `CONSTRAINT_PAUSE` but has no way to know if it was already decided or timed out. Results in stale interactive cards.
**Fix in ADR:** Define `CONSTRAINT_RESOLVED` as a new wire event type. The server emits it (and persists to `session_events`) when: (a) user makes a decision, (b) timeout fires default, or (c) disconnect resolves with `connection_lost`. The PWA uses it to collapse the card on replay. Add `ConstraintResolvedEvent` to `events.py` union.

### Fix 6: Phase 1/2 preference-save inconsistency (Blocking)
**Problem:** Phase 1 includes the preferences table + API but Phase 2 defers "Preference-save from DecisionCard wired to API." Ships dead infrastructure.
**Fix in ADR:** Move preference save to Phase 1. The "Remember this choice" toggle + API call is part of the DecisionCard component. Phase 2 is purely `timeout_expiring` constraint type.

### Fix 7: `timeout_expiring` in Phase 1 Literal list (Minor)
**Problem:** The `ConstraintPauseEvent.constraint` Literal includes `"timeout_expiring"` with a Phase 2 comment.
**Fix in ADR:** Remove from Phase 1 Literal. Add a note that Phase 2 extends the Literal.

### Fix 8: No-active-WS fallback unspecified (Significant)
**Problem:** `register_waiter()` returns `connection_lost` immediately when no active WS exists. ADR doesn't say what happens.
**Fix in ADR:** When no WS is connected, apply the `default_option` immediately (no pause). Log at info level with the constraint name and default applied. This preserves current behavior (silent constraint firing) for headless/API-only usage.

### Fix 9: No telemetry requirements (Significant)
**Problem:** No logging spec for constraint governance events.
**Fix in ADR:** Add telemetry table: `constraint_pause_emitted`, `constraint_decision_received` (with decision value), `constraint_timeout_applied`, `constraint_preference_applied` (when stored preference skips the pause). All carry `trace_id`, `session_id`, `constraint`.

### Fix 10: `STATE_DELTA` payload shape mismatch (Significant)
**Problem:** PWA `useSSEStream.ts:148` only handles `key === 'context_window'` with a single number. ADR proposes multiple new keys.
**Fix in ADR:** Standardize on the existing `context_window` key with an expanded value shape: `{tokens: number, max: number, tool_iteration: number, tool_iteration_max: number}`. The PWA handler already dispatches on this key; just needs to parse an object instead of a bare number. Add backward-compat note: if value is a number, treat as ratio (existing behavior). Describe the PWA change in the Files Changed section.

### Fix 11: Add USER_CANCEL — Stop button (new capability)
**Problem:** No way for the user to stop the agent mid-turn before a constraint fires.
**Fix in ADR:** Add `USER_CANCEL` as a new client→server WS message type. Add to the wire protocol section. The executor checks for a cancel flag between tool iterations (same check-point as `force_synthesis_from_limit`). When set, force synthesis immediately. Emit a `CANCELLED` event (persisted to `session_events`) so the PWA can show a "Stopped by user" pill. Add to `ws_endpoint.py` receiver routing. This is the Send→Stop button pattern from Claude Code.

### Integration from sibling issues

**From FRE-392 (duplicate message delivery):**
- Add FRE-392 as a **prerequisite** (or concurrent fix): constraint decisions sent over a transport that duplicates inbound messages will produce double-execution. The ADR's "duplicate responses silently dropped" (line 135) is necessary but not sufficient — the `CONSTRAINT_RESOLVED` event (Fix 5) also helps because the PWA won't re-render an already-resolved card.
- Add acceptance criterion: "Reconnect during pending constraint pause does not produce duplicate executor actions."

**From FRE-391 (dynamic max_tokens):**
- Acknowledge honestly in the Context section: the motivating truncation-loop example is a symptom of static `max_tokens`, which FRE-391 will address. Constraint governance remains valuable for tool iteration limits, context compression, and future timeout — but the specific artifact truncation case will be better solved upstream.

**From FRE-390 (eval transport coverage):**
- Add a note in Implementation Phases that Phase 1 should include at least one integration test that opens a real WS, triggers a constraint pause, sends a decision, and verifies executor continuation. Reference FRE-390 as the broader coverage gap.

### UI Design Decision: Three Surfaces

**Surface 1: Send → Stop button (like Claude Code)**
The existing Send button (orange up-arrow in input area) transforms into a Stop button while the agent is actively streaming. Tapping Stop sends a `USER_CANCEL` message via WS. The executor catches it between tool iterations, cancels the current loop, and forces synthesis from results gathered so far. A "Stopped by user" pill appears in the message stream. This is independent of constraint pauses — user can stop at ANY time.

New WS client→server message: `{"type": "USER_CANCEL"}`
New executor check: between tool iterations, poll for cancel signal (same pattern as the existing `force_synthesis_from_limit` flag, but triggered externally).

**Surface 2: Status bar below input area**
Persistent bar below the input field (in the space shown by the red dashes in the screenshot). Visible only during active streaming. Shows:
- Context window: `ctx: 34K/128K ████░░ 27%`
- Tool iterations: `tools: 12/25`
- Turn cost: `$0.42`

Tool count turns amber at max-2 (current `tool_budget_warning` threshold). Context turns amber at 70%, red at 85% (hard compression threshold).

Fallback: if too intrusive on mobile, degrade to per-turn header above agent's current response.

**Surface 3: DecisionCard (inline in message stream)**
Fires when a constraint is about to trigger. Buttons for options, countdown timer, "Remember this choice" toggle. Collapses to pill after selection. Same as currently spec'd in the ADR.

**`STATE_DELTA` wire contract:** Single key `"turn_status"` with object value:
```json
{"context_tokens": 34000, "context_max": 128000, "tool_iteration": 12, "tool_iteration_max": 25, "turn_cost_usd": 0.42}
```
Emitted after each LLM call and after each tool execution. PWA `useSSEStream.ts` handler dispatches to a `TurnStatusBar` component. The existing `ContextBudgetMeter` is replaced by this broader component.

### MASTER_PLAN update

Add FRE-391 and FRE-392 to the Needs Approval table:

```
| [FRE-391](https://linear.app/frenchforest/issue/FRE-391) | Medium | Opus | Dynamic max_tokens based on tool/task context — addresses root cause of artifact truncation loop |
| [FRE-392](https://linear.app/frenchforest/issue/FRE-392) | Medium | Sonnet | WS transport duplicate message delivery on reconnect — idempotency guard needed |
```

## Verification

- Read the revised ADR end-to-end for internal consistency
- Verify all 6 Codex blocking findings are addressed
- Verify FRE-390/391/392 integration points are present
- Verify Phase 1/2 split is clean (no dead infrastructure in Phase 1, no Phase 2 items in Phase 1 schema)
- Verify UI design section specifies persistent status bar with context/tools/cost
- `make ruff-check` / `make ruff-format` are N/A (markdown only)
- Commit and push so it's reviewable on GitHub (per feedback memory)

## Linear

User should approve FRE-391 and FRE-392 (currently Needs Approval). FRE-389 stays Needs Approval until the revised ADR is reviewed.
