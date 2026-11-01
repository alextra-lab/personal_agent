# FRE-1722: settings menu, edit part 1

Ticket: FRE-1722 (part 1 of FRE-1698). No ADR, by owner direction on the ticket.
Tier: Complex. It touches schema, a production write path, and the model that each role runs.

## Goal

The owner edits a role binding or a saved choice in the PWA. A saved override wins with no
rebuild. Clear restores the file value. Every accepted edit and Clear writes one audit row.

## Design

### Store (schema)

`docker/postgres/migrations/0037_settings_overrides.sql` and `docker/postgres/init.sql`:

- `settings_overrides(parameter TEXT PRIMARY KEY, value JSONB NOT NULL, updated_by UUID NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL)`. Global, not per user.
- `settings_edit_audit(audit_id BIGSERIAL PRIMARY KEY, changed_at TIMESTAMPTZ NOT NULL,
  changed_by UUID NOT NULL, parameter TEXT NOT NULL, action TEXT CHECK in (set, clear),
  old_value JSONB NULL, new_value JSONB NULL)`. `changed_by` has no foreign key, so the audit
  outlives a user row.

`parameter` is the menu row name (`role.primary`). Parts 2 and 3 add names and validators with no
schema change. SQLAlchemy models go in `service/models.py`, because `init_db` calls `create_all`
and a parity test reads them.

### Read path

`load_model_config` is sync, cached and called everywhere, including outside a request. It
cannot read Postgres. So:

- New `config/setting_overrides.py`: a process-level immutable snapshot of the stored overrides,
  with `current_overrides()` and `replace_overrides()`. No DB import.
- `model_loader.load_model_config`: for the real catalog only, when the snapshot holds a `role.*`
  entry, return a catalog built from the raw file content with those deployments replaced, and
  validated by `ModelConfig.model_validate`. Fixture catalogs never see an override. The result is
  cached with `lru_cache`, keyed on the path and the sorted role overrides. No override means the
  same object as today.
- An override that no longer validates (for example, a later deploy removed the deployment) is
  skipped with a warning. It must never make `load_model_config` raise, because that stops every
  turn. The menu shows the stored value as not usable, with Clear.
- `resolve_role_model_key` reads the raw matrix, not `ModelConfig`. When the root is the real repo
  root, the catalog is the real one, and the snapshot holds that role, it reads the binding from the
  loaded catalog, not from the snapshot. So a stale override falls back to the file value here too
  (Codex plan review). Six callers use this path (captains_log, insights, compressor,
  session_summary, consolidator, entity_extraction).

The snapshot is loaded in the gateway lifespan after `init_db`, and updated after each committed
write with the committed value. A load failure logs a warning and the file values apply.

**Assumption: the gateway is one process.** `docker-compose.cloud.yml`, `Dockerfile.gateway` and
`make dev` run uvicorn with no `--workers`. A second worker would not see an edit until restart. A
test asserts that the compose command has no `--workers`, so a later change fails loudly. A
per-turn DB read is rejected: it adds a query and a failure mode to every turn for a case that does
not exist.

**In-flight turns.** A turn pins `primary` through the selection contextvar. Another role resolves
at each call, so an edit reaches a running turn at that role's next call. This matches "applies at
the next turn or sooner" and needs no per-turn pin.

### Write API (`service/settings_edit.py`, routes in `service/app.py`)

- `PUT /api/v1/settings/overrides` with `{parameter, value}`.
- `DELETE /api/v1/settings/overrides?parameter=...`.
- Order: owner check (403), then validation (422 with a reason), then the write. A refused request
  writes no row.
- Owner: `settings.gateway_auth_enabled` is true, `settings.agent_owner_email` is set, and it equals
  `request_user.email`, case-insensitive. Dev mode takes the email from a plaintext header or falls
  back to the owner, so it never counts as the owner (Codex plan review). `make dev` shows no edit
  control for a role binding.
- `role.<name>` validation: the value is a string, the role exists in the file, the catalog with
  this one deployment replaced passes `ModelConfig.model_validate` (kind, dangling key, inherit
  rules), and the deployment's provider has its credential (`build_provider_availability`). This
  is not a live health check. A provider that is down fails the same way as a file binding to it.
  Any other parameter name is refused with a reason in this part.
- Set and Clear each commit one transaction that holds the override write and one audit row. Then
  the snapshot takes the committed value. There is no re-read, because a failed re-read after the
  commit leaves the snapshot stale. A process-level `asyncio.Lock` covers the write and the update,
  so the snapshot takes the writes in the order of the table (Codex plan review). Clear with no
  stored row returns `cleared: false` and writes no audit row.
- Audit values for a role binding are the values that apply: old is the stored override or the
  file value. New is the new override, or the file value on Clear.

### Saved choices

`PUT` and `DELETE /api/v1/preferences/constraint` stay per user and stay open to every user, as
today. `ConstraintPreferencesRepository.upsert` and `delete` write one audit row
(`saved_choice.<constraint>`) in their own transaction, so the decision card's "Remember this choice"
path is audited too. A clear with no row writes no audit row.

### Menu payload (`service/settings_menu.py`)

`SettingRow` gains:

- `file_default: Overridden | None`: the value when nothing overrides it.
- `edit: SettingEdit | None`: `{kind: role_binding | saved_choice, options: [...], current}`.

`Source` gains `"override"`. `MenuInputs` gains `file_models`, `overrides`, `is_owner`, all with
defaults so that existing fixtures still build. `_scrub_row` also scrubs `file_default` and options.

- Role rows: `file_default` on each row. `edit` and `clearable` only for the owner. Source is
  `"override"` when a stored override applies. When a session selection beats the binding on
  `role.primary`, the source stays `"session"` and a note says the edit reaches new conversations.
  This is a real gap for AC-5: an open conversation stores its own `primary`, so an edit of
  `role.primary` does not change that conversation. The row must say so.
- Saved choice rows: `file_default` always, `edit` for every user, except `PREFERENCE_NEVER_APPLIED`
  constraints, which the API refuses.

### PWA

`SettingsView.tsx`: a select and a Save button on each row with `edit`. Clear dispatches on the row
prefix. A file default line on each row that has one. The "overrides" line is hidden when it
repeats the file default. The group marker counts saved choices and overrides apart. A failed save
shows the API's reason. `agui-client.ts`: `setConstraintPreference`, `setSettingOverride`,
`clearSettingOverride`. `types.ts`: new fields. `public/sw.js`: bump `CACHE_NAME`.

## Steps

1. Tests for snapshot and loader overlay, then `config/setting_overrides.py` and `model_loader.py`.
   Test: `make test-file FILE=tests/personal_agent/config/test_setting_overrides.py`.
2. Migration, `init.sql`, models, repository. Apply the migration to the test stack only.
3. Tests for the routes, then `settings_edit.py` and routes.
   Test: `make test-file FILE=tests/personal_agent/service/test_settings_edit_api.py`.
4. Integration test against test Postgres: AC-1, AC-2, AC-3 on real rows.
   Test: `PERSONAL_AGENT_INTEGRATION=1 uv run pytest -m integration tests/personal_agent/service/test_settings_edit_db.py`.
5. Tests for menu rows, then `settings_menu.py`.
6. PWA tests, then PWA. Test: `cd seshat-pwa && npx vitest run src/__tests__/SettingsView.test.tsx && npm run lint`.
7. Step-6 gates, self-review, PR, handoff.

## Acceptance criteria (from the ticket)

| AC | Proof |
|----|-------|
| AC-1 | API edit, then `resolve_role_target` and `resolve_role_model_key` return the new model. Clear restores the file value. Fake store in `make test`, real table in the integration test. |
| AC-2 | Invalid value (422) and non-owner (403) both leave zero rows in both tables. |
| AC-3 | Each edit and each Clear, for a role binding and a saved choice, writes one audit row with who, when, parameter, old and new. |
| AC-4 | `SettingsView.test.tsx` shows edit control, live value, file default and Clear for a role binding and a saved choice. |
| AC-5 | Post-merge, master and owner. Use a role other than `primary`, or a new conversation. |

## Deploy notes

Run migration 0037 through `AGENT_DATABASE_ADMIN_URL` before the gateway rebuild. The saved-choice
writes need the audit table.
