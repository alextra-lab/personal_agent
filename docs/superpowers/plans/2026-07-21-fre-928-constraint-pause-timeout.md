# FRE-928 — The constraint pause bypasses its own timeout when no socket is attached

Backing ADR: ADR-0122 (blocks FRE-921 / AC-7). Related: FRE-935 (ADR-0123 T2) cedes AC-4 to
whichever ticket lands first — this one.

## Root cause (verified in source, not inherited)

**Constraint waiters are owned by the connection object, not the session.**
`_ConnectionState` (ws_endpoint.py:66-78) holds `waiters` / `waiter_payloads` /
`waiter_timeouts` / `waiter_metadata`. Every one of the three observed failures follows from
that single fact:

| Observed failure | Mechanism | file:line |
|---|---|---|
| AC-1/2 — no socket at pause → instant `connection_lost` | `conn is None` early return, before the `asyncio.Event` or the timeout task exist | `ws_endpoint.py:257-259` |
| AC-5 — reconnect kills a pending decision | fresh handshake evicts old conn → `_cancel_all_waiters(old_conn)` resolves the pending waiter as `connection_lost` | `ws_endpoint.py:530-537` → `:400-416` |
| AC-6 — half-open socket held ~2 min | waiter liveness is inferred from *registration*, never from inbound traffic | `_ConnectionState` has no `last_inbound_at` |

Two corrections to the ticket's premise, both verified:

1. The ticket says the constraint waiter "runs the push coroutine — which does persist the
   event for replay — and then returns connection-lost". It does **not** run `on_registered`
   on the no-connection path (`:257-259` returns before `:273`), so the pause event is
   **not** persisted and there is nothing to replay. Deliberate, per `transport.py:164-166`.
   The bypass-then-persist behaviour the ticket describes is the **approval** waiter
   (`:177-181`), a different function. Net effect on the fix: unchanged — but AC-1 needs the
   push to start happening on this path, which is a behaviour change the ticket assumed was
   already there.
2. AC-6's "detects a dead connection only at the next handshake" is not quite right. The
   receiver already bounds it: `asyncio.wait_for(ws.receive_text(), timeout=ws_ping_timeout_seconds)`
   at `:693-703`, default 60s vs a 25s client ping — 2.4 missed intervals, then close 1001 →
   teardown → unregister. The owner's ~2-min observation is consistent with that window
   starting from the last *received* ping. So the bound exists and is untested; it is not absent.

## Sequencing disagreement — surfaced, not silently overridden

The third ticket comment says the liveness half "must be sequenced FIRST — criteria one, two
and five are all unreliable while the server cannot distinguish a live client from a dead one."

That is true of the **current** connection-scoped design and false of the fix. Once waiters are
session-scoped, "is a socket live right now" leaves the critical path entirely:

- AC-1: no conn → register on session, persist, wait the timeout. Reconnect replays, user answers.
- AC-2: no client ever → timeout fires → default. Never hangs.
- AC-5: reconnect → eviction no longer touches session waiters → replay delivers → answer resolves.
- Half-open: push lands in a dead socket, but the event is persisted, the waiter stays pending,
  and the client's own reconnect (30–140s observed) replays it. Correct outcome, no liveness check.

Liveness detection stops being a prerequisite and becomes telemetry honesty. **Recommendation:
one PR, session-scoping first, liveness as an assertion + a `last_inbound_at` field.** If the
owner still wants liveness sequenced first as a separate ticket, say so and I will split.

## Scope decision — approval waiters are deliberately NOT changed

`register_waiter` (`:151-205`) has the same instant-`connection_lost` shape. Leaving it alone:
it is the HITL tool-approval path, where failing closed fast is a *safety* posture, not a bug,
and changing it is blast radius this ticket did not ask for. Noted in the PR as a non-change.

## Plan

### 1. Session-scoped constraint waiters — `ws_endpoint.py` → verify: new tests in step 6 fail before, pass after

- Add `_ConstraintWaiter` dataclass (`event`, `payload`, `timeout_task`, `metadata`) and
  `_session_constraint_waiters: dict[str, dict[str, _ConstraintWaiter]]` keyed session → request_id.
- `register_constraint_waiter`: delete the `conn is None` early return. Always create the
  waiter entry, always `await on_registered()` (persist + enqueue), always schedule the
  timeout task, then `await event.wait()`. `finally` pops the request_id and drops the
  session entry when it empties (no leak).
- `_constraint_waiter_timeout`: resolve against `_session_constraint_waiters`, not `_active_connections`.
- `_resolve_constraint_decision` / its `_resolve_waiter` call: route to the session registry
  (the receiver already has `conn.session_id`).
- `_cancel_all_waiters` (eviction `:532` + teardown `:578`): **approval waiters only**.
  Constraint waiters survive both and ride their timeout. This is the AC-5 fix.
- `_resolve_all_waiters_user_cancel` (USER_CANCEL / Stop): must still resolve **both**
  registries — a Stop press must not leave the executor blocked for the full timeout.
- **Cleanup coverage (codex finding 2).** The `try/finally` must open **before**
  `on_registered()` and the timeout-task creation, not just around `event.wait()`.
  `_push_event` does DB persist + enqueue work (`transport.py:85-118`); if it raises, or the
  executor task is cancelled mid-push, the session-scoped entry would otherwise leak
  permanently (a connection-scoped leak used to be swept by disconnect — session scope
  removes that accidental safety net). Also cancel the timeout task in the same `finally`.

### 1b. Overlapping turns on one session (codex finding 1) → verify: PWA queue test

`/chat/stream` launches each turn with `asyncio.create_task` and no per-session in-flight
lock (`app.py:2324-2336`), while the PWA holds a single `pendingConstraint`
(`useSSEStream.ts:62,129`) that each `CONSTRAINT_PAUSE` overwrites (`:272-281`). Two
concurrent pauses ⇒ the first card is unanswerable. Today it is cancelled quickly; after
step 1 it would linger the full timeout — so my change makes this scenario *worse* unless
handled.

Server side is already correct (waiters are keyed by `request_id`). Fix the client:
`pendingConstraints: PendingConstraint[]`, append-with-dedupe on pause, remove by
`request_id` on answer/resolve, and expose `pendingConstraint = pendingConstraints[0]` so
**no consumer changes** (3 call sites in `StreamingChat.tsx` keep their current shape).

### 2. Deliberate timeout — `config/settings.py`, `executor.py` → verify: `make test`

- New setting `constraint_pause_timeout_seconds: float = 180.0` (currently a bare `60.0`
  literal at `executor.py:562`, overridden by no call site).
- **180s rationale**: observed reconnect gaps 41s and 52s, churn period 30–140s. 60s does not
  cover the tail; 180s clears the observed worst case with margin and still bounds an
  unattended build at three minutes. **This is the judgment call the ticket delegated —
  flagging it explicitly for sign-off.**
- `_maybe_pause_for_constraint` reads the setting as its default.

### 3. AC-3 — say when the default was applied → verify: test asserts the sentence in `final_reply`

**Revised after codex finding 4.** The original plan put the notice on the tool-result dict.
That does **not** prove AC-3: the tool result is JSON-serialized into a tool-role message
(`tool_dispatch.py:206-209`) and the user-visible text is synthesized later from
`ctx.final_reply` — the primary model is free to drop the field.

Use the **shipped deterministic-disclosure precedent** instead (ADR-0101 §6 / FRE-690,
`executor.py:4710-4714`): "guardrail alterations are disclosed in the response,
deterministically — never left to the model to relay." Exactly AC-3's requirement.

- `types.py`: add `decision_disclosures: list[str]` to `ExecutionContext`, alongside the
  existing `attachment_disclosures`.
- `executor.py:4712`: extend the same block to append `decision_disclosures`.
- `artifact_tools.py:1512-1515` (the no-answer branch): append a sentence naming the model
  that ran and stating no answer was received, e.g. *"No answer was received in time, so this
  artifact was built with `<deployment-key>` (the configured default)."*
- Keep `builder_model` on the tool result for telemetry, but AC-3's proof is the
  `final_reply` assertion, not the dict field.

### 4. AC-4 — absent ≠ zero in the status bar → verify: PWA tests (a)/(b)/(c)

Implemented to **FRE-935's** stricter spec (it is the same defect; that ticket defers to this one):

- `types.ts`: make absence representable — the numeric gauge fields become
  `number | null` on the seed path rather than being defaulted to `0`/`6`.
- `StreamingChat.tsx:177`: delete the `tool_iteration_max: 6` seed. Absent stays absent until
  the server's first `turn_status` (which already carries both fields).
- `TurnStatusBar.tsx:50-53`: `safeNum` must stop collapsing `null`→`0` for gauge fields.
  Render `tools —/—` (explicit unknown) when either number is absent; **never** compute
  `toolsAmber` from an absent ceiling. A real `tool_iteration: 0` renders `0`, visibly
  different from unknown — that is FRE-935's discriminator (c).

### 5. AC-6 — liveness, honestly → verify: new test in step 6

- Add `last_inbound_at: float` to `_ConnectionState`, stamped on every inbound message in
  `_receiver` (not just PING).
- Test-assert the existing bound: a client that stops responding without closing is
  unregistered within `ws_ping_timeout_seconds`. Document the 25s-ping / 60s-timeout ratio
  in the settings docstring so the relationship is not accidentally broken.
- The "does not resolve as delivered" half of AC-6 is satisfied **by construction** after
  step 1: nothing resolves a constraint waiter except an actual decision, USER_CANCEL, or the
  timeout.

### 6. Tests (TDD — each must fail against current behaviour first)

Python, `tests/personal_agent/transport/`:
- `test_no_connection_registers_waiter_and_waits` — AC-2: no conn → does **not** return
  instantly, resolves `timeout_default` when the clock expires.
- `test_no_connection_persists_pause_for_replay` — AC-1a: `on_registered` runs (the event is
  persisted) even with no conn. *Fails today — currently skipped entirely.*
- `test_reconnect_inside_window_delivers_and_resolves` — AC-1: drop before pause, reattach
  inside the window, replayed card answered → `user_choice`. *Fails today.*
- `test_pending_decision_survives_reconnect` — AC-5: evict while pending → waiter still
  pending, new connection's decision resolves the original waiter. *Fails today
  (`_cancel_all_waiters` resolves it `connection_lost`).*
- `test_user_cancel_still_resolves_session_waiter` — regression guard on the Stop button.
- `test_half_open_connection_unregistered_within_ping_timeout` — AC-6.
- Update `test_no_connection_returns_default_connection_lost` (test_constraint_governance.py:129)
  and `test_no_connection_returns_connection_lost` (test_ws_endpoint.py:104) — these two
  **encode the bug as the contract** and must be rewritten, not deleted.
- `test_constraint_pause.py:43` `test_no_ws_default_no_resolution_emitted` — same, rewrite.

PWA, `seshat-pwa/src/__tests__/TurnStatusBar.test.tsx`:
- (a) no turn status → unknown treatment, no amber, no `N/M`.
- (b) `tool_iteration_max: 25` → renders 25 (server-authoritative).
- (c) `tool_iteration: 0` → renders `0`, distinct from (a).
- Existing amber-at-max−2 tests keep passing when both numbers are present.

### 7. Quality gates
`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`cd seshat-pwa && npm run lint && npm run test` · `pre-commit run --all-files` ·
code-review skill at **high** (transport concurrency + a live user-decision path) ·
security-review (touches the WS message path).

## Acceptance criteria → proof map

| AC | Proof |
|---|---|
| 1 — reconnect inside window resolves on the user's real choice | `test_reconnect_inside_window_delivers_and_resolves` + `test_no_connection_persists_pause_for_replay` |
| 2 — no client at all falls back on timeout, never instantly, never hangs | `test_no_connection_registers_waiter_and_waits` + live headless request |
| 3 — default-applied is stated in turn output and names the model | test on the no-answer branch asserting `builder_default_applied` / `builder_model` / `builder_notice` |
| 4 — no warning colour from a fallback constant | PWA (a)/(b)/(c) |
| 5 — pending decision survives a reconnect | `test_pending_decision_survives_reconnect` |
| 6 — dead connection detected from missed pings, not held | `test_half_open_connection_unregistered_within_ping_timeout` + by-construction after step 1 |

## Deploy (master's, not mine)
Gateway rebuild (waiter) + PWA rebuild with `CACHE_NAME` bump (status bar). Both ask-first.

## Codex plan-review (adversarial second opinion) — disposition

| # | Finding | Disposition |
|---|---|---|
| 1 | Overlapping turns lose a pending choice (PWA holds one card) | **Folded in** — step 1b, PWA queue |
| 2 | Registration/`on_registered` not covered by cleanup → leak | **Folded in** — step 1 cleanup coverage |
| 3 | Stale approval-card replay (approval waiter left connection-scoped) | **Out of scope → follow-up ticket.** Pre-existing; my diff does not touch the approval path. A replayed approval card whose waiter was already resolved is a real defect, but it is the approval mechanism's, not this one's |
| 4 | AC-3 unprovable via tool-result dict — model may drop it | **Plan revised** — deterministic `final_reply` disclosure (step 3) |
| 5 | AC-6 liveness is *not* a prerequisite for AC-1/2/5 | **Confirms my thesis** against the ticket comment's sequencing claim |
| 6 | Single-worker uvicorn — in-process registry is sound | **Confirms** (verified independently: no `--workers` in `Dockerfile.gateway:86` or `docker-compose.cloud.yml:427`) |
| 7 | Cloudflare edge WS idle behaviour unprovable from repo | **Live-verify item.** Mitigated: `/chat/stream` is fire-and-forget (`app.py:2242-2255`) so a paused turn holds **no** HTTP request — the 100s edge timeout for HTTP does not apply. The WS carries 25s client pings, well inside any idle window. Confirm on the first live run |

## Open questions for the owner
1. **180s** constraint timeout — endorse, or prefer a different number?
2. Sequencing: one PR as argued above, or still split liveness out first?
3. AC-3 surface: tool-result fields relayed in the turn text (planned), or would you rather
   the PWA render the "default applied" notice off the existing `CONSTRAINT_RESOLVED` event?
