# FRE-1724: settings menu, edit part 3

Ticket: FRE-1724 (part 3 of FRE-1698). Uses the override store from FRE-1722 and the pattern of
FRE-1723. No ADR, by owner direction on FRE-1722. Tier: Complex. It touches governance, cost code and
a production write path. No schema change.

## Goal

The owner edits every remaining menu parameter in the PWA. Read-only is the exception, and each
read-only row shows its reason. A row states when an edit applies: at the next turn, or at the next
gateway restart.

## Measured facts

Measured on `origin/main` at 27b19185. Instruments: an AST scan of every read of an `AppConfig` field
in `src/personal_agent`, and a runtime spy on `AppConfig.__getattribute__` during
`import personal_agent.service.app`.

- `AppConfig` has 339 fields. All are menu rows (`setting.<field>`).
- Only 5 fields are read during the gateway import: `database_url`, `database_echo`,
  `cors_allowed_origins`, `gateway_mount_local`, `captains_log_index_prefix`. Logging also configures
  at import, so `log_dir` joins them. The AST flag on `promotion_min_seen_count` is a false positive:
  it is a `default_factory` lambda, and the spy did not see it.
- In the lifespan, `agent_id` and `otel_exporter_endpoint` (tracing) and `database_url` (preflight) are
  read before `load_overrides_at_startup`. All other lifespan reads run after it.
- Readers hold the one `settings` object or call `get_settings()`, so a change to that object reaches
  every call-time read. `config_guard` builds its own `AppConfig()`, but only the CI guard runs it.
- `AppConfig(field=value)` takes about 15 ms, and it runs the cross-field validators.
- `load_governance_config()` reads the files at each call. Three readers keep the result:
  `orchestrator/executor.py:_get_cached_governance_config`, `ToolExecutionLayer.governance_config`,
  `ModeManager.governance_config`.
- `uncapped_roles` has one reader, the role totality check at start (`role_map.py:192`). A change has
  no effect on a cap.
- `role_estimate` reaches a turn through `load_budget_config()` at `litellm_client.py:1571`.
- `grounding_enforcement.<model_key>` is state that the selector writes on each turn. An edit is
  overwritten.
- `max_concurrency` of a deployment is read once, when `concurrency.py:424` registers the semaphore.

## Classes

`config/setting_classes.py` holds the table (data only).

| Class | Meaning | When an edit changes the value that a reader gets |
|-------|---------|---------------------------------------------------|
| `next_turn` | every gateway read runs at call time | at once, for the next read |
| `next_restart` | at least one read is once per process | at the next gateway start |
| `read_only` | no override can reach it, or it is a secret | never. The row shows the reason |

A field with mixed reads is `next_restart`. The running process then keeps one value, so a reader
does not see a value that another reader does not see.

Read-only reasons:

- `secret`: 12 secrets, `neo4j_user`, and the URLs that hold a database password
  (`database_admin_url`, `sysgraph_database_url`, `managed_database_url`).
- `override store`: `database_url`.
- `import`: `database_echo`, `cors_allowed_origins`, `gateway_mount_local`,
  `captains_log_index_prefix`, `log_dir`.
- `early start`: `agent_id`, `otel_exporter_endpoint`.
- `logging`: `log_level`, `log_format`. Logging reads `APP_LOG_LEVEL` before the settings load.
- `profile`: `environment`, `deployment_profile`, `allow_test_writes_to_prod_substrate`. The checks that
  run while the settings load use them, and later reads must agree with those checks.
- `settings load`: `experimental_openai_base_llm_http_handler`, `owner_storage_allowlist`,
  `managed_elasticsearch_url`, `managed_neo4j_uri`, `managed_reranker_endpoint`, `managed_slm_endpoint`.
- `lockout`: `gateway_auth_enabled`, `agent_owner_email`, `cf_access_aud`, `cf_access_main_aud`,
  `cf_access_team_domain`, `gateway_access_config`, `governance_config_path`,
  `extra_permission_rules_path`. A wrong value locks the owner out or stops the start, and the menu
  then cannot undo it. **This reason is not in the ticket. The handoff names it for master.**
- `command`: `mcp_gateway_command`. The value is a process that the gateway starts. **Also named in
  the handoff.**
- `build`: `build_fingerprint`. `service_port`: the compose command sets the port.
- `cli`: `freshness_backfill_confirm`, `service_url`. Only a CLI process reads them.

Row families:

| Rows | Class | Write |
|------|-------|-------|
| `role.*`, `effort.*`, `cap.*`, `saved_choice.*` | `next_turn` (parts 1 and 2) | existing |
| `role.<role>` with one catalog option (master's note: `embedding`) | `read_only` | none |
| `saved_choice.<c>` in `PREFERENCE_NEVER_APPLIED` | `read_only` | none |
| `session.primary_model`, `session.effort_level` | `next_turn` | existing `PATCH` selection and effort |
| `location.consent` | `next_turn` | existing `PATCH /preferences/location`. Read-only when `location_enabled` is off |
| `tool.<name>`, `sub_agent_tools`, `mode_transition_rules` | `next_turn` | override |
| `deployment.<key>` | `next_turn`. The row notes that `max_concurrency` waits for a restart | override |
| `role_estimate.<role>` | `next_turn` | override |
| `uncapped_roles` | `read_only` | none |
| `grounding_enforcement.<model_key>` | `read_only` | none |

## Design

### AppConfig overlay (`config/setting_overrides.py`)

- `SETTING_PARAMETER_PREFIX = "setting."`.
- `apply_setting_overrides(*, at_start: bool) -> None`. It takes `usable_setting_rows()`: the whole
  set of stored rows first, because two rows can pass only together. If that fails, passes over the
  rows find the set that one row at a time can reach, so the order does not matter. A row that fails
  is skipped with `setting_override_skipped`, and the log holds the type of the error, not the value.
  Each accepted value goes in `vars(settings)[field]`. `vars` does not change `model_fields_set`, so
  the `.env` check of the menu stays right. The first change of a field saves its original in
  `_originals`.
- `at_start=True` covers `next_turn` and `next_restart`. `at_start=False` covers `next_turn` only.
- A `next_turn` field with no row goes back to its original.
- `setting_file_value(field)` returns the original, or the live value when no override applied.
- Startup calls it after `replace_overrides`. `set_override` and `clear_override` call it with
  `at_start=False`, inside `_edit_lock`.

### Validate (`service/settings_edit.py`)

`validate_setting_override(parameter, value)`:

1. A `read_only` field or an unknown field: refuse. The value is not read, and the reason does not
   hold it.
2. Strict type check: `TypeAdapter(Annotated[annotation, *metadata]).validate_json(dumps(value),
   strict=True)`. So `"yes"` does not coerce to `True`.
3. A string leaf with a password in a URL (`://user:pass@`) is refused, for every setting.
4. Cross-field check: `AppConfig(**stored_rows, field=value)`.
5. `sub_agent_max_tool_iterations` and `sub_agent_rounds_by_thoroughness` also run
   `_check_round_ceilings(get_seshat_effort(), candidate)`.
6. The stored form is `to_jsonable_python(getattr(candidate, field))`.

Refusal reasons use `errors(include_input=False)`. A cross-field message can quote a database URL with
its password (`settings.py:3296`). So a hidden field gets the error types only, and any other field
loses a message that holds `://`, `@` or `/`.

### Hidden values (AC-4)

A hidden field (`_is_hidden`) that is editable is write-only:

- `edit.current` is `None`, and the row shows `set` or `not set`.
- The PUT response, the audit `old_value` and `new_value`, and both log lines hold `set` or `not set`.
- Only `settings_overrides.value` holds the value. It must, to apply it. A password in a URL is refused
  by step 3.
- The repository takes `hidden`, because `clear` reads the deleted value itself and `set` writes the
  new value itself. Redaction in `settings_edit` alone leaves both in the audit row.
- The PUT route parses its own body after the owner check. The default 422 of FastAPI echoes `input`,
  and a body that is a bare string or holds a wrong-typed field can hold the value.

`_is_hidden` becomes the public `is_hidden_setting`, and the edit path imports it from the menu module.

### Governance overlay (`config/governance_loader.py`)

- `load_governance_config(None)` applies the stored rows. An explicit `config_dir` does not.
- `file_governance_config()` is the file with no override.
- `LiveGovernanceConfig` holds a config, and it reloads when `current_overrides()` is a new object (as
  `CostGate.config` does). A reload that fails keeps the last config. The three keeping readers use
  it, unless the caller passed a config. `ToolExecutionLayer` and `ModeManager` get a property with a
  setter, so existing tests that assign the attribute keep working.
- A stored row that fails is skipped with a warning. It must never raise.
- `tool.<name>` holds the edited fields. `allowed_paths`, `forbidden_paths` and `unattended_paths` stay
  file-only: their values are hidden and the menu shows counts, so a key with these names is refused.
- `sub_agent_tools` and `mode_transition_rules` hold the whole map, checked by their models.
- Unknown keys are refused, so a typo is not dropped silently.
- Strict: `model_validate_json(dumps(value), strict=True)`, then `GovernanceConfig.model_validate`.

### Model overlay (`config/model_loader.py`)

- `deployment_overrides()` joins `role_overrides()` in the cache key of
  `_load_model_config_with_overrides`.
- `deployment.<key>` holds the edited fields, merged over the file definition. `endpoint` is refused,
  because its value is hidden. The merged catalog goes through `_validate_catalog`, so the catalog
  validators (modes, default mode, reasoning domain) run. One that fails is skipped with a warning.
- `validate_deployment_override(key, value)` is the edit path.

### Budget (`cost_gate/policy.py`)

- `apply_budget_overrides` applies the caps through `apply_cap_overrides`, and `role_estimate.<role>`.
- `litellm_client.py:1571` applies it to the file config, so the estimator of a turn uses the edit.
- `validate_role_estimate_override` uses `RoleConfig`, strict.

### Menu (`service/settings_menu.py`)

- `SettingRow` gains `applies: "next_turn" | "next_restart" | "read_only"` and `read_only_reason`.
- `SettingEdit.kind` gains `setting`, `session_primary`, `session_effort`, `location_consent`.
- `SettingEdit` gains `input: "boolean" | "number" | "text" | "select" | "json"` and `nullable`, read
  from the field annotation. A `Literal` gives `select` with options.
- `_setting_row` adds `file_default`, the source `override`, `clearable`, and `edit` for the owner.
- A pending restart edit: the row shows the live value and a note. A pending Clear also has a note.
- A role with one option drops `edit`, and it reads `read_only`.
- The System group title loses "(read-only)".
- A secret row shows its reason.

### PWA (`SettingsView.tsx`)

- `SettingControl`: a checkbox, a number input, a text input, a select or a JSON textarea, by
  `edit.input`. Invalid JSON disables Save.
- `session_primary`, `session_effort` and `location_consent` call the existing client functions.
- The row shows "applies at the next gateway restart", or the read-only reason.
- Bump `CACHE_NAME` to `seshat-v71-fre-1724`.

### Startup (`service/app.py`, `settings_edit.py`)

- `load_overrides_at_startup` applies the rows with `at_start=True`.
- The served-catalog probe starts after the load, because it reads `slm_base_url` and the catalog.
- The fatal effort check runs before the load, against the file. After the load, a stored ceiling that a
  preset exceeds logs `effort_round_ceiling_conflict` and does not stop the start, because the menu
  then cannot undo the row. An edit refuses this state, so only a later file change can cause it.

## Codex plan review

All seven findings accepted and folded in above: the 422 echo, validator messages that hold a URL,
row order in the check, the effort check before the load, the probe before the load, the audit row on
Clear, and the wording of the read-only reasons.

## Code review

Two findings fixed on the branch: a Clear of a round ceiling is checked as an edit is, and it drops the
effort cache. The audit tells a stored JSON null from no row (`repo.find`).

## Left out on purpose

- Provider rows are not menu rows. `grounding_enforcement` stays state.
- A restart override that passes validation can still stop a start, for example a wrong
  `embedding_dimensions`. Recovery is one SQL delete on `settings_overrides`. The runbook states it.
- No kill switch for overrides. Nothing asked for one.

## Steps

1. Tests, then `config/setting_classes.py`.
   `make test-file FILE=tests/personal_agent/config/test_setting_classes.py`
2. Tests, then the AppConfig overlay.
   `make test-file FILE=tests/personal_agent/config/test_setting_overrides_apply.py`
3. Tests, then the governance overlay and the three readers.
   `make test-file FILE=tests/personal_agent/config/test_governance_overrides.py`
4. Tests, then the deployment overlay.
   `make test-file FILE=tests/personal_agent/config/test_deployment_overrides.py`
5. Tests, then the budget overlay. `make test-file FILE=tests/personal_agent/cost_gate/test_cap_overrides.py`
6. Tests, then `settings_edit.py`.
   `make test-file FILE=tests/personal_agent/service/test_settings_edit_api.py`
7. Tests, then `settings_menu.py`. `make test-file FILE=tests/personal_agent/service/test_settings_menu.py`
8. PWA tests, then the PWA. `cd seshat-pwa && npx vitest run src/__tests__/SettingsView.test.tsx && npm run lint`
9. Integration on the test Postgres:
   `PERSONAL_AGENT_INTEGRATION=1 uv run pytest -m integration tests/personal_agent/service/test_settings_edit_db.py`
10. Step-6 gates, self-review, PR with the table, handoff.

## Acceptance criteria (from the ticket)

| AC | Proof |
|----|-------|
| AC-1 | The PR table, printed from the class table and a real `build_menu`. |
| AC-2 | `test_every_appconfig_field_has_one_class`, `test_every_menu_row_has_a_class`. An AST test also fails if a `next_turn` field gains a read at import, in `__init__` or in `lifespan`, with a seeded negative. |
| AC-3 | Next turn: an API edit of `orchestrator_max_tool_iterations` changes `_resolve_max_iterations()`. Restart: an API edit of `mode_evaluation_interval_seconds` does not change the running value, and a simulated restart changes what `ModeController` reads. Also on real rows. |
| AC-4 | A sentinel secret through PUT and DELETE, and through a malformed body: not in the response, the audit table, or a captured log line. A write-only row: `set` only. |
| AC-5 | Post-merge, owner. |

## Deploy notes

`seshat-gateway` and PWA rebuild. No migration.
