# FRE-1753: warn at gateway startup about a stored override with no audit row

Ticket: FRE-1753. Related: FRE-1722 (the store), FRE-1724 (the master comment of 2026-11-02).
No ADR. The owner decided on 2026-11-08 (decision page, item 18).

## Scope

Each PWA edit writes one row to `settings_overrides` and one row to `settings_edit_audit` in one
transaction (`SettingsOverridesRepository.set` and `.clear`). A direct SQL insert writes no audit row.
A `tool.*` row changes a permission at the next tool call and persists across restarts. Nothing
shows that such a row exists.

At startup, after the overrides load, the gateway checks each stored override. If the latest audit
row of that parameter is not a `set`, or no audit row exists, the gateway logs one warning.

## Design

- `SettingsOverridesRepository.unaudited_parameters(parameters)` runs one query. It selects the audit
  row with the highest `audit_id` for each parameter (`WHERE audit_id IN (SELECT MAX(audit_id) ...
  GROUP BY parameter)`). It does not use `DISTINCT ON`, because SQLAlchemy 2.1 deprecates that
  spelling. It returns the sorted parameters whose latest row is not a `set`, or that have no row.
- "Latest" means the highest `audit_id`. The column is a `BIGSERIAL`, so the order does not depend on
  a clock.
- `settings_edit._warn_about_unaudited_overrides(parameters, trace_id)` opens its own session.
  It is separate from the load session, so a failed audit read cannot undo the load. The whole
  body, including the session open and the session exit, is inside one `try`. An exception that
  escapes would stop the start, because `app.py` awaits the loader directly.
- `_warn_about_unaudited_overrides` returns at once when no override exists: no session, no query.
- One `setting_override_without_audit` warning per parameter. The fields are `parameter` and
  `trace_id`. The value is never a field.
- A failed read logs `setting_override_audit_check_failed` with `error_type` and `trace_id`. The
  error text is not logged, so a future change to the query cannot leak a value. A test seeds the
  exception text with a sentinel.
- `load_overrides_at_startup` calls it last, after `_warn_if_a_preset_exceeds_a_stored_ceiling`.
- Other writers: only `SettingsOverridesRepository` writes `settings_overrides`. Each of its writes adds
  an audit row, so a legitimate row never warns. A saved choice goes to
  `user_constraint_preferences`, not to this table.

## Not in scope

- A check that the stored value equals the audit `new_value`. A direct `UPDATE` of an audited row
  stays undetected. The ticket asks for the latest audit action only.
- A push notification. See AC-4 below.
- A new index on `settings_edit_audit`. The audit table holds owner edits only, so it stays small,
  and the existing `(parameter, changed_at DESC)` index serves the parameter filter. A new index is
  a schema change and a stricter deploy class.

## AC-4: the alert path (assessed, Codex plan review of 2026-11-08)

- Grafana: the one provisioned rule (`config/grafana/provisioning/alerting/grounding_alerting.yaml`)
  names the contact point `empty`. It delivers to nobody.
- ntfy: `scripts/dispatch/owner_notify.py` belongs to the dispatch daemons. The gateway has no ntfy path.
- ADR-0056 error monitor (`telemetry/error_monitor.py`, `WARNING_EVENT_ALLOWLIST`): it emits a
  cluster only at `error_monitor_min_occurrences` (default 5) events of one
  `(component, event, error_type, level)` in 24 hours. One unaudited row at one start gives one
  event, so the monitor reports nothing. The allowlist criterion is a recurring broken loop. This
  event does not fit that criterion, so the plan does not add it.
- Result: no existing path pushes this warning. The reader is the person who reads the startup log
  after a gateway start. The handoff asks master to add the event name to the post-deploy startup check.

## Steps

1. Failing unit tests in `tests/personal_agent/service/test_settings_edit_api.py`. Add
   `unaudited_parameters` to `_FakeRepository`. Tests:
   - one warning for each unaudited parameter, none for an audited one;
   - one warning for each unaudited parameter, none for an audited one (the proof of the feature);
   - an empty override set runs no query (a counter on the fake repository; this tests only the
     empty-input shortcut);
   - a sentinel value is absent from the captured log (with a seeded negative);
   - a repository that raises with the sentinel in its message: the failure is logged, the sentinel
     is absent, the start does not stop, and the overrides still load;
   - a session factory that raises on open, and one that raises on exit: the start does not stop.
   Run: `uv run pytest tests/personal_agent/service/test_settings_edit_api.py -k fre1753 -q`. Expect failures.
2. Failing integration tests in `tests/personal_agent/service/test_settings_edit_db.py` on real rows
   (test Postgres, port 5433). Seed one audited row (through the repository), one row with no audit row,
   and one row whose latest audit row is a `clear` after an older `set`. Assert that the first
   does not warn and the other two do. Assert that a sentinel value is absent from the log. A fixture
   deletes the audit rows and the override rows of the three seeded parameters before and after the
   test, because the audit rows outlive their users. Run:
   `PERSONAL_AGENT_INTEGRATION=1 uv run pytest tests/personal_agent/service/test_settings_edit_db.py -k fre1753 -q`.
3. Implement `unaudited_parameters` in `service/repositories/settings_overrides_repository.py`.
4. Implement `_warn_about_unaudited_overrides` and the call in `service/settings_edit.py`.
5. Run steps 1 and 2 again. Expect passes.
6. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
7. Commit. Run `feature-dev:code-reviewer` and `security-review` on `git diff origin/main...HEAD`.
8. Rebase, push, open the PR, post the handoff comment.

## Acceptance criteria and proof

| AC | Proof |
|----|-------|
| AC-1 | Integration test on real rows: audited row silent, unaudited row and cleared row warn. |
| AC-2 | Unit and integration tests: the sentinel is absent from the captured log, with a seeded negative. |
| AC-3 | Unit test: the repository raises, the failure is logged, the overrides load, no exception. |
| AC-4 | The handoff names who reads the warning, and states that no existing path pushes it. |
