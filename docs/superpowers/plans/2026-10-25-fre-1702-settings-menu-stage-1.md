# FRE-1702 — Settings menu, stage 1 (read-only) — implementation plan

- **Ticket:** FRE-1702 (stage 1 of FRE-1698). Owner approval: "Go", 2026-10-25.
- **Source:** `docs/research/2026-10-25-fre-1698-parameter-inventory.md` (F2, F3, F4, P1, P2, Appendix A).
- **Tier:** Standard. The change touches `src/` logic and a new API surface. Codex plan-review is required.
- **Scope rule:** the menu edits nothing. The Clear button for a saved choice is the only write.

## Assumptions (state before code)

1. The read endpoint is `GET /api/v1/settings`. The ticket names no path for it. The ticket names `GET` and `DELETE` for `/api/v1/preferences/constraint`.
2. `DELETE /api/v1/preferences/constraint` takes the constraint name as the query parameter `constraint_name`. It is idempotent: a missing row returns `cleared: false`, not an error.
3. Both new endpoints use `get_request_user`, the dependency of the existing `PUT` on the same path.
4. "Secret or infrastructure key" means the 11 secret, 73 infrastructure and 2 identity settings of Appendix A, plus a denylist (see Redaction). The API returns `"set"` or `"not set"` for these, never a value. Appendix A grouping alone is not trusted: `slm_health_url` and `url_guard_allowlist` sit in other groups and carry endpoints or domains.
5. Source labels are: `code default`, `.env`, `config file`, `database row`, `session`.
6. A setting has the source `.env` when its name is in `settings.model_fields_set`. Pydantic fills that set from the environment. The check needs no `os.getenv()`. Fields with a `default_factory` that reads the environment (`environment`, via `get_environment`) are absent from that set. A test lists every field with a `default_factory`, and a small fixed set `ENV_DRIVEN_FIELDS` labels those fields `.env`.
7. Rows for groups 1 to 5 come from the loaded catalogs, the database, and the live `CostGate.config` (not the `budget_policies` mirror). Rows for groups 6 to 9 and System come from `AppConfig`.
8. The System view is a second page, `/settings/system`, as P1 states. `/settings` links to it. Both pages render from the same API response.

## Design

### Row shape (Pydantic, frozen)

```
SettingRow:
  name: str
  value: JsonValue            # scalar, list, or dict. Secret/infra: "set" | "not set"
  source: Literal["code default", ".env", "config file", "database row", "session"]
  overrides: Overridden | None   # {"value": JsonValue, "source": <source>}
  clearable: bool = False        # true only for a saved-choice row that exists
SettingsGroup: id, title, rows, error: Literal["unavailable"] | None
SettingsMenu: groups (nine, in P1 order), system (one SettingsGroup)
```

`overrides` answers the question "which default does this value override". For a saved choice it holds the default. For a session model it holds the role binding.

### Groups

| Group | Rows | Live source |
|---|---|---|
| 1 This conversation | primary model, effort level | `session_model_selections`, `sessions.seshat_effort`; the optional query `session_id`. Without it, the YAML default with source `config file` |
| 2 Saved choices | the five static constraints and `artifact_builder`; `constraint_pause_timeout_seconds`; `location_enabled`; `location_precision`; the user's location consent | `user_constraint_preferences` for the request user; `AppConfig`; consent from the Neo4j person node, read as `GET /api/v1/preferences/location` reads it, and only when `location_enabled` is on. A read failure gives `"unavailable"` |
| 3 Models per role | one row per role in `ModelConfig.roles`; value = the deployment that the next turn uses; `overrides` = the YAML binding when a session selection or saved choice wins | `load_model_config()`, session selection, saved choice. For `artifact_builder`, the value comes from `resolve_effective_artifact_builder_deployment` with `build_provider_availability`, the resolver that the turn uses. A saved choice that names an unavailable or off-catalog deployment gives the YAML default as the value, and the row notes the saved choice |
| 4 Effort presets and model modes | three levels; one row per deployment (dict of its fields and modes); `llm_no_think_suffix`; `llm_append_no_think_to_tool_prompts` | `load_seshat_effort()`, `ModelConfig.models` |
| 5 Budgets and cost | 14 caps; per-role estimates; cost thresholds | `app.cost_gate.config` (the `BudgetConfig` that the running gate enforces), source `config file`. The `budget_policies` table is an audit mirror that can be stale after a failed sync, so the menu does not read it. If the gate is not started, the group returns `"unavailable"`. `AppConfig` for the thresholds |
| 6 How a turn runs | `AppConfig` settings of the Appendix A groups `routing and planner`, `context and history`, `timeouts and limits` | `AppConfig` |
| 7 Memory and recall | group `memory and recall` | `AppConfig` |
| 8 Tools and governance | group `tools and governance`; one row per tool (dict) from `tools.yaml`; permission rule counts | `AppConfig`, governance loader |
| 9 Grounding and quality | group `grounding and quality`; `grounding_enforcement_state` rows | `AppConfig`, database |
| System (read-only page) | `background jobs`; `infrastructure`; `secrets`; `identity` | `AppConfig`, set / not set for the last three |

### The group table

`service/settings_menu_groups.py` holds one frozen dict `SETTING_GROUPS: Mapping[str, str]` that maps each of the 361 `AppConfig` field names to an Appendix A group. A script generates it once from the Appendix A table. A test fails when `AppConfig` gains a field that the dict does not name. The test is the guard against drift.

### Builder inputs

`GET /api/v1/settings` depends on `get_request_user` and `get_db_session`. An async function `collect_inputs(db, user_id, session_id)` reads the saved choices, the session row and selections, the consent, and `app.cost_gate.config`. A pure function `build_menu(inputs)` turns them into `SettingsMenu`. Each group builder runs inside its own `try`. A failure logs `str(exc)` with `trace_id` on the server and puts the fixed code `"unavailable"` in the group `error` field. The response never carries an exception text.

### Redaction

Three layers. Each layer works alone.

1. **Group rule.** `_present(name, value)` is the only path from a setting to a row value. It returns `"set"` or `"not set"` when the name is in the secret, infrastructure or identity groups.
2. **Denylist.** `_present` also redacts a name that matches `*_url`, `*_uri`, `*_endpoint`, `*_host`, `*_path`, `*_dir`, `*_origin(s)`, `*_dsn`, `*_token`, `*_key`, `*_secret`, `*_password`, `*_email`, or that is in the fixed set `{slm_health_url, url_guard_allowlist}`. A test fails when a shown string setting matches none of the rules and holds `://` or a leading `/`.
3. **Leaf scrubber.** `scrub(payload)` walks the whole response (dicts, lists, nested models). It replaces any string leaf that contains `://`, starts with `/`, `~` or `$`, or contains `**` with `"set"`. Dict builders for governance and models also drop the keys `endpoint`, `allowed_paths`, `forbidden_paths`, `unattended_paths` and every command list, and show a count in their place.

**AC-4 tests.** (a) A secret setting with a sentinel value gives a row whose value equals exactly `"set"`. (b) A sentinel on each of: an infrastructure setting, `slm_health_url`, `url_guard_allowlist`, a model `endpoint`, a governance path, and an exception text from a failing loader. The sentinel is absent from the whole response body, and the row or the group is still present. (c) A sweep test builds the menu from the live `AppConfig` and the committed YAML, and asserts that no string leaf contains `://`, `/opt/`, `/app/` or `.ssh`.

### Saved choices and the default they override

- Static constraint: `value` = the saved `action_id`, or `"not set"` when no row exists. `overrides` = `{"value": "decision card (safe default: <last option>)", "source": "code default"}`.
- `artifact_builder`: `value` = the saved deployment key. `overrides` = `{"value": artifact_builder_default_key(config), "source": "config file"}`.
- `always_pause` is shown as is. It keeps the card.
- Group 3 row for `artifact_builder` shows the effective deployment as the value and the YAML binding in `overrides`. When the saved choice is valid and available, the two differ and the source is `database row`. This closes the failure that started FRE-1698.

### API additions

- `ConstraintPreferencesRepository.list_for_user(user_id)` and `.delete(user_id, constraint_name) -> bool`.
- `GET /api/v1/preferences/constraint` returns `{"preferences": [{"constraint_name", "preferred_action", "updated_at"}]}`.
- `DELETE /api/v1/preferences/constraint?constraint_name=<name>` returns `{"constraint_name", "cleared"}`. An unknown name returns 422, as the `PUT` does.
- `GET /api/v1/settings[?session_id=<uuid>]` returns `SettingsMenu`. A `session_id` of another user returns 404.

### PWA

- New routes `seshat-pwa/src/app/settings/page.tsx` (nine groups) and `seshat-pwa/src/app/settings/system/page.tsx` (System). One component `components/SettingsView.tsx` takes a `view` prop. It resolves the session as `ObserveView` does: the `?session=` query, then `LAST_SESSION_KEY`, then `resolveLastSessionId()`. It passes the id as `session_id` to the API.
- New helpers in `lib/agui-client.ts`: `getSettingsMenu`, `clearConstraintPreference`. New types in `lib/types.ts`.
- A "Settings" link beside the existing "Observe" link in `StreamingChat.tsx`. The link carries `?session=<id>` as the Observe link does. A test asserts the link target.
- The view renders each row as text: name, value, source badge, and the overridden value. A dict value renders as a nested list. The only button in the view is "Clear", and it appears only when `clearable` is true. The view has no input, select or toggle.

## Steps (TDD: the test first, then the code)

1. **Repository.** Test `tests/personal_agent/service/repositories/test_constraint_preferences_repository.py`: `list_for_user` returns the user's rows only; `delete` returns `True` for a row and `False` for no row. Then add the two methods.
   Run: `make test-file FILE=tests/personal_agent/service/repositories/test_constraint_preferences_repository.py`.
2. **Endpoints.** Tests in `tests/personal_agent/service/test_constraint_preference_api.py`: `GET` lists; `DELETE` clears; `DELETE` of an unknown name returns 422. **AC-3** — a stateful in-memory repository (patched in at the `AsyncSessionLocal` seam that `_load_constraint_preference` uses, and at the `app` dependency) holds a seeded `artifact_builder` row. The test calls `_maybe_pause_for_constraint` and asserts `preference_applied` with the saved key. It calls `DELETE`. It calls `_maybe_pause_for_constraint` again, with `register_and_push_constraint` and `phase_span` patched to take the timeout branch, and asserts a non-preference resolution. Then `resolve_effective_artifact_builder_deployment` returns `artifact_builder_default_key(config)`. Then add the two routes to `service/app.py`.
3. **Group table.** Test `tests/personal_agent/service/test_settings_menu.py::test_every_setting_has_a_group`. Then generate `service/settings_menu_groups.py`.
4. **Builders and redaction.** Tests: **AC-4** (sentinel secret), **AC-2** (seeded row shows beside the default), nine groups and System present in order, every row has a value and a source, no row except a saved choice is `clearable`. Then write `service/settings_menu.py` (pure builder functions) and the route `GET /api/v1/settings`.
5. **PWA.** Vitest `__tests__/SettingsView.test.tsx`: **AC-1** (nine group titles and System; each row shows value and source), **AC-5** (the only button role is "Clear"; no `input`, `select` or `textarea`), Clear calls the `DELETE` and the row refreshes. Then write the view, route, helpers and link.
   Run: `cd seshat-pwa && npm test -- SettingsView && npm run lint && npm run typecheck`.
6. **Playwright spec (required, AC-1)** `seshat-pwa/e2e/settings.spec.ts` with a mocked backend, as the other specs do. The Playwright config builds and serves the production bundle. The spec loads `/settings` and asserts the nine group titles. For each group it asserts that at least one row shows a value and a source label. It loads `/settings/system` and asserts the same. It saves a screenshot of each page. The host has Chromium installed; if the run fails for a host reason, the handoff says so and the criterion stays open.
7. **Gates.** `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`. Commit, then run the self-review on `git diff origin/main...HEAD`.

## Acceptance criteria → proof

| AC | Proof |
|---|---|
| AC-1 | `settings.spec.ts` against the built PWA (nine groups, System page, value and source per row, screenshots); `SettingsView.test.tsx` as a unit check |
| AC-2 | `test_settings_menu.py::test_saved_choice_shows_beside_default` with a seeded row |
| AC-3 | `test_constraint_preference_api.py::test_clear_restores_default_resolution` (through `_maybe_pause_for_constraint`) |
| AC-4 | `test_settings_menu.py::test_secret_row_is_exactly_set` and `::test_no_sentinel_on_any_response_path` and `::test_sweep_no_url_or_path_leaf` |
| AC-5 | `SettingsView.test.tsx::only_clear_is_actionable` and `test_settings_menu.py::test_only_saved_choices_are_clearable` |

## Out of scope

Edits of any other parameter (stage 2 and 3). FRE-1701 (parameters with no reader). The menu shows those parameters as they are.

## Risks

- A response of about 400 rows plus nested dicts is large. Mitigation: one request, no pagination. Measure the body size in the handoff.
- The `ModelConfig` and governance loaders can raise. Mitigation: a group that fails to load returns an empty row list and the fixed code `"unavailable"`. The other groups still render. Exception text goes to the server log only.
- The menu can disagree with the turn when a saved deployment is unavailable. Mitigation: Group 3 uses the turn's resolver. A test covers a saved key whose provider is unavailable.
- The budget audit table can be stale after a failed sync. Mitigation: the menu reads the live gate config. A test covers a gate that is not started.
- The session can be lost on the PWA side. Mitigation: the view resolves the session as `ObserveView` does. A test covers the link and the request URL.
- Governance dicts carry paths. Mitigation: the dict builders drop the path keys and the scrubber runs over the whole response.
- Diff class: the change reads a production table and deletes one row type (`user_constraint_preferences`). The delete is scoped to the request user and to one named constraint. Class: escalated only if review finds a broader delete.
