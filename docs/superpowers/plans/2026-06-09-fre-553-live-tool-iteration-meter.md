# FRE-553 — Restore live `tool_iteration` meter (sub-agent-inclusive)

**Ticket:** FRE-553 (Approved · Tier-2:Sonnet · Bug · Observability Foundation)
**Refs:** FRE-513 (ADR-0088 spine — cause) · FRE-518 (same failure class) · FRE-501 (the meter) · ADR-0076 (`turn_status` STATE_DELTA) · ADR-0088 D3/D4

---

## Root cause (code-confirmed, refining the ticket)

The PWA `TurnStatusBar` renders `tools {tool_iteration}/{tool_iteration_max}` from the
`turn_status` STATE_DELTA, emitted solely by the live projector
(`observability/topology/projector.py`).

**The primary per-iteration tick is NOT lost.** FRE-513 faithfully replaced the inline
`_emit_turn_status(ctx)` with `_report_turn_progress(ctx)` at `executor.py:3011`, called
immediately after `ctx.tool_iteration_count += 1` (3009). For a **non-decomposed** turn the
counter still climbs live per tool-iteration. (The ticket cited 1783-1784 / 196-197 / 1380
but missed 3011.)

**The real gap is the decomposition path.** For an `enforced`-mode DECOMPOSE/HYBRID turn,
the heavy iterations run inside sub-agents (`sub_agent.py:_run_tooled_loop`, cap
`sub_agent_max_tool_iterations`, line 472), dispatched **concurrently** via `asyncio.gather`
(`expansion_controller.py:429`). Those iterations:
- do **not** call `_report_turn_progress`, and
- do **not** increment the primary `ctx.tool_iteration_count`.

Meanwhile each sub-agent model call publishes `turn.model_call_completed` from the cost
boundary → **cost climbs live** (FRE-501 rolled sub-agent cost into the meter). So the
symptom is exactly: **cost climbs, tool counter flat** for the multi-minute expansion window.
`_report_turn_progress` fires only at expansion dispatch start (1743) and after expansion
completes (1788), so the primary count is frozen throughout.

## Design decision — sub-agent-inclusive aggregate

Surface **(primary + Σ sub-agent) iterations** against **(primary_max + Σ sub-agent caps)**.

Rationale:
- Cost is already sub-agent-inclusive (FRE-501); the iteration meter should match for a
  coherent live signal.
- Primary-only would stay near-flat exactly during the expansion window where the owner
  observed the stall — it would not fix the reported symptom.
- Concurrency-safe: track each sub-agent's latest iteration per `task_id` and **sum**, so
  concurrent sub-agents never clobber a shared int; the sum is monotonic per-task.

Preserves ADR-0088 D4: the **projector** remains the sole `turn_status` emit point; the
sub-agent only reports an observation event onto the seam stream. Cost stays on the
`turn.model_call_completed` path (no re-coupling of D3's removed per-loop cost rollup).

---

## Steps (TDD)

### 1. New event `SubAgentProgressEvent` — `src/personal_agent/events/models.py`
Add after `TurnProgressEvent` (~line 1056):
```python
class SubAgentProgressEvent(EventBase):
    """A sub-agent tool-iteration tick, to climb the aggregate live meter (FRE-553).

    Published best-effort from the sub-agent tool loop so the live projector can surface
    sub-agent iterations in the turn meter (cost is already aggregated via
    ``turn.model_call_completed``). Keyed by ``task_id`` so concurrent sub-agents are
    summed rather than clobbering a single counter. ADR-0088 D4: the projector stays the
    sole ``turn_status`` emitter.

    Attributes:
        task_id: Sub-agent task identifier (per-sub-agent join key).
        iteration: Completed tool-iteration count for this sub-agent (1-based).
        iteration_max: This sub-agent's tool-iteration cap.
    """

    event_type: Literal["turn.sub_agent_progress"] = "turn.sub_agent_progress"
    source_component: str = "orchestrator.sub_agent"
    trace_id: str
    session_id: str
    task_id: str
    iteration: int
    iteration_max: int
```
Add dispatch in `parse_stream_event` (~line 1120, beside `turn.progress`):
```python
    if raw_type == "turn.sub_agent_progress":
        return SubAgentProgressEvent.model_validate(payload)
```
Also export it from `src/personal_agent/events/__init__.py` (beside `TurnProgressEvent`,
~lines 25 & 84) so imports stay consistent with the rest of the codebase **(codex e1)**.

### 2. Emit from the sub-agent loop — `src/personal_agent/orchestrator/sub_agent.py`
In `_run_tooled_loop`, factor a tiny best-effort helper and call it (a) **once before the loop**
with `iteration=0` to establish the denominator early **(codex c — avoids the amber bar
yo-yoing when the max jumps on the first real tick)**, and (b) per iteration after the existing
`sub_agent_tooled_iteration` log (~line 556-563). `task_id`, `trace_id`, `session_id` are in
scope; guard on `session_id`:
```python
        if session_id:
            try:
                from personal_agent.events import get_event_bus
                from personal_agent.events.models import (
                    STREAM_TURN_OBSERVED,
                    SubAgentProgressEvent,
                )

                await get_event_bus().publish(
                    STREAM_TURN_OBSERVED,
                    SubAgentProgressEvent(
                        trace_id=trace_id,
                        session_id=session_id,
                        task_id=task_id,
                        iteration=iteration,      # 0 for the started tick, then 1..N
                        iteration_max=max_iterations,
                    ),
                    maxlen=settings.turn_observed_stream_maxlen,
                )
            except Exception:
                logger.debug(
                    "sub_agent_progress_publish_failed", task_id=task_id, trace_id=trace_id
                )
```
Started tick: publish `iteration=0` right before `for iteration in range(max_iterations):`.
Per-iteration tick: publish `iteration=iteration + 1` after the iteration's tools run.
(`settings` is already imported in this module; confirm at implement time.)

### 3. Projector aggregation — `src/personal_agent/observability/topology/projector.py`
- `TurnObservation`: add
  ```python
      sub_agent_iterations: dict[str, int] = field(default_factory=dict)
      sub_agent_iteration_max: dict[str, int] = field(default_factory=dict)
  ```
  (+ docstring lines).
- Import `SubAgentProgressEvent`.
- `handle`: add a branch (after the `TurnProgressEvent` branch). **Max-wins (codex e2)** so the
  per-task count is strictly non-regressing under best-effort/reordered Redis delivery:
  ```python
      elif isinstance(event, SubAgentProgressEvent):
          obs = self._observation(event.trace_id, event.session_id)
          obs.sub_agent_iterations[event.task_id] = max(
              obs.sub_agent_iterations.get(event.task_id, 0), event.iteration
          )
          obs.sub_agent_iteration_max[event.task_id] = max(
              obs.sub_agent_iteration_max.get(event.task_id, 0), event.iteration_max
          )
  ```
  Do **not** pop individual sub-agents — only `TurnCompletedEvent` pops the whole trace
  (`projector.py:133`), so sub-agent contributions persist through synthesis **(codex c)**.
- `_emit`: surface the aggregate (raw fields kept separate; summed at emit time):
  ```python
      "tool_iteration": obs.tool_iteration + sum(obs.sub_agent_iterations.values()),
      "tool_iteration_max": obs.tool_iteration_max
          + sum(obs.sub_agent_iteration_max.values()),
  ```

### 4. Tests
- `tests/observability/topology/test_projector.py`:
  - `test_sub_agent_progress_climbs_aggregate` — primary progress (iter 1/25) + two
    concurrent sub-agents (task-a iter 3/10, task-b iter 2/10) ⇒ surfaced `tool_iteration == 6`,
    `tool_iteration_max == 45`.
  - `test_concurrent_sub_agents_do_not_clobber` — interleave task-a/task-b ticks; assert the
    surfaced count is the **sum** of latest per-task, never a single task's value.
  - `test_reordered_tick_is_non_regressing` — deliver task-a iter 3 then a stale iter 1;
    assert the surfaced count does not drop (max-wins, codex e2).
  - `test_non_decomposed_unaffected` — a `TurnProgressEvent` with no sub-agent events ⇒
    surfaced equals the primary values (regression guard for ADR-0088 D4 path).
- `tests/test_orchestrator/test_sub_agent.py` (or nearest existing sub-agent test):
  - assert `_run_tooled_loop` publishes a `SubAgentProgressEvent` per tooled iteration
    (mock `get_event_bus`, two iterations ⇒ two events with `iteration` 1 then 2).
- `tests/test_events/` parse round-trip: `parse_stream_event` returns `SubAgentProgressEvent`
  for a `turn.sub_agent_progress` payload.

### 5. Docs / decision record
- Docstrings above carry the decision.
- PR body + a Linear comment record: **decision = sub-agent-inclusive aggregate** (acceptance
  item 4). No ADR edit (ADR-0088 lives in the adr session; D4 invariant is preserved, not changed).

---

## Test commands
```bash
make test-file FILE=tests/observability/topology/test_projector.py
make test-file FILE=tests/test_orchestrator/test_sub_agent.py    # adjust to actual path
make test-file FILE=tests/test_events/test_models.py             # adjust to actual path
make test                  # full unit suite green
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```
Expected: new tests fail first (red), pass after implement; full suite green; mypy/ruff clean.

## Acceptance mapping
- [x] live per-iteration climb → sub-agent ticks + intact primary 3011 path (test + manual by master)
- [x] no cost regression → cost untouched, stays on `turn.model_call_completed`
- [x] ADR-0088 D4 preserved → projector remains sole `turn_status` emitter
- [x] decision recorded → sub-agent-inclusive (docstring + PR + Linear comment)

## Out of scope / follow-ups
- Manual live verification on a decomposed run is **master's** post-deploy step (not a PR item).
- Non-tooled single-shot sub-agents have no iterations to surface (acceptable; covered by
  primary-phase progress).
