# FRE-829 — Trigger ledger + fail-closed watcher wiring (ADR-0113 foundation)

Backing ADR: ADR-0113 §1 (role model) + §2 (actuation autonomy — trigger ledger). Foundation
ticket; blocks FRE-831 (send-keys whitelist wrapper), FRE-832 (prime-master revision), FRE-835
(autonomous-merge gate). Codex plan-review completed 2026-07-07 (see "Codex findings" below) —
this revision incorporates its fixes.

## Scope decision (flagged for master)

The ticket names two components: (1) a durable trigger ledger, (2) "fail-closed watcher wiring:
a single-target sensor that talks only to master." The **live** `gating_watcher.py` (FRE-823)
currently sends to **both** master (`/master <PR#>`) and workers directly (`/prime-worker`).
Collapsing to master-only now — before FRE-831's send-keys whitelist wrapper exists to let master
re-actuate to workers — would remove the only working worker-wake path in production with no
replacement. Codex confirmed this scope-out is defensible (ADR's master-only design explicitly
depends on the not-yet-built wrapper). This ticket therefore builds the **ledger** (component 1)
and wires it into the existing watcher's actuation path (crash-safety wiring), but does **not**
change who the watcher talks to — that consolidation is FRE-831/832's job.

## What to build

### 1. New module `scripts/dispatch/trigger_ledger.py`

A durable, idempotent, file-backed event ledger — pure logic + an atomic-JSON IO seam, mirroring
the existing `load_state`/`save_state` pattern in `gating_watcher.py`.

```python
@dataclasses.dataclass(frozen=True)
class LedgerEntry:
    event_id: str                       # reuses the watcher's existing dedup_key (<kind>:<pr>:<sha>)
    source: str                         # trigger.reason, e.g. "master-ready" / "worker-bounce"
    target_pane: str                    # trigger.session, e.g. "cc-master"
    ticket: str                         # str(trigger.pr)
    command: str                        # trigger.command — what reconcile would resend
    preconditions: Mapping[str, str]    # e.g. {"head_sha": trigger.head_sha}
    created_at: float
    send_started_at: float | None = None   # set immediately before the send attempt
    sent_at: float | None = None            # set only after send_to_session returns "sent"
    consumed_at: float | None = None        # terminal — bookkeeping closed (sent OR abandoned)
    surfaced_at: float | None = None        # terminal-pending — ambiguous, owner must clear
```

Three-state crash model on `(send_started_at, sent_at)` (closes codex finding #1 — a bare
`sent_at is None` check cannot tell "never attempted" from "crashed mid-send"):
- `send_started_at is None` → never attempted → **safe to retry** (no send call was ever made).
- `send_started_at set, sent_at is None` → crashed *during* the send call, ambiguous (tmux may or
  may not have received it) → **surface, never auto-retry** (a blind replay here could double-send
  a not-fully-idempotent action).
- `sent_at is not None, consumed_at is None` → we know the send succeeded (only ever set after
  `send_to_session` returns `"sent"`) → **close out only** (`mark_consumed`), no replay.

Functions:
- `record_pending(ledger, *, event_id, source, target_pane, ticket, command, preconditions, now, ttl_s) -> tuple[dict[str, LedgerEntry], Literal["new", "duplicate"]]`
  — `"duplicate"` (ledger unchanged) if an entry exists and either `consumed_at is None` (still
  in-flight/pending/surfaced) **or** `now - consumed_at < ttl_s` (closes codex finding #2: folding
  the trigger's own TTL into the ledger's dedup means a lost/unpersisted TTL-dict write can never
  cause a same-tick double-send — the ledger is a second, crash-safe copy of that suppression
  window, not merely dependent on the pre-existing `sent` dict). Otherwise write a fresh pending
  entry and return `"new"`.
- `mark_send_started` / `mark_sent` / `mark_consumed` — pure entry-field setters returning a new
  ledger dict.
- `reconcile(ledger, *, now, execute_pending, persist, logger) -> dict[str, LedgerEntry]` —
  `execute_pending: Callable[[LedgerEntry], Literal["sent", "busy", "absent"]]` (mirrors
  `send_to_session`'s contract). For every entry with `consumed_at is None and surfaced_at is
  None`, apply the three-state model above; `persist` is called after each entry's transition
  (closes codex finding #3 — the crash guarantee needs its own persistence seam, not just an
  in-memory return value). A `busy`/`absent` result from `execute_pending` leaves the entry
  untouched (retried again next tick — no crash, nothing to surface).
- `snapshot_unconsumed(ledger) -> tuple[LedgerEntry, ...]` — the durable read future `prime-master`
  (FRE-832) will reconstruct from; also this ticket's own AC-1 proof point.
- `load_ledger(path, logger) -> dict[str, LedgerEntry]` — empty dict if the file is absent
  (never existed); if the file **exists but fails to parse**, log a loud warning
  (`trigger_ledger_corrupt`) rather than silently returning `{}` (closes codex finding #7 — a
  silent empty-on-corrupt is a silent loss of in-flight triggers, the exact thing this ledger
  exists to prevent).
- `save_ledger(path, ledger) -> None` — atomic (temp file + `os.replace`), matching
  `gating_watcher.save_state`.
- `prune_ledger(ledger, *, now, retention_s, open_prs) -> dict[str, LedgerEntry]` — drops only
  **terminal** entries (`consumed_at is not None`) once past `retention_s` or once their PR is no
  longer open; entries with `consumed_at is None` (pending or surfaced) are **never** pruned,
  regardless of age (closes codex finding #7 — unbounded-but-safe; only resolved state is dropped).

### 2. Wire into `scripts/dispatch/gating_watcher.py`

- New `run_once` params: `ledger: dict[str, LedgerEntry] | None = None` (defaults to `None`,
  allocated to `{}` inside the function — closes codex finding #6, a mutable default argument),
  `ledger_persist: Callable[[dict], None] = lambda _l: None`.
- Kill-switch check stays first (unchanged); `reconcile()` runs **immediately after** it and
  **before** `board_fetcher()`/`decide()` (closes codex finding #4 — reconciliation must never
  actuate while the kill switch is engaged; the existing early-return already guarantees this by
  ordering).
- For each `Trigger` that already passes `decide()`'s existing TTL/dedup gate, actuation becomes:
  `record_pending` (skip on `"duplicate"`) → `mark_send_started` + persist → `send_to_session` →
  on `"sent"`: `mark_sent` + persist → existing `state[dedup_key] = now; persist(state)` (unchanged)
  → `mark_consumed` + persist. On `"busy"`/`"absent"`: `mark_consumed` directly (an abandoned,
  terminal, non-sent entry — decide() will legitimately re-produce a fresh trigger next tick since
  the TTL dict was never updated, and `record_pending` allows overwriting a consumed entry).
  Ledger entries are created **only** at this exact point — never for a dry-run (`execute=False`)
  or an unroutable (`session is None`) trigger (closes codex finding #5 — those are normal,
  expected non-actuations already retried by the ordinary next-tick `decide()` path, not crashes;
  giving them a durable pending entry would let `reconcile` race the normal flow into a double
  send).
- New CLI flag `--ledger-file` (default `telemetry/trigger_ledger.json`) and
  `--ledger-retention-days` (default 7), wired through `main()`.

### 3. Documentation

- `docs/runbooks/dispatch-orchestrator.md` — add a "Trigger ledger (FRE-829)" subsection under the
  existing "Gating watcher (FRE-823)" section: what it records, the three-state crash model, where
  `surfaced_at` entries need owner attention (and that clearing them is manual today — FRE-832 is
  the read-side), retention.

## Acceptance-criteria proof map (from the ticket)

| AC | Test |
|----|------|
| AC-4 duplicate event → one actuation | `test_record_pending_duplicate_while_unconsumed_blocks`, `test_reconcile_duplicate_pending_actuates_once` |
| AC-4 malformed/idle event → zero | `test_run_once_idle_pr_writes_no_ledger_entry` |
| AC-4 crash between ledger-write and send → complete-exactly-once | `test_reconcile_never_attempted_retries_and_consumes` |
| AC-4 crash between send and mark-consumed → complete-exactly-once, no replay | `test_reconcile_known_sent_closes_out_without_replay` |
| AC-4 (bonus, ambiguous mid-send) → surfaced, never replayed | `test_reconcile_ambiguous_mid_send_surfaces_without_replay` |
| AC-1 in-flight reconstruction survives context clear | `test_snapshot_unconsumed_survives_reload_from_disk` |

## Files

- `scripts/dispatch/trigger_ledger.py` (new)
- `scripts/dispatch/gating_watcher.py` (edit — wiring described above)
- `tests/scripts/test_trigger_ledger.py` (new — ledger unit tests, AC-4/AC-1)
- `tests/scripts/test_gating_watcher.py` (edit — add ledger-wiring tests; existing ~30 tests must
  pass unchanged since new params default to `None`/no-op)
- `docs/runbooks/dispatch-orchestrator.md` (edit)

## TDD steps

1. `LedgerEntry` + `record_pending` (new / duplicate-while-unconsumed / duplicate-within-ttl-after-
   consumed / new-after-ttl-elapsed).
2. `mark_send_started` / `mark_sent` / `mark_consumed` field setters.
3. `reconcile` three-state model (the four tests in the AC table above), each with a fake
   `execute_pending` spy and a fake `persist` recorder.
4. `snapshot_unconsumed`, `load_ledger`/`save_ledger` (round-trip + corrupt-file loud-warning +
   absent-file-is-empty), `prune_ledger` (drops old consumed, never drops unconsumed).
5. Wire into `gating_watcher.run_once`: new tests for the sent/busy/absent/dry-run/unroutable
   ledger-write conditions, plus a reconcile-before-decide integration test (seed a pending entry
   representing a prior crash, assert `run_once` completes it via reconcile before evaluating new
   board state).
6. Re-run the full existing `test_gating_watcher.py` suite unmodified-call-site to confirm no
   regressions from the new optional params.

## Quality gates

`make test-file FILE=tests/scripts/test_trigger_ledger.py` → `make test-file FILE=tests/scripts/test_gating_watcher.py` → `make test` → `make mypy` → `make ruff-check` + `make ruff-format` → `pre-commit run --all-files`.

## Out of scope (follow-ups, do not build here)

- Single-target (master-only) watcher consolidation — FRE-831 (whitelist wrapper) + whatever
  ticket updates `gating_watcher`'s worker-send path once master can re-actuate through it.
- Reading `snapshot_unconsumed` to reconstruct master's in-flight state after `/clear` — FRE-832.
- Any UI/mechanism for the owner to actually clear a `surfaced_at` entry — not specified by this
  ticket's ACs; `surfaced_at` entries are visible in the ledger file today, cleared manually.

## Codex findings (2026-07-07 plan review) — all folded into the design above

1. Blind-replay risk on `sent_at is None` → added `send_started_at` three-state model.
2. Consumed-entry overwrite race if the TTL dict write is lost → ledger's own `ttl_s`-aware dedup
   on `record_pending`.
3. `reconcile` needs its own persistence seam → added `persist` callback, called per-entry.
4. Kill-switch must gate reconciliation → reconcile placed after the existing kill-switch check.
5. Busy/absent/unroutable/dry-run must not create durable pending entries → ledger writes moved to
   exactly the send boundary only.
6. Mutable default argument → `ledger: ... | None = None`, allocated inside the function.
7. Ledger growth + fail-open-on-corrupt → `prune_ledger` (never drops unconsumed) + loud warning on
   unparseable (not present) ledger file.
