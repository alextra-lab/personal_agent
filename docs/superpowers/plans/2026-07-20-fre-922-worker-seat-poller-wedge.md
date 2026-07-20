# FRE-922 — Worker seats wedge their own stream when a background poller lingers (CC #61568)

**Ticket:** FRE-922 (Approved, Tier-1:Opus, stream:build2). Backing incident: FRE-917 sat
undispatched ~90 min because the cc-2build seat left orphaned `run_in_background` bash pollers
alive; Remote Control reported `status=busy` while the pane sat idle at its input prompt, so every
tick emitted `outcome=seat-busy` and refused the CLEAR dispatch. Upstream CC bug class
(anthropics/claude-code #61568 et al.) — not fixable by us; we prevent worker seats from tripping it
and detect+surface it when it recurs.

## Verification decision (AC-1 pivot)

`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` **is a real Claude Code env var** — verified against the
installed 2.1.215 binary (the exact artifact the VPS seats run), the primary source:
- Registered in the known-env-var schema (alongside sibling BG-shell flags
  `CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF`, `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP`).
- Read by a dedicated accessor `function _v(){return Z.CLAUDE_CODE_DISABLE_BACKGROUND_TASKS}`.
- Sits directly against the Bash tool's `run_in_background` option ("use **Bash with
  `run_in_background`**") — the exact feature that spawns the orphan pollers.
- NOT in the public settings docs table; the docs' `CLAUDE_CODE_DISABLE_AGENT_VIEW` is a *broader*
  hammer that would also kill `claude agents` (the dispatcher's RC-status probe) — unusable here.

⇒ **Take path (a)**: set the env var in the seat-launch environment. Structural (a seat that cannot
start a background shell cannot orphan one) and it sidesteps FRE-913's "launcher owns no termination
code" invariant that path (b)'s process-reaping sweep would strain.

## Changes

### Part 1 — Prevention (AC-1), `scripts/dispatch/launcher.py`
1. New module constant `_DISABLE_BACKGROUND_TASKS_ENV = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"`
   with a comment citing CC #61568 / FRE-922 and the verification.
2. In `_build_tmux_command`, always wrap the inner claude argv with an `env` prefix carrying
   `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` — for **both** channel and send_keys modes (the current
   `env` prefix is channel-only). Preserve ordering: `env <vars> claude <flags> [seed] [--channels
   <ref>]`. The channel port, when present, joins the same `env` prefix.
3. Update the `_build_tmux_command` docstring line that claims the send_keys shape is "byte-for-byte
   the pre-channel shape" → now "pre-channel shape plus the background-tasks-disabled env prefix".

### Part 2 — Detect + surface (AC-2/AC-3)
`scripts/dispatch/launcher.py`:
4. New `seat_wedge_signature(topology, runner) -> bool`: `True` iff `seat_is_busy(...) is True` AND
   `session_is_idle(_capture_pane(...))`. A single observation is deliberately ambiguous (a genuine
   mid-turn seat whose spinner the scrape missed looks identical) — the docstring says so and points
   at the orchestrator's N-tick gate. Reuses existing primitives; no new IO shape.

`scripts/dispatch/orchestrator.py`:
5. `DEFAULT_WEDGE_TICKS = 2` (surface when the consecutive count *exceeds* N, i.e. the 3rd tick ≈
   15 min at the 300 s cadence — fast enough to be useful, slow enough to ride out a one-tick
   RC/pane race).
6. **The counter is in-memory** — a `dict[str,int]` owned by the daemon loop, mutated across ticks,
   reset on restart. **Not persisted** (revised per the high code-review — see below). No sidecar
   file, no `load/save_wedge_counts`.
7. `run_once` gains `wedge_counts: dict[str,int] | None = None`, `wedge_ticks: int =
   DEFAULT_WEDGE_TICKS` (defaulted → existing callers/tests unaffected). Threaded into `_apply`.
8. `_apply` launch case, after `execute_plan`:
   - if `result.outcome == "seat-busy"` AND `seat_wedge_signature(topology_for(stream), runner)`:
     increment `wedge_counts[stream]`; every tick where the new count `> wedge_ticks` emit a
     greppable `logger.warning("dispatch_seat_wedged", ...)`; on the **crossing** tick only (new ==
     `wedge_ticks+1`) also call `notifier("dispatch_seat_wedged", ...)` (one actionable master ping
     per episode, mirroring the stall throttle). Persist wedge_counts. **No record written** (same
     as plain seat-busy — the stream stays eligible so it dispatches the moment the wedge clears).
     **No process termination anywhere on this path.**
   - else (any non-wedge launch outcome): reset `wedge_counts[stream]` to 0 if set; persist.
   - Also reset on the `skip` decision (stream no longer trying to dispatch → episode over). Other
     decisions (`await`/`hold`/`clear`/`run_complete`/`stall`) only occur once a DispatchRecord
     exists, which cannot coexist with a live wedge counter (the wedge path writes no record), so no
     reset needed there.
   - `_record_for_result` unchanged: `seat-busy` still returns `None` (no record).
9. `main()`/`tick()`: own the in-memory `wedge_counts` across the loop; `--wedge-ticks` CLI arg.

## Tests (TDD — one outcome-level assertion per AC)

`tests/scripts/test_launcher.py`:
- **AC-1**: `_build_tmux_command`/`plan_launch(...).command[-1]` for a worker seat contains
  `env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 ` before `claude` — for both channel and send_keys modes.
- `seat_wedge_signature` truth table: busy-RC + idle-pane → True; busy-RC + busy-pane → False;
  idle-RC → False; RC-silent(None) → False.

`tests/scripts/test_launcher_channels.py` (update existing invariant — intentional per FRE-922):
- send_keys shape test: now starts with `env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 claude
  --remote-control …`; still no `--channels`/`SESHAT_CHANNEL_PORT`.
- channel shape test: env prefix now `env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1
  SESHAT_CHANNEL_PORT={port} …`; assert both vars present; `--channels` ref + seed ordering unchanged.
- Update module docstring line 11 accordingly.

`tests/scripts/test_orchestrator.py`:
- **AC-2**: a `_WedgeRunner` (live seat, RC agent busy@build1-worktree, capture-pane = idle caret).
  Run `run_once` N+1 ticks with a shared `wedge_counts` + capturing notifier/logger. Assert exactly
  one `dispatch_seat_wedged` notifier event fires (on the crossing tick), the greppable warning log
  fires on the post-threshold ticks, no record is written (stream stays eligible), and **no
  `kill`/`pkill`/`kill-session`/`kill-pane` argv ever appears** in `runner.calls`.
- **AC-3 (regression)**: same but capture-pane = busy spinner (`_BUSY_PANE`). Run N+1 ticks. Assert
  **zero** `dispatch_seat_wedged` events, wedge count stays 0, outcome remains generic `seat-busy`
  (no record). Confirms a genuinely-busy seat is never mistaken for a wedge.

## High code-review revisions (incorporated)
The high review (14 agents) confirmed the blocked-launch reset (refuted a finding I'd already fixed)
and converged 4 finders on one root cause — **persisting the counter made the one-shot ping fragile**
(a restart / changed `--wedge-ticks` / persist-before-notify crash could observe the count *above* the
crossing value and silently lose the alert forever). Fix: **drop persistence — keep `wedge_counts`
in-memory, reset on restart.** Within a run the count climbs by exactly 1 per tick, so the crossing is
hit exactly once and the equality check is sound; this eliminates findings [0] (crossing fragility),
[1] (stale-count-across-stream-churn), and [5] (bool-as-int sidecar loader) by construction, plus the
persist-before-notify window. Also removed the dead `and execute` sub-condition ([4]).
**Accepted with rationale:** [2] the env var disables `run_in_background` fleet-wide, not just for
orphans — but there is no seat-handoff cleanup (FRE-913), so *any* background bash on a worker seat
would itself orphan and wedge the seat; foreground-only is the correct policy for this architecture
(the ticket's own AC-1(b) mandates it). [3] `seat_wedge_signature` re-probes RC/pane that
`execute_plan` already read — bounded (seat-busy is exceptional, 5-min ticks) and the fresher read is
if anything more current; threading the reading out would widen the launcher's return contract on a
Tier-1 module for negligible gain.

## Codex plan-review revisions (incorporated)
- **AC-1 runtime safety (major):** the env var narrowly gates the Bash `run_in_background` param
  only — MCP/channel spawning are separate subsystems — but this is not statically provable from the
  three files. Post-deploy runbook MUST have master relaunch one worker seat with the env set and
  confirm RC registration + channel-mode + MCP startup are unaffected before trusting it fleet-wide.
- **Sidecar reset (major):** reset the wedge counter on **every non-wedge tick** (the confirmed-wedge
  increment is the *only* path that does not reset). Robust to a stale `dispatch_wedge.json`
  coexisting with a `DispatchRecord` after a crash / daemon restart / manual recovery. Implement as
  reset-at-top for any non-`launch` decision + reset in the launch-non-wedge branch.
- **Heuristic framing (major):** `seat_wedge_signature` is a *suspected*-wedge heuristic, not a
  definitive classifier. The N-tick gate is the mitigation for the scrape's documented false-idle: a
  genuinely-busy seat re-renders its spinner within N ticks and resets the count; only a persistently
  idle pane survives. Docstrings + the `dispatch_seat_wedged` wording say "suspected/apparent".
- **Blind spots (minor):** RC-unreadable/ambiguous (`seat_is_busy → None`) never fires the signature
  — documented as an intentional blind spot (that path either dispatches or is genuinely busy).
- **Duplicate streams (minor):** `run_once` order-preservingly de-dups `streams` before the loop so a
  repeated `--streams` value cannot double-increment the counter (also protects existing per-stream logic).

## Deploy (host, not gateway image)
Dispatch tooling runs under systemd on the host. After merge, master restarts
`seshat-dispatch-orchestrator` + `seshat-gating-watcher`. No gateway rebuild. Existing warm seats
pick up the env var only on their **next (re)launch** (a cc-sessions restart or an absent→create) —
note in handoff; the Part-2 detector covers the interim/recurrence.

## Out of scope / follow-ups
- Setting the env var in **cc-sessions** (seat creation, separate `~/cc-env` repo) would cover
  manually-created seats too — note as a follow-up; AC-1 scopes to orchestrator-launched seats.
- Re-notify cadence for a very long-lived wedge (v1 pings once + logs every tick) — note only.
