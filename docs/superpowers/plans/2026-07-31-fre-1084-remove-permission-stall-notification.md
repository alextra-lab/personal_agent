# FRE-1084: Remove the permission-stall notification to master

Ticket: https://linear.app/frenchforest/issue/FRE-1084
Originating: FRE-867 (added the path), FRE-1076 (found 3 defects in the same detector, now moot for this path)

## Why

The dispatch watcher's permission-stall nudge sends master a duplicate of something the owner
already sees directly. Master then spends a turn re-reading and re-relaying it. Owner-directed
removal (2026-07-31) — not a tuning ticket. Keep everything else the watcher does (master-ready
PR notification, worker red-CI poke, context-pressure nudge) untouched. Keep the psql allowlist
bundled-short-flag fix from the same original commit — this is a removal by hand, not a revert.

## Scope (files touched)

1. `scripts/dispatch/gating_watcher.py` — remove the permission-stall path entirely:
   - Import: drop `permission_prompt_snippet` from the `pane_state` import (keep `session_is_idle`)
   - Drop `hashlib` import (its only use is the stall content-hash key, line ~1328)
   - Drop `known_streams` from the `launcher` import (its only use is `_capture_permission_stalls`); keep `topology_for` (used elsewhere)
   - Remove `DEFAULT_PERMISSION_STALL_TTL_S` constant + its comment block (~169-176)
   - Remove `_PERMISSION_STALL_NUDGE` constant + its comment block (~177-196)
   - Remove `run_once`'s `permission_stall_reader` / `permission_stall_ttl_s` params (~1243-1244) and their docstring `Args:` entries (~1286-1291)
   - Remove the `# --- permission-stall nudge (FRE-867) ---` loop block in `run_once` (~1319-1414)
   - Remove `permission_stall_ttl_s` from the `max(...)` call feeding `prune_state` (~1626)
   - Remove `_capture_permission_stalls()` function (~1692-1715)
   - Remove the `--permission-stall-ttl` CLI arg (~1797-1802)
   - Remove the `permission_stall_reader=`/`permission_stall_ttl_s=` kwargs at the `run_once` call site in `tick()` (~1860-1861)

2. `scripts/dispatch/pane_state.py` — `permission_prompt_snippet` has no remaining consumer once
   `_capture_permission_stalls` is gone (verified: only caller in the whole tree). Remove it, its
   two dedicated module constants (`_PERMISSION_PROMPT_MARKER`, `_PERMISSION_OPTION_RE`) and their
   comment blocks, and drop it from `__all__`. Leave `session_is_idle` and its own constants
   (`_BUSY_MARKERS` includes the literal "Do you want" already — that's the unrelated generic
   busy-heuristic, not this feature) untouched.

3. `tests/scripts/test_gating_watcher.py` — remove the now-dead coverage:
   - Drop `permission_prompt_snippet`, `_capture_permission_stalls`, `DEFAULT_PERMISSION_STALL_TTL_S` from the import list
   - Remove the `# --- permission_prompt_snippet (FRE-867) ---` test block (~262-322)
   - Remove `test_prune_state_keeps_permission_stall_key_within_ttl` and `test_prune_state_drops_expired_permission_stall_key` (~1447-1461) — `prune_state` is generic and these only pin the now-dead `permstall:` key shape
   - Remove everything from `# --- _capture_permission_stalls (FRE-867) ---` to end of file (~2275-end): the capture test, `_STALLED_SNIPPET(_2)` fixtures, and every `test_run_once_permission_stall_*` test, including the `..._defaults_do_not_affect_pr_only_ticks` regression guard (moot — the param no longer exists)
   - Add a new regression test in the `resolve_queued_triggers` coverage: a ledger containing one unconsumed entry with `source="permission-stall"` and `queued_at` set must be retired (`consumed_at` set) WITHOUT calling `reoffer` — see item 0 below for why.

0. **New (not pure deletion) — `resolve_queued_triggers` in `scripts/dispatch/gating_watcher.py`** (~line 1160,
   before the `_is_superseded` check): retire any still-unconsumed `entry.source == "permission-stall"`
   ledger entry (mark it consumed, log it, `continue`) instead of falling through to `reoffer`.
   **Why this is required, not optional:** the production ledger at `/opt/seshat/telemetry/trigger_ledger.json`
   currently holds one live unconsumed entry, `permstall:cc-1build-c1df84feb591` (created 2026-07-31 07:39:54
   UTC, `queued_at` set, `sent_at`/`consumed_at` both `None`). Its `ticket` field is `"cc-1build"` (non-numeric,
   like a context-pressure entry), so the existing PR-closure obsolescence check (`entry.ticket.isdigit()`)
   never fires for it, and it would fall through to `reoffer(entry)` — i.e. `resolve_queued_triggers` would
   still send a stale permission-stall nudge to master on the first tick after this fix deploys and the
   watcher restarts, defeating AC-1. Deleting the producer code doesn't retire pre-existing persisted state;
   this guard does. (No equivalent guard is needed in `trigger_ledger.reconcile()` — it never calls `reoffer`
   for a `queued_at`-set entry; that path is explicitly owned by `resolve_queued_triggers`, per its own
   docstring.)

4. `.claude/skills/master/SKILL.md` — remove the "A permission-stall nudge..." paragraph block
   (current lines 25-37, three paragraphs) from Step 1. It is the only skill doc in the tree that
   mentions this path (confirmed via grep across `.claude/skills/`).

## Out of scope (explicitly, per ticket)

- `.claude/hooks/allow-readonly-psql.sh` and its test — untouched. Verify its test still passes
  as evidence for the ticket's own AC, don't touch the file.
- Every other watcher trigger (master-ready PR, worker red-CI poke, context-pressure) — untouched.

## Verification

- `grep -rn "permission_stall\|permission-stall\|permstall\|_PERMISSION_STALL_NUDGE\|DEFAULT_PERMISSION_STALL_TTL_S\|permission_prompt_snippet\|_capture_permission_stalls" scripts/ tests/ .claude/skills/` → zero hits
- `bash .claude/hooks/test_allow_readonly_psql.sh` → `ALL PASS` (unchanged behavior, run as evidence not as a code change)
- `make test-file FILE=tests/scripts/test_gating_watcher.py`
- `make mypy` / `make ruff-check` / `make ruff-format` / `pre-commit run --all-files`

## Deploy (master's job, not this session's)

The watcher's systemd service (`seshat-gating-watcher.service`) is currently stopped (master
stopped it 2026-07-31 07:44 UTC per the ticket's operational note). Restarting it only has effect
once this PR is merged and deployed to `/opt/seshat` main — the service's `WorkingDirectory` is
`/opt/seshat`, not this worktree. This session does not deploy or restart the service (build-skill
boundary); the PR's post-deploy runbook will tell master to restart it and check one tick's log for
a non-permission-stall trigger plus `active` status, per the ticket's AC-4. The runbook will also note
that the one stale unconsumed `permstall:` ledger entry already on disk self-heals (is retired, not
redelivered) on the first tick after restart, via item 0 above — no manual ledger edit needed.
