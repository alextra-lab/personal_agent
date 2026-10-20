# FRE-1625 — litellm 1.106.0, mcp 2.x, and three catalog entries (no binding change)

Ticket: FRE-1625. Source: FRE-1621 note `docs/research/2026-10-20-cloud-model-and-pricing-refresh.md`
(§4.5, §6.1–6.3, §9, §10 ticket B). Tier: Standard (dependency upgrade, `src/` validation logic,
cost registration). FRE-1622 is not merged, so no `fast` / `deep` modes are added.

## Facts established before the plan (offline, `LITELLM_LOCAL_MODEL_COST_MAP=True`)

1. litellm 1.106.0 is the latest 1.106.x on PyPI. Its bundled map has `gpt-6-luna`, `gpt-6.1-sol`,
   `claude-haiku-5-5` and `claude-sonnet-5-5` at the official prices.
2. **litellm 1.106.0 breaks every tool call when `mcp` 1.x is installed** (litellm #46045). `completion()`
   imports `litellm.responses.mcp.chat_completions_handler` for any call with `tools`, and that chain
   imports `mcp.types.CacheableResult` (mcp 2.x only). Reproduced with `gpt-6.1-sol` and `gpt-5.4-mini`.
   The fix (#45961) is on litellm main, in no stable release. **Owner decision 2026-10-20: keep 1.106.0
   and raise `mcp` to `>=2.2.0,<3`.**
3. `mcp` 2.x breaks our MCP client: `CallToolResult.isError` / `structuredContent` became snake_case
   attributes, `Tool.model_dump()` emits snake_case keys (we read `inputSchema`), and a tool rejection
   is raised as `MCPError` instead of returned with `isError`. `ClientSession(read, write)`,
   `initialize()` and `stdio_client` yielding `(read, write)` are unchanged. The gateway is off in
   production (`AGENT_MCP_GATEWAY_ENABLED` is not `true`).
4. Routing on 1.106.0: `responses_api_bridge_check` keeps `gpt-6-luna` + function tool at effort `none`
   on `chat`, and sends `gpt-6.1-sol` + function tool at `medium` (and at unset) to `responses`.
5. litellm accepts `reasoning_effort="none"` on `gpt-6.1-sol` client-side, so only our catalog can
   refuse it. litellm refuses `temperature` on gpt-6 when effort is unset or above `none`; it accepts
   it on `gpt-5.4-mini` with effort unset (its default is `none`). So a general tightening of the
   `openai_gpt5` rule would be wrong; the Sol rule must be per model.
6. 1.106.0 adds `thinking.display: "summarized"` to every adaptive Anthropic mapping (litellm #37979,
   in 1.100.0), including the existing `claude_sonnet`. Existing tests asserting
   `thinking == {"type": "adaptive"}` must be updated. `anthropic_thinking_present` telemetry may now
   read `true` more often.
7. Release-note risk #39230/#40350 (background cost-map swap) is closed: `adopt_model_cost_map` calls
   `utils.reapply_runtime_model_cost_registrations()`, so our `register_model` prices survive a swap.

## Design: per-model effort domain

Add one optional field to `ModelDefinition` (`src/personal_agent/llm_client/models.py`):

```python
reasoning_effort_domain: frozenset[Literal["none", "low", "medium", "high", "xhigh"]] | None = None
```

It narrows the dialect's `DIALECT_VALUE_DOMAINS["reasoning_effort"]` for one model. In
`ModelConfig._llm_deployments_declare_valid_modes`, for each `kind=llm` model that sets it:

1. The dialect must carry `reasoning_effort` in `DIALECT_FIELDS`, and the model domain must be a subset
   of the dialect domain (when the dialect has one). Else `ValueError`.
2. Every mode's `reasoning_effort`, when set, must be in the model domain. Else `ValueError`.
3. On `openai_gpt5`, when `"none"` is not in the model domain, no mode may set `temperature` or `top_p`
   (the existing rule: sampling only at effort `none`, which this model cannot reach). Else `ValueError`.

Rejected alternative: a new `Dialect` for Sol. It needs entries in `SYNTHESIS_RETAINS_TOOLS` and
`LANDING_ACCEPTS_JSON_SCHEMA`, which are measured claims that cannot be measured under the FRE-1573 hold,
and a new `_dialect_params` arm. The wire vocabulary is the same as `openai_gpt5`; only the value domain
differs, which is what the domain mechanism already expresses.

The config guard needs no change: `_dialect_declares_effort` accepts any `openai_gpt5` domain value, and
load-time validation refuses `none` on Sol before the guard runs.

## Steps

1. `pyproject.toml` + `uv.lock`: `litellm==1.106.0`, `mcp>=2.2.0,<3` (locked 2.3.0). **Done.**
2. `src/personal_agent/mcp/client.py`: `model_dump(by_alias=True, mode="json")`, `is_error`,
   `structured_content`, catch `MCPError` → `MCPToolError("mcp_tool_error")`. Tests in
   `tests/test_mcp/test_client.py` now use real `mcp.types` objects. **Done; 65 pass.**
3. Test first: `tests/personal_agent/llm_client/test_fre1625_new_cloud_entries.py` (the eight ticket
   tests, below). Confirm red.
4. `models.py`: add the field + the three validator rules.
5. `config/models.yaml`: add `gpt-6-luna`, `claude_haiku_5_5` (note §9 verbatim) and `gpt-6.1-sol`:
   `id: "gpt-6.1-sol"`, `provider: "openai"`, `max_tokens: 128000`, `context_length: 1050000`,
   concurrency 1/10, `default_timeout: 180` (copied from `claude_sonnet`, the entry it compares to),
   `reasoning_effort_domain: [low, medium, high, xhigh]`, modes `default: {reasoning_effort: medium}`,
   `worker: {reasoning_effort: low}`, prices `0.000002` / `0.00001`. Change no existing entry.
6. Update the existing tests that pin `thinking == {"type": "adaptive"}` to the 1.106.0 shape.
7. Regenerate `tests/personal_agent/config/catalog_snapshot_golden.json`
   (`uv run python -m tests.personal_agent.config.test_catalog_snapshot`), revert any `slm.*` drift from
   `.env`, and add the 12th rebaseline section to the test docstring.
8. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## The eight ticket tests (offline, remote map off, no network)

Module-level `os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"` has no effect if litellm is already
imported. So each test that reads the map asserts `litellm` read the local map
(`litellm.model_cost` contains the four models), which holds on 1.106.0 either way because the bundle
has them.

1. `get_optional_params(model="gpt-6-luna", custom_llm_provider="openai", reasoning_effort="none",
   temperature=0.0)` returns both.
2. A `ModelConfig` whose `gpt-6-luna` mode sets `reasoning_effort: high` + `temperature: 0.0` raises
   `ValidationError` naming the `openai_gpt5` rule.
3. `reasoning_wire_shape("claude-haiku-5-5", "anthropic", {}, "low")` gives
   `thinking.type == "adaptive"` and `output_config == {"effort": "low"}`.
4. After `register_model_pricing(load_model_config())`, `litellm.cost_per_token` for 1M/1M tokens equals
   (0.10, 0.50) for `openai/gpt-6-luna` and `anthropic/claude-haiku-5-5`, and (2.0, 10.0) for
   `openai/gpt-6.1-sol`.
5. `responses_api_bridge_check(model="gpt-6-luna", custom_llm_provider="openai", tools=[fn],
   reasoning_effort="none")[0]["mode"] == "chat"`.
6. Same for `gpt-6.1-sol` at `"medium"` → `"responses"`.
7. `LiteLLMClient.respond()` for key `gpt-6.1-sol` with one tool, real `litellm.acompletion`, network
   blocked by patching `httpx.AsyncClient.send` (catches every httpx client and transport, including
   the guarded client and litellm's aiohttp transport). The fake returns a fixture Responses API body
   with one `function_call` item. Assert: the request URL path ends `/responses`; the returned
   `tool_calls` has the fixture's `name`, `arguments` and `call_id` as id. The fixture is built from the
   documented Responses API shape, not recorded live (the FRE-1573 hold forbids a live call). This test
   also guards the #46045 import chain, because it runs `completion()` with `tools`.
8. A `ModelConfig` whose `gpt-6.1-sol` mode sets `reasoning_effort: none` raises; a mode that sets
   `temperature` (with `reasoning_effort: medium`, and with effort unset) raises.

Plus: AC-3 test that `config/model_roles.yaml` resolves no role to any of the three new keys.

## Acceptance criteria (from the ticket)

| AC | Evidence |
|---|---|
| AC-1 eight unit tests pass on the new litellm, remote map off | `test_fre1625_new_cloud_entries.py` |
| AC-2 guard boots with all three entries; test/mypy/ruff pass | config-guard suite + gates |
| AC-3 `model_roles.yaml` unchanged; no role resolves to a new key | git diff + AC-3 test |
| AC-4 (master, post-deploy) primary picker lists all three keys | runbook in the handoff |

## Revisions after the codex plan review (2026-10-20)

1. **Runtime bypass of Sol's domain (High, accepted).** A call-site `reasoning_effort`, `temperature`
   or `top_p` could leave the domain on top of a valid mode. Added `_gate_model_effort_domain` in
   `litellm_client.py`, run on the assembled request before the cost gate.
2. **The Responses bridge drops the egress guard (High, confirmed at runtime).** litellm 1.106.0's async
   bridge (`completion_extras/litellm_responses_transformation/handler.py`) does not forward `client=`,
   so a Sol tool call left through litellm's own client with no layer-2 request hook. **Owner decision
   2026-10-20: fail closed.** Added `_refuse_unguarded_responses_bridge`: when
   `responses_api_bridge_check` says `responses`, `respond()` raises `LLMClientError` before the cost
   gate reserves. Test 7 therefore splits: 7a proves litellm's translation at its own boundary, 7b
   proves the fail-closed refusal, 7c is the guarded positive control (Luna at `none`, chat route,
   request hooks present). A follow-up ticket gives the Responses path a guarded client.
3. **`MCPError` mapped too broadly (Medium, accepted).** Only `INVALID_PARAMS` or an application code
   outside the JSON-RPC reserved range maps to `mcp_tool_error`. Timeouts, closed connections and
   protocol codes re-raise to the gateway's `mcp_call_failed`.
4. **Price test token count (Medium, already fixed).** Base rates are tested at 10K prompt tokens,
   long-context rates at 300K / 150K.

Also found while running the suites: litellm 1.106.0 `AsyncHTTPHandler.create_client` calls the public
`create_async_transport`, so the egress-guard tests' patch of `_create_async_transport` (now an alias)
no longer replaced the transport. The tests now patch the public name.

## Out of scope

Role binding swaps, Sonnet 5.5 forced-tool change, `ModeSpec` literal widening (`max`), eval judges,
`fast`/`deep` modes (FRE-1622).
