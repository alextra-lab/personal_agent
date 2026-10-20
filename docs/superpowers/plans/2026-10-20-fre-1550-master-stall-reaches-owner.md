# FRE-1550 — A stall in master's own input reaches the owner

Ticket: FRE-1550 (Approved, Tier-2, un-parked by the owner 2026-10-20). Related: FRE-1540 (alerts to
master), FRE-1556 (send gate), FRE-1555 (master channel). Tier: **Standard** (daemon behaviour, a
network send, a secret). Codex plan-review: required.

## Problem

Two daemons route their "master is stuck" signal to `cc-master`:

- `gating_watcher.py` types `[DISPATCH ALERT]` into `cc-master` for an undelivered worker trigger
  (`_alert_master_if_due`). A held master trigger only logs `gating_trigger_unconfirmed_too_long`.
- `orchestrator.py` types the same alert for a stale notify-ledger entry
  (`_alert_stale_notifications`), and logs `dispatch_alert_deferred master_outcome=busy` every tick.

When `cc-master` is the stuck party (busy, or an unsent draft), `send_to_session` refuses, so the alert
never lands. 2026-10-04: five hours with no signal to the owner.

## Decisions (owner, 2026-10-05 and 2026-10-20)

- **Transport:** an outbound ntfy webhook. A one-shot `claude -p` PushNotification cannot force a send.
- **Setting:** `AGENT_OWNER_NTFY_URL` (URL with topic). Unset by default. Unset means the owner path
  logs a warning and sends nothing. The URL never goes in the repo, a log line or a test fixture.
- **Message:** one short line: what is stuck and the PR or ticket. Timeout. Never blocks the loop.
  A failed send is logged and retried on the next tick. One notification per condition.
- **Tests:** a local fake HTTP server. No call to ntfy.sh.

## Design

**D1. Config read.** The dispatch daemons do not import `personal_agent.config`. They read
`AGENT_SESHAT_CHANNEL_SECRET` with `load_channel_secret()`: the process env first, then an exact-key
`.env` parse at the git toplevel. `load_owner_ntfy_url()` follows that pattern. The settings class has
`extra="ignore"`, so the new `.env` key cannot break the gateway. No `os.getenv` outside that pattern.

**D2. `scripts/dispatch/owner_notify.py` (new).**

- `OwnerNotifier = Callable[[str], bool]`. `True` means the message was accepted (2xx).
- `load_owner_ntfy_url() -> str | None`.
- `post_ntfy(url, message, *, opener, timeout_s=5.0) -> bool`. Never raises. Plain-text POST with
  `Title: Seshat dispatch`, `Priority: high`. Accepts only `http` and `https` URLs.
- `make_owner_notifier(url, logger, *, opener=urlopen, timeout_s=5.0) -> OwnerNotifier`. With
  `url is None` it logs `owner_notify_unconfigured` and returns `False`. It logs `owner_notify_sent`
  or `owner_notify_failed` with the exception class only. The URL is never logged.

**D3. Ledger.** `LedgerEntry.owner_alerted_at: float | None = None`. Set once by
`mark_owner_alerted`. `record_pending` and `record_surfaced` carry it forward on the same rule as
`alerted_at` (always carried), so a retry or a renotify never repeats the owner push. `load_ledger`
reads it. An old file without the field loads as `None`.

**D4. `master_alert.py`.** `OWNER_ALERT_AFTER_S = 900.0`: the deferral limit after the master alert
is due. `is_owner_due(entry, now)`: `alerted_at is None and owner_alerted_at is None and
now - created_at >= ALERT_AFTER_S + OWNER_ALERT_AFTER_S`. Three one-line formatters for the owner
(`format_owner_undelivered_trigger`, `format_owner_notify_entry`, `format_owner_held_master_trigger`).
Each ends with "Check the cc-master input box." and each names the PR or the ticket.

**D5. Watcher.**
- `_alert_master_if_due(..., owner_notifier)`. After a refused send to master, if `is_owner_due`, it
  sends one owner line. On `True` it calls `mark_owner_alerted` and persists. On `False` it leaves the
  latch open, so the next tick retries (the failing trigger runs again every tick).
- `resolve_queued_triggers(..., owner_notifier)`. At the existing `escalation_s` crossing, a held
  master trigger sends one owner line naming the PR. The latch is `owner_alerted_at` on the entry
  (persisted), not the in-memory `escalated` set, so a restart does not repeat the push. The
  existing log line and its in-memory latch stay as they are. This pass is the stale-draft detection
  for `cc-master`: a held draft makes `reoffer` refuse, so the entry stays unconfirmed.
- `run_once(..., owner_notifier: OwnerNotifier | None = None)`. `None` means no owner path, so every
  existing caller and test keeps its behaviour. `main` passes the real notifier.

**D6. Orchestrator.** `_alert_stale_notifications(..., owner_notifier)`. A refused master send no
longer ends the pass: the loop sets `master_blocked`, stops trying master for the rest of the tick,
and sends an owner line for every entry that is `is_owner_due`. `run_once` gets the same optional
param. `main` passes the real notifier.

**D7. Route unchanged for worker alerts (AC-3).** The owner path runs only after a refused send to
master, and only past the deferral limit. A reachable master still gets the alert as today.

**Known edge.** The limit counts from `created_at`, the episode start. After a long daemon outage, a
still-open entry can reach the owner on the first tick where master refuses. The text is true (the
condition is open past 30 minutes), so this is accepted.

## Steps (TDD: red, then green)

1. `tests/scripts/test_owner_notify.py` (new): fake `http.server` in a thread. Cases: the POST body
   and `Title` header arrive; a 500 returns `False`; a hung server returns `False` within the
   timeout; a closed port returns `False`; a `file://` URL is refused; unconfigured logs a warning and
   sends nothing; no logged field contains the URL; `load_owner_ntfy_url` reads the env var, then the
   `.env` key by exact name, then `None`. Then create `scripts/dispatch/owner_notify.py`.
   Run: `make test-file FILE=tests/scripts/test_owner_notify.py`.
2. `tests/scripts/test_trigger_ledger.py`: round trip, old JSON, carry in `record_pending` and
   `record_surfaced`. `tests/scripts/test_master_alert.py`: `is_owner_due` boundaries, one-line
   formats. Then edit `trigger_ledger.py` and `master_alert.py`.
3. `tests/scripts/test_gating_watcher.py`: AC-1 worker, AC-2 held master trigger, AC-3 (below). Then
   edit `gating_watcher.py`.
4. `tests/scripts/test_orchestrator.py`: AC-1 notify entry, AC-3. Then edit `orchestrator.py`.
5. Docs: `docs/runbooks/dispatch-orchestrator.md` subsection, `.env.example` entry (name only, empty),
   module docstring item 4 of `gating_watcher.py`.
6. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`. Commit. Self-review on `git diff origin/main...HEAD`.

## Acceptance criteria and proof

| AC | Proof (outcome asserted) |
|----|--------------------------|
| AC-1 | Watcher: master pane busy, seat busy, ticks each 60 s. No owner line before 1800 s. Exactly one at 1800 s that names PR #412 and `cc-2build`. No master text. A second test uses a draft-holding master pane. Orchestrator: a notify entry, master busy: none at 1799 s, one at 1800 s naming source, stream, ticket. |
| AC-2 | Watcher: a held master trigger (master busy) crosses `escalation_s`. One owner line naming the PR. 20 more ticks, and a reload of the ledger between ticks: still one. A failed send retries and latches only on success. |
| AC-3 | Watcher and orchestrator: master idle. Master receives the alert text. The owner notifier is never called. The worker alert text is byte-identical to today's. |
| Scope 1 | `make_owner_notifier(None, ...)` returns `False`, logs a warning, and makes no request. |
| Scope 2, 3 | `post_ntfy` against the local fake server: one short line, timeout honoured, failure returns `False`. One watcher test runs the real notifier against the fake server. |
| AC-4 | Live, master and owner. Runbook in the handoff comment. |
