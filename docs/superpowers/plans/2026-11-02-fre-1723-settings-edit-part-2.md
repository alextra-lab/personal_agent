# FRE-1723: settings menu, edit part 2

Ticket: FRE-1723 (part 2 of FRE-1698). Uses the override store from FRE-1722. No ADR, by owner
direction on FRE-1722. Tier: Complex. It touches cost code and the values that a turn resolves.
No schema change.

## Goal

The owner edits an effort preset or a budget cap in the PWA. A stored override wins with no
rebuild. Clear restores the file value. A cap edit changes what the cost gate enforces.

## Parameters

| Parameter | Value | Validated by |
|-----------|-------|--------------|
| `effort.default_level` | a level name | `SeshatEffortConfig`, strict |
| `effort.level.<level>` | `{expansion, sub_agent_rounds, planner_policy, primary_mode}` | `EffortSpec` through `SeshatEffortConfig`, strict, plus the round ceiling |
| `cap.<window>.<role>` | a number, in US dollars | `CapEntry`. The cap must exist in the budget file |

These names are the existing menu row names. Role estimates and `uncapped_roles` are not in scope.

## Design

### Effort (`config/seshat_effort.py`)

- `file_seshat_effort()`: the old `get_seshat_effort` body, cached once. The file values.
- `get_seshat_effort()`: the file values with the stored overrides applied. The result is cached
  with `lru_cache`, keyed on the overrides as sorted `(parameter, canonical JSON)` pairs. With no
  override, it returns the file object.
- `effort_spec`, `hydrate_effort` and `session_api._effort_levels` already read
  `get_seshat_effort`, so they need no change. A turn resolves its spec once at launch
  (`app.py:416`, `app.py:2718`), so an edit reaches the next turn and never a running turn.
- A stored override that no longer validates (for example, `.env` lowered the ceiling) is skipped
  with a warning. It must never raise, because every turn reads this path.
- `validate_effort_override(parameter, value)` returns the canonical value to store. It uses
  `model_validate(strict=True)`, so `"yes"` and `true` do not coerce. A `level` key in the value is
  refused, because the key of the parameter names the level.
- Startup still validates the file only (`app.py:902`). This runs before the overrides load.

**ADR-0157 D7 rule 2.** A change to the default level's values, or to which level is the default,
is a routing change that needs a passing D7 run. The ticket states: "An owner edit in the UI is the
owner's own decision, so it needs no further approval." So the menu does not block the edit. The row
of the default level and the `effort.default_level` row carry a note. The handoff names this for
master. The pin test reads the file values and stays valid.

An edit of `effort.default_level` reaches new conversations only, because a session stores its
level at creation. The row note says so.

### Budget caps (`cost_gate/`)

`CostGate.config` is a frozen object set at start. Readers: `reserve` (`gate.py:146`),
`snapshot_counters` (`gate.py:535`), `_counter_ids_for_role` (`gate.py:649`) and the menu. The gateway
builds one gate (`app.py:966`). Twenty scripts in `scripts/` build their own gate from the file, and
they share the same counters (Codex plan review).

- `CapEntry.cap_usd` gains `Field(ge=0, max_digits=10, decimal_places=6)`. This matches
  `budget_policies.cap_usd DECIMAL(10, 6)`, and it refuses a negative cap in the loader's own model.
  A bad file then refuses at load. All 14 deployed caps pass this check. Zero stays valid: it
  refuses every paid reservation, and the snapshot and the dashboard already handle a zero cap.
- `policy.cap_parameter(cap)`, `policy.validate_cap_override(config, parameter, value)` and
  `policy.apply_cap_overrides(config, overrides)`. A bool, a string or a non-finite number is refused.
  A cap is stored as a JSON number (a float), so that a stored row passes the same check on reload.
- `apply_cap_overrides` skips a stored row that fails the check, with a warning, and the file cap
  applies. It must never raise, because every paid reservation reads it. A row for a cap that the
  file no longer holds has no effect.
- `CostGate.__init__` stores `file_config`. `CostGate.config` becomes a read-only property: the file
  config with the stored caps applied, recomputed only when the override mapping object changes
  (`replace_overrides` makes a new object for each write). So every reader above follows an edit, and
  the order of the startup load and the gate construction does not matter.
- `CostGate.connect` reads the `cap.*` rows into the process copy. So a script gate enforces the
  stored caps too, with no change to the scripts, and it does not take the role bindings. A script
  that runs during an edit keeps the caps that it read at connect. A read failure logs a warning, and
  the file caps apply. An eval stack has no such rows, so it reads the file caps.
- `budget_policies` mirrors what the gate enforces, because the Cost and Budget dashboard joins on it:
  - the startup sync passes `cost_gate.config`, not the file config;
  - after a committed cap edit or Clear, `settings_edit` calls `sync_budget_policies_to_db` with
    `gate.config`, inside `_edit_lock`. A failure logs an error and does not undo the edit, because
    the gate does not read this table. This matches the startup path.

### Write path (`service/settings_edit.py`)

- `validate_override` returns the value to store, and `set_override` stores that value.
- It dispatches on the prefix. Any other name stays refused with a reason.
- `file_value` gains the two families. A budget file read error on Clear gives `None`, as for a
  parameter that the file no longer holds.

### Menu (`service/settings_menu.py`)

`MenuInputs` gains `file_effort` and `file_budget`, both defaulting to `None`. `collect_inputs` reads
`get_seshat_effort` (live), `file_seshat_effort` and `gate.file_config`.

- `SettingEdit.kind` gains `effort_default_level`, `effort_preset` and `budget_cap`.
- `SettingEdit` gains `preset_choices`: the allowed `planner_policy` and `primary_mode` values, read
  from the `Literal` types, so the PWA does not repeat them.
- Effort and cap rows gain `file_default`, the source `override` when the stored value applies, a
  note when it does not, and `edit` and `clearable` for the owner only.
- `session.effort_level` names the right source for the default it overrides.

### PWA (`SettingsView.tsx`)

- `effort_default_level`: the existing select.
- `budget_cap`: a number input and Save. It sends a number.
- `effort_preset`: a checkbox, a number input and two selects, and Save. It sends the whole preset.
  With expansion off, it sends null rounds and a null planner policy, as the `fast` preset does.
- A refused save shows the API's reason on the row, as today. Bump `CACHE_NAME`.

### Left unfixed on purpose

Codex plan review: a cancel or a lost connection can land after the commit and before the process copy
changes. The row is then new, and this process keeps the old value until a restart. The gate and the
menu still agree with each other, and the owner sees an error. A shield around the write does not fit,
because the request's session closes on a cancel. This is the same as part 1.

### Public repo

`config/governance/budget.yaml` is gitignored because the caps are private. Tests build their own
`BudgetConfig`. No deployed cap value goes in a test, the plan, the PR or a comment.

## Steps

1. Tests, then the effort overlay.
   `make test-file FILE=tests/personal_agent/config/test_seshat_effort_overrides.py`
2. Tests, then `CapEntry`, `policy.py` and the gate property.
   `make test-file FILE=tests/personal_agent/cost_gate/test_cap_overrides.py`
3. Tests, then `settings_edit.py` and the startup sync.
   `make test-file FILE=tests/personal_agent/service/test_settings_edit_api.py`
4. Integration tests on the test Postgres: a real reservation is refused, a restart keeps the
   override, and `budget_policies` follows.
   `PERSONAL_AGENT_INTEGRATION=1 uv run pytest -m integration tests/personal_agent/service/test_settings_edit_db.py`
5. Tests, then `settings_menu.py`. `make test-file FILE=tests/personal_agent/service/test_settings_menu.py`
6. PWA tests, then the PWA.
   `cd seshat-pwa && npx vitest run src/__tests__/SettingsView.test.tsx && npm run lint`
7. Step-6 gates, self-review, PR, handoff.

## Acceptance criteria (from the ticket)

| AC | Proof |
|----|-------|
| AC-1 | An API edit of a preset, then `effort_spec` and `rounds_for_turn` return the new value. Clear restores the file value. |
| AC-2 | An API edit lowers a cap, then `CostGate.reserve` raises `BudgetDenied` above the new cap on real counters. Clear restores the file value. After a simulated restart (empty process copy, startup load, a new gate) the override still holds. |
| AC-3 | A negative cap and invalid presets get 422 with a reason and write no row. Each edit writes one audit row. |
| AC-4 | Post-merge, owner. PWA tests cover the controls before that. |

## Deploy notes

`seshat-gateway` and PWA rebuild. No migration. FRE-1722's migration 0037 must already be applied.
