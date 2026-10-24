# FRE-1368 — Thinking canary (ADR-0141 D6.1)

Owner scope decision, 2026-10-24: **canary only.** The other two deliverables of the ticket are
already overtaken on `main`. The evidence is in the table below and is posted on the ticket.

| Ticket item | State on `main` | Disposition |
|---|---|---|
| Thinking canary, two arms | Not built | **This PR** |
| Budget probe (AC-c) | Ran 2026-09-04 on the single served model: `thinking_budget: 256` left about 924 tokens of reasoning untouched. Recorded on FRE-1362. FRE-1430 F2 and FRE-1423 reproduced it. The field left the catalog (ADR-0145 D3a). Catalog comments (`config/models.yaml:145-148`, `:247-249`) already say "wire-inert". | Superseded. No code. Evidence comment. |
| Arithmetic guard (AC-b) | `thinking_budget_tokens` is no longer a config field. `ModeSpec` has `extra="forbid"`. The invariant `max_tokens <= thinking_budget_tokens` has no operand. | Superseded. No code. Evidence comment. |

Codex plan review, 2026-10-24: 3 blocking and 9 non-blocking findings. All are folded in below.
Finding 12 confirmed the supersession evidence.

## Design

The canary calls the production dispatch path twice and reads `LLMResponse.reasoning_trace`.

| Arm | Client | Mode | Assertion |
|---|---|---|---|
| on (positive control) | `get_llm_client_for_key(<primary key>)` | catalog default (`enable_thinking: true`) | reasoning chars `> 0` |
| off | `get_llm_client_for_key(<primary key>)` | the `sub_agent` binding mode (`worker`, `enable_thinking: false`) | reasoning chars `== 0` |

The canary resolves the primary deployment key once from the catalog and passes it to the trusted-config
door `get_llm_client_for_key`. This door ignores the per-turn selection context, so a user selection
cannot change the target (review finding 4).

Rules:

1. **The canary does not write the verdict (review findings 1 and 3).** `run_scheduled_slm_health_probe`
   caches the snapshot that the probe returns, with no canary fields. The canary runs after that cache
   write. The runner then copies the snapshot with the canary fields and writes only that copy to
   Elasticsearch. The cache never holds a canary field. `probe_slm_health` is not changed in behaviour.
   Red shows on the ES document that the probe already writes (`agent-monitors-slm-health-*`). A separate
   document kind in that index would break the `service-health` skill, which reads the newest document.
2. **A skip is not a verdict.** The canary skips when: the primary is not local, the primary is not served,
   the local provider is busy, the primary default mode
   does not enable thinking, `sub_agent` does not inherit the primary deployment, or the `sub_agent` mode
   does not disable thinking. The last three belong to the declaration guard (FRE-1007). A skip records
   `thinking_canary_skip_reason`.
3. **A dispatch error is not a verdict on the control.** A slot timeout (`InferenceSlotTimeout`) counts
   as a dispatch error: the arm is not measured. It records `thinking_canary_error` and leaves
   `thinking_canary_ok` as `None`. Only a completed arm with the wrong reasoning reading is red.
4. **Bounded cost (review finding 2).** Each arm passes `timeout_s=45`, `max_retries=0`,
   `priority=InferencePriority.BACKGROUND` and `priority_timeout=2`. The on-arm cap is `max_tokens=256`.
   The off-arm cap is `max_tokens=64`. The worst case for the scheduler tick is two arms of 45 s each.
5. **Prompt (review finding 8).** Both arms send `What is 17 + 25? Reply with the number only.` Flash-Next
   spends 20-23 reasoning tokens on a trivial prompt (`docs/reference/SLM_SERVER_CLIENT_SEMANTICS.md`).
   The on-arm passes on `finish_reason="length"` when reasoning is non-empty. No arm needs visible content.
6. **Off by default:** `slm_thinking_canary_enabled=False`. Master enables it after a preflight (ticket
   comment, 2026-10-24). It runs on the scheduled tick only.
7. **Telemetry label.** No new `ModelRole`. A new member needs a lane in `cost_gate/role_map.py` and in the
   gitignored `budget.yaml`, and a half-change refuses startup on the deployed box. The canary uses
   `ModelRole.PRIMARY` and `ModelRole.SUB_AGENT` and a `PromptIdentity` callsite
   `slm_health.thinking_canary.on` / `.off`. Known consumer: `get_low_rating_buckets` counts every
   `model_call_completed` trace per callsite, so the canary adds two zero-rating callsite rows when on.
   The handoff names this for master. A call-side verdict writer (FRE-1436) must exclude these callsites
   when it lands (review finding 11).

## Files

| File | Change |
|---|---|
| `src/personal_agent/observability/slm_health/thinking_canary.py` | New. `ThinkingCanaryResult`, `run_thinking_canary()`. Never raises. |
| `src/personal_agent/observability/slm_health/snapshot.py` | Five nullable fields: `thinking_canary_ok`, `thinking_canary_skip_reason`, `thinking_canary_error`, `thinking_canary_on_reasoning_chars`, `thinking_canary_off_reasoning_chars`. Attribute docs. |
| `src/personal_agent/observability/slm_health/probe.py` | Extract `_resolve_probe_target()` from `_probe_generation` (behaviour kept). The canary imports it. |
| `src/personal_agent/observability/slm_health/scheduler_runner.py` | After the cache write, run the canary when the setting is on. Write the enriched copy to ES. |
| `src/personal_agent/config/settings.py` | `slm_thinking_canary_enabled`. |
| `docker/elasticsearch/monitors-slm-health-index-template.json` | Map the five fields. Update the `_meta.description` field count. |
| `docs/reference/CONFIG_INVENTORY.md` | Row for the new setting (regenerate with `scripts/audit/config_inventory.py`). |
| `tests/observability/test_slm_health_thinking_canary.py` | New. |
| `tests/scripts/test_es_templates.py` | Compare the complete snapshot field set with the template mapping (review finding 9). |
| `tests/observability/test_slm_health_sink.py` | Assert non-null canary values serialize. |

## Steps (TDD)

1. **Red:** write `tests/observability/test_slm_health_thinking_canary.py`. It dispatches through the
   real factory and real `litellm.acompletion`, and replaces only `httpx.AsyncHTTPTransport.handle_async_request`
   (the pattern of `tests/personal_agent/llm_client/test_local_via_litellm.py`). The stub server behaves
   like llama-server: it streams `reasoning_content` chunks unless the request body has
   `chat_template_kwargs.enable_thinking == false`.
   - `test_healthy_path_is_green` — on-arm reasoning `> 0`, off-arm `== 0`, `ok is True`.
   - `test_stripped_disable_control_turns_off_arm_red` — patch `_dialect_params` to drop
     `chat_template_kwargs`; the stub then streams reasoning to the off arm; `ok is False`; the error names the off arm. **AC-a, direction 1.**
   - `test_stripped_reasoning_mapping_turns_on_arm_red` — patch the response adapter so `reasoning_trace`
     is `None`; `ok is False`; the error names the on arm. **AC-a, direction 2.**
   - `test_red_canary_leaves_the_cached_verdict_untouched` — seed the cache, run the real
     `run_scheduled_slm_health_probe` with a red canary, assert the cached snapshot (status and every
     canary field) is unchanged and the ES write receives the canary result (review finding 3).
   - `test_canary_never_calls_set_cached_snapshot` — patch it, run the canary alone, assert no call.
   - `test_on_arm_passes_when_generation_stops_at_length` — both shapes: dedicated `reasoning_content`
     and an unclosed inline `<think>` (review finding 7).
   - Skip cases: primary not local; primary default mode thinking off; `sub_agent` mode thinking not off;
     `sub_agent` not inheriting; provider busy; `InferenceSlotTimeout`.
   - `test_dispatch_error_is_inconclusive_not_red` — transport error and a 45 s timeout.
   - `test_selection_context_does_not_change_the_target` — set a selection for `primary`, assert the
     request still goes to the catalog primary.
   - `test_canary_calls_carry_the_canary_callsite` — `model_call_completed` carries the canary identity.
   Run: `make test-file FILE=tests/observability/test_slm_health_thinking_canary.py` — expect failures (module missing).
2. **Green:** snapshot fields, `thinking_canary.py`, the probe helper extraction, the setting, the runner.
3. ES template mapping, `_meta`, and the strengthened template and sink tests.
4. Config inventory row; `tests/scripts/test_config_inventory.py` stays green.
5. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
6. Commit. Self-review on `git diff origin/main...HEAD` with `feature-dev:code-reviewer`. Run
   `security-review` if the diff touches network, input, files or secrets.

## Acceptance criteria (this PR)

| AC | Proof |
|---|---|
| AC-a | The two `stripped_*` tests, through the real dispatch path |
| AC-b | Superseded — evidence on the ticket (no `thinking_budget_tokens` operand exists) |
| AC-c | Superseded — FRE-1362 comment 2026-09-04 holds the numbers; catalog comments already match |
| Master condition | `test_red_canary_leaves_the_cached_verdict_untouched`; no live run in this PR |

## Live run

None in this PR. The canary ships off. Master's post-deploy runbook enables it after the preflight.
Estimated load when on: 2 short generations per 300 s tick, about 10 s each at the Flash-Next median of
26 tok/s, with a 45 s ceiling per arm.

## Diff class

Not a production write path. It adds an outbound call on the existing dispatch path, off by default. It
changes no cost or governance code (the `ModelRole` and budget-lane change is dropped) and no destructive
code. Schema: one additive ES template mapping (reversible deploy class). Self-serve.
