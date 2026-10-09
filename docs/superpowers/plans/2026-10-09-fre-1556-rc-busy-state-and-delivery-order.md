# FRE-1556 — Busy state from Remote Control status, a delivery order that never types into a busy seat, and the end of force-delivery

**Ticket:** FRE-1556 · **ADR:** ADR-0155 D2 track A, D7 (design intent) · **Umbrella:** FRE-1553
**Stream:** build2 · **Tier:** Standard (touches dispatch delivery logic) → Codex plan review required.

## Design decisions (read first)

1. **RC status vocabulary.** Live `claude agents --json --all` reports `idle` and `busy` (observed 2026-10-09).
   The ADR names `running`, `waiting`, `pending`. The map covers all of them:
   - `idle` → `idle`
   - `busy`, `running`, `waiting`, `pending` → `busy`
   - `completed`, `failed`, `killed`, or no live entry for the seat → `ended`
   - any other value, an unreadable registry, or two live entries with one name → `unknown`
2. **Match by RC `name`**, which equals the tmux session name (`cc-master`, `cc-1build`, ...). Master has no worktree
   topology, so the launcher's cwd match cannot serve it. An entry with no `status` key (a stopped background record)
   is not live. Zero live entries → `ended`.
3. **RC `unknown` falls back to the pane.** This is the ADR's "pane only as a fallback". With a pane-only reading the
   behaviour equals today's. An unreadable registry therefore never stalls dispatch, and no existing test changes.
4. **The send-keys gate** (`ADR D7 point 3`), one function used by `send_to_session`, so every typing path inherits it
   (worker triggers, master fallback, the alert to master, the whitelist, the orchestrator's alert):
   - RC `busy` or `ended` → never type.
   - RC `idle` → type only if the draft is *known empty*.
   - RC `unknown` → the pane alone decides (bare empty prompt), as today.
   - *Known empty*: a fresh mod report decides when one exists (heartbeat ≤ 90 s, `disabled` not true, engine version on
     the allow-list). Otherwise the pane must show a bare empty prompt. A fresh mod report that says "draft present", or
     a pane that contradicts the mod, counts as not known empty.
5. **Busy state for AC-1.** `seat_busy_state()` returns `idle | busy | ended | absent`. RC `idle` + pane `❯ /master 1218`
   → `idle` (RC wins). The *send* gate still refuses, because the draft is not known empty.
6. **End force-delivery.** Delete `_force_deliver` and the `force_deliver` parameter of `resolve_queued_triggers`.
   At `queued_escalation_s` the entry keeps its once-per-run `gating_trigger_unconfirmed_too_long` warning (the alert
   to the owner). The idle-gated re-offer continues each tick. Nothing is ever typed blind.
7. **The alert.** Unchanged channels: the `[DISPATCH ALERT]` line to `cc-master` (worker triggers, 15 min, already
   built in FRE-1540) and the warning log (master triggers, 30 min). The master line goes through `send_to_session`, so it
   obeys the same gate. A master trigger cannot alert master by typing into master; the warning log is its alert.
8. **Inbox (D7 point 1).** Observation only. When a fresh mod report exists for the target seat, log
   `gating_inbox_would_use` (seat, trigger_id) and go on to the channel. No mod exists until FRE-1558, so the log is
   dormant now.
9. **Per-tick logging (AC-4).** `run_once` takes `seat_reader: Callable[[], Sequence[SeatReading]]` (default: none, like
   `context_reader`). `main()` wires the real reader. One `claude agents` call and one `tmux` capture per seat per tick.
   Event `gating_seat_readings`, one per seat: `seat`, `pane`, `rc_status` (raw), `rc_state` (mapped), `mod_state`,
   `mod_fresh`. Runs in dry-run too.
10. **Seats observed:** `cc-master`, each stream's tmux session from the launcher topology, and `cc-explore`
    (ADR-0155 Context names all five).

## Files

| File | Change |
|------|--------|
| `scripts/dispatch/seat_readings.py` (new) | RC map, pane reading, mod report reader, `seat_busy_state`, `draft_known_empty`, `read_seat_readings`, `SeatReading` |
| `scripts/dispatch/gating_watcher.py` | `send_to_session` uses the gate; delete `_force_deliver`; `resolve_queued_triggers` drops force; `run_once` logs readings and the inbox observation; `main` wires `seat_reader`; docstring |
| `scripts/dispatch/launcher.py` | none (reuse `_rc_agents`) |
| `tests/scripts/test_seat_readings.py` (new) | unit tests for the new module |
| `tests/scripts/test_gating_watcher.py` | new AC tests; update the tests that assert force-delivery |
| docs | `docs/reference/` dispatch runbook lines that describe force-delivery, if any (grep) |

## Steps (TDD)

1. **Write failing tests** `tests/scripts/test_seat_readings.py`:
   - status map table (each value → state), absent seat → `ended`, stopped background entry → `ended`, two live entries → `unknown`, unreadable → `unknown`.
   - `seat_busy_state`: AC-1 (`❯ /master 1218` + RC `idle` → `idle`); RC `unknown` + busy pane → `busy`; tmux absent → `absent`.
   - `draft_known_empty`: RC idle + bare prompt → True; RC idle + draft → False; RC busy → False; fresh mod `draft_present: true` → False; fresh mod empty + pane draft → False; stale mod ignored; `disabled` mod ignored; RC unknown + bare prompt → True.
   - `read_seat_readings`: every seat has an `rc_status` entry, including a seat missing from the registry (AC-4).
   Run: `make test-file FILE=tests/scripts/test_seat_readings.py` → expect failures (module missing).
2. **Write failing watcher tests** in `tests/scripts/test_gating_watcher.py`:
   - AC-2: RC `busy`, RC `waiting`, RC `idle` with draft pane → worker trigger, channel down; ticks at t0 and t0+16 min → zero `send-keys` to the seat; exactly one `[DISPATCH ALERT]` to master.
   - AC-3: master trigger, channel down, seat never idle, ticks to t0+45 min → zero `send-keys` to master; one `gating_trigger_unconfirmed_too_long` warning; ledger entry still queued.
   - AC-4: `run_once` with a reader logs one `gating_seat_readings` per seat per tick over 60 ticks.
   - inbox observation: fresh mod report → one `gating_inbox_would_use`, delivery still by channel.
   - update `test_queued_escalates_once_naming_pr`, `test_queued_force_delivers_after_escalation_when_pane_never_goes_idle` and siblings that assert force-delivery: they now assert *no* `send-keys`.
3. **Implement `seat_readings.py`**, then change `send_to_session`, delete the force path, add logging. Run the new tests until green.
4. **Full gates:** `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
5. **Commit, then self-review** (`feature-dev:code-reviewer` on `git diff origin/main...HEAD`; `security-review` — subprocess and file reads are touched).
6. **Rebase, push, open PR, post the handoff comment.**

## Acceptance-criteria map

| AC | Proof |
|----|-------|
| AC-1 | `test_ac1_held_draft_with_rc_idle_reads_idle` |
| AC-2 | three parametrised tests: zero `send-keys` to the seat, one alert |
| AC-3 | 45-minute test: zero `send-keys`, alert fires |
| AC-4 | per-tick test over 60 ticks; live check after deploy: `journalctl -u seshat-gating-watcher --since "1 hour ago" \| grep gating_seat_readings` — 5 seats × ~60 ticks, each with `rc_status` |

## Risks

- RC `unknown` → pane fallback keeps today's behaviour. Stricter reading ("no RC, no typing") would make `claude agents` a single point of failure; flagged in the handoff.
- The mod does not exist yet (FRE-1558). The mod reader is tested with fixture files only. The engine-version allow-list is an empty module constant until FRE-1558 fills it, so a mod report is never "fresh" in production now.
- Diff class: touches the delivery path of dispatch (not a production data write path, no schema, no cost code). Self-serve.
