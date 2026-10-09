# FRE-1548 — The planner request on claude_sonnet carries a JSON schema

Ticket: FRE-1548. Backing design: ADR-0154 D4, D6, D7. Tier: Standard (touches `src/` logic and the probe).

## Facts found while reading the code

- The planner call sends `response_format={"type": "json_object"}` (`orchestrator/expansion_controller.py`, `_run_planner`).
- litellm 1.98 with `claude-sonnet-5` sends nothing for that request. A schema becomes `output_format` (native structured output). Observed with `litellm.utils.get_optional_params`.
- The model map entry for `claude-sonnet-5` has `supports_native_structured_output: true`. On the tool path, litellm cannot force the tool while thinking is on. So only the native path gives a guarantee.
- `LiteLLMClient` has a `provider` attribute. The local binding has provider `slm_local`, so it can be told apart from `anthropic` without a new field.
- The cloud client returns `reasoning_trace=None` always. `executor.py` copies a non-empty `reasoning_trace` into the next assistant message. So the client contract must not change. The fix stays inside the planner.
- The committed probe `scripts/eval/fre1537/cloud.py` hardcodes `{"type": "json_object"}`. The probe prompt admits `SINGLE` (decline rule). The schema for the probe must admit `SINGLE` and an empty `tasks` list. The production prompt today does not.

## Decisions

1. `planner_response_format(provider, *, admit_single=False)` in `expansion_controller.py` returns the `json_schema` request for provider `anthropic`. It returns the unchanged `{"type": "json_object"}` for every other provider. So the local request and the OVH request are byte-identical to main.
2. The production planner call passes `admit_single=False`. The probe passes `admit_single=True`, because its prompt admits `SINGLE`. FRE-1515 will pass `True` when it adds the decline rule.
3. The schema is built from `WORKER_TYPES` and `THOROUGHNESS_LEVELS`, the same sources as the prompt. A drift test pins the enums.
4. `_planner_reasoning` (replaces `_planner_reasoning_chars`) also reads the provider message in `response["raw"]`: `reasoning_content`, thinking text, and the payload of a redacted block. The thinking block count and `usage.reasoning_tokens` go to the `planner_completed` log line. No schema migration.
5. The probe fingerprint gains `response_format_sha256`. A schema change is then a routing change that needs a new run (ADR-0154 D7).

## Steps

| # | Step | Verify |
|---|------|--------|
| 1 | Write the failing tests in `tests/personal_agent/orchestrator/test_planner_response_format.py` | `uv run pytest tests/personal_agent/orchestrator/test_planner_response_format.py -q` fails |
| 2 | Add `planner_plan_schema` and `planner_response_format`. Use them at the planner call | AC-1 and AC-2 tests pass |
| 3 | Replace `_planner_reasoning_chars` with `_planner_reasoning`. Log blocks and tokens in `planner_completed` | AC-3 tests pass |
| 4 | Probe: `CloudSession.call` uses `planner_response_format(target.provider, admit_single=True)`. Add `response_format_sha256` to `build_cloud_fingerprint`. Update the probe tests and README | `uv run pytest tests/test_eval/test_fre1537_*.py -q` |
| 5 | Prove AC-2 against main: capture the full litellm kwargs of the local planner call on `origin/main` and on the branch, then compare | The two dumps are equal |
| 6 | Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files` | All pass |
| 7 | Commit. Self-review with `feature-dev:code-reviewer` and `security-review` on `git diff origin/main...HEAD`. Fix findings. Code is then frozen | No open finding |
| 8 | Run the probe once on `claude_sonnet`, candidate `{"effort": "low"}`, `--max-usd 2` on both paid steps | `score` report and fingerprint |
| 9 | Pass (11 of 11): add `planner: effort: low` to `claude_sonnet` in `config/models.yaml`. Rerun `replay` with the same tag and no `--candidate` (no call). Miss: add no mode | The replay stops with no new call |
| 10 | Rebase, gates, push, PR. Post the handoff comment | CI green |

## Acceptance criteria and their proof

- AC-1: a test drives `_run_planner` with the real `claude_sonnet` client and a patched `litellm.acompletion`. It finds `response_format` of type `json_schema` with the plan schema. A second test captures the HTTP body and finds `output_format`.
- AC-2: a test drives the same call with the real `qwen3.8-flash-next` client. It finds `response_format == {"type": "json_object"}`. Step 5 shows the whole kwargs dump equal to main.
- AC-3: a test returns a real `litellm.ModelResponse` with a thinking block. It finds `planner_reasoning_chars` above 0 in `planner_completed` and `planner_outcome`. A parity test shows the same number as the probe's `row_from_response`.
- AC-4: step 8 and step 9. The report and fingerprint go on the ticket. The run stays under 2 USD.

## Risks

- The schema may change the plan quality of Sonnet. The probe measures it. A miss means no mode.
- Anthropic rejects some schema keywords. litellm filters them. The wire test and the paid run show it.
- A second paid run needs the owner. A code change after step 7 means a new run, so the code is frozen first.

## Codex plan review (2026-10-09)

- The schema is stricter than `_validate_plan_json` on `strategy` and `memory_relevance`, and it forbids the legacy task keys `tools`, `expected_output` and `mode`. This is intended. The prompt never asks for them. The tests state it.
- No 400 risk was found in the schema. litellm adds `additionalProperties: false` and removes `minItems` and `maxItems`. The paid run is the final proof.
- The local and OVH requests stay byte-identical, because only provider `anthropic` takes the schema.
- `response["raw"]` must be read defensively. The code uses `isinstance` checks and `.get`.
- `response_format_sha256` is part of the fingerprint identity. A run directory from before this change cannot resume. The README says so.
