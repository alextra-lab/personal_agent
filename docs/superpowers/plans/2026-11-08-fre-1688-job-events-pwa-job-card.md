# FRE-1688 — Job events and the PWA job card (ADR-0159 D10.6)

Backing design: ADR-0159 D9 (job states, end states), D10.6 (events, card), D8.9 (`main_moved`), AC-7
(the job outlives the turn and the gateway). Pattern: `emit_session_selection` / `emit_session_effort`
in `src/personal_agent/transport/agui/transport.py`, and the attach snapshot in
`ws_endpoint._send_attach_replay`.

## Finding that shapes the design

The attach replay (`_send_attach_replay`) replays persisted events only from the in-flight turn start,
else from the session's last seq. A page that loads after a job ended therefore receives **no** old
`session_events`. Persisting the end event is not enough for AC "a client that connects after the end
receives it by replay". The attach path must also send a **snapshot built from the `dev_jobs` row**,
the same way it already sends `session_effort`. A client that reconnects with `last_seq` (no attach)
gets the persisted events by the existing replay.

## Design

1. **One event key, full-state payload.** `StateUpdateEvent(key="dev_job", value=<card payload>)`.
   Every payload is the complete card state (newest wins, idempotent), so a replayed or reordered
   event cannot corrupt the card. `payload["event"]` is `"state"`, `"step"` or `"end"`.
2. **Payload** (`src/personal_agent/dev_agent/job_card.py`, pure function `card_payload(row, ...)`):
   `event`, `job_id`, `mode`, `state`, `ended` (bool), `step` (str or null), `spend_usd` (decimal
   string, from `COALESCE(dev_jobs.spend_usd, dev_job_spend.committed_usd)`), `cap_usd` (decimal
   string), `failure_reason`, `check_result` (JSON object; the asyncpg pool returns JSONB as text, so `card_payload` decodes a `str` with `json.loads`), `repo_url`, `result_artifact_id`,
   `main_moved` (bool or null). Spend is a string so no float drift: the test compares it to the row.
3. **Sink.** `JobEventSink` Protocol in `job_card.py` with `async notify(job_id, event, step=None)` (named `notify`: a `.publish(` call trips the bus-identity guard).
   `PoolJobEventSink(pool, emit, cap_usd, github_org)` reads the row, builds the payload, calls
   `emit(session_id, payload)`. `emit` is `emit_dev_job` from the transport, passed in by
   `dev_agent/gateway.py`, so `dev_agent` does not import the transport. Step events are throttled to
   one per 2 s per job and skip an unchanged step. State and end events are never throttled.
4. **Supervisor.** `JobSupervisor(events=...)` (default: a no-op sink). It publishes `state` after the
   `RUNNING`, `CHECKING` and `DELIVERING` transitions, and `end` after `_end` commits the cleanup
   fence. Recovery uses `_end`, so `failed`/`supervisor_restart` publishes too. A publish failure is
   logged and never fails the job or the end.
5. **Step source.** `relay_adapter_factory` already receives the OpenCode adapter's `on_progress`
   string (`step N`, `tool X`). It calls `events.notify(job.id, "step", step)`.
6. **Transport.** `emit_dev_job(*, session_id, payload)` in `transport.py` calls `_push_event`.
7. **Attach snapshot.** `JobCardStore(db).latest(session_id)` (SQLAlchemy `text()`, same SQL columns)
   returns the payload of the session's newest job, or `None`. `_send_attach_replay` sends it as a
   `dev_job` STATE_DELTA frame (`seq: None`) **after the replayed events and before `REPLAY_COMPLETE`**,
   only when a job exists. The row is newer than any persisted event, so the snapshot must win: sent
   first, an older persisted `checking` event could replace a `delivering` row state (Codex review).
   Existing attach tests that expect `[effort, REPLAY_COMPLETE]` stay valid.
8. **PWA.** `DevJobCardData` type; `useAgentStream` handles `key === 'dev_job'` (shape-guarded, keyed
   by session like `serverEffort`; an `ended` card is not replaced by an older non-ended card of the
   same job); `DevJobCard.tsx` renders state, step, `spend / cap`, and at the end the `ArtifactCard`
   (success) or the failure reason, repo link and check result (failure). `main_moved === false`
   shows "main did not move to this result" (D8.9). `StreamingChat` renders it near the
   `classifiedError` slot.

Assumptions: the card for a session shows its newest job, including one that ended long ago (the
session is the job's record). The attach snapshot carries no `step`: the step is not stored, so a
reconnecting client sees the state until the next step event.

## Acceptance criteria and proof

| # | Criterion (ticket) | Test |
|---|---|---|
| 1a | A connected client receives the end state | `tests/personal_agent/transport/test_ws_integration.py::TestDevJobCard::test_a_connected_client_receives_the_end_state` |
| 1b | A client that connects after the end receives it | `...::TestDevJobCard::test_a_client_that_attaches_after_the_end_receives_the_snapshot` |
| 2 | After a seeded restart, `failed`/`supervisor_restart` reaches the session | `test_supervisor_integration.py::TestEvents::test_a_restart_end_state_reaches_the_session`: real DB, real `PoolJobEventSink` and `emit_dev_job`, `_push_event` captured; the captured envelope is a `STATE_DELTA` `dev_job` with `state=failed`, `failure_reason=supervisor_restart` |
| 2b | Normal, `failed_check` and delivery-failure ends publish | `TestEvents` cases through the same real chain; `check_result` arrives as an object |
| 2c | Production wiring passes the sink | `test_gateway_wiring.py`: `start_supervisor` builds the supervisor with a `PoolJobEventSink`, and the relay factory's progress callback publishes a step |
| 1c | The real snapshot query works | `test_job_card_integration.py`: `JobCardStore.latest` on `AsyncSessionLocal` (test stack) returns the ended row's payload, and `None` for a session with no job |
| 3 | The card shows the row's spend | backend: `test_job_card.py::test_spend_equals_the_row`; PWA: `DevJobCard.test.tsx` "shows the spend of the payload" |
| 4 | `cd seshat-pwa && npm run lint` exits 0 | the command |

Also: unit tests for the payload builder, the throttle, and a failing sink not failing the job.

## Steps

1. Tests first (all fail): `test_job_card.py`, `TestEvents`, `TestDevJobCard`, `test_dev_job_event.py`,
   `DevJobCard.test.tsx`, a `useAgentStream` test for `dev_job`.
   Run: `make test-file FILE=tests/personal_agent/dev_agent/test_job_card.py` (expect ImportError).
2. `dev_agent/job_card.py`: SQL, `card_payload`, `JobEventSink`, `NullJobEventSink`, `PoolJobEventSink`,
   `JobCardStore`.
3. `transport.py`: `emit_dev_job`. `ws_endpoint.py`: snapshot frame. `ws_harness.py`: `FakeJobCardStore`,
   `/__test/dev_job`.
4. `supervisor.py`: `events` argument, publish calls. `gateway.py`: build the sink (cap from
   `load_broker_config().job_cap_usd`, org from settings), pass it to the supervisor and the factory.
5. PWA: `types.ts`, `useAgentStream.ts`, `DevJobCard.tsx`, `StreamingChat.tsx`.
6. Gates: `make test`; DB tests `uv run python -m pytest -m integration tests/personal_agent/dev_agent -q`
   (test stack :5433 only, no LLM); `make mypy`; `make ruff-check`; `make ruff-format`;
   `cd seshat-pwa && npm run lint && npm run typecheck && npm test`; `pre-commit run --all-files`.
