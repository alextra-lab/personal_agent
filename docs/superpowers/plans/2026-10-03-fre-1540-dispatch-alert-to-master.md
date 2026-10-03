# FRE-1540 — Dispatch alerts that reach master

Ticket: FRE-1540 (Approved, Tier-2). Related: FRE-1405 (notify ledger), FRE-1538 / PR #1194 (the
2026-10-03 case), ADR-0116 (channel). Tier: **Standard** (multi-file behaviour in the dispatch
daemons, no `src/`, no schema, no cost). Codex plan-review: required.

## Problem

Two failures reach nobody:

- The watcher retries a worker trigger every minute. Each try fails (`channel_delivery_failed`,
  then `busy`). Nothing escalates. 155 attempts on 2026-10-03.
- The orchestrator writes `dispatch_*` conditions to `telemetry/dispatch_notify_ledger.json`.
  Only the master prime step reads that file. 4,119 ticks of `dispatch_seat_wedged` went unseen.

The watcher reaches `cc-master` reliably. Master can send the owner a push notification.

## Design

**D1. Message form.** A plain one-line prefix: `[DISPATCH ALERT]`. The PR trigger is `/master <n>`.
A slash command would need a new command file and could collide with the skill name. A prefix that
does not start with `/` cannot invoke a skill by mistake. The message states its own instruction, so
it works even if the skill is not loaded. One line only: `send-keys -l` then `Enter` would submit a
newline early.

**D2. Where the clock lives.** `LedgerEntry.created_at` becomes "when the current unresolved episode
began". Two writers reset it today:

- `record_pending` replaces an abandoned entry on every retry.
- `record_surfaced` replaces the notify entry on every renotify.

Both will carry `created_at` and `alerted_at` forward from the previous entry. Three new fields:

| Field | Meaning |
|-------|---------|
| `attempts: int = 0` | Delivery attempts in this episode (worker triggers). |
| `last_failure: str = ""` | Why the latest attempt did not deliver. |
| `alerted_at: float \| None = None` | When master was alerted. Set once. |

`record_pending` carries from an entry that is consumed with `sent_at is None` (abandoned):

- `alerted_at` always carries. A restart of any length must not repeat an alert (codex finding 1).
- `created_at` and `attempts` carry only when `consumed_at` is within `EPISODE_GAP_S` (1800 s).
  An older gap restarts the clock.
- A sent entry starts a new episode with nothing carried.

`record_surfaced` carries `created_at` and `alerted_at` from an entry with `consumed_at is None`.

**D3. Watcher.** After each failed attempt of a `kind == "worker"` trigger the watcher records
`last_failure`, then checks `alerted_at is None and now - created_at >= 900`. If due, it sends one
alert to `cc-master` with the existing `send_to_session` (idle-gated). On `sent` it calls
`mark_alerted` and persists. On `busy` or `absent` it logs `gating_alert_deferred` and retries on
the next tick, because the failing trigger runs again each tick. Unroutable worker triggers (no
session) take the same path. The record and the due check run on every tick, **before** the 6 h
log-suppression `continue` (codex finding 2). One abandoned entry is recorded and consumed in a
single persist, so `reconcile` never sees an unconsumed entry with no command.

Reason text: the transport failure first when there is one, then the outcome. Examples:
`channel_delivery_failed+busy`, `busy`, `absent`, `unroutable`.

Crash window: send, then persist. A crash between them can repeat one alert. This is
at-least-once on purpose (same direction as FRE-922/924).

**D4. Orchestrator.** After the per-stream loop in `run_once`, on `execute` ticks with a
`notify_ledger_path`, one pass reads the notify ledger. Each entry with `consumed_at is None`,
`alerted_at is None` and `now - created_at >= 900` gets one alert via `send_to_session`. On `sent`,
`mark_alerted` and `save_ledger`. The orchestrator stays the only writer of that file.

**D5. Shared module** `scripts/dispatch/master_alert.py`: `ALERT_PREFIX`, `ALERT_AFTER_S`,
`is_due(entry, now)`, `format_undelivered_trigger(...)`, `format_notify_entry(...)`. Fields are
collapsed to one line and capped at 200 characters.

**D6. Master skill.** One section in `.claude/skills/master/SKILL.md`, keyed on the prefix. A
contract test pins that the section contains `ALERT_PREFIX`.

## Out of scope

Repair of the `seshat-dispatch` channel (owner decision). Alerts for master-kind triggers (the
watcher already force-delivers those after 30 minutes).

## Steps (TDD: red, then green)

1. `tests/scripts/test_trigger_ledger.py`: failing tests for `mark_alerted`, `mark_failure`,
   `record_pending` carry rules, `record_surfaced` carry rules, save/load round trip of new fields,
   old JSON without the new fields. Then edit `scripts/dispatch/trigger_ledger.py`.
   Run: `make test-file FILE=tests/scripts/test_trigger_ledger.py`.
2. Create `scripts/dispatch/master_alert.py` with its own tests in
   `tests/scripts/test_master_alert.py` (format, one line, cap, `is_due`).
3. `tests/scripts/test_gating_watcher.py`: failing tests AC-1, AC-3 (below). Then edit
   `scripts/dispatch/gating_watcher.py` (`run_once` worker failure branch, unroutable branch).
   Run: `make test-file FILE=tests/scripts/test_gating_watcher.py`.
4. `tests/scripts/test_orchestrator.py`: failing tests AC-2, AC-3. Then edit
   `scripts/dispatch/orchestrator.py` (`run_once` tail pass). Run:
   `make test-file FILE=tests/scripts/test_orchestrator.py`.
5. Master skill section, `tests/scripts/test_dispatch_skill_contracts.py` guard,
   `docs/runbooks/dispatch-orchestrator.md` subsection.
6. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`. Commit. Self-review on `git diff origin/main...HEAD`.

## Acceptance criteria and proof

| AC | Proof (test, outcome asserted) |
|----|-------------------------------|
| AC-1 | Watcher, busy seat, ticks every 60 s from 0 to 1200 s: exactly one `send-keys -l` text to `=cc-master:0.0` starting `[DISPATCH ALERT]` that names PR #412, `cc-2build`, the reason, the first-attempt time and the attempt count. No alert before 900 s. Second test: seat idle at tick 5, trigger delivered, no master text over 1200 s. Variants: channel failure reason; unroutable; master busy at due time (deferred, then one delivery). |
| AC-2 | Orchestrator, notify entry seeded at t=0: no alert at 899 s, one alert at 900 s naming source, stream, ticket, question; none at 1200 s. Entry consumed at 600 s: no alert. A renotify after the alert neither re-alerts nor resets the clock. Dry run sends nothing. |
| AC-3 | Watcher: ledger saved and reloaded from disk between ticks, once before the alert (clock survives) and once after (no second alert). Orchestrator: fresh `run_once` on the reloaded file sends nothing for an alerted entry. |
| AC-4 | Live, master-owned. Runbook in the handoff comment. |
