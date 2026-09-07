# FRE-1444 — the worker's timeout comes from its role

**Ticket:** FRE-1444 (Approved, Tier-1:Opus, stream:build2)
**Design intent:** ADR-0145 D1 correction clause, AC-13 · FRE-1421 F5
**Blocked by:** FRE-1442 (Done — `resolve_role_target` now applies binding budget on any
resolved deployment, which is the mechanism this ticket depends on)

---

## Scope

1. `SubAgentSpec.timeout_seconds` becomes `float | None`, default `None`.
2. `settings.worker_timeout_seconds` is deleted. `expansion_controller` stops pinning both
   worker budget fields onto every spec.
3. `worker_hard_deadline_seconds` becomes a queue-absorption allowance. The outer deadline
   becomes `effective_timeout + absorption` instead of a fixed 85.
4. The seam: the client exposes its resolved default timeout. `run_sub_agent` resolves the
   effective timeout ONCE and uses that one number for the outer net, while dispatch keeps
   omitting `timeout_s` so the client resolves the same number for the inner call.

**Deviation from D1's sketch, and why.** D1 says dispatch "still omits `timeout_s`" and lets the
client fall to the effective definition. Only the LOCAL branch does that
(`litellm_client.py:1708`); the cloud branch applies a timeout only when the call names one
(`:1179`). A cloud-placed `sub_agent` deployment is reachable today — `claude_sonnet`,
`claude_haiku`, `gpt-5.4-mini` and `qwen3.8-27b-ovh` all self-pair in `defaults_by_primary` — so
omitting would dispatch a cloud worker with NO role budget.

Two ways to close it. Making the cloud branch fall back globally was rejected: every
factory-built cloud client carries a definition, so it would silently give the primary executor,
the session summariser, span extraction and Captain's Log feedback a new inner timeout, none of
it measured. Instead `run_sub_agent` passes the number it already resolved
(`timeout_s=effective_timeout`). This keeps D1's actual invariant — one number, resolved once,
used by the inner call and the outer net — while changing nothing for any other producer.
Handing back the value the client itself declared is not the "explicit beats the declaration"
trap D1 removes; it IS the declaration.

Proved by a seeded negative: reverting to `timeout_s=spec.timeout_seconds` fails
`test_ac1_the_roles_value_also_binds_on_cloud_placement` and nothing else.

**Fold-in, naming.** `worker_hard_deadline_seconds` is renamed `worker_queue_absorption_seconds`
(85.0 → 25.0). The field stops being an absolute deadline; keeping the old name on a 25 would
read as a budget cut, and a stale 85 override would then mean 85s of absorption. Measured, not
inferred: neither `AGENT_WORKER_HARD_DEADLINE_SECONDS` nor `AGENT_WORKER_TIMEOUT_SECONDS` appears
in any `.env`, `.env.example`, compose file or Docker config, AND
`docker exec cloud-sim-seshat-gateway env` shows no `AGENT_WORKER_*` variable at all. The rename
strands no operator override. `docs/reference/CONFIG_INVENTORY.md` is regenerated with
`scripts/audit/config_inventory.py generate`, not hand-edited.

**Out of scope.** `config/model_roles.yaml` keeps its current `sub_agent` binding. FRE-1445 moves
`default_timeout: 90` onto that binding. The deployment `qwen3.8-flash-next-instruct` already
declares `default_timeout: 90`, so the live number is 90 either way.

---

## Steps

### 1 — Failing tests first

New file `tests/personal_agent/orchestrator/test_worker_timeout_from_role.py`. Built on the
seeded-catalog + captured-`acompletion`-kwargs pattern of
`tests/personal_agent/config/test_role_binding_field_classes.py` (FRE-1442), because the ACs ask
what is dispatched, not what is configured.

Fixture catalog: one local provider, one deployment `worker_deployment` declaring
`default_timeout: 600`, and a `sub_agent` binding carrying `default_timeout: 90`. Three
distinguishable numbers — 600 (deployment), 90 (role), 60/120 (the two wrong answers the ticket
names) — so an assertion can only match one.

| Test | AC | Asserts |
|---|---|---|
| `test_ac1_dispatched_timeout_is_the_roles_value` | AC-1 | `run_sub_agent` with a spec carrying no override dispatches `timeout.read == 90.0`. Equality, so 60, 120 and 600 all fail. |
| `test_ac1_the_roles_value_also_binds_on_cloud_placement` | AC-1 | The same claim on the cloud branch, which resolves the number differently. |
| `test_every_dispatched_spec_defers_both_budgets` | AC-1 | The fan-out's own specs name neither budget — the settings pin is gone at its source. |
| `test_ac2_deadline_exceeds_the_timeout_by_the_absorption` | AC-2 | `_effective_hard_deadline(spec, 90.0) == 115.0`, and `> 90.0`. |
| `test_ac3_a_spec_with_no_override_computes_a_deadline` | AC-3 | The AC-1 run completes (`result.success is True`) — reaching dispatch at all proves the `None` case raised nothing. Plus a direct `_resolve_effective_timeout(spec, client) == 90.0`. |
| `test_ac4_the_outer_deadline_still_fires` | AC-4 | A hanging `respond` against a small absorption terminates via the outer `wait_for`: `success is False`, error matches `Timeout after`, and NOT `generation budget` (which would name the inner branch). |
| `test_ac5_changing_the_binding_moves_both_numbers` | AC-5 | Binding at 30 instead of 90: dispatched timeout 30.0 AND derived deadline 55.0. Both move. |

Confirm each fails before implementing.

### 2 — `sub_agent_types.py`

`timeout_seconds: float | None = None`. Docstring: `None` defers to the client's resolved
default, which is the role's effective `default_timeout` (ADR-0145 D1/D2).

### 3 — `litellm_client.py` — the seam

Add a `default_timeout_seconds` property returning `float | None`
(`float(self.model_def.default_timeout)`, `None` when no definition was supplied — only
possible for a direct cloud construction outside the factory). Read-only: neither dispatch
branch changes, so no producer acquires a timeout it did not have before.

### 4 — `sub_agent.py`

- New `_resolve_effective_timeout(spec, llm_client) -> float`: the spec's override, else the
  client's declared default. Raises `ValueError` when neither names one — an unconfigurable
  sub-agent, rather than inventing a number.
- `_effective_hard_deadline(spec, effective_timeout)` — new second parameter. Single-call
  deadline becomes `spec.hard_deadline_seconds or (effective_timeout + absorption)`, still
  clamped to at least `effective_timeout`, still scaled by the iteration cap for a tool-granted
  spec.
- `run_sub_agent`: resolve once at the TOP OF THE `try`, then log `sub_agent_start` with the
  resolved number (today it logs `spec.timeout_seconds`, which is `None` on every production
  spec after this change). Both move inside the try so a resolution failure produces an audit
  record and a reported `SubAgentResult`, not an escaping exception.
- `_run_tool_loop` takes `effective_timeout` and passes it as `timeout_s` — see the deviation
  note above.

### 5 — `expansion_controller.py`

Delete both spec lines and the now-orphaned `settings = get_settings()` at `:601`.

### 6 — `settings.py`

Delete `worker_timeout_seconds`. Rename `worker_hard_deadline_seconds` →
`worker_queue_absorption_seconds`, default `25.0`, description rewritten to state it as an
allowance added to the role's budget.

### 7 — Recalibrate existing tests

- `test_sub_agent.py` — `_effective_hard_deadline` calls gain the second argument;
  `test_no_tools_keeps_single_call_sizing` moves 60.0 → 85.0 (60 + 25), which is AC-2's shape
  on the pre-existing test.
- `test_sub_agent_types.py:25` — `spec.timeout_seconds` is now `None`, not 120.0.
- `test_expansion_controller.py:318` — drop the `worker_timeout_seconds` fixture pin.

### 8 — Docs

Regenerate `docs/reference/CONFIG_INVENTORY.md`. Amend `LiteLLMClient.__init__`'s `model_def`
docstring, which says "Ignored for cloud placement" — the new property reads it on either
placement.

---

## Verification

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

## Acceptance criteria

| AC | Criterion | Proof |
|---|---|---|
| AC-1 | Worker's effective timeout is the role's 90 | `test_ac1_dispatched_timeout_is_the_roles_value` — dispatched `timeout.read` equality |
| AC-2 | Deadline exceeds the timeout by the absorption | `test_ac2_deadline_exceeds_the_timeout_by_the_absorption` |
| AC-3 | A spec with no override computes a deadline without raising | `test_ac3_a_spec_with_no_override_computes_a_deadline` |
| AC-4 | The deadline still fires | `test_ac4_the_outer_deadline_still_fires` |
| AC-5 | Changing the binding moves both numbers | `test_ac5_changing_the_binding_moves_both_numbers` |
