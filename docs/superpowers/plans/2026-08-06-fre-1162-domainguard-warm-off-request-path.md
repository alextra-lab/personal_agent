# FRE-1162 — Move DomainGuard's blocklist refresh off the request path

**Status:** Revised after codex plan-review (2026-08-06, session `019fd64c-f48e-7d80-94b6-92c22b9c5104`)
— 3 blocking findings addressed below. Pending explicit owner approval before implementation
(Standard risk-tier: touches `src/` logic in `security.py` + `brainstem/scheduler.py`).

## Codex review findings addressed

1. **[Blocking] Cold-start gap weakens the refusal obligation.** The original plan relied solely
   on the scheduler's periodic job (first tick ~60s after `_lifecycle_loop` starts, per
   `LIFECYCLE_CHECK_INTERVAL_SECONDS`). Codex: a domain present in a warm disk cache but absent
   from `_BUNDLED_BLOCKLIST` would be wrongly allowed for any request in that ~60s window,
   contradicting ADR-0132 D2's "refuse on every enumerated seam" obligation. **Fix:** an explicit,
   synchronous warm in `BrainstemScheduler.start()` — before the background tasks are spawned —
   so the guard is loaded before the scheduler returns control to app startup, not just eventually
   via the periodic tick. This is the ticket's literal "at startup" branch, not the "or" fallback.
2. **[Blocking] Fixed 300s interval doesn't track the configured TTL.** Codex: `ensure_loaded()`
   only fetches once actually stale, so a hardcoded cadence unrelated to the guard's TTL setting
   either over-polls a short custom TTL or under-covers it. **Fix:** derive the warm interval from
   `settings.url_guard_cache_ttl_seconds` (already read by `get_domain_guard()` — no new setting)
   as an instance attribute set in `__init__`, matching how `joinability_probe_interval_seconds`
   and `slm_health_probe_interval_seconds` are already instance attributes rather than module
   constants.
3. **[Blocking] `test_note_staleness_resets_after_refresh` didn't test what its name claimed.**
   Fixed in the test list below: mark stale → `note_staleness()` (1st warning) → `refresh()` →
   force staleness again → `note_staleness()` → assert a **2nd** warning (proves the flag actually
   resets, not just that a freshly-loaded guard stays quiet).
4. **[Wording fix, non-blocking]** The plan's design section previously claimed `_refresh()`
   "keeps serving the last-known ... list" on a fetch failure. Codex, correctly: on failure with no
   valid disk cache, `_refresh()` replaces `_blocklist` with `_BUNDLED_BLOCKLIST`, discarding
   whatever richer list was previously in memory — not "last-known". This is **pre-existing**
   `_refresh()` behavior, unrelated to this ticket's scope (timing of the fetch, not fallback
   quality); corrected below and flagged as a discovered-but-not-fixed item, same as
   `url_guard_enabled`.
5. **[Minor, adopted]** GIL-safety of the `_blocklist` frozenset reassignment confirmed sound by
   codex (atomic reference swap, no intervening `await`, no cross-thread use) — no design change
   needed, noted here for the record.
6. **[Minor, adopted]** New transport test: a stale-but-already-loaded blocklist guard must still
   refuse a matching domain without awaiting `ensure_loaded()`.
7. **[Minor, adopted]** The new blocklist-mode "never calls ensure_loaded" test uses a plain
   `AsyncMock()` (not `wraps=guard.ensure_loaded`) so a regression that actually calls it fails
   loudly instead of silently performing a real fetch through the wrapped mock.

## Ticket

[FRE-1162](https://linear.app/frenchforest/issue/FRE-1162) — "DomainGuard's blocklist refresh
runs on the request path — move it off, so no user turn ever pays the feed fetch." Backlog,
quantified at the FRE-1147 / ADR-0132 D2 gate: `_guard_request_hook` (the request hook installed
by `create_guarded_http_client`, `src/personal_agent/security.py:319-337`) calls
`await guard.ensure_loaded()` on every request when `mode != OFF`. `ensure_loaded()` fetches the
URLhaus feed (15s timeout) inline whenever the 1-hour cache has gone stale — so whichever egress
seam is called first after staleness pays the fetch on a user turn.

No backing ADR obligation beyond D2's existing wiring — this is a standalone latency bug in that
wiring, not a new architectural surface. No acceptance criteria are written on the ticket; the
verifications below (derived directly from the bug's own description: "no user turn ever pays the
feed fetch", "the hot path only ever reads memory") stand in, per lifecycle-rules' standalone-bug
carve-out.

## Design

Three changes (the startup warm was added after codex review — see findings above):

1. **Request-path hook stops fetching, in every mode.** Today `_guard_request_hook` skips
   `ensure_loaded()` only in `GuardMode.OFF` (AC-c from ADR-0132). Replace the call with a new
   synchronous, non-awaiting `DomainGuard.note_staleness()` that logs (once per staleness episode,
   not once per request — a stalled warm job would otherwise flood the log) and returns
   immediately. `check_url()` then reads whatever `_blocklist` is already in memory — the
   startup-warmed list from the moment the scheduler starts, kept fresh thereafter by the
   periodic job.

2. **An explicit startup warm in `BrainstemScheduler.start()`.** Before spawning
   `_lifecycle_task`/`_session_summary_task`, `start()` now awaits one
   `get_domain_guard().ensure_loaded()` call (skipped when `mode is GuardMode.OFF`) and stamps
   `_last_domain_guard_warm_run`. This closes the cold-start gap codex flagged: without it, any
   request arriving before `_lifecycle_loop`'s first ~60s tick would see only
   `_BUNDLED_BLOCKLIST` (3 entries), which could wrongly allow a domain the disk cache already
   knows is bad — a real (if narrow) regression against ADR-0132 D2's refusal obligation. This is
   the ticket's literal "warm at startup" branch: latency here (up to the 15s feed-fetch timeout,
   worst case) lands on app boot, never on a user turn.

3. **A periodic warm job on `BrainstemScheduler._lifecycle_loop`.** New interval-gated block
   (below the existing SLM-health block, same inline shape as the joinability-probe / SLM-health /
   embedding-backfill jobs — not the newer `brainstem/jobs/<name>.py` module pattern, since the
   actual logic already lives in, and is already tested in, `DomainGuard.ensure_loaded()`/
   `refresh()`; the scheduler only needs to call it on a timer). Calls
   `await get_domain_guard().ensure_loaded()` when `guard.mode is not GuardMode.OFF`, wrapped in
   the same try/except-log-continue shape every other lifecycle job uses so a warm failure never
   crashes the scheduler loop. The interval is **derived from the configured TTL**, not a fixed
   constant (codex finding #2): `self.domain_guard_warm_interval_seconds = min(300.0, max(30.0,
   ttl_seconds / 4))`, read once in `__init__` from `settings.url_guard_cache_ttl_seconds` (the
   same setting `get_domain_guard()` already reads — no new config surface). At the default
   3600s TTL this is 300s (5 min, the ceiling); for a short custom TTL of e.g. 40s it's 30s (the
   floor) instead of a fixed 300s wildly overshooting a short TTL; at 800s TTL it's 200s (neither
   bound binds). `ensure_loaded()` itself already no-ops when fresh, so the extra calls are cheap.

`ensure_loaded()`'s internal fetch-failure fallback (disk cache → URLhaus → bundled list,
`security.py:190-220`) is untouched. Corrected description (codex finding #4): on a fetch failure
**with no valid disk cache**, `_refresh()` replaces `_blocklist` with `_BUNDLED_BLOCKLIST`
outright — it does *not* retain whatever richer list was previously in memory. This is
pre-existing behavior, orthogonal to this ticket's scope (the timing of the fetch, not the
fallback's quality); noted under "Explicitly out of scope" below rather than fixed here.

## Files touched

- `src/personal_agent/security.py`
  - `DomainGuard.__init__`: add `self._logged_stale: bool = False`.
  - New private helper `_mark_loaded()` — sets `_last_loaded = datetime.now(timezone.utc)` and
    resets `_logged_stale = False`; replaces the three duplicated
    `self._last_loaded = datetime.now(timezone.utc)` assignments inside `_refresh()`
    (cache-hit branch, fetch-success branch, fallback branch).
  - New public method `DomainGuard.note_staleness()` — sync, no I/O; no-ops when
    `mode is GuardMode.OFF` or the cache isn't stale; otherwise logs
    `domain_guard_stale_on_request_path` (WARNING) once and sets `_logged_stale = True`.
  - `ensure_loaded()` docstring: update — no longer "safe to call from every request"; now the
    method the scheduler's warm job calls.
  - `_guard_request_hook`: replace `if guard.mode is not GuardMode.OFF: await guard.ensure_loaded()`
    with `guard.note_staleness()`; update its docstring (the "skips ensure_loaded in OFF mode"
    rationale moves into `note_staleness()`'s own OFF check).

- `src/personal_agent/brainstem/scheduler.py`
  - `BrainstemScheduler.__init__`: add `self._last_domain_guard_warm_run: datetime | None = None`
    and `self.domain_guard_warm_interval_seconds` (derived from
    `settings.url_guard_cache_ttl_seconds`, see Design §3) — near the SLM-health block, ~line 223.
  - `start()`: before creating `_lifecycle_task`/`_session_summary_task`, local-import
    `GuardMode, get_domain_guard` from `personal_agent.security`, await
    `get_domain_guard().ensure_loaded()` when not `OFF`, stamp `_last_domain_guard_warm_run`;
    wrapped in try/except logging `domain_guard_startup_warm_failed` so a feed outage at boot
    never blocks app startup (the existing `ensure_loaded()`/`_refresh()` fallback chain already
    prevents this from raising in practice, but the wrapper is defensive).
  - `_lifecycle_loop`: new block after the SLM-health block (~line 1097), before the daily-archive
    block — local import `from personal_agent.security import GuardMode, get_domain_guard`
    (matching the existing local-import style for joinability/SLM-health jobs), interval-gated on
    `_last_domain_guard_warm_run` using `self.domain_guard_warm_interval_seconds`, calls
    `guard.ensure_loaded()` when not `OFF`, try/except logs `domain_guard_warm_failed` on any
    exception without advancing the timestamp (so it retries next tick — same shape as
    `embedding_backfill_failed`).

- `tests/test_security/test_domain_guard.py` — new `TestNoteStaleness` class:
  - `test_note_staleness_logs_once_when_stale_and_blocklist_mode`
  - `test_note_staleness_noop_when_off_mode`
  - `test_note_staleness_does_not_log_twice_for_same_staleness_episode`
  - `test_note_staleness_silent_when_fresh`
  - `test_note_staleness_resets_after_refresh` — **corrected per codex finding #3**: mark stale →
    `note_staleness()` (assert 1st warning) → `refresh()` → force staleness again (advance a
    patched clock or set `_last_loaded` back beyond TTL) → `note_staleness()` again → assert a
    **2nd** warning was logged. Proves `_logged_stale` actually resets on reload, not merely that
    a freshly-loaded guard is quiet.

- `tests/test_security/test_transport_factory.py`
  - Update module docstring bullet 2 (currently: "`GuardMode.OFF` never touches `ensure_loaded()`")
    → hook never touches `ensure_loaded()` in **any** mode now (FRE-1162); only `note_staleness()`.
  - New test `test_blocklist_mode_never_calls_ensure_loaded` (mirrors the existing OFF-mode test,
    asserts the same for `GuardMode.BLOCKLIST` with a stale guard) — **per codex finding/minor #7**,
    uses a plain `AsyncMock()` for `guard.ensure_loaded`, not `wraps=guard.ensure_loaded`, so a
    regression that actually calls it fails the assertion instead of silently performing a real
    fetch through the wrapped mock. This is the actual regression proof for the ticket: the mode
    that pays the fetch today must never touch `ensure_loaded()` from the hook.
  - New test `test_hook_calls_note_staleness` — spies on `note_staleness` to confirm the hook
    still performs *some* freshness signal, not just silently dropping the check.
  - New test `test_stale_blocklist_still_refuses_without_fetching` (codex minor #6): a guard whose
    `_last_loaded` is already past TTL, but whose `_blocklist` contains a matching entry, still
    raises `EgressBlockedError` for that domain — proving the blocking decision is unaffected by
    staleness, only the fetch is skipped.

- `tests/test_brainstem/test_scheduler.py` — new `TestDomainGuardWarmScheduling` class, same
  `_lifecycle_loop` + `stop_after_first_sleep` pattern as `TestQualityMonitorScheduling`, plus a
  `start()`-focused case:
  - `test_start_warms_domain_guard_before_spawning_tasks` — patches
    `personal_agent.security.get_domain_guard` to return a mock guard (`mode=BLOCKLIST`,
    `ensure_loaded=AsyncMock()`), calls `await scheduler.start()`, asserts `ensure_loaded` awaited
    once and `_last_domain_guard_warm_run` is set before returning (closes codex finding #1).
  - `test_start_skips_domain_guard_warm_in_off_mode` — mock guard `mode=OFF`, `start()` doesn't
    await `ensure_loaded`.
  - `test_lifecycle_loop_warms_domain_guard_when_due` — same mock pattern, runs one loop
    iteration, asserts `ensure_loaded` awaited once and `_last_domain_guard_warm_run` advances.
  - `test_lifecycle_loop_skips_domain_guard_warm_when_recent` — `_last_domain_guard_warm_run = now`,
    asserts `ensure_loaded` not called.
  - `test_lifecycle_loop_skips_domain_guard_warm_in_off_mode` — mock guard `mode=OFF`, asserts
    `ensure_loaded` not called (matches `note_staleness()`'s own OFF no-op — both layers agree
    OFF never touches the network).
  - `test_lifecycle_loop_domain_guard_warm_survives_failure` — `ensure_loaded` raises, asserts the
    loop doesn't crash, logs `domain_guard_warm_failed`, and `_last_domain_guard_warm_run` stays
    `None` (retries next tick).
  - `test_domain_guard_warm_interval_derived_from_ttl` — construct scheduler with
    `settings.url_guard_cache_ttl_seconds` patched across a few values and assert the derived
    `scheduler.domain_guard_warm_interval_seconds`: `ttl=40` → `30.0` (floor binds), `ttl=800` →
    `200.0` (neither bound binds), `ttl=3600` (default) → `300.0` (ceiling binds).

## Test commands

```bash
make test-file FILE=tests/test_security/test_domain_guard.py
make test-file FILE=tests/test_security/test_transport_factory.py
make test-file FILE=tests/test_security/test_egress_seams.py   # regression: seam wiring unaffected
make test-file FILE=tests/test_brainstem/test_scheduler.py
make mypy
make ruff-check
make ruff-format
```

Full suite before PR: `make test`.

## Explicitly out of scope

- `url_guard_enabled` (`config/settings.py`) is declared but read nowhere in `get_domain_guard()`
  — a pre-existing dead config flag noticed while mapping this ticket's territory. Not touched:
  fixing it is a separate, unrelated defect (a dead setting, not a request-path latency bug) and
  folding it in would blur this PR's diff. Will flag it in the ticket close-out comment as a
  discovered-but-not-fixed item.
- No change to `_refresh()`'s fetch/fallback/cache logic, `check_url()`, or any of the 11 egress
  seams wired per ADR-0132 D2 — this ticket is purely about *when* the fetch happens, not the
  guard's blocking decisions.
- **[Found during self-review, not fixed]** `BrainstemScheduler` — the only caller of
  `DomainGuard.ensure_loaded()` after this change — is gated behind an OR of 7 settings flags
  (`service/app.py:877-885`: `enable_second_brain`, `data_lifecycle_enabled`, `insights_enabled`,
  `freshness_enabled`, `quality_monitor_enabled`, `promotion_pipeline_enabled`,
  `feedback_polling_enabled`). 5 of the 7 default `True`, so a deployment would need to explicitly
  disable all 7 simultaneously to skip scheduler construction entirely — but if it did, the guard
  would never warm beyond its 3-entry bundled fallback for the process lifetime (previously, the
  per-request `ensure_loaded()` self-healed regardless of scheduler state). Not fixed here: it
  requires an unusual configuration, and a robust fix would mean warming unconditionally inside
  `service/app.py`'s `lifespan()` — a large integration-style function with no existing unit test
  seam, which this ticket's TDD approach can't cleanly cover. Flagged for master/owner awareness in
  the ticket close-out comment rather than papered over with an untested code path.
