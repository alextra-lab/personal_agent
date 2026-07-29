# FRE-1040 — Response never renders live: per-session event sequence + bounded client flush

**Ticket:** FRE-1040 (Urgent, `stream:build1`, Tier-1:Opus) · **Backing ADRs:** ADR-0075 (websocket
transport), ADR-0123 §6 (full-state replacement / self-correction argument)
**Related:** FRE-590 (removed the DONE flush for `ackSeq>0`), FRE-518 (out-of-order guard),
FRE-542 (gap-aware dedup), FRE-1034 (made the loop concurrent — the trigger), FRE-986 (phase surface)

---

## 1. Root cause — verified against source, not inherited

Two facts, individually reasonable, are jointly incompatible:

| Side | Fact | Source |
|------|------|--------|
| Server | `session_events.seq` is drawn from **one global** Postgres sequence shared by every session | `docker/postgres/init.sql:405-415`, `docker/postgres/migrations/0005_websocket_session_events.sql` |
| Client | The receive path dispatches only a **contiguous** run starting at `ackSeq+1`; anything else stays in `pendingBuf` forever | `seshat-pwa/src/lib/agui-client.ts:429-464` |

When two conversations are alive, session B consumes seq values in the middle of session A's series.
A's client then waits at `ackSeq+1` for a number that belongs to B and will never arrive on A's
socket. The `TEXT_DELTA` carrying the response sits in `pendingBuf` permanently.

Why the observed pattern matches exactly:
- **New conversation, first turn** renders — `ackSeq===0` hits the cold-start DONE flush
  (`agui-client.ts:448`).
- **Second turn onward / any existing conversation** never renders — `ackSeq>0`, so FRE-590's
  tightened guard deliberately declines to advance past the hole, expecting reconnect replay to
  fill it. Replay cannot: `SELECT … WHERE session_id=:sid AND seq > :last_seq`
  (`event_buffer.py:88-96`) correctly returns only A's events, so the hole is never filled and the
  client re-stalls at the same point after every reconnect.

FRE-1034 (2026-07-28) freed the event loop and let sessions interleave, turning an occasional hole
into a per-turn one. It is the trigger, not the defect — do not revert it.

## 2. Fix — two parts, both required

### A. Server: per-session monotonic sequence (removes the cause)

A global sequence cannot satisfy a per-session contiguity check. Make the numbers mean what the
client already assumes.

The counter must be **durable and independent of the rows**, not `MAX(seq)`: `session_events` has a
24h TTL sweep (`cleanup_expired`), so a `MAX`-derived counter would reset to 0 once a session's rows
age out, re-issuing seqs at or below a client's stored `ackSeq` — a permanent blackout.

1. **Migration `0023_session_events_per_session_seq.sql`** (+ mirror in `init.sql`):
   - `sessions.last_event_seq INTEGER NOT NULL DEFAULT 0`.
   - Backfill, **guarded on the column not already existing** so a re-run cannot bump live counters
     and manufacture a hole: every existing session starts at the current global high-water mark
     (`SELECT last_value FROM session_events_seq`). This is ≥ every seq ever issued, hence ≥ every
     client's stored `ackSeq`, so no new per-session seq can be mistaken for a duplicate.
   - **Keep** the legacy `DEFAULT nextval('session_events_seq')` on `session_events.seq` so an image
     rollback still writes. New code always supplies `seq` explicitly, so the default is dead code
     on the happy path.
2. **`SessionEventBuffer.append`** allocates the seq explicitly, in one transaction:
   `UPDATE sessions SET last_event_seq = last_event_seq + 1 WHERE session_id = :sid RETURNING
   last_event_seq`, then `INSERT … (session_id, seq, …)`. The `UPDATE` takes a row lock, so
   concurrent appends for the same session serialise; different sessions never contend.
   No row returned ⇒ unknown session ⇒ raise (the FK already required it).
3. **`SessionModel.last_event_seq`** column added to the ORM model.

### B. Client: replay-first hole recovery (makes a genuine hole self-correcting)

Per-session numbering removes *foreign* holes but not *genuine* ones — `transport.queue_full` drops
an already-sequenced envelope from the live queue (`transport.py:135-141`), and the sender's
`max_sent_seq` guard can skip one. Today either stalls the client forever. ADR-0123 §6's argument
applies: reconstruction with no self-correction is the wrong shape.

**Rejected: a plain "flush the buffer after N ms" timer.** Codex plan-review (2026-07-29) verified
from history that this reinstates precisely the regression FRE-590 removed — advancing `ackSeq` past
a hole is what makes reconnect replay unable to recover the missing event — and that the reconnect
backoff (1000 ms, doubling to 30 000 ms, `agui-client.ts:497`) can already exceed any fixed timeout,
so the "reconnect always wins the race" argument does not hold. The timer must trigger **recovery**,
never data loss.

1. **Server** (`ws_endpoint._sender`): after the replay loop, when replay actually ran
   (`last_seq > 0`), send `{"type": "REPLAY_COMPLETE", "seq": null}` — the authoritative "that is
   everything I hold above your watermark" marker. Safe for older clients: the PWA hook's event
   switch (`useSSEStream.ts:223`) has no `default` branch, so an unrecognised type is a no-op.
2. **Client** (`agui-client.ts`):
   - Stall detected (the contiguous drain left `pendingBuf` non-empty) → arm a **3000 ms** timer.
   - Timer fires, **first time for this hole** → force a reconnect (reset backoff, detach the old
     socket's handlers, reconnect immediately). `connect()` already clears `pendingBuf` and the
     server replays from `ackSeq`, which is *untouched* — so a fillable hole is filled and FRE-590's
     guarantee holds exactly. Skipped (timer simply re-arms) when a connect is already in flight or
     the socket is not OPEN — recovery is already under way, and bumping `connectGeneration` under
     an in-flight `connect()` would strand the `connecting` flag.
   - **`REPLAY_COMPLETE` arrives and the buffer is still stalled** → the server has proven it cannot
     fill the hole → flush in seq order, advancing `ackSeq`. Deterministic, not wall-clock.
   - **Rollout-window fallback** (new PWA against an old gateway that sends no `REPLAY_COMPLETE`):
     the timer firing a *second* time for the same hole flushes instead of reconnecting again. One
     `recoveryAttemptedForSeq: number | null` slot suffices because `ackSeq` only moves forward.
     This also makes a reconnect loop structurally impossible.
   - Worst case to render: ~3 s when replay heals it; ~3 s + reconnect when the hole is genuinely
     unfillable. Never unbounded.
- Keep the `ackSeq===0` cold-start DONE flush: a client with cleared localStorage on an existing
  session still starts at 0 while that session's seqs are high.
- Bump `CACHE_NAME` `seshat-v38-phase-state` → `seshat-v39-per-session-seq` (client JS changed).

### C. Codex review findings and their resolution (round 2, 2026-07-29)

| Finding | Verdict | Resolution |
|---|---|---|
| **BLOCKER** — `REPLAY_COMPLETE` may declare a hole unfillable while a missing event's "fire-and-forget" Postgres write is still pending (`ws_endpoint.py:720-723`) | **Not reachable** — the cited comment is stale. `_persist_and_enqueue` **awaits** `buf.append` (which commits) *before* enqueuing, all inside the per-session emit lock (`transport.py:115-141`), so seq order == commit order == enqueue order. Any event the client has already *seen above* the hole was enqueued after the hole's row committed. Events committed after the replay query carry **higher** seqs and cannot fill a lower hole. | No change, but the stale comment is corrected in the same edit so the next reader is not misled. The single-transaction counter (below) additionally removes the one case that *could* have burned a seq without a row: a failed insert used to consume a `nextval` and leave a permanent hole; now it rolls the counter back with it. |
| **MAJOR** — `REPLAY_GAP` is off by one: `last_seq < oldest` reports a gap when `oldest == last_seq + 1`, i.e. when nothing is missing (`ws_endpoint.py:729`) | **Real, pre-existing** | Folded in — the correct predicate is `last_seq + 1 < oldest`. A spurious `REPLAY_GAP` triggers a full history rehydrate in the client (`useSSEStream.ts:460`), which is exactly the "only a reload recovers it" behaviour this ticket is about. Same code path, one-line fix, covered by a test. |
| **MAJOR** — a stale stall timer can fire after the buffer has already drained | **Real** | Explicit lifecycle rule added: after every drain attempt, `pendingBuf.size > 0 ? arm : clear`; the timer callback re-checks the buffer is still non-empty before acting; cleared on reconnect and on `close()`. |
| Q2 — the insert must share the counter's transaction; deadlock risk unverifiable from its sources | **Addressed** | `append` runs `UPDATE … RETURNING` + `INSERT` + one `commit` in a single transaction. No lock cycle exists: `append` takes the `sessions` row lock then a *fresh* `session_events` row nobody else contends for, and no other writer of the `sessions` row touches `session_events` in the same transaction. |

### D. Pre-PR self-review findings and their resolution

Four parallel passes: correctness bug-hunt, git-history/comment-invariant, project-standards, security.

| Finding | Severity | Resolution |
|---|---|---|
| **Cold-start regression, self-inflicted.** At `ackSeq===0` the "hole" is seq 1, which never arrives — an existing session's numbering continues from wherever it left off. The stall timer would force a reconnect, and `connect()` clears `pendingBuf` *while the server gates replay on `last_seq > 0`* — so a CONNECT carrying 0 replays nothing, sends no `REPLAY_COMPLETE`, and the response already received is destroyed. This would have **broken the one path that works today** (first turn of a new conversation, which currently renders via the DONE cold-start flush) for any response taking >3 s. | **Blocker** | Fixed: `onStall` flushes instead of reconnecting when `ackSeq===0`. Safe because with no watermark "everything above 0" is everything held — the DONE fallback already does exactly this, only later. Regression test added; it failed before the fix and passes after. |
| **TypeScript union mismatch.** `REPLAY_COMPLETE` was absent from the closed `AGUIEventType` union, so `parsed.type === 'REPLAY_COMPLETE'` was a `TS2367` error. Neither eslint (not type-aware) nor vitest (does not typecheck) catches it, and CI's `pwa-unit` job ran only vitest — so it would have surfaced as a **`next build` failure at deploy time**. | **Blocker** | Fixed: added to the union. Folded in the missing guard — a `typecheck` npm script plus a Typecheck step in `pwa-unit`, the counterpart of the backend's `mypy` gate. tsc is clean across the PWA. |
| **Stdlib `ValueError`** where CLAUDE.md requires `personal_agent.exceptions`. | Standards | Fixed: new `UnknownSessionError`; the integration test asserts the specific type. |
| `high_water` read as `BIGINT` into an `INTEGER` column could overflow the backfill. | Minor, declined | Unreachable: `session_events.seq` is itself `INTEGER`, so the global sequence would have failed on insert long before the counter could exceed the range. If it somehow did, the migration aborts loudly inside its transaction — the correct outcome. Noted rather than coded around. |
| `REPLAY_GAP` branch does not `return`, so replay (and now `REPLAY_COMPLETE`) also runs. | Pre-existing, correct as-is | Verified deliberate and now *better*: the client REST-rehydrates on `REPLAY_GAP`, and `REPLAY_COMPLETE` then flushes events that previously stalled forever. Left alone. |

Security pass: clean on all five traced boundaries (parameter binding, cross-session exposure, client-driven DoS via the forced reconnect, the `sessions` row lock, logging/migration data safety). History pass: no regression against FRE-518, FRE-542, FRE-590 or ADR-0075, and no comment left stale.

## 3. Acceptance criteria and how each is proven

| # | Criterion (from the ticket's PROOF REQUIRED, as revised by master's root-cause comment) | Proof |
|---|---|---|
| AC-1 | Two conversations alive at once, alternating turns, each renders its response live with no manual switch | Integration test on real PG: interleaved appends across two sessions yield two **independently contiguous** series (the property that makes the client's drain succeed) — plus live verification by the owner post-deploy |
| AC-2 | A session whose seq series contains a hole left by another session still renders the response | PWA vitest: hole-recovery test — the stall forces a reconnect, and on `REPLAY_COMPLETE` with the hole still open the buffered `TEXT_DELTA` is dispatched and `ackSeq` advances |
| AC-3 | Self-correcting: a `TEXT_DELTA` withheld from the live socket and delivered only on reconnect replay still renders | PWA vitest: the stall timer reconnects **without** advancing `ackSeq`; the replayed `TEXT_DELTA` dispatches normally and no flush occurs |
| AC-4 | No regression of FRE-518 (out-of-order reordering), FRE-542 (dedup) or FRE-590 (`ackSeq` never advances past a recoverable hole) | Existing `agui-client.gap-dedup.test.ts` passes unchanged; AC-3's test is the FRE-590 guard |
| AC-5 | Existing clients are not blacked out by the migration | Migration backfills to the global high-water mark; integration test asserts a pre-existing session's first new seq exceeds it |

## 4. Steps

1. `tests/integration/test_session_event_seq.py` (marked `integration`, real PG :5433) — failing first.
2. `tests/personal_agent/transport/test_event_buffer_seq.py` (unit, mocked session) — failing first.
3. `seshat-pwa/src/__tests__/agui-client.hole-recovery.test.ts` — failing first.
4. Migration `0023_…` + `init.sql` mirror + `SessionModel.last_event_seq`.
5. `SessionEventBuffer.append` rewrite + docstring/module-docstring correction.
6. `ws_endpoint._sender` emits `REPLAY_COMPLETE` after replay.
7. `agui-client.ts` replay-first hole recovery + `sw.js` CACHE_NAME bump.
8. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run
   --all-files`, `cd seshat-pwa && npm run lint && npm test`.
9. Apply the migration against the test PG to prove it, then run the integration test.

## 5. Deliberately NOT in this PR

- **Raising `session_events` retention beyond 24 h** (master's added follow-up). Genuinely separate:
  a storage-policy call with a 7× row-count implication, not needed to meet this objective. Filed as
  a new Needs-Approval ticket.
- Reverting FRE-1034 — explicitly ruled out by the ticket.
