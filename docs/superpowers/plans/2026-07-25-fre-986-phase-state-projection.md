# FRE-986 — Server-side current-phase projection (ADR-0123 §6)

**Ticket:** FRE-986 (Approved, Tier-1:Opus, stream:build2) · **ADR:** ADR-0123 §6, AC-3
**Backing chain:** FRE-934 (deltas, shipped) → **FRE-986 (this)** → FRE-937 (AC-7 seam)
**Architecture decision (owner-approved 2026-07-25):** Option A — lightweight in-process registry
+ full-state `phase_state` snapshot on the existing direct-emit path. NOT the Redis-bus projector
(that would couple phase state to Redis / go dark under `NoOpBus` — a reliability regression vs.
today's Redis-independent phase deltas).

## The gap

ADR-0123 §6: the surface must be *a projection of current phase state, not an accumulation of the
event log* — mirroring `turn_status`, a **full-state replacement keyed by session** so a reconnecting
client converges from the newest message alone and **self-corrects** if a `PhaseEnd` is dropped.

Today (FRE-934/936): `PhaseStart`/`PhaseEnd` are **deltas**; reconnect replays the delta log from
`session_events` (`seq > last_seq`) and re-applies each. No component holds current phase state as a
replaceable snapshot. A dropped/reordered `PhaseEnd` leaves a completed phase stuck rendering as
"running" — the exact symptom §6 rules out. Passing AC-3's numeric test today rests on 24h retention,
not on the specified mechanism.

## The fix (Option A)

Emit a session-keyed `phase_state` **full-state snapshot** (a `StateUpdateEvent(key="phase_state")`,
reusing the entire existing STATE_DELTA path — adapter, transport, persistence, client switch) on
every phase transition, carrying the complete set of currently-active phases with their **verbatim
server start timestamps**. The client consumes it as authoritative (like `turn_status`): newest wins,
missing-from-snapshot ⇒ resolve. Deltas stay on the wire unchanged (AC-2 gap semantics + AC-7 summary).

### Race-freedom invariant (load-bearing)

The snapshot's registry-read and its `seq` assignment must be **atomic under the per-session emit
lock**, so the highest-seq snapshot was built from the most-recent registry state. Registry mutations
are synchronous (no `await`) and happen-before their own snapshot emit; therefore the globally-last
snapshot-under-lock reflects every mutation. Building the snapshot *outside* the lock reintroduces a
real race (a preempted `phase_end` enqueuing a stale `{}` at a higher seq than a concurrent
`phase_start`'s snapshot). ⇒ extract `_push_event`'s locked body into `_persist_and_enqueue(session_id,
make_event)` that calls `make_event()` **inside** the lock.

## Server changes — `src/personal_agent/transport/agui/transport.py` (only file)

1. **Registry** (module-level): `_phase_registry: dict[str, dict[str, _PhaseRecord]]`
   (session → phase_id → record). `_PhaseRecord` = frozen dataclass {phase: Phase, phase_id, started_at,
   detail, parent_id}. Helpers:
   - `_phase_registry_add(session_id, ...) -> bool` — **session-sticky tracking** (codex #2): if the
     session is already tracked, always append the phase and return `True`. If it is a *new* session and
     `len(_phase_registry) >= _MAX_PHASE_SESSIONS (8192)`, **reject** — do not add, log a warning, return
     `False` (that session degrades to today's delta-only behaviour). We **never evict an active session**,
     so we can never manufacture a false authoritative empty snapshot for a tracked session.
   - `_phase_registry_remove(session_id, phase_id)` — pop the phase; drop the session key when it empties
     (sessions self-clean on last phase end; phase_span guarantees pairing, so the cap is a pure backstop).
   - `_phase_snapshot_value(session_id) -> {"active": [rec-as-dict, ...]}`.
2. **Refactor** `_push_event` → thin wrapper over new `_persist_and_enqueue(session_id, make_event)`
   which builds+converts+persists+enqueues under `_get_emit_lock`. External behaviour of `_push_event`
   unchanged (existing monkeypatch tests still capture it).
3. **Harden `_get_emit_lock`** (fold-in, codex #3): on eviction, **skip held locks**
   (`lock.locked()`), evicting the oldest *unheld* entry; if all are held, do not evict (transient
   growth). Preserves the FRE-518 one-stable-lock-per-session invariant my race-freedom proof relies on;
   also hardens the existing `turn_status` path.
4. **`_emit_phase_snapshot(session_id)`**: `await _persist_and_enqueue(session_id, lambda:
   StateUpdateEvent(key="phase_state", value=_phase_snapshot_value(session_id), session_id=session_id))`.
   Snapshot value read happens inside the lock (race-free).
5. **`emit_phase_start`**: after the `if not session_id: return` guard,
   `tracked = _phase_registry_add(...)` (synchronous, authoritative) → best-effort
   `_push_event(PhaseStartEvent)` → **if `tracked`**, best-effort `_emit_phase_snapshot(session_id)`
   (own try/except; independent of the delta). An untracked (rejected) session emits no snapshot.
   Never raises (AC-6).
6. **`emit_phase_end`**: `was_tracked = session_id in _phase_registry` (capture before remove) →
   `_phase_registry_remove(session_id, phase_id)` (synchronous) → best-effort `_push_event(PhaseEndEvent)`
   → **if `was_tracked`**, best-effort `_emit_phase_snapshot` (builds `{active:[remaining]}`, or the
   legitimate `{active:[]}` on the final end). Registry removal is authoritative even when the delta
   emit fails. An untracked session emits no snapshot (no false empty — codex #2).

`phase_span` is unchanged (it drives `emit_phase_start`/`emit_phase_end`, which now carry the registry).

## Client changes — `seshat-pwa/src/`

- `lib/types.ts`: add `PhaseSnapshotEntry { phase: PhaseName; phase_id; started_at; detail; parent_id }`
  and `PhaseStateData { active: PhaseSnapshotEntry[] }`. Add an optional
  `snapshotResolved?: boolean` field to `PhaseNode` (codex #1) — marks a node resolved by the snapshot
  safety net (provisional `completed`) rather than by its own authoritative `PhaseEnd`.
- `hooks/useSSEStream.ts` `case 'STATE_DELTA'`: add `key === 'phase_state'` branch with a **runtime
  shape guard** (codex #5): require `value` non-null object and `value.active` an array (accept
  `{active:[]}`); else ignore. Then `setPhases(prev => reconcilePhaseSnapshot(prev, value.active))`.
- `reconcilePhaseSnapshot(prev, active)` (pure, unit-tested):
  1. running node absent from snapshot ⇒ resolve to `completed` with `snapshotResolved: true`,
     `endedAt = p.endedAt ?? Date.now()` — the dropped-`PhaseEnd` self-correction. The marker keeps it
     **upgradable** by a later terminal delta.
  2. snapshot entry not present by `phaseId` ⇒ append a `running` node with `startedAt = e.started_at`
     verbatim (AC-3(b) byte-equality; the "deliver only the state message" convergence).
  3. running node present in snapshot ⇒ untouched (still running, elapsed continues from startedAt).
  Idempotent: same snapshot twice ⇒ stable.
- **Terminal handlers widened** (codex #1): `RUN_ERROR` and `CANCELLED` sweeps change their predicate
  from `state === 'running'` to `state === 'running' || snapshotResolved === true`, so a phase the
  snapshot provisionally completed after a dropped `PhaseEnd(ok=false)` is still upgraded to
  `error`/`cancelled`. A genuine `PhaseEnd`-completed node (unmarked) is never touched, so a turn-level
  `RUN_ERROR` cannot mislabel a legitimately-completed earlier phase. `DONE` (→ completed) is unchanged.
  `PHASE_END` clears `snapshotResolved` when it authoritatively resolves the node.

## Tests (TDD — failing first)

**Server** `tests/personal_agent/transport/test_phase_state.py` (new; exercise the **real**
`_persist_and_enqueue`/`_get_emit_lock` — monkeypatch only `AsyncSessionLocal`+`SessionEventBuffer`
(a fake buffer assigning monotonic seq) and `get_event_queue` (a capture queue); reset `_phase_registry`
per test):
- `emit_phase_start` registers the phase and emits a `phase_state` snapshot whose `active` contains it,
  with `seq` **greater** than its own `PhaseStart` delta (delta-before-snapshot ordering).
- two concurrent starts → snapshot lists both; end one → snapshot lists only the survivor.
- end the last phase → snapshot `active == []`; session key dropped from registry.
- snapshot `started_at`/`detail`/`parent_id` carried verbatim from the registry.
- **race (codex, strengthened):** interleave two starts + one end through the real locked path with a
  seq-assigning fake; assert the **highest-seq** enqueued snapshot equals the final registry state
  (never a stale `{}` above a live start's snapshot).
- **dropped-end convergence (codex #4):** start then end, but drop the `PhaseEnd` delta from the
  capture queue; assert a persisted `phase_state` snapshot with a **higher seq** than the start carries
  `active == []`; feed *that snapshot envelope alone* to the client reconciler test and assert the node
  resolves. Proves convergence without a replayed `PhaseEnd`.
- **cap (codex #2):** with the registry at `_MAX_PHASE_SESSIONS`, a new session's `emit_phase_start`
  emits the delta but **no** snapshot; its `emit_phase_end` also emits no snapshot (no false empty).
- **lock eviction (codex #3):** a held session lock is not evicted when a new session's lock is created
  at cap; the same session keeps one lock instance across delta+snapshot.
- **AC-6:** force the persist path to raise → `emit_phase_start`/`emit_phase_end` return normally
  (no propagation); registry still mutated.
- existing `test_phase_events.py` still green (its `_push_event` recorder captures only the delta;
  the snapshot goes through `_persist_and_enqueue`, which those tests will also stub to a no-op).

**Client** `seshat-pwa/src/__tests__/phase-state.test.ts` (new, pure reconcile) +
additions to `useSSEStream.phases.test.tsx`:
- deliver only a `phase_state` STATE_DELTA (no prior deltas) → one running PhaseNode with the entry's
  `startedAt` byte-equal to `started_at`.
- running node + snapshot omitting it → resolves to `completed` with `snapshotResolved:true`
  (self-correction).
- **terminal upgrade (codex #1):** node snapshot-resolved to completed, then `RUN_ERROR` → `error`;
  same for `CANCELLED` → `cancelled`; and a genuine `PhaseEnd(ok=true)` completed node is **not**
  upgraded by a later `RUN_ERROR`.
- multi-active snapshot (parent + 2 children) → all running.
- same snapshot twice → stable (idempotent).
- **shape guard (codex #5):** `phase_state` with null/non-object value or non-array `active` → ignored,
  no throw; `{active:[]}` accepted.

## Acceptance-criterion proof (AC-3, the one this ticket unblocks)

- **AC-3(a)** currently-active phase from the snapshot alone → client-reconcile test #1.
- **AC-3(b)** phase-start timestamp byte-equal to server → reconcile carries `started_at` verbatim +
  server registry stores the same string used in the `PhaseStart` delta.
- **AC-3(c)** completed-before-drop phases not re-narrated active → they are absent from the snapshot
  (registry removed them) and their replayed `PhaseEnd` deltas resolve them.
- **Self-correction (ticket's core proof):** dropped `PhaseEnd` → next snapshot resolves the stuck
  phase → reconcile test #2. A replay-only implementation cannot pass this.

## Quality gates
`make test-file FILE=tests/personal_agent/transport/test_phase_state.py` → module green; then
`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`;
`cd seshat-pwa && npm run lint && npm test`. code-review `high` (transport/reconnect logic);
security-review N/A (no new inputs/egress/auth — additive cosmetic emission on an existing path).

## Out of scope / not this ticket
- AC-7 assembled-seam live proof (FRE-937). Live deployed-stack verification is master's, post-merge.
- No Redis bus, no new event model, no new durable schema (snapshot is an existing-kind `session_events`
  row, same as `turn_status`).
