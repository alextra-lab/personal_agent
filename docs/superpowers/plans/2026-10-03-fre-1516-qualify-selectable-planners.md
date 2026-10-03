# FRE-1516 plan: qualify the selectable primaries as thinking-off planners

Ticket: FRE-1516. Backing ADR: ADR-0154 D2, D4, D7, AC-10. Blockers (merged): FRE-1511 (the probe), FRE-1541 (the local binding).

## Scope

Qualify two deployments, one at a time, on the committed probe (`scripts/eval/fre1537/`):

- OVH `qwen3.8-27b-ovh` (dialect `ovh_qwen`).
- `claude_sonnet` (dialect `anthropic_adaptive`).

A deployment that passes every D7 threshold receives a `planner` mode in `config/models.yaml`. A deployment that misses a threshold receives no mode. The miss goes to the owner.

## Owner decisions (2026-10-03, this session)

1. The Sonnet candidate mode is `effort: low`. No dialect change. Reason: the owner chose it over a new thinking-off lever. The probe measures whether thinking is off in effect.
2. The paid runs are authorized with a cap of 5 USD in total. The cap is one global budget: every paid command of this ticket passes `--max-usd 5`, and the probe sums the cost of all tags in the run directory (see Review changes).

## Candidate modes

| Deployment | Candidate `planner` mode | Reason |
|---|---|---|
| `qwen3.8-27b-ovh` | `temperature: 1.0`, `reasoning_effort: none` | The default mode's sampling (the `ovh_qwen` dialect reaches only temperature). `none` gave 0 reasoning tokens in FRE-1430 F3. |
| `claude_sonnet` | `effort: low` | The default mode has no sampling (`anthropic_adaptive` rejects temperature). `low` emitted no thinking block on 10 of 10 calls in FRE-1430 F13. |

## Finding that shapes the work

The committed probe speaks only to llama.cpp: a fixed URL, `chat_template_kwargs`, `repetition_penalty`, `cache_prompt` and `/props`. OVH and Anthropic reject those parameters (FRE-1430 matrix). So the probe needs a transport for managed deployments. This is a supporting change folded into this PR.

Design choices:

- The managed transport calls the production client, `LiteLLMClient.respond`, the way the planner call does. Raw `litellm.completion` is not allowed outside `llm_client/` (ADR-0141 AC-6, enforced by a pre-commit rule). The first draft of this plan called litellm directly. The rule blocked it, and the existing paid scripts show the accepted path: `get_llm_client_for_key` and a real `CostGate`.
- `respond` reserves and records cost through the `CostGate`. The probe registers a real gate on the eval Postgres (port 5434) and refuses to start on any other database (FRE-375). The cost rows never reach production.
- Because the request is production's, the probe mirrors nothing: not the dialect parameters, not `allowed_openai_params`, not the Anthropic cache blocks, not `sanitise_messages`. Tests read the keyword arguments that reach `litellm.acompletion`.
- The candidate mode replaces `client.model_def` with a copy of the catalog definition whose default mode is the candidate. The client reads its mode from `model_def` at call time.
- Candidate modes are validated against `DIALECT_FIELDS` and `DIALECT_VALUE_DOMAINS`, so a candidate cannot carry a field that the loader would refuse.
- The cloud client sets `reasoning_trace` to `None`. The probe reads the reasoning evidence from the provider message in `raw`.
- The timing arm and the `primary_between` call are llama.cpp prefix-cache steps. They decide no D7 threshold. A managed run skips both.

## Review changes (codex plan review, adopted)

1. The cap is global. The probe sums `cost_usd` over every tag of the run directory. Before a call it refuses when spent plus the worst-case input cost (characters divided by 3, at the input rate) would reach the cap. Output cost is counted when the call returns. A failed call is counted at its input estimate. The `--max-usd` value is 5 for every command, and the commands run one at a time.
2. The request is the production request (see Design choices). A test asserts the keyword arguments that reach `litellm.acompletion` for each deployment.
3. A candidate run implies the mode name `planner`. `--candidate` and `--mode planner` are the same mode name, so the fingerprint of a candidate run and of the landed run compare equal. An offline test proves it with `_same_configuration`.
4. The managed fingerprint does not call the served model a build. `engine.build` reads `managed (provider reports no build)` and `model.quant` reads `managed (not reported)`. The reply's `model` goes to a separate `engine.served_model` field, which is part of the identity. The handoff names this as a judgement call for the owner.
5. A thinking block fails the thinking-off check even when it holds no visible text. The row records `thinking_blocks` (count) and `reasoning_tokens`. The scorer fails the reasoning threshold when characters, thinking blocks or reasoning tokens are above 0. This replaces the informational line of the first draft. Local rows carry neither field, so the local result does not change.

## Files

| File | Change |
|---|---|
| `scripts/eval/fre1537/cloud.py` | New. `CloudTarget`, `resolve_target`, `CloudSession`, `Budget`, `assert_eval_database`, `row_from_response`. |
| `scripts/eval/fre1537/llama.py` | Extract `planner_user(inputs, label, digest)` from `planner_body`. No behaviour change. |
| `scripts/eval/fre1537/replay.py` | Arguments `--deployment`, `--candidate`, `--max-usd`. `run_decide` takes an optional `CloudTarget`. |
| `scripts/eval/fre1537/longhist.py` | `run_longhist` takes an optional `CloudTarget`. A managed row has no `primary_between`. |
| `scripts/eval/fre1537/fingerprint.py` | `build_cloud_fingerprint`. |
| `scripts/eval/fre1537/score.py` | The reasoning threshold also counts `thinking_blocks` and `reasoning_tokens`. |
| `scripts/eval/fre1537/README.md` | A section for managed deployments, the cost cap and the landed-mode check. |
| `tests/test_eval/test_fre1537_cloud.py` | New. Offline tests. |
| `config/models.yaml` | A `planner` mode on each deployment that passes. Not before. |

## Steps

### Step 1 — Tests first (all offline)

File: `tests/test_eval/test_fre1537_cloud.py`. Run: `uv run pytest tests/test_eval/test_fre1537_cloud.py -q`. Expected before the code: import error. Then each test fails for its own reason.

1. `test_the_ovh_candidate_reaches_the_provider_as_thinking_off` and the Sonnet twin: with the provider boundary, the cost gate and the cost tracker replaced and the production client real, the keyword arguments that reach `litellm.acompletion` hold `reasoning_effort: none`, `temperature: 1.0`, `allowed_openai_params` and the `ovhcloud/` model for OVH. For Sonnet they hold `reasoning_effort: low`, no temperature, the `anthropic-beta` header and a `cache_control` system block. They hold none of the llama.cpp keys.
2. `test_a_candidate_outside_the_dialect_is_refused`: `enable_thinking` on OVH, `temperature` on Sonnet, and `reasoning_effort: high` on OVH each raise `ValueError`. A local deployment and a missing catalog `planner` mode raise too.
3. `test_reasoning_evidence_is_seen_and_the_scorer_fails_it`: `reasoning_content`, a thinking block with text, a redacted block with no text, an inline `</think>`, and a reasoning-token count each give evidence, and the scorer fails the reasoning threshold on each (the seeded negatives). A token count of 0 passes.
4. `test_a_provider_error_becomes_cloud_call_error`, `test_a_cost_gate_denial_stops_the_run`, `test_run_decide_records_an_error_row_and_its_input_estimate`.
5. The database guard: the production port, the unit-test stack port, a remote host and a URL with no port are refused. Only the eval port on a local host passes. A session on the production database does not start.
6. The cost cap: rows of another tag count. A call whose worst-case input cost reaches the cap is refused before any call. A small call passes.
7. `test_a_managed_long_history_row_has_no_primary_call`.
8. The fingerprint: the build and quant say `managed`, the first reply fills `engine.served_model`, a different served model is a different configuration, and a candidate run and the landed run compare equal.
9. The arguments: the timing arm, a missing cap, a llama mode, and `--mode default` without a deployment are refused.
10. Existing tests: `uv run pytest tests/test_eval/test_fre1537_*.py -q` still passes (the `planner_user` extraction and the scorer change).

The first draft of the tests was written before the code and failed on the missing module. The production-client design replaced it after the ADR-0141 guard blocked raw litellm. The final tests were checked with three mutations: no mode override, no database guard and thinking blocks not counted. Each mutation made the matching tests fail.

### Step 2 — Implement

1. `llama.py`: extract `planner_user`. Run the existing probe tests.
2. `cloud.py`:
   - `resolve_target(key, mode, candidate=None)` loads the catalog, refuses a local placement, validates the mode against the dialect, and reads the declared per-token rates.
   - `CloudSession` owns one event loop, one real `CostGate` on the eval Postgres and one production client. `call(system, user)` runs `respond` the way the planner call does.
   - `row_from_response` reads the reasoning evidence from the provider message and the cost from the response.
   - `Budget` is the global cap.
3. `replay.py` and `longhist.py`: thread the optional session and budget.
4. `fingerprint.py`: `build_cloud_fingerprint`.
5. `score.py`: the reasoning threshold also counts thinking blocks and reasoning tokens.
6. Run `ruff` and `mypy` on the touched files, then the tests.

### Step 3 — Codex review of the code, then commit

Commit first. Then `feature-dev:code-reviewer` on `git diff origin/main...HEAD`. Fix every confirmed finding on this branch.

### Step 4 — Capture and render (no model call, no cost)

Run the README steps 1 to 4 against the current `main`. The eval substrates are up. Secrets come from `pass`, set in a subshell, never printed. The command never runs `docker compose`.

### Step 5 — OVH run (paid, under the cap)

1. Seeded negative on the instrument: three expand fixtures, one trial each, `--deployment qwen3.8-27b-ovh --mode default --tag ovh-default`. Expected: reasoning characters above 0. If the instrument shows 0 here, stop and investigate before any claim.
2. The candidate: `replay` (decide arm only, 3 trials), `longhist`, `score` with `--candidate '{"temperature": 1.0, "reasoning_effort": "none"}' --tag ovh-planner --max-usd 1.5`.
3. Post the report and the fingerprint on FRE-1516.

### Step 6 — Sonnet run (paid, under the cap)

Same as Step 5 with `--deployment claude_sonnet`, the candidate `{"effort": "low"}`, tag `sonnet-planner`, `--max-usd 3.0`. The seeded negative uses `--mode default` (effort high). If the default mode also shows 0 reasoning characters on the expand fixtures, the negative proves nothing for Sonnet. The unit test of Step 1 item 6 then stands as the instrument check, and the handoff says so.

### Step 7 — Land or report

- Pass: add the `planner` mode to `config/models.yaml` with a comment that links the posted result. Confirm the landed mode with the same command and the same tag and `--mode planner`. The run resumes, makes no call, and `ensure_compatible` fails if the fingerprint differs.
- Miss: add no mode. Report the miss and the failed thresholds to the owner. Do not retry with a changed candidate without the owner.

### Step 8 — Gates, PR, handoff

`make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`. Then `feature-dev:code-reviewer` and, if the diff touches secrets or the network, `security-review`. Sync with `origin/main`, open the PR, post the handoff comment on FRE-1516.

## Acceptance criteria and their proof

| AC | Proof |
|---|---|
| AC-1 Every selectable deployment has a posted result | Two comments on FRE-1516: one report and fingerprint per deployment, produced by `score` from the repo. |
| AC-2 Thinking is off in effect | Report rows: reasoning characters 0 on every call, declined-call completion-token p50 at most 40. The seeded negative shows the instrument can see reasoning. |
| AC-3 A mode lands only with a passing result | The PR adds a mode only for a deployment whose posted report shows PASS on every threshold. The landed-mode check shows the fingerprint equals the catalog. |

## Risks

- A managed API reports no engine build and no quant. The fingerprint records the served model name that the reply reports and `managed (not reported)`. The handoff names this as a judgement call.
- Sonnet's `max_tokens` is the client's ceiling (128,000), as in production. One runaway call could cost up to 1.92 USD in output. The global cap counts it when it returns. The runs go one command at a time, so the exposure stays inside the cap.
- The probe sets `client.model_def` after the factory builds the client. A change to how `respond` reads its mode shows as a failure of the two request tests.
- The cloud `respond` path hard-codes `reasoning_trace=None`. So the production log field `planner_reasoning_chars` (FRE-1541) reads 0 for a managed deployment whatever the model did. Out of scope here. The handoff reports it, and a follow-up ticket is filed if confirmed.
