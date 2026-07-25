# FRE-934 — ADR-0123 T1: put inference phases on the AG-UI transport

**Ticket:** FRE-934 (Approved, Tier-1:Opus, stream:build1) · **ADR:** ADR-0123 Decision §1–§2, Sequencing step 1
**Scope:** transport event contract + best-effort emitters at the inference boundaries. **No UI.**
**ACs owned:** AC-1, AC-2, AC-6, AC-8 — all provable from the event stream alone.

---

## Design summary

The transport models tool execution (`ToolStartEvent`/`ToolEndEvent`) but not **inference**. Every
long silence measured in the ADR was an inference step. Add a `PhaseStart`/`PhaseEnd` event family
alongside the tool events, emitted at the currently-invisible boundaries: the **planning** inference
call, the **artifact-build** sub-agent, the **synthesis** step, the **expansion** fan-out (parent +
concurrent sub-agent children), and the **human-wait** pauses (`Waiting for your choice`). Emission
rides the existing best-effort `_push_event` path — a failed emit is a cosmetic loss, never a failed
turn.

### Phase enum (closed contract) — `transport/events.py`

```python
class Phase(str, Enum):
    PLANNING = "planning"                    # primary inference (step_llm_call / STEP_PLANNING_*)
    ARTIFACT_BUILD = "artifact_build"        # artifact-draft sub-agent build
    SYNTHESIS = "synthesis"                  # final response assembly (step_synthesis)
    EXPANSION = "expansion"                  # parent phase over a sub-agent fan-out
    SUB_AGENT = "sub_agent"                  # one child within an EXPANSION
    WAITING_FOR_CHOICE = "waiting_for_choice"  # human pause (excluded from AC-2 gap clock)
```

Only phases this ticket actually emits are in the enum (no speculative members). T3 (client) maps
these to display copy; it does not reopen the contract.

### Events

```python
@dataclass(frozen=True)
class PhaseStartEvent:
    phase: Phase
    phase_id: str            # unique instance id; pairs with end, distinguishes concurrent children
    session_id: str
    started_at: str          # ISO-8601 UTC server timestamp (client computes elapsed from this)
    detail: str | None = None
    parent_id: str | None = None   # set → this is a child of that parent phase instance

@dataclass(frozen=True)
class PhaseEndEvent:
    phase: Phase
    phase_id: str
    session_id: str
    parent_id: str | None = None
```

Added to the `InternalEvent` union.

### Wire format — `transport/agui/adapter.py`

Two new match arms (before the `case _` fallback), mirroring the tool arms:

```python
case PhaseStartEvent(...):
    envelope = {"type": "PHASE_START", "session_id": sid,
                "data": {"phase": phase.value, "phase_id": pid, "started_at": ts,
                         "detail": detail, "parent_id": parent}}
case PhaseEndEvent(...):
    envelope = {"type": "PHASE_END", "session_id": sid,
                "data": {"phase": phase.value, "phase_id": pid, "parent_id": parent}}
```

`to_agui_event` already appends `seq`. Add the two events to the adapter import.

### Emitters + span helper — `transport/agui/transport.py`

Best-effort primitives (swallow **all** exceptions incl. `_push_event` raising → AC-6 by
construction) and an `asynccontextmanager` that guarantees start/end pairing across every exit path
and is a no-op when `session_id` is falsy (headless/CLI/eval):

```python
async def emit_phase_start(*, session_id, phase, phase_id, started_at, detail=None, parent_id=None):
    if not session_id: return
    try: await _push_event(PhaseStartEvent(...), session_id)
    except Exception: log.exception("transport.phase_start_emit_failed", ...)

async def emit_phase_end(*, session_id, phase, phase_id, parent_id=None):
    if not session_id: return
    try: await _push_event(PhaseEndEvent(...), session_id)
    except Exception: log.exception("transport.phase_end_emit_failed", ...)

@asynccontextmanager
async def phase_span(*, session_id, phase, detail=None, parent_id=None):
    if not session_id:
        yield None; return
    phase_id = uuid4().hex
    await emit_phase_start(session_id=session_id, phase=phase, phase_id=phase_id,
                           started_at=datetime.now(UTC).isoformat(), detail=detail, parent_id=parent_id)
    try:
        yield phase_id
    finally:
        await emit_phase_end(session_id=session_id, phase=phase, phase_id=phase_id, parent_id=parent_id)
```

## Emission sites (lazy imports, matching the existing `_maybe_pause_for_constraint` convention)

> **Revised after codex plan-review.** Two source-verified corrections: (a) the HYBRID expansion hook
> runs *inside* `step_llm_call` (`executor.py:4380`), so a whole-function PLANNING span would overlap
> the EXPANSION parent → two concurrent parents, violating "one active phase". (b) `step_synthesis`
> (`:5084`) does **no inference** — it appends disclosures and updates the session; the real "final
> synthesis inference" is the post-tool `step_llm_call` round. Both fixed by bracketing phases
> **tightly around the `respond()` calls**, which also maps 1:1 to ADR §2's three named boundaries.

1. **PLANNING / SYNTHESIS** — `orchestrator/executor.py::step_llm_call`, wrap **only** the
   `await asyncio.wait_for(llm_client.respond(...), ...)` statement (:4248-4262) in
   `async with phase_span(session_id=ctx.session_id, phase=_round_phase(ctx)):`. `_round_phase`
   returns `PLANNING` on the first inference round (no tool has run — `ctx.tool_iteration_count == 0`)
   and `SYNTHESIS` on any post-tool round. This is exactly ADR §2's "planning inference step
   (step_planning_started)" and "final synthesis inference". The span sits **inside** the existing
   inner `try` (:4247) so its end pairs on success and on the `TimeoutError` raise; the pre-call
   deadline-exhausted early return (:4220-4245) never opens a span (no inference happened → no
   unpaired start). Tight scope ⇒ the span is closed before the expansion hook / tool processing, so
   PLANNING never overlaps EXPANSION.

2. **~~SYNTHESIS around step_synthesis~~ — DROPPED.** `step_synthesis` is bookkeeping, not inference
   (codex finding). SYNTHESIS is the post-tool `step_llm_call` round above.

3. **ARTIFACT_BUILD** — `tools/artifact_tools.py::artifact_draft_executor`. Wrap the sub-agent
   `builder_client.respond(...)` (:1593-1602) in `async with phase_span(ARTIFACT_BUILD, detail=title)`
   (or emit_start before the `try` :1592 + `emit_phase_end` in a new `finally`). Brackets the
   multi-minute build incl. its timeout/error raises. Runs during `step_tool_execution` — outside
   `step_llm_call` — so no overlap with PLANNING/SYNTHESIS.

4. **EXPANSION + SUB_AGENT children** — `orchestrator/expansion_controller.py::_run_dispatch`. Wrap
   the dispatch `try` (:405) in `async with phase_span(EXPANSION, detail=f"{len(specs)} sub-agents")
   as parent_id:`. Replace the inline `run_sub_agent(...)` comprehension with a nested
   `_dispatch_one(spec)` that wraps each call in `async with phase_span(SUB_AGENT, detail=spec.task…,
   parent_id=parent_id)`. Parent's `finally` fires after `gather` → parent end is ordered after every
   child end (AC-8). No-op when `session_id is None`.

5. **WAITING_FOR_CHOICE (constraint pauses)** — `executor.py::_maybe_pause_for_constraint`. Wrap the
   `await register_and_push_constraint(...)` (:656) in `async with phase_span(WAITING_FOR_CHOICE,
   detail=constraint)`. Covers the ADR-0122 builder card, attachment-cost gate, and context-compression
   pause in one place. Only the actual pause is wrapped (a stored-preference bypass never reaches it).

6. **WAITING_FOR_CHOICE (tool approval)** — `tools/executor.py` (:239). Wrap the
   `await transport.request_tool_approval(...)` in `async with phase_span(WAITING_FOR_CHOICE,
   detail=tool_name)` (session_id may be `""` → guarded no-op).

## Tests (TDD — write first, watch fail, implement)

- `tests/personal_agent/transport/test_adapter.py` — `PHASE_START`/`PHASE_END` round-trip: type,
  data fields (phase value, phase_id, started_at, detail, parent_id), seq passthrough.
- `tests/personal_agent/transport/test_phase_events.py` (new):
  - `emit_phase_start/end` push the right `InternalEvent` (patch `transport._push_event` recorder).
  - **AC-6:** with `_push_event` patched to raise on every call, `emit_phase_*` and a `phase_span`
    block both complete without propagating; failure is logged.
  - `phase_span` no-op when `session_id` is `None`/`""` (nothing pushed, yields `None`).
  - `phase_span` emits exactly one start + one end with matching `phase_id`; end even when the body
    raises.
- `tests/personal_agent/orchestrator/test_expansion_controller.py` — **AC-8:** patch `run_sub_agent`
  (staggered delays) + `_push_event` recorder; `_run_dispatch` with a 3-task plan and a real
  `session_id`. Assert: 1 EXPANSION start; 3 SUB_AGENT starts each `parent_id == expansion phase_id`;
  3 SUB_AGENT ends; EXPANSION end ordered strictly after all 3 child ends. With `session_id=None`,
  zero phase events.
- `tests/test_orchestrator/test_executor.py` (or a focused new file) — **AC-1 (planning+synthesis):**
  drive `step_synthesis` directly asserting a SYNTHESIS start/end pair with a server timestamp;
  planning pair asserted via a focused `step_llm_call` path where the harness already mocks the LLM
  client, else a targeted emitter-wiring test.
- `tests/personal_agent/tools/test_artifact_tools.py` — **AC-1 (artifact):** patch
  `builder_client.respond`; assert an ARTIFACT_BUILD start/end pair (and end still fires on the
  timeout/error raise).
- **AC-2 (sequence + gap mechanics):** a test assembling the ordered event stream of a
  planning→artifact→synthesis turn asserts all three phases present and ordered, and that a
  gap-computation excluding `WAITING_FOR_CHOICE` intervals sees no boundary missing. The live
  10-second-gap measurement on the deployed stack is AC-7/master's live check (noted in handoff).

## Quality gates
`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files` · self-review (code-review high — src/transport/executor) +
security-review (touches transport emission; no new egress/auth/secret surface).

## Open decision — AC-2's 10s-gap clock (owner call, surfaced before coding)

Codex flagged a genuine ADR-internal tension. AC-2: "no gap between consecutive **semantic** events
(phase/child/tool start or end) exceeds 10 s", and §4 explicitly forbids heartbeat/timer filler as
non-semantic. But §3 says a long inference is **named + shown with a client-side advancing elapsed
counter**, and no partial inference output is streamed. So a single 43 s inference emits `PhaseStart`
at t=0 and `PhaseEnd` at t=43 — a 43 s span between two semantic events. Taken literally, AC-2 is
**unsatisfiable for exactly the case the ADR exists to fix**, unless the active-phase interval is
treated the same way `Waiting for your choice` is: excluded from the gap clock because an active
phase with a start-timestamp (advancing elapsed) is **not silence**.

**DECIDED (owner, 2026-07-25): exclude active-phase intervals.** the AC-2 gap clock measures *dead
air* — intervals with **no active phase** — and excludes both `WAITING_FOR_CHOICE` **and** any
interval where a phase is running (its start-timestamp is the non-silence signal, §3). Under this reading the emitters satisfy AC-2 by
making phases **contiguous** (no unnamed stretch between the end of one phase and the start of the
next > 10 s). This is the only reading consistent with §3/§4 forbidding streamed filler. The
alternative — emitting periodic in-phase events — is the gaming §4 outlaws. The live 10 s
measurement is AC-7/master's deploy-time check; this ticket makes the stream carry a contiguous,
gap-free phase sequence so the metric is computable and passes under the recommended reading.

*(This is the one item requiring owner sign-off — it defines how master's gate reads AC-2.)*

## AC-6 precision (codex finding)
The emit helpers catch `Exception` (a failed persist/enqueue/serialize is cosmetic loss).
`BaseException`/`asyncio.CancelledError` are **not** swallowed — a cancelled turn must stay cancelled.
Helpers live in `transport/agui/transport.py` (no lazy-import fragility there); call sites import
`phase_span`/`emit_phase_*` at function top, before the guarded block, so the reference exists in the
`finally`.

## Out of scope (folded-out, not this ticket)
- Client/PWA rendering (T3/FRE-936), unknown-is-unknown status bar (T2/FRE-935), collapsed summary +
  live AC-7 (T4/FRE-937).
- **Parallel-tool parent grouping**: tool children already stream as `ToolStart/End` (ADR: "not
  re-implemented"); no AC here needs a synthetic tool parent.
- **Skill-routing inference** (`route_skills`, `executor.py:3722`): a fast classification call, not
  one of ADR §2's three named inference boundaries. Left invisible for now; note as a possible
  follow-up if it proves slow. Adding it would expand scope beyond the ADR's named set.
