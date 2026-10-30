# FRE-1719 — delete the per-tool policy fields that nothing reads

Ticket: FRE-1719. Follow-up to FRE-1701 (PR #1394). Owner default: delete.

## Measured (branch base `origin/main` 32c74f20)

| Field | Readers in `src/` (outside `governance/models.py`) | Choice |
|-------|------------------------------------------------------|--------|
| `ToolPolicy.allowed_commands` | settings menu display only | delete |
| `ToolPolicy.forbidden_commands` | settings menu display only | delete |
| `ToolPolicy.requires_outbound_gateway` | none | delete |
| `ToolPolicy.rate_limit_per_hour` | none (21 `tools.yaml` entries set it) | delete |
| `ToolDefinition.rate_limit_per_hour` | none (26 tool definitions set it) | delete |
| `ToolPolicy.category` | none. `ToolDefinition.category` is a separate field with readers | delete (owner, 2026-10-30, in the build seat) |

Readers that stay: `allowed_in_modes`, `forbidden_in_modes` (`tools/executor.py`),
`max_file_size_mb` (`tools/executor.py`), `allowed_paths`, `forbidden_paths`
(`tools/executor.py`, `tools/primitives/_governance.py`), `unattended_paths`
(`tools/primitives/write.py`), the four `loop_*` fields (`orchestrator/executor.py`).

Out of scope, noted in the handoff: `tools.yaml` keys that `ToolPolicy` does not declare
(`risk_level` 93, `timeout_seconds` 20, `requires_sandbox` 1, `description` 1). Pydantic ignores
them. The menu does not show them (owner, 2026-10-30: leave them, note it).

## Steps

1. Failing test first: `tests/personal_agent/governance/test_tool_policy_readers.py`.
   - `test_every_tool_policy_field_has_a_reader`: for each `ToolPolicy.model_fields` name, an AST
     pass over `src/personal_agent/**/*.py` finds a read of that field on a name that holds a
     `ToolPolicy`. A name holds one when the module binds it from `<expr>.tools.get(...)`,
     `<expr>.tools[...]`, a loop over `<expr>.tools.values()` / `.items()`, or a `ToolPolicy`
     annotation. A direct `<expr>.tools[...].field` chain also counts. The pass skips
     `governance/models.py` (the definition) and `service/settings_menu.py` (the display reads
     every field through `model_dump`, which is not enforcement). The unread set must equal an
     allowlist `dict[str, str]` (empty), and each reason must be non-blank.
   - A plain attribute-name match is not enough: other types load `.category`, so it hides the
     unread `ToolPolicy.category` (observed on the first run).
   - Limits, stated in the test docstring. A read through an alias, `self.<attr>`, a helper return
     or a variable `getattr` name is not seen. Such a read makes the test fail, and the fix is to
     extend the matcher or add an allowlist entry. The pass is not scope-aware, and it treats any
     `.tools` mapping as governance. These two limits can hide an unread field.
   - Seeded negative: the helper returns `False` for a name that no file reads.
   - Seeded positive: the helper returns `True` for `loop_max_per_signature`.
   - Run `make test-file FILE=tests/personal_agent/governance/test_tool_policy_readers.py`.
     Expect a failure that lists the five `ToolPolicy` fields.
2. `src/personal_agent/governance/models.py`: delete the five fields from `ToolPolicy`.
3. `src/personal_agent/tools/types.py`: delete `ToolDefinition.rate_limit_per_hour`. Delete each
   `rate_limit_per_hour=...` keyword in `src/personal_agent/tools/**` (single-line keywords).
4. `config/governance/tools.yaml`: delete the 21 `rate_limit_per_hour:` lines and the 99
   `category:` lines. Update the header comment if it names them.
5. `src/personal_agent/service/settings_menu.py`: drop `allowed_commands` and
   `forbidden_commands` from `_COUNTED_TOOL_FIELDS`.
6. `src/personal_agent/mcp/governance.py`: stop the template and the YAML writer from emitting
   `category`. Update `src/personal_agent/mcp/AGENTS.md`.
7. Tests:
   - Drop `category=` from the 8 `ToolPolicy(...)` calls (`tests/test_tools/test_executor.py`,
     `test_primitives_edit.py`, `test_permission_ask_flow.py`).
   - Drop `category` from the policy dicts in `tests/test_tools/test_primitives_read.py`,
     `test_primitives_write.py`, `tests/personal_agent/governance/test_permissions_config.py` and
     the seed YAML in `tests/test_mcp/test_governance.py`.
   - Replace the `category` asserts in `tests/test_config/test_governance_loader.py` and
     `tests/test_mcp/test_governance.py`.
   - Replace the `forbidden_commands` sentinel in
     `tests/personal_agent/service/test_settings_menu.py` with a path field.
   - Add a menu test: no `tool.*` row carries any of the five deleted names.
   - Add an MCP test: discovery appends a tool to a temporary `tools.yaml`, then
     `load_governance_config()` loads that directory and holds the new tool.
8. Docs: `docs/reference/TOOL_INTEGRATION_GUIDE.md` examples (drop `rate_limit_per_hour` and the
   YAML `category`). Historical ADRs, specs and completed plans stay as written.

## Verify

- `make test-file FILE=tests/personal_agent/governance/test_tool_policy_readers.py` → pass.
- `make test` → pass, with the same known failure as `main` at most.
- `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
- `git grep -n -E 'allowed_commands|forbidden_commands|requires_outbound_gateway|rate_limit_per_hour' -- src config/governance/tools.yaml tests`
  → only the deleted-name list in `test_no_tool_row_shows_a_deleted_policy_field`.
- `git grep -n -E '^\s+category:' -- config/governance/tools.yaml` → no hits.

## Risk

- `ToolPolicy` ignores unknown keys, so a `tools.yaml` from an older deploy still loads. A
  production `tools.yaml` that sets these keys keeps loading, and the keys do nothing, as now.
- No enforcement or execution change: no code read the deleted fields (Codex plan review found
  no reader either). The settings menu changes on purpose: each `tool.*` row loses the five
  properties. The PWA renders the row object generically (`SettingsView.tsx`), so it needs no
  change.
