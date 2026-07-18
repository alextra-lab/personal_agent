# FRE-913 — Restore persistent worker seats

**Ticket:** FRE-913 (Approved, Urgent, Tier-2) · design review PASSED by owner 2026-07-18
**Backing:** no ADR (owner ruling 3 — restores pre-2026-07-08 behaviour, not new architecture)
**Related:** FRE-909 (exact tmux targets) · FRE-911 (permission mode) · FRE-912 (session-id lock collision)
**Branch:** `fre-913-persistent-worker-seats`

## Objective

A dispatch must **prepare** a seat, never **destroy** one. Today `execute_plan` unconditionally
`kill-session`s the seat and immediately recreates it, churning the seat's Remote Control (RC)
registration on every dispatch and costing the owner mobile visibility.

## Discovery — three findings that shape the design

### F1. The channel cannot deliver slash commands (corrects ticket step 4)

Ticket step 4 says delivery "uses the existing ADR-0116 channel path where the seat is
channel-mode, falling back to send-keys". **This is not implementable as written.**

`webhook.mjs` injects a channel POST as an MCP notification rendered as a
`<channel source="seshat-dispatch">` tag — *text the seat reasons over*. It is not a keystroke pipe.
`/clear` and `/model` are Claude Code **client** commands interpreted by the TTY input handler;
delivered as channel text they are inert prose, not executed commands. `/clear` in particular can
only be actuated by a keystroke — nothing in the channel path can reset a conversation.

**Resolution:** the prepare path is **send-keys for all three commands, regardless of the seat's
`mode`.** The channel remains what ADR-0116 built it for — the watcher's structured gating events
(PR CI state the seat reasons over). This is a correction to the ticket's step 4, not a scope
change; every other property step 4 asks for (ordering, exact-pane targets) is preserved.

### F2. RC registration silently falls back to a different name — the regression, observed live

While building this ticket, `claude agents --json --all` on the VPS reported this very seat as:

```
{"pid": 1153456, "cwd": "/opt/seshat/.claude/worktrees/build",
 "sessionId": "c77b0d9f-...", "name": "build-41", "status": "busy"}
```

The seat was launched as `--remote-control cc-build`, and its tmux session **is** `cc-build`, but it
registered under RC name **`build-41`**. The requested name was still held by the not-yet-released
prior registration, so RC allocated a fallback name. The process is alive and working; it is simply
**not where the owner's mobile view looks for it.**

This is direct live evidence for the ticket's thesis, and it sharpens AC-4:

> **Registration verification must assert the seat registered under the REQUESTED name.**

`find_warm_session` matches by **cwd**, so it would happily match `build-41` and report success —
verifying "an agent exists for this worktree" is exactly the check that cannot detect this failure.
The create path verifies `name == topology.tmux_session`.

### F3. `session_is_idle` is needed in the launcher, but the import direction forbids it

`gating_watcher` imports `launcher` at module level (line 100), so `launcher` cannot import
`gating_watcher` back. The idle heuristic is a pure text function with module-level constants →
extract to a new `scripts/dispatch/pane_state.py`; `gating_watcher` imports it from there. No
behaviour change, and its existing tests keep passing against the same function.

## Design

### THE INVARIANT — the dispatcher owns no termination code (owner-directed 2026-07-18)

> "Dispatcher should not have terminate tmux code. If it does, remove it. Tmux terminals and the cc
> sessions running inside MUST NOT be terminated — or we lose the whole reason they are valuable as
> long running sessions: we can use a warm context."

This is stronger than the ticket's "no dispatch destroys a seat", and it supersedes it. The launcher
does not merely *avoid* killing on the happy path — it **loses the capability entirely**. Audit: the
dispatch tree contains exactly **one** termination call, `launcher.py:614`. It is deleted and no
`kill-session`, `kill-pane`, `kill-server`, or `respawn-pane` replaces it, on any path.

**Why capability-removal beats a guarded call.** FRE-909's incident was a kill that resolved to the
*wrong* seat and destroyed a live worker mid-build. A guard is only as good as its targeting; code
that cannot kill cannot kill the wrong thing. This is enforced by a source-level test (below), so the
property survives future edits rather than living in a comment.

**Consequence — seat lifecycle is not the dispatcher's job.** `cc-sessions` is the seat manager and
owns create/reset/recover. The launcher only *dispatches into* seats. Where the two previously
overlapped is exactly where the regression came from (the ticket's own archaeology: two tools doing
the same operation, one knowing about Remote Control and one not).

### Seat state (ticket step 2)

```python
def seat_state(topology, runner) -> SeatState:
    if runner(["tmux", "has-session", "-t", exact_session(...)]).returncode != 0:
        return "absent"                      # nothing exists → safe to create
    cmd = runner(["tmux", "list-panes", "-t", exact_pane(...),
                  "-F", "#{pane_current_command}"]).stdout.strip()
    return "live" if cmd == "claude" else "unhealthy"
```

Only two actions follow, and **neither destroys anything**:

| state | meaning | action |
|---|---|---|
| `live` | seat exists, `claude` running | **reuse** — deliver in-session; process never touched |
| `absent` | no tmux session at all | **create** — `new-session` cannot collide with nothing |
| `unhealthy` | session exists, `claude` not running | **surface a card. Touch nothing.** |

The draft had a third `dead` branch that killed a stale shell before recreating. **Removed** — that
was termination code, and it is the owner's rule that forbids it. An `unhealthy` seat is reported to
the owner and recovered with `cc-sessions`, which is the tool that owns that job. This also resolves
codex #4 more cleanly than my trichotomy did: the false-dead failure mode cannot exist if nothing is
ever killed.

Verified live on the VPS: our real launch shape — `env SESHAT_CHANNEL_PORT=… claude --remote-control
cc-build …` — reports `pane_current_command` as `claude` (the `env` prefix execs away), so `live` is
correct for every seat the launcher actually creates.

Exact targets throughout (FRE-909).

### `plan_launch` stays pure; liveness is injected (ticket step 5)

`plan_launch` gains `seat: SeatState` (`"live" | "absent" | "unhealthy"`). The caller probes; the
planner decides. This keeps the planner side-effect-free and unit-inspectable — the
property the module is built around — while still letting the plan itself name reuse-vs-create.

`PlanOutcome` gains `"reuse"`. New `LaunchPlan.deliveries: tuple[str, ...]` carries the ordered
in-session commands (empty for every non-reuse outcome). Decision table:

| context | seat state | outcome | deliveries | creates a process? |
|---|---|---|---|---|
| CLEAR | live | `reuse` | `/clear`, `/model <m>`, `/build <T>` | no |
| CLEAR | absent | `launch` / `prepare` | — | yes (create path) |
| CLEAR | unhealthy | `seat-unhealthy` | — | **no — never destroys, never creates** |
| KEEP | any | `manual-continuation` | — | no (**unchanged**) |
| CLEAR, `model_set` off | — | `manual-model-required` | — | no (unchanged) |

**KEEP stays `manual-continuation` — reverted after codex review.** The draft made KEEP on a live
seat automatic. Codex pushed back and is right: `launcher.py:482` documents the contract as "KEEP is
never machine-auto-launched", and `orchestrator._record_for_result` maps `manual-continuation` →
`surfaced` (owner-gated) vs `launch` → `launched` (owned, in-flight, stall-watched). Auto-KEEP
therefore removes a **human gate** across every worker stream — a contract change, not a fix, and
outside a ticket whose objective is "stop destroying live seats". KEEP costs nothing here: it never
delivered anything before and still doesn't, so a KEEP dispatch trivially cannot restart a seat.
AC-3's KEEP half ("leaves the conversation intact") is satisfied by construction.

**The model switch is unconditional.** Ticket step 4 says "if the ticket's tier differs from the
seat's current model". Reading a live seat's current model has no reliable probe (`claude agents
--json` does not report it). `/model <tier>` is idempotent, so delivering it always is both simpler
and strictly safer than skipping it on an unprovable comparison — never a seat silently on the wrong
tier. Owner ruling 2 already accepts in-session model change as satisfying the contract.

### Delivery (ticket step 3) — idle-guarded, change-confirmed, fail-closed

The draft polled "is the pane idle" after each command. **Codex found that unsound and it is the
most important finding in the review:** `tmux send-keys` only *queues* input, so a capture taken
immediately after sending `/clear` can still show the **old** idle prompt — `session_is_idle` returns
`True` on a pane that has not processed anything yet. The launcher would then type `/build` into a
session about to be cleared, and the queued command dies with the conversation. Silent loss of the
dispatch.

Three corrections:

1. **Idle guard before the first delivery.** If the pane is not idle, the seat is mid-turn → do not
   deliver at all; return `seat-busy`. Mirrors the watcher's existing worker-trigger discipline
   (`gating_watcher.py:842`, "busy returns without sending") so a build mid-turn is never interrupted.
2. **Confirm processing, not mere idleness.** Snapshot the pane before sending; after sending, wait
   for the text to **change** *and then* settle to idle. `/clear` wipes the transcript, so the change
   is unmistakable. "Currently idle" is never accepted as evidence a command ran.
3. **Fail closed on timeout.** The draft said "proceed anyway". Wrong — proceeding is what loses the
   command. On timeout, stop, deliver nothing further, and return `delivery-failed` with
   `launched=False`. Bound: 30 s per command.

`reuse` is only claimed once **`/build` itself** is confirmed delivered — a prepare that clears the
seat and then fails before `/build` must never be recorded as an in-flight run (codex #5).

### Create path (ticket step 4, the exceptional path)

Unchanged command construction — deterministic session id, FRE-911 `acceptEdits`, channel wiring
from `topology.mode`. Added around it, and **only** here:

1. **Wait for the RC name to be free** — poll `claude agents --json --all` until no agent reports
   `name == topology.tmux_session`, bounded (~15 s). Per codex #3 this is **risk reduction, not a
   proof**: the CLI listing is an observable proxy and the allocator may still race. The verify+retry
   in step 4 is the actual safety net, not this poll.
2. `tmux new-session` — reached **only** from `absent`, where there is nothing to collide with and
   nothing to kill.
3. **Verify registration — by identity, not by cwd** (F2). Poll `claude agents --json --all` for an
   agent with `name == topology.tmux_session` **and** `sessionId == plan.session_id`. Codex #3 asked
   that the match prove it is *this* seat rather than a stale agent holding the name; we already pass
   `--session-id`, so identity is exactly checkable. cwd-matching (what `find_warm_session` does) is
   precisely the check blind to the `build-41` failure.
4. On verification failure → `registration-unverified`, `launched=False`, surfaced to the owner.

**AC-4's "retry once" is dropped — deliberately, and this needs master's eye.** A retry means killing
the seat just created and making another, which is termination code the owner's rule forbids. It is
also the wrong reflex: a seat that came up as `build-41` is **alive and working**, merely misnamed —
killing it to chase a nicer name destroys a healthy `claude` process and its warm context to fix a
*visibility* problem. So the launcher reports "seat is up but RC-registered as `build-41`; mobile
visibility degraded" and leaves it running. AC-4's retry clause was written assuming a kill was
available; the owner's later directive removes that assumption. Everything else in AC-4 (create an
absent seat, verify registration, never claim an unproven launch) is delivered.

New `ResultOutcome` members: `"reuse"`, `"registration-unverified"`, `"delivery-failed"`,
`"seat-busy"`, `"seat-unhealthy"`.

### `LaunchPlan` invariants (codex #5)

`command` (create) and `deliveries` (reuse) are two side-effect carriers on one dataclass, so a
`__post_init__` enforces exactly one: `launch`/`prepare` ⇒ `command` set, `deliveries` empty;
`reuse` ⇒ `deliveries` non-empty, `command` `None`; every manual outcome ⇒ neither. An invalid
combination raises rather than half-executing.

### Orchestrator record mapping (codex MISSED-2)

`_record_for_result` gains:

- `reuse` → `launched` (owned in-flight run, same as `launch`/`prepare`).
- `registration-unverified`, `delivery-failed`, `seat-busy`, `seat-unhealthy` → **`surfaced`**, not
  `None`. The draft wrote no record, which leaves the stream eligible and re-dispatches **every
  tick** — a bad create path would churn the seat in a loop. `_decide_surfaced` (`orchestrator.py:397`)
  holds a surfaced record until the owner acts, which is the correct behaviour for a failure needing
  a human. Verified against the existing `_decide_surfaced` → `hold` path.

### Dirty-worktree preflight is retained on the reuse path (codex MISSED-1)

CLEAR reuse keeps `reset_worktree=True`, so `_preflight_worktree` still runs and a dirty worktree
still aborts the dispatch before anything is delivered. The existing guard in `execute_plan` keys off
`plan.reset_worktree`, so this needs no change — but it is a deliberate decision, not an accident:
uncommitted work must not be clobbered, and aborting cleanly beats sending `/build` into a seat whose
Step 0 safety gate would halt anyway.

## Files

| File | Change |
|---|---|
| `scripts/dispatch/pane_state.py` | **new** — `session_is_idle` + constants, extracted (F3) |
| `scripts/dispatch/gating_watcher.py` | import `session_is_idle` from `pane_state` |
| `scripts/dispatch/launcher.py` | `seat_is_live`, `deliver_to_seat`, RC wait/verify, `plan_launch(seat_live=)`, `deliveries`, new outcomes, `execute_plan` branch |
| `scripts/dispatch/orchestrator.py` | map the two new outcomes in `_record_for_result` |
| `tests/scripts/test_launcher.py` | invert the kill test → the regression test; new path tests |
| `tests/scripts/test_gating_watcher.py` | import site for `session_is_idle` |
| `scripts/dispatch/*.md` / runbook | document the persistent-seat lifecycle |

## Test plan (ticket step 6)

Against `tests/scripts/test_launcher.py`'s existing `_RecordingRunner`:

1. **THE regression test** — live seat + CLEAR ⇒ **no `kill-session` argv is ever issued** and no
   `new-session`. Directly replaces `test_execute_kills_existing_slot_before_new_session`.
2. **THE INVARIANT test (structural, two layers).**
   (a) *Behavioural*: across **every** seat state × context × capability combination, no argv issued
   by `execute_plan` contains a tmux termination verb (`kill-session`/`kill-pane`/`kill-server`/
   `respawn-pane`) — a table-driven sweep, not one happy-path assertion.
   (b) *Source-level*: `launcher.py`'s source contains no termination verb at all, so the capability
   cannot be reintroduced by a later edit without the test going red. This is the durable guard for
   the owner's rule.
3. Live seat ⇒ `/build <ticket>` is delivered (AC-2).
4. CLEAR delivers `/clear` (AC-3).
5. KEEP ⇒ `manual-continuation` on every seat state; delivers nothing, kills nothing.
6. Absent seat ⇒ `new-session` issued, **no `kill-session`** (AC-4).
7. `unhealthy` seat (session exists, pane not `claude`) ⇒ `seat-unhealthy` card; **no kill, no
   create, nothing delivered**.
7. Create path verifies by `name` **and** `sessionId`; an agent registering as `build-41` ⇒ retry
   once ⇒ `registration-unverified` when it never registers (AC-4, F2).
8. A stale agent holding the right *name* but a different `sessionId` ⇒ **not** accepted as verified.
9. Every delivery/probe target is exact-match (`=cc-build:0.0` / `=cc-build`) — FRE-909.
10. Delivery order is `/clear` → `/model` → `/build`.
11. Pane busy at dispatch ⇒ `seat-busy`, nothing delivered (no mid-turn interruption).
12. Pane never changes after `/clear` ⇒ `delivery-failed`, `/build` **never** sent, `launched=False`.
13. `LaunchPlan` invariant: a `reuse` plan carrying a `command`, or a `launch` plan carrying
    `deliveries`, raises.
14. `_record_for_result`: `reuse` → `launched`; each failure outcome → `surfaced` (not `None`).
15. `plan_launch` remains pure — no runner calls for any plan-only path.

Commands: `make test-file FILE=tests/scripts/test_launcher.py` · then
`tests/scripts/test_gating_watcher.py`, `test_orchestrator.py`, `test_launcher_channels.py` · then
`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

## Acceptance criteria → proof

| AC | Proof |
|---|---|
| AC-1 no restart of a live seat | Test 1 (no kill/new-session argv). Live: seat pid before == after a dispatch |
| AC-2 work still delivered | Test 2. Live: seat is running the ticket's build |
| AC-3 CLEAR in-session, KEEP intact | Tests 3, 4, 9 |
| AC-4 create path verifies + retries once | Tests 5, 6, 7 |
| AC-5 seat stays visible on mobile | **Owner-only** — cannot be verified by build or master; called out in handoff |

## Risks

- **`/model` mid-session semantics.** Owner ruling 2 accepts it; unverified whether it applies to a
  queued turn. Mitigated by the change-confirmed wait between commands.
- **Blast radius.** This is the machinery that launches every worker. A create-path bug strands a
  stream. Mitigations: the create path stays closest to today's proven construction; the reuse path
  cannot destroy anything by design; every new failure mode maps to `surfaced` (owner-visible, held)
  rather than a silent retry loop.
- **Self-referential dispatch.** Per the owner's build note, dispatching this ticket ran the old
  kill-and-recreate path one final time (and produced finding F2 live).

## Deliberately NOT folded in — follow-up ticket

**Launcher/watcher TTY race (codex #6).** `gating_watcher.send_to_session` injects into the same
pane with the same exact-pane target, so a CI-red worker trigger can interleave with the launcher's
`/clear` → `/model` → `/build` sequence. Codex recommends a per-seat delivery lock.

Not folded in, for three reasons: (a) the race **pre-exists** this ticket and is not worsened by it —
today the launcher *kills* the seat out from under a concurrent watcher send-keys, which is strictly
worse than interleaving with it; (b) both worker triggers already run `require_idle=True`, and the
pane is busy for nearly all of a prepare, so the watcher naturally skips — partial, not airtight;
(c) a cross-process lease between two independent systemd units is a genuinely separate design with
its own failure modes (stale lock ⇒ a permanently undeliverable seat). That is sequenceable work,
which is exactly what the build skill says to ticket rather than fold in. → file Needs-Approval.

---

## Post-implementation corrections

Two rounds of review changed the design after the plan above was approved. Recorded here so the
shipped behaviour, not the intent, is what the document describes.

### Owner directive — the dispatcher owns no termination code

Superseded the plan's `dead` seat branch and AC-4's "retry once". Both required killing a session.
See § THE INVARIANT above; the `dead` state is gone and an `unhealthy` seat is surfaced, never
reclaimed.

### High-effort code review (10 verified defects, all in new code)

| # | Defect | Correction |
|---|---|---|
| 1 | `seat-busy` → `surfaced` **wedged the stream permanently**. It is transient (the seat is mid-turn), but `_decide_surfaced` holds until the owner acts — trading a self-healing delay for a permanent stall. | `seat-busy` writes **no record**; the stream retries next tick. Only `delivery-failed`/`seat-unhealthy` (neither self-clears) surface. |
| 2 | The reuse path **never verified the seat's worktree**. The deleted create path pinned cwd via `new-session -c <worktree>`; reuse silently inherited a guarantee that no longer existed, so a `cc-build` started elsewhere would take a `/build` and commit against the wrong tree. | `seat_state` verifies the worktree — via the RC registry's `cwd`, falling back to `#{pane_current_path}`. |
| 3 | Registration verification **condemned healthy seats when RC was merely unreadable** (`_rc_agents` returned `[]` for both "no agents" and "could not parse"). | `_rc_agents` returns `None` for unreadable; absence of evidence no longer counts as evidence of failure. |
| 4 | `registration-unverified` reported `launched=False` for a seat that **was running and already seeded**, denying it stall detection — and its card told the owner to reset it, i.e. to kill work in flight. | Reports `launched=True` + `launched` record; the card explicitly says *do not* reset until the run lands. |
| 5 | `/clear` on an **already-empty** conversation redraws identically, so strict change-detection condemned a correctly-processed no-op. | An idle-and-unchanged pane is accepted for non-final commands only. |
| 6 | Deleting the teardown removed the only handler for a `new-session` name collision → **unbounded per-tick `launch-failed` loop** if the probe ever misread a live seat as absent. | On create failure, re-probe: a live seat returns `seat-busy` and is reused next tick. Self-healing, no kill. |
| 7 | The final `/build` was confirmed by **pane text change alone** — but `send-keys -l` echoes the command *before* Enter is processed, so a lost `/build` read as a successful dispatch. | The final command is confirmed **only** by the seat going busy (RC's structured `status`). |
| 8 | `pane_current_command == "claude"` misclassified a healthy seat as `unhealthy` whenever a child process held the pane → surfaced → wedged. | RC registration is now the **primary** liveness signal; the pane check is the fallback. |
| 9 | The launcher/watcher TTY race (no mutual exclusion on send-keys). | **Not fixed here** — see § Deliberately NOT folded in. |
| 10 | `execute_plan` blocks the orchestrator tick on real sleeps (up to ~90 s), and `subprocess_runner` had no timeout, so a wedged `tmux` blocked forever. | Poll bounds cut to 8–10 s; `subprocess_runner` bounded at 15 s, reporting a timeout as a non-zero result. |

### Security review

No findings. One hardening adopted: the ticket id — the only externally-sourced value that reaches a
seat's keyboard — is now shape-checked (`^[A-Z][A-Z0-9]*-[0-9]+$`) beside the existing model
allowlist, rather than trusting Linear's API to keep its format.

### Owner-directed fold-ins (beyond FRE-913's objective)

- **Readiness now prefers RC's structured `status`** over scraping the rendered TUI. The scrape has
  no supported contract and has produced both false-busy (FRE-845) and false-idle readings; it
  remains only as the fallback when RC cannot answer.
- **`.claude/hooks/check-pytest-lock.sh` removed** (owner-directed). It matched the substring
  `pytest` anywhere in a command, so it blocked read-only diagnostics during a run. One-pytest-at-a-time
  is now a documented convention. **This leaves ADR-0110's concurrency argument (§ risk table, AC-5)
  referencing a mechanism that no longer exists** — a drift note is recorded in the ADR, but whether
  to amend it, or specify a better guard, is an owner/ADR-session decision, not a build one.
