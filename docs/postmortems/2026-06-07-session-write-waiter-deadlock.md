# Post-mortem: session-write waiter pop/get race deadlocks the next /chat turn

> Date: 2026-06-07 · Severity: High (latent prod defect, surfaced by eval) · Status: fix pending (FRE-520, Needs Approval)

## Summary

The first FRE-453 canonical eval baseline run (`fre453-baseline-01`, local profile) died after
3/18 cases: case 4's scored turn hung server-side for the full 1200 s client timeout and the
harness exited with `httpx.ReadTimeout`, writing no report. Root cause is a race in
`src/personal_agent/events/session_write_waiter.py` (FRE-51/FRE-158): a follow-up `/chat`
request that arrives before the session-writer consumer releases the previous turn's append
waiter **pops the Future and awaits it forever** — the later release looks the Future up by
`get()`, finds nothing, and no-ops. The await has no timeout, so the request hangs
indefinitely. This is a production-reachable defect (fast consecutive sends on one session),
not an eval-only artifact.

## Timeline (UTC, 2026-06-07)

| Time | Event |
|---|---|
| 15:43:33 | Baseline run starts (18 cases, `--profile local`, eval identity per FRE-481 pattern) |
| 15:44:30 | Case 1 `trivial_conversational` clean (trace `7a5d3420`) |
| 15:45:16 | Case 2 `memory_recall` clean (trace `fa678187`) |
| 15:46:16 | Case 3 `opening_ritual` clean (trace `5726c5f8`) |
| 15:47:18 | Case 4 `closing_ritual` setup turn 1 OK (trace `cb95e684`, session `eb92a21d`) |
| 15:47:49 | Setup turn 2 OK (trace `1ae57865`); handler registers session-write waiter, publishes `request.completed`, returns |
| 15:47:49 | Scored stimulus arrives (trace `7abc4a00`): `request_received`, then **zero further events** — it popped + awaited the unresolved waiter |
| 15:47:49+ | Consumer appends setup turn 2's assistant message **successfully** (session shows `user/assistant/user/assistant`), calls `release_session_write_wait` → `get()` → `None` → no-op |
| 15:49–16:04 | Gateway healthy; SLM probes `up` (170–400 ms); only housekeeping events |
| 16:07:49 | Harness `httpx.ReadTimeout` (1200 s) → run dies, exit masked to 0 by `| tee` |
| ~16:20 | Root cause isolated to the pop/get race; FRE-520 filed |

## Root cause

```python
async def await_previous_session_write(session_id):
    fut = _session_write_waiters.pop(session_id, None)   # removes the Future
    if fut is not None:
        await fut

def release_session_write_wait(session_id):
    fut = _session_write_waiters.get(session_id)         # popped → None → no-op
    if fut is not None and not fut.done():
        fut.set_result(None)
```

Ordering dependency: if the consumer releases **before** the next turn awaits (the common
case — appends are fast, humans type slowly), the popped Future is already resolved and the
await returns instantly. If the next turn arrives first (zero think-time harness; PWA
double-send), the pop makes the Future unreachable to every release path — including the
dead-letter path whose stated purpose is "so the API never deadlocks".

## Why it surfaced now

The FRE-453 harness fires the next session turn immediately after the previous response
returns — every multi-turn case is a race per turn. `closing_ritual` is the first 3-turn case
in dataset order; cases 2–3 (2-turn) happened to win the race.

## What went well

- Route-trace ledger + ES + `api_costs` triangulation pinned the hang to a single code line
  from durable telemetry alone (no reproduction needed): `request_received` with zero
  follow-on events ruled out model/tool loops; healthy SLM probes ruled out the backend;
  the 4-message session proved the awaited append had completed.
- The harness's instrument-health posture (exit non-zero on missing rows) behaved correctly;
  the failure was upstream of it.

## What went wrong / lessons

1. **`| tee` masked the crash** — the background task reported exit 0 (tee's status). Run
   eval harnesses with `set -o pipefail` or redirect (`> log 2>&1`) instead of piping.
2. **Pre-run endpoint probing skipped** — standing lesson from FRE-481 (probe primary +
   sub-agent endpoints before launching a harness) was not applied. It would not have caught
   this bug, but the discipline stands.
3. **An await on a cross-task Future must have a timeout** — availability beats strict
   ordering for a best-effort consistency mechanism (fix sketch in FRE-520).
4. ~~"local profile ran on Haiku"~~ — initial misread: Haiku rows in `api_costs` are the
   `skill_routing` model (`AGENT_SKILL_ROUTING_MODE=model_decided`); the primary tier ran on
   local Qwen3.6-35B-A3B via `slm.frenchforet.com` ($0, hence invisible in `api_costs`).
   Owner-corrected. Reading: check `purpose` before attributing model usage.

## Action items

| # | Action | Tracking |
|---|---|---|
| 1 | Fix pop/get race + add bounded `wait_for` + regression test | FRE-520 (Needs Approval, High) |
| 2 | Re-run `fre453-baseline-01` after FRE-520 ships | FRE-453 (In Progress) |
| 3 | Harness launches: no bare `\| tee` without `pipefail` | this doc (process) |
| 4 | EVAL-channel side-effect suppression (`eval_mode_side_effects_suppressed`) may blank the harness's background-surface layer — validate expectations on the rerun | FRE-453 rubric pass |

## References

- FRE-520 (bug) · FRE-453 (eval set) · FRE-51 / FRE-158 (waiter heritage)
- `src/personal_agent/events/session_write_waiter.py` · `service/app.py` `/chat` phases
- Traces: `7abc4a00` (hung), `1ae57865` (append completed), session `eb92a21d`
- Run log: `/tmp/fre453_run.log` (host-local, not committed)
