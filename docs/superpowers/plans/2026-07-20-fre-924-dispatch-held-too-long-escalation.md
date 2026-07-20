# FRE-924 — Orchestrator holds a surfaced/failed dispatch indefinitely without escalating its age

**Ticket:** FRE-924 (Approved, Tier-1:Opus, stream:build2, Urgent). Backing incident: FRE-920's
dispatch hit `delivery-failed`; the orchestrator surfaced a manual card and then re-emitted
`card-already-surfaced` (a `hold` decision) **every tick for ~2.5 h** — a per-tick warning, but
nothing turned "held" into "surfaced to the owner as demanding attention." It only came to light
because the owner noticed an idle stream and asked. Sibling to FRE-922 (RC-busy-while-idle wedge
detector) — a *different* stuck-state the wedge detector does not watch — and to FRE-923 (the
delivery-atomicity half). This is the **detection/escalation half**.

## Root cause (in code)

`_decide_surfaced` returns `StreamDecision(kind="hold", reason="card-already-surfaced")` for every
tick a surfaced record is still the stream's NEXT. In `_apply`, `hold` falls into the `case _:`
no-op — so the only signal is the per-tick `dispatch_decision`/`card-already-surfaced` info log.
There is no age threshold that escalates a long-held card.

## Design decision — age-based threshold, in-memory one-shot latch

Two choices, both taken from the ticket's own "What":

1. **Threshold = wall-clock age, not a tick count.** The `surfaced` record already carries
   `launched_at` (set to `now` at first-surface in `_record_for_result`, and *not* replaced by
   `_decide_surfaced`, so it stays pinned to first-surface time). Age `= now - launched_at`
   directly measures "held this long" — the thing the incident was about (2.5 h) — and is
   cadence-independent (unlike FRE-922's tick counter, whose signal is a genuinely per-tick RC/pane
   probe with no durable timestamp, so a counter is the right model *there* but not here). The ticket
   explicitly points at `launched_at`.

2. **One-shot = in-memory latch (`dict[str, str]`, stream → the ticket already escalated this
   episode), reset on any non-`hold` decision — never persisted.** This is the FRE-922 lesson
   applied: a persisted counter can be first-observed *above* its crossing value after a restart /
   config change / crash and silently lose the single alert forever. An in-memory latch starts empty
   each run; the first tick past the threshold escalates and latches; the episode ends (owner acts →
   `clear`, or the stream moves on) → the latch is dropped so a *later* surfaced hold is a fresh
   episode that can escalate again. The latch is keyed by **ticket, not just stream** (a `dict` value
   rather than a `set` member): if the surfaced ticket on a stream is swapped for a different one
   while staying in `hold` (only reachable via external state surgery — the normal path emits `clear`
   first), the new ticket still escalates. Escalation is thus **exactly once per (stream, ticket)
   hold-episode within one daemon run**; a hold outliving a restart re-escalates once on the next
   tick (age already past threshold) — at-least-once across restarts, by design, and arguably
   desirable (a 2.5 h-stuck card should re-announce itself after a daemon restart).

**Threshold value:** `DEFAULT_HELD_ESCALATION_S = 1800.0` (30 min), CLI-overridable via
`--held-escalation-timeout` (mirrors `--stall-timeout`). Distinct from the stall timeout (1 h): a
stall is a *launched* run yielding no PR (a long Opus build is normal → generous); a held card is a
*surfaced* card awaiting the **owner**, where 30 min is a fair window to act promptly (AC-3) yet far
short of 2.5 h. The escalation is a one-shot ping, so even a mistimed fire is a single actionable
notification, never spam.

## Changes — `scripts/dispatch/orchestrator.py` only

1. New constant `DEFAULT_HELD_ESCALATION_S: float = 1800.0` beside `DEFAULT_WEDGE_TICKS`, with a
   comment citing FRE-924 / the incident and the in-memory-latch rationale.
2. `run_once` gains `held_escalated: dict[str, str] | None = None` and
   `held_escalation_s: float = DEFAULT_HELD_ESCALATION_S` (both defaulted → existing callers/tests
   unaffected). `if held_escalated is None: held_escalated = {}`. Threaded into `_apply`.
3. `_apply` gains `held_escalated: dict[str, str]`, `held_escalation_s: float`. At the top, alongside
   the existing wedge reset, add: `if decision.kind != "hold": held_escalated.pop(stream, None)` —
   any non-hold decision ends the episode (the wedge counter's `launch` special-case does not apply:
   a `launch` is never a `hold`, so it resets like every other non-hold decision).
4. New `case "hold":` before `case _:` — fetch the record; if present, call `_note_held(...)`.
   `case _:` still catches `await`/`skip`.
5. New helper `_note_held(stream, ticket, held_escalated, *, now, launched_at, held_escalation_s,
   trace_id, notifier, logger)`:
   - `age = max(0.0, now - launched_at)` (clamp: a future/corrupt `launched_at` never logs a negative
     `held_seconds` — it reads as freshly-surfaced, the conservative choice). Return early if
     `age <= held_escalation_s` (within threshold — AC-3) **or** `held_escalated.get(stream) ==
     ticket` (this (stream, ticket) already escalated this episode — AC-1 one-shot).
   - otherwise `held_escalated[stream] = ticket`, then emit **both** a greppable
     `logger.warning("dispatch_held_too_long", ...)` **and** one `notifier("dispatch_held_too_long",
     ...)` ping — both carry `stream`, `ticket`, `held_seconds` (rounded), `trace_id`. Unlike the
     wedge (warns every post-threshold tick, pings once), the held escalation emits **both once** —
     AC-1 says "exactly one `dispatch_held_too_long` escalation"; the *per-tick* trail is the existing
     `card-already-surfaced` info log, left untouched.
   - **No state mutation, no `state.pop`, no termination command** anywhere on this path (AC-2).
6. `main()`/`tick()`: own an in-memory `held_escalated: dict[str, str] = {}` across the loop (beside
   `wedge_counts`); add `--held-escalation-timeout` CLI arg (`type=float`, default
   `DEFAULT_HELD_ESCALATION_S`); pass `held_escalated` + `held_escalation_s` into `run_once`.
7. Docstring updates: `run_once`/`_apply` Args; module-level note is not required (the helper
   docstring carries the rationale).

## Codex plan-review revisions (incorporated)
Verdict **approve-with-changes** (the core model — age-based threshold, in-memory latch,
reset-on-non-hold, reset placement — confirmed sound against source). Incorporated:
- **Latch keyed by (stream, ticket), not stream alone** (`dict[str, str]`): closes the
  replacement-ticket suppression gap (surfaced ticket A→B on one stream while staying in `hold`,
  reachable only via external state surgery — the normal path emits `clear` first). Reset-by-stream
  stays a clean `pop(stream, None)`.
- **Clamp `age = max(0.0, now - launched_at)`**: a future/corrupt `launched_at` (clock skew, manual
  state edit) never yields a negative `held_seconds`; it reads as freshly-surfaced (no premature
  escalation).
- **Restart semantics stated plainly**: exactly-once *per (stream, ticket) episode within a daemon
  run*; at-least-once across a process restart (in-memory latch, by design — mirrors `wedge_counts`).
  Named in the helper docstring and a test.
- **Added tests**: duplicate-stream de-dupe (mirrors the wedge regression), replacement-ticket
  re-fire, and the `>`/`<=` boundary at exactly the threshold.

## Tests (TDD — one outcome-level assertion per AC), `tests/scripts/test_orchestrator.py`

A `_run_held(...)` helper mirroring `_run_wedge`: pre-seed `state = {"build1":
_launched_record(phase="surfaced", launched_at=0.0)}`, board `[_issue("FRE-786", "Approved", _OPUS |
{"context:keep"})]` (so `_decide_surfaced` → still-NEXT → `hold`), a plain `_RecordingRunner` (hold
never shells tmux/gh), a shared `held_escalated` set, capturing notifier + logger. Run N ticks at a
chosen `now`.

- **AC-1** (`test_held_card_escalates_once_past_threshold`): run several ticks with `now` past
  threshold → exactly **one** `dispatch_held_too_long` notifier event **and** exactly **one**
  warning log; both carry `stream="build1"`, `ticket="FRE-786"`. Distinct from the per-tick
  `card-already-surfaced` `dispatch_decision` info (unaffected).
- **AC-2** (`test_held_escalation_never_mutates_or_kills`): after escalation, the record is still in
  `state` and unchanged (same `launched_at`, same `phase="surfaced"`), and `_no_termination_argv(runner)`
  holds (reuse the FRE-922 helper).
- **AC-3** (`test_fresh_surfaced_card_within_threshold_does_not_escalate`): run several ticks with
  `now` **within** threshold → **zero** `dispatch_held_too_long` events/logs; latch stays empty.
- **Per-episode re-arm** (`test_held_escalation_re_fires_on_a_new_episode`): escalate once; then a
  tick where the owner acted (board flips the ticket off `Approved` → `clear`) drops the latch; a new
  surfaced card aged past threshold escalates again — proves the latch is per-episode, mirroring the
  FRE-922 re-arm test.
- **Replacement-ticket re-fire** (`test_held_escalation_re_fires_when_surfaced_ticket_changes`):
  card A escalates; state is swapped in place to a surfaced card B (still `hold`, no intervening
  `clear`); B escalates once — proves the `(stream, ticket)` keying (codex finding #3).
- **Duplicate streams** (`test_duplicate_streams_do_not_double_escalate_held`): a repeated
  `--streams` value escalates a held stream once per tick, not once per repeat (mirrors the wedge
  de-dupe regression).
- **Boundary** (folded into AC-3): at `age == threshold` exactly → no escalation (`age <= threshold`
  returns); one second past → escalates. Pins `>` vs `>=`.

## Quality gates
`make test-file FILE=tests/scripts/test_orchestrator.py` → module green; then `make test` · `make
mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`. Self-review via the
**code-review** skill at `low`–`medium` (single-file, localized decision-loop change, no
schema/security/cost/network surface) before the PR.

## Deploy (host, not gateway image)
Same class as FRE-922: after merge, master restarts `seshat-dispatch-orchestrator` (host systemd).
No gateway rebuild. The systemd `ExecStart` passes no `--held-escalation-timeout`, so the 30-min
default applies on restart. The in-memory latch starts empty on restart (by design); any card
already held past threshold re-escalates once on the first post-restart tick.

## Out of scope / follow-ups
- FRE-923 (delivery atomicity — the partial-clear-send strand) is the sibling half, separate PR.
- Re-notify cadence for a very long-held card (v1 escalates once per episode) — note only; the
  per-tick `card-already-surfaced` trail remains the continuous signal.
