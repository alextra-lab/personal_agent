# FRE-1701 — Remove or refuse the parameters whose change has no effect

Ticket: FRE-1701 (Approved, owner go 2026-10-30). Source: FRE-1698 inventory findings F7, F8, F9,
plus item 4 (comment by cc-1build, FRE-1702).

Rule from master's guidance: the default choice is **delete**. Wire an item only if a current
behavior depends on its promise. A wired cost, budget or governance behavior needs the owner's OK.
This plan wires nothing, so no OK is needed.

## Measurement at `origin/main` 5152f432

- The FRE-893 audit (`scripts/audit/config_usage_audit.py`, AST alias-aware scan) finds 27
  `AppConfig` fields with no production read: the ticket's 22, plus `service_host`,
  `experimental_openai_base_llm_http_handler`, `log_level`, `log_format`, `scaleway_secret_key`.
- The last four have a real reader that the audit does not see:
  - `log_level`, `log_format`, `experimental_openai_base_llm_http_handler`: read as `config.<field>`
    by module functions inside `settings.py` (lines 3843, 3886, 3887).
  - `scaleway_secret_key`: read by name through `config/models.yaml` `auth_env`
    (`litellm_client.py:1425` `getattr(_settings, provider_def.auth_env)`).
- `service_host` has no reader. Uvicorn takes the host from its command line.
- Governance: no reader for `mode_constraints`, all four `safety.yaml` sections, the three listed
  `ModeDefinition` fields, `tool_categories`, `mode_policies`. `ModeDefinition.thresholds` and
  `ModeDefinition.background_monitoring_enabled` also have no reader.
- Item 3: `set_current_selection` receives only `{"primary": ...}` (`service/app.py:411`, `:2621`).
  The PWA calls the PATCH only for `primary` (`StreamingChat.tsx:112`). Live rows: `primary` 276, other roles 0.
- Item 4: the PWA never calls the constraint PUT (it calls DELETE only). Live
  `user_constraint_preferences` rows: 0.

## Choice per item (AC-1)

| Item | Choice |
|------|--------|
| 22 listed `AppConfig` fields | delete |
| `service_host` (fold-in: AC-2 test finds it) | delete |
| `governance/models.yaml` `mode_constraints` | delete the file, the loader read, `ModeModelConstraints`, `ModelRoleConstraints` |
| `safety.yaml` `rate_limits`, `content_filtering`, `outbound_gateway`, `human_approval` | delete the file, the loader read, `SafetyConfig` and its five sub-models |
| `modes.yaml` `max_concurrent_tasks`, `require_approval_for`, `allowed_tool_categories` | delete from YAML and `ModeDefinition` |
| `modes.yaml` `thresholds`, `background_monitoring_enabled` (fold-in) | delete from YAML, `ModeDefinition`, and `ModeThresholds` |
| `tools.yaml` `tool_categories` | delete from YAML, `GovernanceConfig`, `ToolCategory` |
| `tools.yaml` `mode_policies` (no model field; Pydantic drops it) | delete from YAML |
| Item 3: selection PATCH for `artifact_builder`, `sub_agent` | refuse (422) every role that the turn does not read |
| Item 4: constraint PUT for `attachment_cost`, `spend_threshold` | refuse (422) |

## Steps

1. **AC-2 test first.** In `tests/scripts/test_config_usage_audit.py`, add
   `test_every_appconfig_field_has_a_production_reader` with an explicit
   `_NO_PRODUCTION_READER: dict[str, str]` allowlist (field → reason; empty at the end). Add a
   seeded-negative test and positive tests for the two new evidence sources. Run → fails (27 unread).
2. **Audit evidence.** In `scripts/audit/config_usage_audit.py`:
   - `settings_module_read(name)`: AST pass of `settings.py` with `collect_field_reads`, so
     `config.<field>` reads in module functions count.
   - `auth_env_read(name)`: `config/models.yaml` names the field as a provider `auth_env`.
   - `has_production_read(name)`: src reads OR self read OR settings-module read OR manifest OR
     auth_env. `categorize` uses it. Run → fails with exactly the 23 dead fields.
3. **Delete the 23 fields** from `settings.py`, `service/settings_menu_groups.py`, `.env.example`.
   Remove `AGENT_CLOUD_WEEKLY_BUDGET_USD` from `docker-compose.eval.yml` (2 lines). Fix the
   `config/governance/budget.yaml` comment that says to keep it in sync.
   Fix the readers in tests and scripts:
   - `tests/test_config/test_settings.py:92`, `tests/test_orchestrator/test_routing.py`,
     `tests/test_orchestrator/test_routing_delegation.py`, `tests/integration/test_e2e_flows.py`,
     `tests/personal_agent/orchestrator/test_intra_turn_digest.py`: drop the dead setattr/override.
   - `tests/test_orchestrator/test_compression_gate_proof.py:335`: the soft path is gone (FRE-941),
     so assert `frozen_ceiling < hard_threshold`.
   - `scripts/eval_04b_occupancy_curve.py`: pin `SOFT_RATIO = 0.65` as the historic value; drop
     the two print lines of deleted fields.
   - `scripts/eval/fre475_compression_ab/offline_mechanism.py`: drop `tool_result_digest_keep`
     from `DIGEST_CONFIG` (its `getattr(settings, key)` would raise).
   Run step-1 test → passes.
4. **Governance.** Delete `config/governance/safety.yaml` and `config/governance/models.yaml`.
   Edit `modes.yaml`, `tools.yaml`. Edit `governance/models.py`, `governance/__init__.py`
   exports, `config/governance_loader.py` (stop reading the two files). Update
   `tests/test_config/test_governance_loader.py` and drop `mode_constraints={}` from the
   `GovernanceConfig(...)` calls in tests. Add a test: `GovernanceConfig` has no
   `mode_constraints`/`safety`/`tool_categories` field and the loader loads a dir with no
   `safety.yaml`/`models.yaml`.
5. **Item 3.** `config/model_loader.py`: `TURN_SELECTED_ROLES = frozenset({"primary"})`, with a
   comment naming the two `set_current_selection` call sites. `gateway/session_api.py`
   `update_session_selection`: 422 before any storage if `body.role not in TURN_SELECTED_ROLES`.
   Tests: PATCH for `artifact_builder` and `sub_agent` → 422 and no upsert; `primary` → 200.
6. **Item 4.** `orchestrator/constraint_options.py`: `PREFERENCE_NEVER_APPLIED =
   frozenset({"attachment_cost", "spend_threshold"})`. `service/app.py`
   `update_constraint_preference`: 422 for those names, before the upsert.
   `service/settings_menu.py` imports the shared constant in place of `_NEVER_APPLIED`.
   Tests: PUT for each → 422 and no upsert; `tool_iteration_limit` PUT → 200.
7. **Docs.** Regenerate the `CONFIG_INVENTORY.md` autogen block; fix its governance rows.
   Fix the examples in `config/AGENTS.md`, `brainstem/sensors/AGENTS.md`,
   `docs/reference/CODING_STANDARDS.md`, `docs/guides/CONFIGURATION.md`,
   `docs/guides/USAGE_GUIDE.md`. ADRs and archived plans stay as history.
8. **Gates.** `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`. Self-review. PR lists the choice per item and the live `.env`
   line `AGENT_CLOUD_WEEKLY_BUDGET_USD` for the owner to remove.

## Test commands

- `make test-file FILE=tests/scripts/test_config_usage_audit.py`
- `make test-file FILE=tests/test_config/test_governance_loader.py`
- `make test-k K=selection`
- `make test-k K=constraint_preference`
- `make test-file FILE=tests/personal_agent/service/test_settings_menu.py`
- `make test`

## Diff class

Escalated: the diff deletes governance config and the dead `cloud_weekly_budget_usd` cost
setting. Flag for owner `/code-review ultra` before merge.

## Out of scope (reported, not changed)

`ToolPolicy` fields `allowed_commands`, `forbidden_commands`, `requires_outbound_gateway`,
`rate_limit_per_hour` have no reader in `src/` either. The settings menu shows them per tool.
Not in this ticket's item list. Reported to master in the handoff.
