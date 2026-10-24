# FRE-1677 — A tool approval waits across a socket gap

Ticket: FRE-1677 (Approved, `stream:build2`). Related: FRE-928 (the same fix for constraint waiters),
ADR-0075 (WebSocket transport), ADR-0063 Amendment A/B (approval prompts), ADR-0156 D8.

## AC-1 — cause, with evidence

Source: `current.jsonl` in the gateway telemetry volume, session `afe4d1cd`, times in UTC.
The gateway container logs for 12:49 are gone (the container was replaced at 15:29).

| Time | Event |
|---|---|
| 12:33:48 | `ws.disconnected` for all 3 sockets of the PWA (sessions `2e84961f`, `912cb5ea`, `afe4d1cd`), within 0.7 s |
| 12:41:26 | all 3 sockets reconnect (a gap of 7.6 min) |
| 12:42:41–47 | `ws.disconnected` for all 3 sockets again |
| 12:49:26.525 | `permission_evaluated` bash → `ask` |
| 12:49:26.604 | `transport.approval_request_queued` (the card is persisted for replay) |
| 12:49:26.605 | `transport.approval_decision_received` decision=`connection_lost` (1 ms later) |
| 13:40:41 | the next `ws.connected` for any of the 3 sessions |

Cause: the PWA had no socket for this session from 12:42:47 to 13:40:41. At 12:49:26
`register_waiter` found no entry in `_active_connections` and returned `connection_lost` at once.
It did not wait. The ticket premise "the owner was connected" is not supported: the server saw no
socket. A mobile socket drops and returns all the time (FRE-928).

Second defect with the same root: `_cancel_all_waiters` resolves a pending approval with
`connection_lost` when its socket drops or a reconnect supersedes it. A card that was already shown
is refused even when the same client returns one second later.

Root: approval waiters live on the connection object (`_ConnectionState.waiters`). FRE-928 moved
constraint waiters to a session registry for this exact reason and left approval waiters behind.

## Design

1. Approval waiters move to a session registry `_session_approval_waiters[session_id][request_id]`,
   the same shape as `_session_constraint_waiters`.
2. A disconnect or a supersede no longer resolves an approval waiter. The card is persisted and
   replayed on reconnect. `settings.approval_timeout_seconds` (60 s) bounds the wait. A timeout still
   denies, so the safety posture is unchanged.
3. `USER_CANCEL` still resolves pending approval waiters.
4. Headless guard: a session that never opened a socket in this process (no live connection and no
   cancel event, see `get_cancel_event`) still returns `connection_lost` at once, as before.
   Reason: eval and `/chat` callers have no PWA, and the eval harness keys on
   `approval_connection_lost`. A 60 s stall per approval there would be a regression.
5. Diagnostic: `transport.approval_request_queued` logs `client_connected`, so the next incident is
   readable from one log line.
6. Gap memo (codex review, High): `_approval_gap_expired` holds sessions where an approval timed out
   with no live socket. While a session is in it and has no socket, `register_waiter` fails fast with
   `connection_lost`. A connect clears it. This keeps one socket gap from costing 60 s per queued
   approval (approvals are serialized by `_APPROVAL_LOCKS`).
7. Cleanup (codex review, Medium): the `try/finally` opens before the timeout task and the push, as in
   `register_constraint_waiter`. Tests cover cancel during push, cancel while waiting, and push failure.

Known and left alone (codex review, Low): a timed-out approval event stays in the replay buffer, so a
later reconnect flashes an expired card that the PWA auto-denies. This exists today and is harmless
(the server drops decisions for missing waiters). Noted in the handoff.

Not changed: `approval_timeout_seconds` default (60 s). A budget or timing change is the owner's
decision. This incident gap was 58 min, so no timeout would have saved it.

## Steps

1. Tests first (`tests/personal_agent/transport/test_ws_endpoint.py`), confirm they FAIL on current code:
   - `test_no_socket_at_request_waits_and_the_card_reaches_the_reconnected_client`: session has
     connected before (cancel event exists), no entry in `_active_connections`. Start
     `register_waiter` with a 5 s timeout and an `on_registered` that records the push. After 50 ms, put
     a new connection in `_active_connections` and call `_resolve_waiter(new_conn, id, approve)`.
     Assert decision `approve` and that the push ran. Reproduces 12:49:26.
   - `test_disconnect_does_not_resolve_approval_waiter`: the socket drops mid-wait; the waiter is still
     pending; a reconnect then answers it → `approve`.
   - `test_supersede_does_not_resolve_approval_waiter`: same with a replacement connection.
   - `test_never_connected_session_still_fails_fast`: the old `connection_lost` test, kept.
   - `test_user_cancel_resolves_approval_waiter`, `test_timeout_with_no_socket_returns_timeout`,
     `test_registry_cleaned_after_resolution`.
   Command: `make test-file FILE=tests/personal_agent/transport/test_ws_endpoint.py`
2. Implement in `src/personal_agent/transport/agui/ws_endpoint.py`:
   - add `_ApprovalWaiter` and `_session_approval_waiters`;
   - rewrite `register_waiter`, `_waiter_timeout`, `_resolve_waiter`, `_resolve_all_waiters_user_cancel`;
   - delete `_cancel_all_waiters`, its two call sites, and the `waiters`, `waiter_payloads`,
     `waiter_timeouts` fields of `_ConnectionState`; fix the module and class docstrings.
3. `src/personal_agent/transport/agui/transport.py`: add `client_connected` to the approval log.
4. Update tests that encoded the old contract: `test_ws_endpoint.py` (3 tests),
   `tests/security/test_pivot2_pentest.py` case 15 sub-case b,
   `tests/personal_agent/transport/test_constraint_governance.py` (2 uses of `_cancel_all_waiters`
   become a plain connection swap).
5. Docs: one line in ADR-0075 notes that approval waiters are session-scoped (FRE-1677).
6. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Acceptance criteria and proof

| AC | Proof |
|---|---|
| AC-1 | The table above, posted on the ticket |
| AC-2 | `test_no_socket_at_request_waits_and_the_card_reaches_the_reconnected_client`: it fails on the old code (`connection_lost` at once) and passes after |
| AC-3 | Live, master side. After deploy, the owner's next approval shows the card. Logs: `transport.approval_request_queued` with `client_connected=true`, then `approval_granted` |
