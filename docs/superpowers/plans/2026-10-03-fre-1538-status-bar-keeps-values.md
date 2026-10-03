# FRE-1538 — PWA status bar keeps its values (plan)

Ticket: FRE-1538. Backing design: ADR-0092 §D9 (two-lane bar), ADR-0076. Related: FRE-573, FRE-575,
FRE-1401, FRE-1414, FRE-928, FRE-935.

## Owner decisions (2026-10-03, on the ticket)

1. Tools lane: keep the last turn's count until the next send. Reset at the send.
2. ctx lane: never reset at a send, reconnect, remount or reload. A new reading replaces it
   (a compaction reading is a new reading). A new session shows —/—.
3. Both lanes survive a page reload. The ctx reading survives a gateway restart.
4. Cross-session rule (FRE-1401, FRE-1414) and absent ≠ zero (FRE-928, FRE-935) stay.

## Storage choice: per-session localStorage (not a server snapshot)

- The bar shows the **live** `session_context_tokens` from `turn_status`. The REST session
  endpoint offers only an **estimate** (`estimate_messages_tokens`). An estimate beside a real
  ceiling is the exact misreading FRE-1401 removed. Only the stored live reading shows the
  same number the owner saw.
- The projector's session lane is process-local (FRE-1401). A server snapshot needs a new
  durable store plus an ADR-0092 §D3 change. That is a separate backend ticket.
- localStorage is keyed by session id. It gives cross-session isolation by construction. It
  survives a reload and a gateway restart. FRE-575 already uses it for tools.
- Known limit: a reading is per device. A turn that ends while the page is reloaded shows the
  last reading received before the reload, because the page opens a WebSocket only on send.
  This limit goes in the PR body and the handoff comment.

## Design

New module `seshat-pwa/src/lib/turn-status-store.ts` (single owner of the storage key, the
cold status, and the merge rule):

- `COLD_TURN_STATUS` — moved from `StreamingChat.tsx`, unchanged values.
- `hasCtxReading(s)` — true only when `context_max` is a finite number > 0 and
  `session_context_tokens` is a finite number. A `null` ceiling is **not** a reading.
- `mergeTurnStatus(prev, next)` — returns `next`. If `next` has no ctx reading and `prev` has
  one, the three ctx fields (`context_tokens`, `session_context_tokens`, `context_max`) are
  carried over from `prev`. Tools fields always come from `next`.
- `persistTurnStatus(sid, status)` — the stored record is a pure function of the **displayed**
  status, so storage and screen cannot diverge. `ctx` is stored when `hasCtxReading(status)`.
  `tools` is stored when both tool fields are finite numbers. A lane with no reading is left
  out of the record. A status with no reading at all removes the key. Never store a null.
  Swallow storage errors. The caller passes the **merged** status, so the ctx carry rule has
  already run.
- `loadTurnStatus(sid)` — `COLD_TURN_STATUS` plus the stored `ctx` and `tools`. Each lane loads
  as an **atomic group**: ctx needs all three fields finite and `context_max` > 0, and tools
  needs both fields finite. A half-valid group is dropped whole. This stops a valid ceiling from
  pairing with the cold numerator 0 and showing `0/max`.
- `clearStoredTools(sid)` — remove `tools` from the stored record.

Key: `seshat-turn-status-<sid>`. The old key `seshat-tool-state-<sid>` is no longer read or
written. A stale old entry is harmless. It costs one lost restore on first view after deploy.

`useAgentStream.ts`:
- `lastTurnStatusRef` is the source of truth for the **displayed** status. One helper,
  `commitTurnStatus(next)`, sets the ref and calls `setTurnStatus(next)` synchronously. The
  `turn_status` handler, `seedTurnStatus` and `sendMessage` all go through it. No code mutates
  the ref inside a React state updater (unsafe under replay or Strict Mode).
- `seedTurnStatus` keeps its public signature. For an updater argument it evaluates
  `fn(lastTurnStatusRef.current)` itself, then commits. A late REST hydration therefore merges
  onto the latest live status, never onto a stale snapshot.
- `turn_status` handler: `merged = mergeTurnStatus(lastTurnStatusRef.current, next)`;
  `commitTurnStatus(merged)`; `persistTurnStatus(ownerSessionId, merged)`. It uses the FRE-1414
  bound `ownerSessionId`, never `currentSessionRef`.
- `sendMessage`: replace `lastTurnStatusRef.current = null` with a tools-lane reset (tool
  fields → null on the displayed status; `clearStoredTools(sessionId)`). ctx and cost stay.
- `DONE`: delete the FRE-575 localStorage block. Persistence now happens per `turn_status`.

`StreamingChat.tsx`:
- The layout effect seeds `loadTurnStatus(sessionId)` instead of the cold constant. The bar is
  correct before the first paint, on a remount and on a switch.
- The `getSession` handler stops parsing localStorage. It still merges cost.
- Rewrite the FRE-1401 comment to cite the owner decisions of 2026-10-03.

## Tests (TDD, vitest) — files and commands

Command: `cd seshat-pwa && npx vitest run <file>`.

1. `src/__tests__/turn-status-store.test.ts` — merge, persist, load, absent ≠ zero, garbage,
   half-corrupt record (valid ceiling, missing numerator → cold, never `0/max`).
2. `src/__tests__/StreamingChat.statusBar.test.tsx` — renders `StreamingChat` with mocked
   `@/lib/agui-client`, `next/navigation`, `useSessionConfig`, and a stub `ChatInput` that
   exposes a send button. Reads the real `TurnStatusBar` text.
   - AC-1/AC-2: session with 3 tool rounds, DONE, unmount, mount again → `tools 3/6`, ctx
     reading shown. Must fail on current source with `—/—`.
   - AC-2: send → `tools —/—`; ctx unchanged; first live reading of the new turn replaces tools.
   - AC-3: a `turn_status` with a `null` ceiling after a send keeps the ctx reading; a lower
     reading (compaction) replaces it; a fresh session id shows `—/—`.
   - AC-4: write through one mount, clear nothing, unmount, mount a fresh tree → both lanes back.
   - Reconnect: fire `onWsDisconnected` then `onWsConnected` on the live socket → bar unchanged.
   - Delayed hydration: `getSession` resolves **after** a live reading → the live ctx and tools
     survive; only cost comes from REST.
   - Finite → null tools inside one turn → display and stored record both show no reading.
   - AC-5: A has values, B has none → B shows `—/—`; back to A shows A's values; a late event
     for A (FRE-1414 gate) does not change B.
3. Existing `useAgentStream*.test.tsx` and `TurnStatusBar.test.tsx` stay green. Any test that
   asserts the old reset or the old key is changed only where these decisions replace it, and
   the PR names it.

Gates: `cd seshat-pwa && npm run lint && npx vitest run`, then the root gates from the build
skill (`make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run
--all-files`).

## Risk tier

Standard: PWA `src/` logic, no schema, security, cost or memory. Codex plan review required.
Diff class: self-serve (no production write path, no deletion, no schema, no cost code).

## Codex plan review (1 round, 2026-10-03) — disposition

- Same-session concurrent turns (a straggler from turn 1 after a send on turn 2): **not fixed**.
  The code already defers it (`useAgentStream.ts:300`). A second send closes the first socket.
  A connection-generation token is a separate change. Recorded as a known limit.
- Null tools erasing a known reading, ref/state race, half-valid stored groups: **fixed in this
  plan** (storage mirrors the display, one commit helper, atomic groups).
- Test gaps: reconnect, delayed hydration, half-corrupt record, finite → null: **added**.
  Two callbacks for one session: not added (same deferred limit). Gateway restart is covered
  only by localStorage surviving it — the store never touches the gateway.

## Out of scope

- A server snapshot of the session lane (separate backend work).
- Persisting `compaction_count` and `cache_reset_count`. They are not headroom readings.
- Opening a WebSocket on mount (the mid-turn reload limit above).
