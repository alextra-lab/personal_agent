# FRE-1700 — A Scaleway worker call waits at most 180 s

Ticket: FRE-1700 (Approved 2026-10-25). Backing ADR: ADR-0145 D2. Tier: Standard (`src/` logic).

## Problem

`resolve_role_target` applies the `sub_agent` binding's `default_timeout: 600` on any resolved
deployment (ADR-0145 D2, budget class). A worker on `glm-5.3-scaleway` or
`deepseek-v4.1-flash-scaleway` therefore waits up to 600 s. The deployments declare 180 s.
Trace 1d2d89fe held a turn for 274 s.

## Design

Add an optional deployment field `max_timeout: int | None` (seconds) to `ModelDefinition`. It is a
ceiling on any role's timeout on that deployment. Set it to 180 on the two Scaleway entries.

`resolve_role_target` applies the binding's budget first, as today. It then lowers
`default_timeout` to `max_timeout` when the result is larger.

A model validator on `ModelDefinition` rejects `default_timeout > max_timeout`. This keeps the
no-binding early return of `resolve_role_target` safe without a second clamp.

### How this fits ADR-0145 D2

D2 says the budget "always applies" because it describes the job, not the model. That stays true.
The role still owns the number. The ceiling states a fact about the deployment: the longest wait
that the owner accepts from that API. The ceiling does not drop the binding's budget. It limits the
budget. A deployment with no `max_timeout` behaves exactly as before.

### Scope of the ceiling

The ceiling limits the timeout that a role resolves to through `resolve_role_target`. It is not a
wire-level limit on every call. Two doors skip it:

- A call site that passes its own `timeout_s` (for example `artifact_builder`). D2 already records
  these sites as bypassing the binding.
- `get_llm_client_for_key` (`factory.py:278`) reads the raw deployment. The validator makes the
  deployment's own `default_timeout` safe there. The cloud branch sends a timeout only when the
  caller passes one (`litellm_client.py:1517`).

The production worker path uses `get_llm_client("sub_agent")` (`executor.py:5864`), so it is covered.

### Catalog snapshot

`max_timeout` is not added to `_BEHAVIOUR_FIELDS` in `test_catalog_snapshot.py`. The golden resolves
roles on the default primary, and no cell selects a Scaleway key. The dedicated live-catalog tests of
this ticket cover the two Scaleway keys. A golden rebaseline would add noise and no proof.

### Out of scope
- No change to the `sub_agent` binding. Local workers keep 600 s.
- No ADR edit. The ADR session owns that. The PR names the gap.

## Steps

1. **Test first** — `tests/personal_agent/config/test_deployment_timeout_cap.py`
   - AC-1: build a catalog with the `sub_agent` binding at 600 and a cloud deployment with
     `max_timeout=180`. Resolve `sub_agent` on it. Expect 180.
   - AC-1 on the live catalog: select each Scaleway key as primary, resolve `sub_agent`. Expect 180.
   - AC-2: resolve `sub_agent` on `qwen3.8-flash-next` in the live catalog. Expect 600.
   - A deployment without `max_timeout` keeps the binding's value, even when it is larger than 180.
   - A cap above the binding's value does not raise the timeout (binding 90, cap 180, result 90).
   - Validator: `default_timeout=300, max_timeout=180` raises `ValidationError`.
   - AC-3a: a worker client built from the capped definition (cap 1 s, hanging `litellm.acompletion`)
     dispatches with `timeout.read == 1.0`. `run_sub_agent` returns `success is False` and
     `stop_reason == "timeout"` (the inner budget fires first, `sub_agent.py:2103`). Elapsed time
     stays below the outer deadline. The error does not start `Timeout after`: that text belongs to the
     outer `wait_for` (`sub_agent.py:2663`).
   - AC-3b: `ExpansionController.execute` with two workers. The patched `run_sub_agent` calls the
     real one: the hanging capped client for task 0, a working client for task 1. The turn ends with
     `degraded is False`, `successful_count == 1`, `failed_count == 1`. The failed result is the real
     AC-3a timeout, not a fabricated one.
   Run: `uv run pytest tests/personal_agent/config/test_deployment_timeout_cap.py -x`.
   Expect failures on AC-1 and the validator before step 2.
2. `src/personal_agent/llm_client/models.py` — add the field, the docstring entry, the validator.
3. `src/personal_agent/config/model_loader.py` — clamp in `resolve_role_target`. Update its
   docstring (budget bullet).
4. `config/models.yaml` — add `max_timeout: 180` to both Scaleway entries. Add the field to the
   header comment list.
5. Run `make test-file FILE=tests/personal_agent/config/test_deployment_timeout_cap.py`, then the
   related files (`test_catalog_snapshot.py`, `test_role_binding_field_classes.py`,
   `test_worker_timeout_from_role.py`, `test_inherit_deployment_resolution.py`), then the gates.

## Acceptance criteria

| AC | Proof |
|----|-------|
| AC-1 | `test_sub_agent_on_scaleway_primary_is_capped` (both keys). Fails before step 2. |
| AC-2 | `test_sub_agent_on_default_primary_keeps_600` |
| AC-3 | `test_hanging_capped_worker_ends_as_failed_result`, `test_turn_goes_on_without_the_timed_out_worker` |
| AC-4 | Master. Post-deploy runbook in the handoff comment. |

## Risks

- A future deployment sets `max_timeout` below the binding by mistake. Mitigation: the ceiling is
  explicit in the catalog and covered by the live-catalog test.
- `config_guard` could reject the new key. Mitigation: it checks binding keys only
  (`_BINDING_FIELDS`). Step 5 runs the guard tests.
