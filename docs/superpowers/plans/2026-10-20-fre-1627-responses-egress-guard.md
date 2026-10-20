# FRE-1627 — an egress-guarded Responses API route for `LiteLLMClient`

Ticket: FRE-1627 (follow-up to FRE-1625). Design intent: ADR-0141 D2 (two-layer egress guard).

## Path decision (the ticket's first step)

The ticket says: upgrade if a litellm release after 1.106.0 forwards `client=` on the async
Responses bridge, else build option 1.

Evidence, 2026-10-20:

- PyPI: 1.106.0 (2026-10-18) is the latest stable release. After it there are only
  `1.107.0.dev1..dev3`, `1.107.0rc1` and `1.107.0rc2` (2026-10-20).
- `1.107.0rc2` `completion_extras/litellm_responses_transformation/handler.py`, `acompletion`:
  `transform_request(...)` still has no `client=kwargs.get("client")`, and the call is still
  `await aresponses(**request_data, aresponses=True)`. The diff against 1.106.0 touches only
  structured-output streaming. The sync `completion` forwards `client=`. The async one does not.

Result: option 2 is not available. Build option 1.

## Design

New module `src/personal_agent/llm_client/responses_route.py`:

- `routes_to_responses(request) -> bool` — the `responses_api_bridge_check` call that
  `_refuse_unguarded_responses_bridge` makes today, moved here so both sites share it.
- `to_responses_request(request, *, client) -> dict` — the assembled chat kwargs become
  `litellm.aresponses` kwargs. An explicit map, and an unmapped key raises `LLMClientError`
  (no silent drop, the same reason `drop_params` is off):
  - `messages` → `input` + `instructions`, through litellm's public
    `LiteLLMResponsesTransformationHandler.convert_chat_completion_messages_to_responses_api`
    (the converter the bridge uses, proven by FRE-1625 test 7a). A system-only request carries
    the system message as an input item, as the bridge does (the API rejects an empty input).
  - `max_tokens` → `max_output_tokens`; `reasoning_effort` → `reasoning={"effort": ...}`.
  - `tools` → flat function tools; `tool_choice` dict → `{"type": "function", "name": ...}`.
  - `response_format` → `text` through the handler's public
    `transform_response_format_to_text_format`.
  - `model`, `api_key`, `api_base`, `temperature`, `top_p`, `timeout`, `num_retries` pass through.
  - `client` → the guarded `AsyncHTTPHandler`.
  - `store=False`: Chat Completions does not store by default, and the Responses API does.
    The client is stateless (no `previous_response_id`), so storage has no use.
- `to_chat_response(response) -> litellm.ModelResponse` — `output_text` parts → content,
  `refusal` parts → `message.refusal` (ADR-0150 D1, from the codex plan review),
  `function_call` items → tool calls (`id = call_id`), `incomplete` status → `length` or
  `content_filter`, usage through litellm's public
  `ResponseAPILoggingUtils.transform_response_api_usage_to_chat_usage`. A response `error` raises.
- `async aresponses_as_chat(request, *, client) -> ModelResponse` — the three steps above.

Changes in `litellm_client.py`:

- `_RESPONSES_ROUTE_PROVIDERS = frozenset({"openai"})`. Only `openai` has a native litellm
  Responses config, whose handler keeps an `AsyncHTTPHandler` `client=`
  (`llm_http_handler.async_response_api_handler`). A provider without one falls back to
  `litellm_completion_transformation`, which this change does not verify.
- `_guarded_async_http_handler(guard)` — the cached handler, extracted from
  `_build_guarded_client` so both routes share one cache entry.
- `respond()`: `use_responses = provider in _RESPONSES_ROUTE_PROVIDERS and routes_to_responses(kwargs)`.
  If true, dispatch `aresponses_as_chat(kwargs, client=_guarded_async_http_handler(guard))`.
  Else call `_refuse_unguarded_responses_bridge` (unchanged) and `litellm.acompletion`.
  The rest of `respond()` (cost, telemetry, `LLMResponse`) reads the chat `ModelResponse` as today.
- `_respond_local` keeps its refusal: it streams, and this change adds no guarded stream route.
- `config/models.yaml` `gpt-6.1-sol` comment: the refusal note becomes the guarded route note.

## Steps

1. Tests first (they fail on the current branch):
   - `tests/personal_agent/llm_client/test_fre1625_new_cloud_entries.py`: test 7b (Sol tool
     call refused) becomes the AC-1 test: Sol + tool through `respond()` sends one request to
     `/v1/responses`, the sending client carries `_guard_request_hook`, the tool call returns
     with name, arguments and `call_id`, the body has `store: false` and
     `reasoning.effort: medium`, and the gate reserves once.
   - `tests/personal_agent/llm_client/test_litellm_client_egress_guard.py`: new class for the
     Responses route (AC-2), triggered by `litellm.route_all_chat_openai_to_responses`:
     layer 2 alone blocks a blocked host, and a 307 redirect hop to a blocked host is checked
     and never reaches the transport.
   - AC-3: a bridged request on a provider outside `_RESPONSES_ROUTE_PROVIDERS`
     (`ovhcloud`, model id `responses/test-model`) is still refused before any send or
     reservation. The existing local-path refusal tests stay.
   - `tests/personal_agent/llm_client/test_responses_route.py`: unit tests of the translation
     (tool messages and tool results, tool_choice, text-only reply, incomplete status,
     error, unmapped key refused, system-only input).
   - Run: `make test-file FILE=tests/personal_agent/llm_client/test_fre1625_new_cloud_entries.py`
     and the same for the other two files → the new tests fail.
2. Implement `responses_route.py` and the `litellm_client.py` changes → the same three runs pass.
3. Update the `gpt-6.1-sol` comment in `config/models.yaml`, and the FRE-1625 test module docstring.
4. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 | the replaced test 7b in `test_fre1625_new_cloud_entries.py` |
| AC-2 | the new Responses-route class in `test_litellm_client_egress_guard.py` |
| AC-3 | the `ovhcloud` bridged refusal test, plus the existing local-path refusal tests |
| AC-4 | post-deploy, master: owner turn on `gpt-6.1-sol`, `model_call_completed` with `model: openai/gpt-6.1-sol` and no `litellm_responses_bridge_refused` |

## Out of scope

- Reasoning items across turns. The chat history keeps no Responses reasoning items, so each
  turn re-reasons. The API accepts this. Carrying encrypted reasoning is a separate change.
- Streaming on the Responses route (the cloud path does not stream).
