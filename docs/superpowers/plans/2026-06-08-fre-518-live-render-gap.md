# FRE-518 — Live-render gap: out-of-order live enqueue orphans the final response

- **Ticket:** [FRE-518](https://linear.app/frenchforest/issue/FRE-518) (Approved, High bug, Tier-1:Opus, Observability Foundation)
- **Refs:** ADR-0075 (WS transport + `session_events` replay), ADR-0076 (`turn_status` STATE_DELTA), ADR-0088 D4 (projector, FRE-513), incident trace `f136738b` / session `cf3b40cc`
- **Related:** FRE-513 (PR #178) — the change that introduced the concurrent emitter

## Root cause (high-confidence; proven by the regression test)

The symptom is a **live-UX loss, not data loss**: a cloud turn completed, the reply was
persisted (route_traces + `session_events` + `messages`), but it never rendered live —
only a navigate-away/back (REST message re-hydration) surfaced it.

The defect is an **out-of-order live-enqueue race that poisons the monotonic seq-dedup**,
orphaning the lower-seq event from *both* the live path and reconnect-replay:

1. `_push_event` (`transport/agui/transport.py`) persists every event to `session_events`
   — Postgres assigns a monotonic `seq` via `await buf.append(...)` (DB I/O) — then
   synchronously `queue.put_nowait(envelope)` with `envelope["seq"]=seq`.
2. Post-FRE-513 the **projector** (`cg:turn-projector`, a Redis-stream consumer **task**)
   emits `turn_status` via `emit_turn_status → _push_event`, **concurrently** with the
   **main chat coroutine** pushing the final response TEXT_DELTA (`service/app.py:317`).
3. Two concurrent `_push_event` calls run on **separate DB connections**. The `seq` is
   assigned at INSERT execution, but the `await buf.append` resume order can be the
   **opposite** of the seq-assignment order → the **higher-seq** `turn_status` can
   `put_nowait` **before** the **lower-seq** response delta.
4. With the higher-seq event enqueued first:
   - The **sender** sends `turn_status`, advances `max_sent_seq`, then sees the response
     delta `seq <= max_sent_seq` → **skips it** (`ws_endpoint.py:667`).
   - The **client** sets `lastSeq` to the higher value; the response delta is then
     `<= lastSeq` → **dropped** (`agui-client.ts:278`), and on reconnect it replays
     `seq > lastSeq` → the lower-seq response delta is **never replayed**.
5. The reply is durable but unreachable over WS. Only full REST re-hydration shows it.
   The disconnect-at-completion is what makes the orphaning **permanent**: there is no
   later in-order live event to self-correct the skip.

This matches the master's forensic conclusion: the **latent transport edge** (seq-monotonic
dedup that assumes *enqueue order == seq order*) is **pre-existing in ADR-0075**; FRE-513's
projector is the **second concurrent emitter** that triggers it. The fix belongs in the
transport, **not** in reverting FRE-513.

> Inference note: the exact live interleaving is inferred from the code (high confidence),
> not from per-event seq logs of the incident. The regression test (step 1) is the proof —
> it reproduces the orphaning pre-fix and shows the fix closes it.

## Fix

Restore the invariant **enqueue order == seq order** by serializing the
`persist → set seq → enqueue` critical section **per session** with an `asyncio.Lock`.
Lock-acquisition order then equals seq-assignment order equals enqueue order, so:
- no seq-bearing live event is ever enqueued out of order;
- the sender never skips a not-yet-sent lower-seq event;
- the client never advances `lastSeq` past an event it has not received;
- reconnect-replay (`seq > lastSeq`) always recovers anything a disconnect missed.

Backend-only, single surface (gateway). No PWA change required. No persistence change (AC4).

**The serialization must cover the terminal DONE path too** (codex review finding): the DONE
row is persisted via `buf.append(...)` **directly** (not `_push_event`) at
`service/app.py:368-382` and `gateway/chat_api.py:204-220`, and the `None` sentinel is
enqueued there. If those stay outside the lock, the DONE seq and sentinel can interleave
with a concurrent emit. Fix: a serialized `emit_done(session_id)` helper in `transport.py`
that, **under the same per-session lock**, persists the DONE row (seq after all prior locked
emits) then enqueues the `None` sentinel; replace the two inline blocks with a call to it.

### Why a lock and not reordering / a client change

- Reordering at the sender can't fix the **client-side** `lastSeq` advance, and the client
  is a separately-deployed surface (PWA deploys are serialized per MASTER_PLAN). The
  server-side ordering fix is provably correct from one surface (codex agreed: server-side
  seq-ordered emission is the most surgical correct option).
- The lock is held across one INSERT+commit for a single session; turn emits for one
  session are low-frequency and already logically sequential (a 50-delta stream already
  awaits `_push_event` per call). Negligible latency cost; non-nested, single-path
  acquisition → no deadlock.

### Residual, accepted (documented, not load-bearing)

A projector `turn_status` can still be emitted **after** `emit_done` enqueues the sentinel
(the projector consumes `turn.completed` on its own task). The sender exits on the sentinel,
so that trailing `turn_status` is not sent **live** — but it is persisted (`session_events`)
and **replay-recovered** on reconnect, and it is a status-bar metric, **not** the final
render. The load-bearing final response TEXT_DELTA is pushed at `service/app.py:317`
**before** `emit_done`, in the same coroutine, so with the lock it is always enqueued in
seq-order ahead of the sentinel and can never be orphaned. We do **not** change the sender's
sentinel semantics (out of scope; would be a behavioral change for a non-critical event).

### Lock-registry teardown (codex finding)

The per-session lock registry is bounded by insertion-order eviction (cap
`_MAX_EMIT_LOCKS = 4096`), mirroring the projector's `_MAX_TRACKED_TRACES` pattern. The cap
is far beyond any realistic concurrent-session working set, so a lock that is actually held
is never evicted in practice; eviction only reclaims long-idle entries. (`_session_queues`
in `ws_endpoint.py` is an existing unbounded per-session registry with no teardown — this
is strictly better.)

## Steps (TDD)

**Step 1 — Failing regression test (CI teeth, AC1+AC3).**
File: `tests/personal_agent/transport/agui/test_transport_ordering.py` (new).
- Drive **two concurrent `_push_event` calls** for one session through a fake
  `SessionEventBuffer.append` that assigns an increasing `seq` at call-entry then awaits
  an asymmetric delay (first call slow, second fast) to force the seq inversion on resume.
- Assert the drained queue envelopes are in **strictly ascending `seq`** order.
- Pre-fix: queue is `[2, 1]` → fails. Verify it fails first.
- Exact run: `cd /opt/seshat/.claude/worktrees/build && uv run pytest tests/personal_agent/transport/agui/test_transport_ordering.py -x -q`
  → expected pre-fix: `1 failed` on the ascending-order assertion.

**Step 2 — Implement the per-session emit lock + serialized `emit_done`.**
File: `src/personal_agent/transport/agui/transport.py`.
- Add a bounded per-session lock registry: `_session_emit_locks: dict[str, asyncio.Lock]`,
  cap `_MAX_EMIT_LOCKS = 4096`, + `_get_emit_lock(session_id) -> asyncio.Lock` that creates
  on first use and evicts the oldest insertion-order entry past the cap (mirrors the
  projector's `_MAX_TRACKED_TRACES`).
- In `_push_event`, wrap the critical section
  `seq = await buf.append(...)` → `envelope["seq"] = seq` → `queue.put_nowait(envelope)`
  in `async with _get_emit_lock(session_id):`. Keep the persist-failure `except` returning
  early (inside the lock is fine — it releases on exit).
- Add `async def emit_done(session_id)` that, under the same lock, persists the DONE row
  (`buf.append(... "DONE" ...)`) then `queue.put_nowait(None)` — the serialized terminal
  helper.

**Step 3 — Route both terminal sites through `emit_done`.**
- `src/personal_agent/service/app.py:368-382`: replace the inline DONE `buf.append` +
  `queue.put_nowait(None)` block with `await emit_done(session_id)` (keep the surrounding
  `finally` + dedup release).
- `src/personal_agent/gateway/chat_api.py:204-220`: same replacement in its `finally`.
- Add a focused test that `emit_done` enqueues the sentinel **after** a concurrently-emitted
  event's seq (DONE row seq is the highest): `tests/personal_agent/transport/agui/test_transport_ordering.py`.
- Re-run step-1 test → expected: `1 passed`.

**Step 4 — Module + full suite green.**
- `cd /opt/seshat/.claude/worktrees/build && uv run pytest tests/personal_agent/transport/ -q`
  → expected: all pass (existing sender/replay tests unaffected — ordering is now stricter,
  never looser).
- Then the gates below.

## Quality gates (all green before PR)
```
cd /opt/seshat/.claude/worktrees/build
make test-file FILE=tests/personal_agent/transport/agui/test_transport_ordering.py
uv run pytest tests/personal_agent/transport/ -q
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

## Acceptance criteria mapping
- **AC1 (reproduce):** step-1 test reproduces the orphaning (fails pre-fix).
- **AC2 (fix):** lock guarantees `lastSeq` never outruns an unreceived event, so
  reconnect/return re-delivers the final response via replay — and live delivery now
  survives a disconnect-at-completion (response is sent in-order before any later event,
  or replayed). No manual navigate-away/back needed.
- **AC3 (CI teeth):** step-1 ordering invariant test is the regression guard.
- **AC4 (no persistence change):** untouched — only the in-process enqueue ordering changes.

## Out of scope / follow-ups (file as Needs Approval, Observability Foundation)
- **PWA gap-aware client dedup:** the client advances `lastSeq` to every higher seq it sees
  and drops `seq <= lastSeq` (`agui-client.ts:277-280`); a gap-buffering client would be
  correct even against a misbehaving server. Defense-in-depth, separate surface (PWA).
  File if owner wants belt-and-suspenders.

## Halt conditions respected
- One phase = one PR. Backend-only. No persistence/row changes. No deploy (master's role).
