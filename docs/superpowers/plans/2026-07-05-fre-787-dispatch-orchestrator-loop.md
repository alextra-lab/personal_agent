# FRE-787 — Dispatch orchestrator loop (`scripts/dispatch/orchestrator.py`)

**Ticket:** FRE-787 (Approved, In Progress, Tier-1:Opus, stream:build1, **context:keep** — built on the warm
FRE-786 launcher context)
**Backing ADR:** ADR-0110 §2 (dispatch orchestrator: resolve → launch → advance), §4 (completion fallback).
**Carries AC:** AC-4 (outcome = open PR + In Review, never merge/deploy/close), AC-5 (pytest-lock hook stays
live; no double-dispatch, no hook-stripping), AC-7 part b (advance only on durable PR+In-Review, never on
silence; stall path on no-PR). Plus the assembled seam (master-owned live verification).
**Integrates:** `next_resolver.py` (T1) + `launcher.py` (T2), both shipped.

## Design — pure `decide` + injected IO seams (mirror resolver/launcher)

`scripts/dispatch/orchestrator.py`, stdlib + `structlog` (the ADR mandates "structlog with trace_id"),
frozen dataclasses, Google docstrings, argparse CLII with `main(argv) -> int`. **`--once` runs a single tick
(dry-run: plans, no launch); `--loop` runs the systemd daemon tick loop; `--execute` actually launches.**
No `src/` change — dev-process tooling under `scripts/`. The systemd unit + runbook are **FRE-788 (T4)**, not
this ticket.

### State

```
@dataclass(frozen=True)
DispatchRecord:
    stream, ticket: str
    phase: Literal["launched", "surfaced"]   # launched = an owned in-flight session; surfaced = a manual card shown
    launched_at: float
    session_id: str | None
    run_confirmed: bool = False   # the run delivered a PR (reached In-Review + open-PR) — stop stall-watching
    stall_notified: bool = False
```

One record per stream the orchestrator is tracking. **`phase` is the fix for codex #1** — a record is written
**only for an outcome that actually owns or surfaces work**, and the two phases are never conflated (a manual
card is never mistaken for an owned in-flight build awaiting a PR). Persisted to `telemetry/dispatch_state.json`
(gitignored) so the daemon survives restart; read on start, and **written atomically (temp + `os.replace`)
after every stream action within a tick — not just at tick end** — so a crash immediately after a launch cannot
lose the record and re-launch (codex #2, closes the Approved-before-In-Progress window). `decide` stays pure
over an injected record; persistence is an injectable `persist(state)` seam (default = the JSON file).

### The pure decision

`decide(stream, issues, record, *, now, stall_timeout_s, tracked_pr_open) -> StreamDecision` where
`DecisionKind = Literal["launch","await","stall","run_complete","clear","skip","hold"]`.

**Two distinct transitions — the owner's refinement.** A PR at In-Review is *at master's gate and can be
bounced*, so In-Review must **not** free the stream. The loop separates: **`run_complete`** = the dispatched
run delivered a PR (In-Review + open-PR) → stop stall-watching, but the stream **stays occupied** (bounce-safe);
**`clear`** = the ticket reached a **terminal merge state** (`Awaiting Deploy`/`Done`/`Canceled`/`Duplicate`)
→ the stream frees for the next dispatch. This is **identical to the current prime-worker busy-guard**: a
stream is occupied through the whole review/bounce cycle and frees only at merge.

**No record** (nothing tracked):
| Condition | kind | rationale (AC) |
|---|---|---|
| `resolve_next` returns a ticket **with a Tier label** | `launch` | idle + NEXT → dispatch intent (run_once executes + records per the matrix below) |
| `resolve_next` returns a ticket **without a Tier label** | `skip` (no-tier) | cannot pick a model — never launch at an unknown tier |
| `resolve_next` returns None | `skip` (occupied/no-candidate) | busy guard or empty queue |

**Record present, `phase="launched"`** (an owned in-flight session) — `state` = the tracked ticket's board state:
| Condition | kind | rationale (AC) |
|---|---|---|
| `state` ∈ terminal (`Awaiting Deploy`/`Done`/`Canceled`/`Duplicate`) | `clear` | **merge/close landed — stream frees** (identical to now); drop the record |
| `state` == `In Review` AND `tracked_pr_open` AND not yet `run_confirmed` | `run_complete` | run delivered a PR → set `run_confirmed`, **keep record** (still at master's gate, bounce-safe) (AC-7b) |
| `state` ∈ (`In Review`, `In Progress`) — already progressing/confirmed | `await` | at the gate or building; hold (a bounce keeps it In Review — never re-dispatch) |
| not progressing (still `Approved`/unknown) · `now - launched_at > stall_timeout_s` · not `run_confirmed` | `stall` | launched but no PR in time → notify, **keep record, never advance/re-launch** (AC-7b "never on silence") |
| otherwise (within timeout) | `await` | still starting |

**Record present, `phase="surfaced"`** (a manual card was shown — KEEP or manual-model-required):
| Condition | kind | rationale |
|---|---|---|
| tracked ticket left `Approved` (owner acted) OR is no longer the resolved NEXT | `clear` | situation changed — drop the record so the stream re-evaluates |
| otherwise | `hold` | card already surfaced; do nothing (throttles manual-card spam across ticks) |

`resolve_next` (imported) already encodes the **busy guard** (In Progress/In Review → None), priority order,
and blocked-head skip — so **no-double-dispatch (AC-5/AC-6) holds by construction**: an occupied stream never
yields a launch, and a `launched` record prevents re-launch of an in-flight ticket even before the worker flips
it to In Progress (closes the stall-relaunch gap). The stream frees for the next NEXT **only at the terminal
merge state**, never at In-Review — so a master bounce during review can never trigger a premature re-dispatch.

### run_once action matrix — LaunchResult → record (codex #1/#7)

On a `launch` decision, run_once calls `plan_launch(...)`, and under `--execute` `execute_plan(...)`, then:
| launcher outcome (`LaunchResult`) | record written | notes |
|---|---|---|
| `launch` / `prepare` (`launched=True`) | **`phase="launched"`**, `launched_at=now` — persisted immediately | an owned in-flight session; await the durable signal |
| `manual-continuation` (KEEP) · `manual-model-required` | **`phase="surfaced"`** | log the card once; never a machine launch; `hold` until the owner acts (`clear`) |
| `worktree-dirty` · `launch-failed` | **no record** | log the failure; stream stays eligible to retry next tick (a dirty tree may clean; a transient tmux failure may clear) |
| dry-run (no `--execute`) | **no record** | just prints the decision/plan — the AC-4/AC-7b inspection surface |

This is codex's required action matrix: a record is written **only** for outcomes that actually launch/prepare
or surface a manual card — never for a `launched=False` error outcome — so the orchestrator can never write a
false in-flight record that wedges a stream.

`model_for_labels(labels) -> str | None`: `Tier-1:Opus→opus`, `Tier-2:Sonnet→sonnet`, `Tier-3:Haiku→haiku`,
else `None` (→ `skip`, never a guessed tier).

### IO seams (all injectable for tests)

- **board fetch** — `next_resolver.fetch_board(stream, key)` (Linear GraphQL). 
- **launch** — `launcher.plan_launch(...)` then, under `--execute`, `launcher.execute_plan(...)`. The
  orchestrator passes the resolved model + `context_keep`; it **never adds a hook-stripping flag**
  (`--safe-mode`/`--bare`) — the launcher's command has none, and a unit test asserts the launched argv
  contains neither, so the pytest-lock PreToolUse hook stays live (AC-5).
- **completion probe** — `_open_pr_exists(ticket, runner) -> bool` via `gh pr list --search <ticket> --state
  open --json number` (mirrors reconcile_board's `check_merged_pr` gh pattern); combined with the tracked
  ticket's board state == `In Review` for the durable signal.
- **notifier** — `Notifier` protocol (`__call__(event, **fields)`); default logs a `structlog.warning`. The
  stall path calls it once per stall (throttled by `record.stall_notified`). A richer push-notification
  channel is a T4/ops concern.
- **logger** — `structlog.get_logger()`, every emit carries a per-tick `trace_id` (uuid4 string) — ADR-0074
  identity threading on new log sites.

### The loop

- `run_once(streams, state, *, now, key, stall_timeout_s, launch_fn, pr_probe, notifier, logger, execute)
  -> dict[str, DispatchRecord]`: for each stream — fetch board, resolve `tracked_pr_open` for any record,
  `decide`, then act (launch via seam / notify / clear record / no-op) and return the new state. Fully
  unit-testable with fakes; **the only wall-clock/network is injected**.
- `main`: wire real seams, load/save the state file, and either run one tick (`--once`) or loop with
  `time.sleep(interval)` (`--loop`). `--once` without `--execute` is a dry-run that prints each stream's
  decision — the inspection surface for AC-4/AC-7b.

### Boundary (AC-4)

The orchestrator has **no merge/deploy/close code path** — it only reads Linear, launches sessions, and
advances its own record. After a dispatched run the durable outcome is an open PR + the ticket at In Review
(set by the GitHub integration, not the orchestrator). A unit test asserts `run_once` never calls any
merge/deploy/close seam (there is none) and that `advance` only clears the record — it never mutates `main`,
Linear beyond reads, or the PR.

## Steps

1. **Failing tests** `tests/scripts/test_orchestrator.py` (mirror resolver/launcher test style). Cover:
   - `decide` LAUNCH: idle board with an Approved+labeled NEXT (no record) → `launch`, model from tier,
     `context_keep` from label. **(dispatch)**
   - `decide` SKIP occupied: board with an In Progress ticket on the stream → `skip` (busy guard). **(AC-5/6)**
   - `decide` SKIP no-tier: NEXT lacks a `Tier-*` label → `skip` (reason no-tier), never a launch at an
     unknown model.
   - `decide` AWAIT: launched record, ticket In Progress, within timeout → `await`.
   - `decide` RUN_COMPLETE: launched record, ticket In Review, `tracked_pr_open=True`, not yet confirmed →
     `run_complete` (sets `run_confirmed`), record **kept** (stream still occupied). **(AC-7b)**
   - `decide` CLEAR-on-merge: launched record, ticket **Awaiting Deploy** (terminal) → `clear` — the stream
     frees only at merge, not at In-Review. **(owner refinement — identical to now)**
   - `decide` bounce-safety: launched record, ticket In Review (a bounce keeps it there) → `await`, **never**
     `clear`/`launch` — the stream stays occupied through the review/bounce cycle. **(owner refinement)**
   - `decide` STALL: launched record, ticket still Approved, `tracked_pr_open=False`, past timeout, not
     confirmed → `stall`, **not** run_complete/clear. **(AC-7b "never on silence")**
   - `decide` no-complete-on-silence: launched record, past timeout, no PR, ticket not In Review → never
     returns `run_complete` or `clear`. **(AC-7b)**
   - `decide` surfaced HOLD: record `phase="surfaced"`, ticket still the Approved NEXT → `hold` (no re-card).
   - `decide` surfaced CLEAR: record `phase="surfaced"`, ticket left Approved (owner acted) → `clear`.
   - `model_for_labels` for each tier + the no-tier `None`.
   - `run_once` LAUNCH path (fake launch_fn returning `launched=True`): launch called once with the resolved
     model/context; a `phase="launched"` record is written **and persisted immediately**; the launched argv
     (from the real launcher) contains **no** `--safe-mode`/`--bare`. **(AC-5 no hook-strip; codex #2)**
   - `run_once` manual outcome (fake returns `manual-continuation`/`manual-model-required`): a `phase="surfaced"`
     record is written, **not** `launched` — the stream is not treated as an owned in-flight build. **(codex #1)**
   - `run_once` error outcome (fake returns `worktree-dirty`/`launch-failed`): **no record** written; stream
     stays eligible next tick. **(codex #1 — no false in-flight record)**
   - `run_once` restart idempotency: a state with a `phase="launched"` record for the stream → `decide` yields
     `await`/`advance`/`stall` (never a second `launch`), proving a crash-restart never re-launches. **(codex #2)**
   - `run_once` never double-dispatches: given an occupied stream (busy guard), launch_fn is **never** called.
     **(AC-5/6)**
   - `run_once` CLEAR (terminal-merge) drops the record and calls **no** merge/deploy/close seam (none
     exists); `run_complete` keeps the record. **(AC-4)**
   - `run_once` STALL calls the notifier once (throttled by `stall_notified`) and does **not** clear the record
     or launch. **(AC-7b)**
   - `_open_pr_exists` parses a fixture `gh pr list` payload (match / no-match).
   - `main(["--once"])` dry-run over a fixture board (monkeypatched fetch) → exit 0, prints a decision per
     stream; asserts no launch and **no record persisted** without `--execute`.
   Run: `make test-file FILE=tests/scripts/test_orchestrator.py` → **fail** (no module).

2. **Implement** `scripts/dispatch/orchestrator.py`. Re-run file test → **all pass**.

3. **Doc touch**: module docstring documents the decision table, the durable-completion/stall contract, and
   that RC programmatic completion (mechanic c) is a deferred latency optimization (Open question 1). No
   README/runbook here (that is FRE-788).

4. **Quality gates**: file test → `make test` (full) → `make mypy` → `make ruff-check`/`ruff-format` →
   `pre-commit run --all-files` (incl. ADR-0074 identity threading, since this uses structlog).

5. **PR** off latest `origin/main`; ticket handoff to master with the AC-proof table + the seam division
   (unit-proven vs the master-owned live assembled seam).

## Acceptance-criteria proof (this PR vs the seam)

| AC | Proven in this PR (unit/dry-run) | Deferred to the assembled seam (live, master-owned) |
|---|---|---|
| AC-4 | orchestrator has no merge/deploy/close path; `advance` only clears the record; dry-run shows the outcome contract | one live dispatch ends at an open PR + In Review, `main` log unchanged by the orchestrator |
| AC-5 | no launch into an occupied stream; launched argv carries no hook-stripping flag | live two-worker run: single pytest process, hook logs the block, no `--safe-mode`/`--bare` |
| AC-7b | advance only on In-Review + open-PR; stall path on no-PR past timeout; never advances on silence | live: force RC completion off, confirm advance on the durable signal only |

The ADR assigns the **assembled seam** (resolve → launch → owner-monitored run → end-at-PR → advance, once
per stream) to **master**; this PR delivers the loop + its decision logic, fully unit-proven, plus a `--once`
dry-run for inspection.

## Codex plan-review outcome (2026-07-05): **revise before build — 2 must-fix, both folded in above**

1. **Launcher-outcome × record** — a record is now written **only** for real launch/prepare (`launched`) or a
   manual card (`surfaced`); a `launched=False` error outcome (`worktree-dirty`/`launch-failed`) writes **no
   record**. The explicit action matrix + `phase` field close the false-in-flight-record wedge. Tests added.
2. **Persistence/idempotency** — the `launched` record is persisted atomically (`os.replace`) **immediately
   after a successful launch**, and a restart-idempotency test proves a re-loaded `launched` record never
   re-launches. Closes the Approved-before-In-Progress crash window.
Codex confirmed as correct: the durable PR+In-Review completion signal (#2), the AC-5 unit/seam split (#3),
the stall false-positive tolerance (#4), the RC-completion deferral (#5), and structlog use (#6, using
`structlog.get_logger(__name__)`). Nothing over/under-built once the matrix is specified (#7).

## Owner decisions (2026-07-05 — approval gate answered)

1. **Completion / stream-free signal — RESOLVED by the owner's refinement.** Do not free the stream at
   In-Review (a PR can be **refused/bounced** by master). Advance to the next dispatch on the **merge/closed
   ticket** — *identical to the current prime-worker loop*. Folded in above as the `run_complete` (In-Review +
   PR, stop stalling) vs `clear` (terminal merge, free stream) split; the stream stays occupied through the
   review/bounce cycle. RC programmatic completion (`claude agents --json`) stays deferred (latency
   optimization only).
2. **Stall notifier** = log + pluggable notifier (throttled `structlog.warning`); concrete device push is
   FRE-788. **Confirmed.**
3. **State file** = `telemetry/dispatch_state.json` (gitignored). **Confirmed.**
