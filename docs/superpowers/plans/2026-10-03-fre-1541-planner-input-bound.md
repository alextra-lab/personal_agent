# FRE-1541 — the planner's bounded input, briefing as the only path, and a `planner` mode

Backing design: ADR-0154 D1, D4, D7. Tier: Standard (touches `src/` logic and `config/`). One phase, one PR.

## Acceptance criteria (from the ticket)

- AC-1 The bound holds on adversarial input (oversized history, oversized message, message plus digest over 64,000).
- AC-2 No `current` path: `ast-grep` for `planner_brief_mode` over `src/` and `config/` returns nothing. A three-turn test shows history before the query.
- AC-3 The planner call requests mode `planner`: `enable_thinking: false` with default sampling on `qwen3.8-flash-next`. A deployment without the mode sends its default mode. Probe rows show 0 reasoning characters.
- AC-4 The local binding passes every D7 threshold on the committed probe, in its `planner` mode. Output and fingerprint posted on the ticket. A miss leaves the mode out of `config/models.yaml`.

## Design decisions

1. **One pure builder.** `build_planner_user_message(query, strategy, messages, *, digest_text, history_max_chars, input_max_chars)` in `expansion_controller.py` returns a frozen `PlannerUserMessage(content, history_chars, digest_chars, message_chars)`. It raises `PlannerInputTooLarge` when the framed message plus the digest exceed the bound. No model call follows.
2. **The bound counts the whole user message**, framing included, because the ticket says "the planner user message never exceeds it". "Message" is the framed tail `Strategy: …\nQuery: …\n\nProduce the JSON plan.` It is never cut.
3. **Fill order.** tail, then digest, then history. History budget = `min(planner_history_max_chars, planner_input_max_chars - len(tail) - len(digest block) - len(history header and separator))`. History is trimmed whole-message from the oldest end by the existing `_render_planner_history`. A budget of 0 or less omits the block.
4. **Digest.** The production digest builder is ADR-0154 D5, a separate ticket. This ticket adds only the `digest_text` parameter, default empty, placed between history and query. `_run_planner` passes none yet.
5. **Failure path.** `PlannerInputTooLarge` is caught in `_run_planner`. It logs `planner_failed` with `reason="input_too_large"` and the per-input counts, then falls to today's fallback planner (the ticket: "follows today's failure path").
6. **Mode request.** `get_llm_client(..., mode=None)` gains a `mode` argument. A helper in `config/model_loader.py` applies a requested mode. It is extracted from the existing `binding.mode` block of `resolve_role_target`, so one rule serves both: use the mode if the deployment declares it, else keep the default mode and log `role_binding_mode_missing_on_deployment`. The executor builds the planner client with `mode="planner"`.
7. **Response evidence.** `planner_completed` logs `planner_mode` (the mode name that ran), `planner_reasoning_chars` (`len(reasoning_trace)` plus inline `<think>` text in `content`, as the probe counts it), and `planner_input_chars` (system, history, digest, message).
8. **Remove `planner_brief_mode`.** Delete the setting, the `brief_mode` parameters and the `current` branches. Production `.env` still holds `AGENT_PLANNER_BRIEF_MODE=briefing`. `AppConfig` uses `extra="ignore"`, so the stale key is harmless.
9. **Probe.** `scripts/eval/fre1537/render.py` `build_user_message` calls the production builder (master's comment on FRE-1541). Add a `planner` probe mode that reads its parameters from the `planner` mode in `config/models.yaml`, so the probe qualifies what ships.
10. **`config/models.yaml`.** Add the `planner` mode to `qwen3.8-flash-next` only after AC-4 passes.

## Steps

Each step: failing test, run it, implement, run it.

| # | Step | Files | Test command |
|---|------|-------|--------------|
| 1 | Settings: add `planner_input_max_chars` (64,000, `ge=0`); remove `planner_brief_mode`; rename and rewrite the settings test | `src/personal_agent/config/settings.py`, `tests/personal_agent/config/test_planner_brief_mode.py` → `test_planner_input_settings.py` | `make test-file FILE=tests/personal_agent/config/test_planner_input_settings.py` |
| 2 | Builder and `PlannerInputTooLarge` (AC-1) | `src/personal_agent/orchestrator/expansion_controller.py`, `tests/personal_agent/orchestrator/test_planner_input_bound.py` | `make test-file FILE=tests/personal_agent/orchestrator/test_planner_input_bound.py` |
| 3 | `_run_planner` and `execute` use the builder; drop `brief_mode`; `input_too_large` failure path; `planner_completed` evidence (AC-1, AC-2) | `expansion_controller.py`, `tests/personal_agent/orchestrator/test_expansion_controller.py` | `make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py` |
| 4 | Mode helper and `get_llm_client(mode=)` (AC-3) | `src/personal_agent/config/model_loader.py`, `src/personal_agent/llm_client/factory.py`, `tests/personal_agent/config/test_requested_mode.py` | `make test-file FILE=tests/personal_agent/config/test_requested_mode.py` |
| 5 | Executor builds the planner client with `mode="planner"`; wire-level test that the request carries `enable_thinking: false` with default sampling on `qwen3.8-flash-next`, and the default mode on a deployment with no `planner` mode (AC-3) | `src/personal_agent/orchestrator/executor.py`, `config/models.yaml` (test fixture copy), tests | `make test-k K=planner_mode` |
| 6 | Probe: production builder in `render.py`; `planner` probe mode from `config/models.yaml` | `scripts/eval/fre1537/render.py`, `llama.py`, `replay.py`, `longhist.py`, `tests/test_eval/test_fre1537_render.py` | `make test-file FILE=tests/test_eval/test_fre1537_render.py` |
| 7 | Docs: `.env.example`, `docs/reference/CONFIG_INVENTORY.md`, `scripts/eval/fre1537/README.md` | those files | `pre-commit run --all-files` |
| 8 | Gates | all | `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files` |
| 9 | AC-4: ask the owner, run the probe in `planner` mode, then add the `planner` mode to `config/models.yaml` on a pass | `config/models.yaml` | the README steps 1 to 7 |

AC-2 check: `ast-grep run -p 'planner_brief_mode' -l py src/` and `grep -rn planner_brief_mode src config` return nothing.

## Risks

- A planner test elsewhere mocks `llm_client` without `model_def`. The `planner_mode` lookup must not raise on such a client.
- Probe code runs inside the gateway image. The image is built from the repo, so the production builder is available there.
- The diff touches no write path, no deletion and no schema. It adds a config mode and changes call parameters, so the diff class is self-serve unless review finds otherwise.
