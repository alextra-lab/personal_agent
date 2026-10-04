# FRE-1543 — the call history and the tools count survive a page reload

**Ticket:** FRE-1543 (Approved, Tier-1). Replaces the tools half of FRE-1538.
**Design intent:** ADR-0075 (WS transport, `session_events` replay), ADR-0076 (`turn_status`),
ADR-0092 §D3/§D9 (session lane), ADR-0123 T4 (the collapsed turn summary).
**Risk tier:** Complex — `src/` logic, transport protocol, production write path. Codex plan review required.
**Diff class:** escalated (production write path: the stored assistant message and `sessions.metadata`).

## Problem

iPadOS reloads the PWA page when the owner switches apps. The page holds the turn's state only in
memory:

- The folded call history (`phaseSummary`) is built in memory at DONE. The server never stores it.
- The page opens a WebSocket only inside `sendMessage`. A reloaded page has no socket, so it sees
  no live progress of a turn in flight and no `turn_status`.
- The FRE-1538 per-session localStorage misses the final reading when the page is suspended at the
  turn end.

## Design decisions

### D1 — Call history storage: a field on the stored assistant message

`turn_summary` is a new top-level key on the assistant message dict in `sessions.messages` (JSONB).
Why this and not a table keyed by trace:

- The REST `GET /sessions/{id}/messages` returns the raw message dicts, so the field reaches the
  client with no endpoint change.
- No schema change, no migration, no join. The record lives and dies with its message (retention
  prune clears both).
- A table would need its own retention, its own join in the messages endpoint, and a migration.

The server builds the record from the transport events the turn itself sent. A per-turn
recorder in a `ContextVar` (set by the turn's own task; sub-agent tasks copy the context)
collects every envelope that `_persist_and_enqueue` persisted. A seq range of `session_events`
is not used: two turns of one session can run at once, and phase events carry no `trace_id`, so
a range could blend them (codex review finding 1). The record holds the content the live panel
showed:

- phases: `phase_id`, `phase`, `detail`, `duration_ms` (= `ended_at − started_at`, server
  timestamps; a phase with no end uses the build time), `state` (`completed` / `error` from
  `PHASE_END.ok`; a phase with no end takes the terminal state), `parent_id`; first-start order.
- tools: tool names from `TOOL_CALL_START`, deduplicated, first-seen order.
- `terminal_state`: `cancelled` if a `CANCELLED` event is in the turn, else `error` if a
  `RUN_ERROR` is, else `completed`.
- A turn with no phases and no tools stores no record (the panel never renders for it — same rule
  as `attachTurnSummary`).

Stored shape (snake_case, Python side):
`{"phases": [{"phase_id", "phase", "detail", "duration_ms", "state", "parent_id"}], "tools": [...], "terminal_state": "..."}`.
The PWA maps it to `TurnSummary` with a defensive parser (a malformed record renders no panel,
never throws).

### D2 — Status snapshot source: `sessions.metadata.turn_status`

`emit_turn_status` (the projector is its sole caller) also writes the reading to
`sessions.metadata['turn_status']`. It survives a gateway restart (Postgres) and has no TTL (unlike
`session_events`, swept at 24 h). The write follows the existing key-level `jsonb_set` pattern in
`SessionRepository` (no clobber of other metadata keys).

Merge rule, a mirror of the PWA `mergeTurnStatus`: when the new reading has no ctx reading
(`context_max` not a positive number), the stored ctx fields (`context_tokens`,
`session_context_tokens`, `context_max`) carry over. The tools fields always come from the new
reading. The read-merge-write runs in one transaction with `SELECT … FOR UPDATE`.

Send reset (FRE-1538 rule, unchanged): at turn start the server sets the stored
`tool_iteration` / `tool_iteration_max` to null, so a snapshot taken during the new turn, before its
first reading, shows the tools lane reset — not the previous turn's count.

### D3 — Attach: the PWA opens the session's socket on load, and the server answers with a snapshot

New CONNECT field: `{"type": "CONNECT", "last_seq": 0, "attach": true}`. The PWA sends it once,
after REST hydration of the displayed session succeeded (page load, reload, session switch). A
normal CONNECT (send path, reconnects) is unchanged and gets no snapshot — a snapshot there would
bring back the previous turn's tools count after the send reset.

Server attach algorithm in `_sender`, in this order:

1. Discard the items already in the session's live queue. They are older than the REST hydration
   or covered by step 4 (every event is persisted before it is enqueued). This also drops a stale
   DONE sentinel that would otherwise close the new socket at once.
2. Send the snapshot: one `STATE_DELTA` `turn_status` with `seq: null` (not persisted), only when a
   stored reading exists (absent ≠ zero: no reading, no event).
3. Find the replay start: the in-flight turn's start seq if a turn of this session is running in
   this process, else the session's current `last_event_seq`.
4. Replay `session_events` after that start (the in-flight turn's phases, tool calls, turn_status
   readings, pauses).
5. Send `REPLAY_COMPLETE` with `last_seq` (the client's new watermark) and `turn_in_flight`
   (true when a turn is running and the replay held no DONE).
6. Live loop, with the `max_sent_seq` duplicate guard set from steps 3–4.

In-flight registry: a process-local dict `session_id → start seq` (refcounted for overlapping
turns). `_process_chat_stream_background` registers after it appends the user message, before any
event of the turn, and unregisters after `emit_done`. Process-local is correct: a gateway restart
ends every in-flight turn, so after a restart no turn is in flight.

Ordering proof for step 3 vs the registry: a turn that registers after step 3 emits only events
with seq above the value read in step 3, and those reach the live queue after step 1, so step 6
delivers them.

### D4 — PWA

- `connectWebSocket(..., { attach: true })`: on the attach connect it resets the stored watermark
  to 0 (a stale watermark from the dead page would drop the in-flight events it never rendered) and
  sends `attach: true`. Replayed events wait in the existing out-of-order buffer; `REPLAY_COMPLETE`
  flushes them (existing), then sets the watermark to `last_seq` and is forwarded to the hook.
  Attach stays pending until a `REPLAY_COMPLETE` arrives, so a socket lost mid-replay attaches again.
  Later reconnects of that connection are normal CONNECTs.
- `useAgentStream.attach(sessionId)`: opens the attach stream unless a stream for that session is
  already open (a send already in progress). `REPLAY_COMPLETE` with `turn_in_flight: true` sets
  `isStreaming`, so the live footer and the Stop button show.
- `StreamingChat`: after hydration seeds the messages, it calls `attach(sessionId)`. Hydration maps
  `turn_summary` → `phaseSummary` (also the REPLAY_GAP rehydrate in the hook).
- FRE-1538 localStorage: **kept**. It is not redundant: it paints the bar before the socket opens
  and when the socket cannot open. The snapshot is authoritative and overwrites it within one round
  trip, and the existing STATE_DELTA handler then persists the merged value.
- `CACHE_NAME` bump in `public/sw.js`.

#### D5 — Changes from the codex plan review

| Finding | Change |
|---------|--------|
| 1. A seq range can blend two concurrent turns | Per-turn `ContextVar` recorder (D1). |
| 2. Eviction: the old sender can take a queue item it can no longer send | A superseding connection cancels the old sender and waits for it to stop before its own replay. Test: `test_eviction_stops_the_old_sender_before_the_new_replay`. |
| 3. A turn that ends between REST hydration and attach is lost | On `REPLAY_COMPLETE` with no turn in flight, a history that ends with a user message is fetched once more. |
| 4. Replay inflates phase durations; a replayed DONE overwrites the stored summary | `PHASE_END` uses the server `ended_at`. DONE keeps a summary already on the message (this also fixes the DONE-after-CANCELLED relabel to "Completed"). |
| 5. A→B→A: a closed socket can still deliver a frame | `onmessage` drops frames once the connection is closed or superseded. |
| 6. `REPLAY_COMPLETE.last_seq` must be the highest seq sent | It is: `max(replay start, replayed seqs)`, computed from what was sent. |

## Known limits (accepted, stated in the PR)

- One socket per session: opening a session on device B evicts device A's socket (code 4001). A
  reconnects when it becomes visible again (existing visibility handler). Before this change, B
  evicted A only on a send.
- A turn that starts in the few milliseconds between a page's REST hydration and its attach
  replays without its user message in the list.
- The refetch fallback (D5, finding 3) runs once. If the async assistant-message append is still
  not done at that moment, the reply shows after the next reload.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 reproduced | `seshat-pwa/src/__tests__/StreamingChat.reload.test.tsx` — drive a turn with two tool rounds to DONE, unmount, clear localStorage, render a fresh page whose REST history is what the server stores. Red on current source: no `turn-summary`, `tools —/—`. |
| AC-2 history survives | Same test green: the panel header and phase rows equal the live turn's. Server: `tests/personal_agent/transport/test_turn_summary.py` (builder), `tests/personal_agent/events/test_request_completed_turn_summary.py` (the session writer stores `turn_summary`; the direct-append path too), REST returns the stored field. |
| AC-3 snapshot | WS harness test: emit `turn_status`, wipe the in-process state (queues, connections — the projector is not in the path), attach → first frame is `turn_status` with tools and ctx. Postgres (:5433) test of `TurnStatusStore` (merge carry-over, tools reset, reload on a separate connection). PWA test: fresh page, empty localStorage, attach snapshot → bar shows `tools 2/25` and ctx. |
| AC-4 turn in flight | WS harness: register a turn, emit one completed round, attach → replay holds that round, `REPLAY_COMPLETE.turn_in_flight` true; a later live round arrives. PWA: fresh page attach, replayed round then a live round → both phases show, Stop visible. |
| AC-5 unchanged rules | Existing FRE-1538 / FRE-1401 / FRE-1414 / FRE-935 suites green; plus: an attach on session A never paints on session B. |
| AC-6 schema | N/A — no schema change (message JSON field + metadata JSONB key). |
| AC-7 live | Post-deploy runbook in the handoff. |

## Steps (TDD: failing test first in each)

1. `src/personal_agent/transport/turn_summary.py` — `TurnSummaryPhase`, `TurnSummaryRecord`
   (frozen pydantic), `build_turn_summary(events, now)`, the per-turn recorder. Test: `make test-file FILE=tests/personal_agent/transport/test_turn_summary.py`.
2. `src/personal_agent/transport/agui/turn_state.py` — `merge_turn_status`, `TurnStatusStore`
   (`save`, `load`, `clear_tools`), in-flight registry. `SessionEventBuffer.latest_seq`.
   Tests: `tests/personal_agent/transport/test_turn_state.py` (pure + :5433).
3. `transport.py` — `emit_turn_status` stores (best-effort); `open_turn`, `close_turn`;
   `_persist_and_enqueue` records each persisted envelope for the running turn.
4. `ws_endpoint.py` — CONNECT `attach`; `_sender` attach branch. Harness fakes gain
   `latest_seq` and a fake store. Tests in `tests/personal_agent/transport/test_ws_integration.py`.
5. `events/models.py` `RequestCompletedEvent.turn_summary`; `request_completed_handlers.py` and
   `app.py` direct append store it; `app.py` starts the recorder and calls `open_turn` /
   `close_turn`.
6. PWA: `types.ts`, `phase-summary.ts` (`parseTurnSummary`), `agui-client.ts` (attach),
   `useAgentStream.ts` (`attach`, REPLAY_COMPLETE, REPLAY_GAP mapping), `StreamingChat.tsx`,
   `public/sw.js`. Tests: `cd seshat-pwa && npx vitest run`, then the Playwright suite.
7. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`, `cd seshat-pwa && npm run lint`.
