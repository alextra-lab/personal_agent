# FRE-1608 — Reflection inputs are real

Ticket: FRE-1608 (Self-analysis 1/4, ANALYSIS). Tier: Standard (touches `src/` logic and a settings default).
Design intent: `docs/specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md`, ADR-0056 (failure-path reflection),
ADR-0129 (durations moved to Tempo spans), ADR-0156 D6.3 (`tool_call_completed success=false`).

## Evidence base (production ES, `agent-logs-*`, 30 days, read-only, 2026-10-14)

| Event | Count | Fields the old builder read | Present? |
|---|---|---|---|
| `model_call_completed` | 2458 | `duration_ms` | 0 |
| `tool_executed` | 0 | the whole event | absent |
| `tool_call_completed` | 1928 | `tool_name`, `success` | yes; no duration |
| `tool_call_failed` | 75 | `tool_name`, `error` | yes |
| `model_call_error` | 19 | `error`, `error_type` | yes |
| `planner_failed` | 0 in 30 days, 9 in 45 days (emitted by `expansion_controller.py`) | `reason` | yes |
| `expansion_controller_complete` | 66 (`degraded=true` on 2) | `degraded` | yes |

The `errors` line of `_summarize_telemetry` reads `e["message"]`. In ES `message` repeats the event name,
so it never carried an error text. The real field is `error`.

## Decisions

| Defect | Decision |
|---|---|
| 1 duration | State "not available". Durations are Tempo span attributes (ADR-0129). No Tempo call from the reflection path. |
| 2 tool names | Read `tool_call_completed` and `tool_name`. Count a failed call once, even when it logs both `tool_call_failed` and `tool_call_completed success=false`. |
| 3 rating | Remove the per-callsite mean, the quality line and the wording that tells the model to read a low mean as a signal. A turn's own rating does not exist at reflection time (the user rates later), so nothing is passed. `load_mean_rating_lookup` is removed. `get_mean_rating_by_callsite` stays: the FRE-1601 connector-leak tests use it as their detector vehicle. |
| 4 failure path | Default `failure_path_reflection_enabled` to `True`. Keep the flag as a kill switch. Excerpt also covers `tool_call_completed success=false`, `model_call_error`, `planner_failed`, `fallback_planner_used`, `graceful_degradation_triggered`, `turn_degraded`, the two `*_exceeded_mid_call` events and `expansion_controller_complete degraded=true`. |
| 5 eval isolation | `_apply_read_before_emit` returns early when `eval_mode` is true. `repo.read_before_emit` increments or inserts on every branch except `decided_skip`, so a skip is the only safe form. Codex review found a second path: `CaptainLogManager.save_entry` merged an eval entry into a production file with the same fingerprint and raised its `seen_count`. `_find_entry_by_fingerprint` now matches only a file of the same `eval_mode`. |
| 6 cadence gate | Keep the eval bypass. FRE-523 made it deliberate, and `test_eval_isolation.py` requires eval reflection to run. Defect 5 removes the harm the bypass did. No code change. Observation, not fixed here: the FRE-453 harness maps only the manual-fallback reflection events, so it does not see the DSPy path. |
| 7 `prompt_component_ids` | Not a defect in this pipeline. The field is present on all 2458 events. It is an empty list on 1770 of them, and ES `exists` does not match an empty array. Only `orchestrator.primary` composes named components (688 of 688). The other callsites (`role.sub_agent` 1277, `role.span_extraction` 267, `role.entailment` 84, `role.primary` 67, `role.entity_extraction` 54, `role.session_summary` 19, `role.artifact_builder` 2) use the fallback identity. Fix here: the manifest says "not recorded for callsite X" instead of "(none)". Owner of the cause: the call sites of `llm_client/prompt_identity.py` that pass no `component_ids`. |

## Steps

1. **Fixtures** (scratchpad script, not committed). Pull the 6 canceled tickets' traces. Keep event name and
   whitelisted fields only. Redact `error` text. Write `tests/test_captains_log/fixtures/fre1608_traces.json`
   and `production_event_fields.json`.
2. **Tests first** — `tests/test_captains_log/test_reflection_inputs_real.py`:
   - AC-1: record every `(event, key)` the builders read on the real traces. Assert each pair exists in the production field snapshot.
   - AC-2: replay the 6 traces. Assert no `0ms`, no `2.00`, and `had_errors=True` on FRE-1373 and FRE-1412.
   - AC-3: replay FRE-1404 (clean). Assert `had_errors=False`, empty excerpt, no error or degradation line.
   - AC-4: stub repo. `eval_mode=True` makes zero `read_before_emit` calls. `eval_mode=False` makes one.
   - AC-5: no model call anywhere in these tests.
   Confirm each fails on `origin/main`.
3. **Implement** in `captains_log/reflection.py`, `captains_log/prompt_manifest.py`, `captains_log/reflection_dspy.py`, `captains_log/manager.py`, `config/settings.py`. A refused call logs only `error_code`, so the excerpt falls back to it when `error` is absent. The manual fallback prompt gets the failure facts through the telemetry summary, not the excerpt.
4. **Update dependants**: `test_prompt_manifest.py`, `test_es_client_connector_hygiene.py`, and the six tests that patch `reflection.load_mean_rating_lookup` (`test_dialect_parameter_block.py`, `test_reflection_missing_skills.py`, `test_reflection_thread_offload.py`, `test_reflection_dspy_gated.py`, `test_reflection_manual_fallback_role.py`, `test_reflection_prompt_manifest_field.py`).
5. **Docs**: `docs/specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md`, `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` (Stream 1), `.env.example`, `docs/guides/CONFIGURATION.md`, `docs/reference/CONFIG_INVENTORY.md`.
6. **Gates**: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`. Commit, then self-review on `git diff origin/main...HEAD`.

## Verify commands

```bash
make test-file FILE=tests/test_captains_log/test_reflection_inputs_real.py
make test-file FILE=tests/test_captains_log/test_prompt_manifest.py
make test-file FILE=tests/test_captains_log/test_failure_excerpt.py
```
