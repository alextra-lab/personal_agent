# FRE-1673 — `conversation_context_loaded` must show what the history window dropped

Ticket: FRE-1673 (Approved, Tier-2). Source: FRE-1667 study, finding G3. Feeds the FRE-1134 window experiment.
Tier: Standard (`src/` logic, telemetry only). No ADR. No schema change.

## Problem (verified on `origin/main` be70a975)

- `service/app.py` cuts stored history to the last `conversation_max_history_messages` at two sites
  (chat stream, ~L488; chat, ~L2387) and hands only the cut list to the session manager.
- `step_init` (`orchestrator/executor.py`) sets `session_message_count = len(ctx.messages)` from that cut list.
  The field `total_messages_in_db` therefore never exceeds the window.
- `messages_truncated` is `0` on the gateway path by its documented definition: it counts only
  `step_init`'s own `apply_context_window`, which does not run there. It cannot see the window.

## Design

Carry the dropped count on the in-memory `Session`, because the session manager is the only object
that crosses from `app.py` to `step_init`.

1. `orchestrator/session.py`: add `Session.messages_dropped_by_window: int = 0`.
   `update_session(..., messages_dropped_by_window: int = 0)` sets it whenever `messages` is given.
2. `service/app.py`: one helper `_apply_history_window(db_messages, max_history) -> tuple[list, int]`
   replaces the duplicated slice at both sites. Both `update_session` calls pass the dropped count.
3. `orchestrator/executor.py`: `step_init` reads `session.messages_dropped_by_window`.
   `_emit_conversation_context_loaded` gains two fields, on both call sites:
   - `stored_messages_before_window` = `session_message_count + dropped`
   - `messages_dropped_by_window` = `dropped`
4. Keep `total_messages_in_db`, `messages_loaded`, `messages_truncated` unchanged. No dashboard or
   alert in the repo reads them (grep of `*.json`, `*.yml`, `*.ts`, `*.py`, `*.md`).
   Update the helper docstring so `total_messages_in_db` says "after the window".
5. Docs: `docs/specs/CONVERSATION_CONTINUITY_SPEC.md` gets a note on the two new fields.
   ADR-0092 is a decision record and stays unchanged.

## Tests (TDD, offline)

`tests/test_orchestrator/test_frozen_reset_emit.py` (existing emit harness) and
`tests/test_service/test_chat_history_depth.py`, `test_chat_stream_request_completed.py`, `tests/test_orchestrator/test_session.py`.

| Criterion | Test |
|---|---|
| 30 stored, window 20 -> stored 30, dropped 10 | `chat()` and `/chat/stream` tests: the manager receives 20 messages and `messages_dropped_by_window=10`. `step_init` test: a session of 20 messages plus a count of 10 emits `stored_messages_before_window == 30`, `messages_dropped_by_window == 10` |
| 6 stored -> dropped 0 | `chat()` test: 6 messages and a count of 0. `step_init` test: stored 6, dropped 0 |
| no persisted session | `step_init` test: both fields read 0 |
| stale count cleared | `SessionManager` test: a replacement without a count resets it |
| both emit sites | the helper requires both new arguments, so mypy fails if a site omits them. The gateway site is driven by the emit tests |
| old fields unchanged | existing `test_frozen_reset_emit.py` assertions stay green |

Commands: `make test-file FILE=tests/test_orchestrator/test_frozen_reset_emit.py`, the same for the
service and session test files, then `make test`, `make mypy`, `make ruff-check`,
`make ruff-format`, `pre-commit run --all-files`.

## Codex plan review (2026-10-24)

- Arithmetic, the `if prior_messages:` guard, stale values and other callers: confirmed sound.
- Finding: the attachment-cost exit and the lifetime-stop exit in `step_init` return before either
  emit, so those turns emit no event. Verified. These exits pre-date this ticket and the ticket asks
  for correct counts, not for a new emit. Left unchanged and noted in the handoff.
- Finding: tests must cover both service handoffs and the reset of a stale count. Accepted. Added a
  `chat()` test, a `/chat/stream` test and a `SessionManager` test.

## Out of scope

- Raising or changing the window value (FRE-1134).
- Making `apply_context_window` reachable on the gateway path (ADR-0092).
- Emitting the event on the two early exits above.
- Any Grafana or alert change: none reads these fields.
