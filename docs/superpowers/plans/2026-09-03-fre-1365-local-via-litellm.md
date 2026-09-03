# FRE-1365 — ADR-0141 T2: local placement dispatches through `LiteLLMClient`

**Ticket:** FRE-1365 (Approved, Urgent, Tier-1) · **ADR:** ADR-0141 D1, D4, D5, D7 · **Umbrella:** FRE-1362
**Predecessor on branch history:** FRE-1364 (ADR-0141 D2, the two-layer egress guard) — merged.

---

## 1. Scope

The cutover ticket. Local placement stops using `LocalLLMClient`'s raw-httpx transport and
dispatches through `LiteLLMClient` / `litellm.acompletion()`.

In scope:

1. Factory placement branch collapses — one constructor for every placement.
2. The four production `LocalLLMClient()` sites move to the factory (or are rewritten).
3. `openai/{model_id}` dispatch string + `api_base` = deployment endpoint; catalog and
   telemetry provider stay `slm_local`.
4. Non-standard parameters through the SDK `extra_body` flattening path.
5. No 8192 `max_tokens` default for local placement.
6. Parity: streaming, error taxonomy, per-role timeouts, retry reconciliation, history
   sanitiser, four trace headers, telemetry.
7. Reasoning preservation, both shapes.
8. Text tool-call parser as an unconditional fallback on the local path.
9. Cost gate skipped for local placement.

Out of scope (other tickets in the chain):

* `LocalLLMClient` deletion and the confinement rules — FRE-1367 (T4).
* Concurrency re-homing — FRE-1366 (T3). See §7, risk R1.
* Canary, wire-shape health probe, config-guard arithmetic — FRE-1368 (T5).

---

## 2. Verified mechanisms (measured, not assumed)

ADR-0141's spine is "configuration proves a path exists, never that it runs". Both mechanisms
this plan depends on were exercised against litellm 1.98.0 / openai 2.24.0 through an
`httpx.MockTransport` before the plan was written.

| Claim | Result |
|---|---|
| `client=AsyncOpenAI(...)` is used verbatim; `api_base` is ignored when `client=` is set | Confirmed — `OpenAIChatCompletion._get_openai_client` returns the passed client from its `else` branch. **The injected client's `base_url` is therefore authoritative.** |
| `extra_body={...}` flattens to top-level request JSON | Confirmed — wire body keys were `cache_prompt, messages, min_p, model, repetition_penalty, stream, stream_options, temperature, thinking_budget, top_k, top_p`. Literal `extra_body` absent. |
| `openai/` prefix is stripped from the wire `model` | Confirmed — wire model was `unsloth/qwen3.6-35-A3B`. |
| `max_tokens` is absent when not passed | Confirmed — no `max_tokens` key in the captured body. |
| `extra_headers` reaches the wire | Confirmed — `x-trace-id` / `x-span-id` present. |
| litellm stream chunks feed our existing `_aggregate_streaming_chunks` | Confirmed — `reasoning_content`, concatenated tool-call argument fragments, and `usage.prompt_tokens_details.cached_tokens` all survive. |

The last row decides the response design: the local path reuses the **existing, battle-tested**
`_aggregate_streaming_chunks` + `adapt_chat_completions_response` pair. Streaming parity (AC-b),
reasoning preservation (AC-e) and the text tool-call fallback (AC-g) are then parity by
construction, not by re-implementation.

---

## 3. Design

### 3.1 `LiteLLMClient` gains local placement

Three keyword-only constructor additions, all defaulting to today's cloud behaviour:

| Parameter | Type | Purpose |
|---|---|---|
| `placement` | `Placement` (default `CLOUD`) | Selects the local branches. |
| `model_def` | `ModelDefinition \| None` | Sampler parameters, endpoint, timeout. Required when `placement is LOCAL`. |
| `max_tokens` | annotation widens `int` → `int \| None` | `None` means omit-means-unbounded (D5). |

Derived state:

```python
self._is_local = placement is Placement.LOCAL
self._litellm_model = f"openai/{model_id}" if self._is_local else f"{provider}/{model_id}"
self._telemetry_model = model_id if self._is_local else self._litellm_model
```

`_telemetry_model` keeps the local `model` field byte-identical to what `LocalLLMClient`
emits today (the bare catalog id), so existing Kibana/Grafana filters keep matching. The
`openai/` prefix stays inside the client, per D1.

### 3.2 Egress guard

* Add `"slm_local"` to `_OPENAI_SDK_ROUTE_PROVIDERS` — local rides the OpenAI-SDK route.
* `_build_guarded_client` takes `base_url: str | None` instead of `provider_def`, so the
  per-deployment `endpoint` override reaches the injected `AsyncOpenAI`. This is required,
  not cosmetic: §2 proved the injected client's `base_url` is what dispatch uses.
* Layer 1 checks the same resolved base URL.

### 3.3 `respond()` — the local branches

| Concern | Local behaviour |
|---|---|
| `api_base` | `model_def.endpoint or provider_def.base_url` |
| `max_tokens` | `max_tokens` argument, else `self.max_tokens` (may be `None`); the key is **omitted** when `None` |
| Sampler params | `extra_body = {top_k, min_p, repetition_penalty, cache_prompt: True}` plus **one** thinking shape: `chat_template_kwargs={"enable_thinking": False}` when `disable_thinking`, else `thinking_budget` when `thinking_budget_tokens` is set |
| Standard params | `temperature` (call site, else `model_def.temperature`), `top_p`, `presence_penalty`, `parallel_tool_calls` |
| Tool strategy | tools stripped when `effective_tool_strategy` is not `NATIVE` (ported from `client.py`) |
| Streaming | `stream=True` + `stream_options={"include_usage": True}` |
| Timeout | `timeout_s` argument, else `model_def.default_timeout` |
| Retries | `num_retries` = `max_retries` argument, else `settings.llm_max_retries` — one retry layer, not two |
| Headers | `extra_headers` carries `traceparent` (via `opentelemetry.propagate.inject`), `X-Trace-Id`, `X-Span-Id`, and `X-Session-Id` when present |
| Cost gate | skipped entirely — no `reserve`, no `commit`, no `refund`, no cost-tracker write |
| Response | `[c.model_dump() async for c in stream]` → `_aggregate_streaming_chunks` → `adapt_chat_completions_response` |
| Errors | mapped to `LLMTimeout` / `LLMRateLimit` / `LLMConnectionError` / `LLMServerError` |
| Telemetry | `emit_model_call_started/completed` with `provider="slm_local"`, `model=self._telemetry_model`, `endpoint=<resolved api_base>` |

Cloud placement is untouched on every row.

### 3.4 Error taxonomy

A module-level `_map_local_dispatch_error(exc) -> LLMClientError`:

| Source | Mapped to |
|---|---|
| `litellm.Timeout`, `httpx.TimeoutException`, `openai.APITimeoutError` | `LLMTimeout` |
| `litellm.RateLimitError` (429) | `LLMRateLimit` |
| `litellm.InternalServerError`, `litellm.ServiceUnavailableError`, other 5xx | `LLMServerError` |
| `litellm.APIConnectionError`, `httpx.ConnectError`, `httpx.RequestError` | `LLMConnectionError` |
| anything else | `LLMClientError` |

`EgressBlockedError` and `asyncio.CancelledError` propagate unchanged.

Applied on the **local** branch only. Cloud keeps its `LLMClientError` wrap. Every mapped
class already subclasses `LLMClientError`, so no caller that catches the base class changes.

### 3.5 Factory collapse

`_build_client` always returns `LiteLLMClient`. A `None` `model_def` now raises
`LLMClientError` rather than falling through to a bare `LocalLLMClient()` — under D1 there is
no such fallback, and a silent wrong-model dispatch is exactly the FRE-1343 door the ADR closes.

### 3.6 Production call sites

| Site | Change |
|---|---|
| `memory/service.py:271` | `get_llm_client("sub_agent")` |
| `second_brain/session_summary.py:649` | `get_llm_client("session_summary")` |
| `second_brain/entity_extraction.py:1190` | `get_llm_client("entity_extraction")` |
| `captains_log/reflection.py:367` | Construction removed |

The fourth is not a `respond()` caller. Its client feeds exactly one consumer:
`reflection_dspy.py:437`'s `llm_client.get_dspy_lm(...)`, reached only when
`captains_log_role is None`. `resolve_role_model_key` is annotated `-> str` and raises rather
than returning `None`, so `reflection.py` never passes `None`. The `llm_client` parameter is
therefore dropped from `generate_reflection_dspy`, and the fallback becomes
`configure_dspy_lm(role=captains_log_role or ModelRole.CAPTAINS_LOG)` — which already handles
local placement as `openai/{model_id}` + `api_base` (`dspy_adapter.py:119`). DSPy stays out of
scope; only the dead client hand-off goes.

---

## 4. Deliberate scope limits

Three ADR-0141 D7 dispositions are applied to **local placement only**, not to cloud:

1. **Text tool-call fallback.** D7's "unconditional" contrasts with gating on
   `tool_calling_strategy` — it is about strategy, not placement. Firing the text parser on
   Anthropic answers would be a new behaviour with a real false-positive risk, not a
   preservation.
2. **Reasoning extraction.** Cloud keeps `reasoning_trace=None`. AC-e is a local criterion.
3. **Error taxonomy and trace headers.** Both are parity obligations against
   `LocalLLMClient`. Sending `X-Trace-Id` to Anthropic would be a new egress of identity data.

Each is recorded in the handoff so master can adjudicate against D7 directly.

---

## 4a. Codex plan review, round 1 — dispositions

| Finding | Disposition |
|---|---|
| Text tool-call fallback restricted by placement contradicts D7's "unified response" | **Held, with evidence.** `tool_call_parser.py` format 4 is `[tool_name, {"arg": "value"}]`, documented as a "common malformed fallback". That pattern matches ordinary bracketed prose and code, so firing it on cloud answers has a real false-positive surface — the artifact builder and any judge that quotes text. D7's row is a *preservation* disposition for a `LocalLLMClient` capability; extending it to cloud is new behaviour, not preservation. Local-scoped, flagged to master as the one clause read narrower than the reviewer read it |
| Retry categories and backoff differ from `LocalLLMClient` | **Accepted as documented divergence.** litellm retries a refused connection where `LocalLLMClient` did not, and issues `2 * num_retries + 1` transport requests (measured). The load-bearing contract — `max_retries=0` issues exactly one request — is asserted at the transport |
| Differentiated timeouts lost (connect 10 s vs read 600 s) | **Fixed.** `timeout=httpx.Timeout(connect=10, read=timeout_s, write=10, pool=10)`; measured to reach `request.extensions["timeout"]` intact, and asserted there |
| `LLMInvalidResponse` collapsed into `LLMClientError` | **Fixed.** The mapper re-raises any existing `LLMClientError` subclass unchanged |
| Historical tool-call `index` back-fill dropped | **Fixed.** Extracted from `build_chat_completions_request` into a shared `normalise_tool_call_indices`, called on both paths |
| Default `tool_choice="auto"` not preserved | **Fixed.** Applied on the local path when tools are present |
| `MODEL_CALL_ERROR` telemetry omitted | **Fixed.** Emitted on the local failure path |
| `cache_read_tokens` not required in the completed event | Already asserted by AC-d's test (`== 900`) |
| Prompt identity not required to derive from real wire inputs | **Fixed.** Derived from the sanitised messages and the strategy-filtered tools |
| ACs 1–7 pass vacuously on presence-only checks | The AC table was a summary; the tests assert **values** — `top_k == 20`, the endpoint URL, exact tool name and arguments, exact reasoning strings, the session-id value, and `X-Span-Id` equal to the emitted `span_id`. Strengthened further: response content on AC-b, `model_call_started` emission on AC-d, `traceparent` carrying the real span id on AC-f, exact arguments on AC-g |
| AC-c passes while `cost_tracker.connect()` still does a round-trip | **Fixed.** The cost tracker is not acquired at all on the local path, and the test asserts it |

## 4b. Self-review at the Step-6 gate — findings fixed on-branch

`feature-dev:code-reviewer` returned one finding: a concrete sharpening of R1
(the concurrency gap), with the `entity_extraction` slot-timeout numbers. Carried
into R1 above; no code change, because FRE-1366 owns the fix.

`security-review` returned no finding at its reporting bar, and two sub-bar
observations. Both were acted on:

1. **`EgressBlockedError` was being flattened to `LLMConnectionError`.** It
   subclasses `httpx.RequestError`, so the mapper's connection arm swallowed it
   — reporting a network failure for a policy refusal, and contradicting
   `_respond_local`'s own `Raises:` line. The mapper now returns it unchanged,
   on both layers. Layer 1 was never affected (it raises outside the mapper);
   this is what keeps the ADR-0132 contract on layer 2's redirect hops.
2. **The local route had no seeded negative of its own.** The FRE-1364 suite
   covers the same injection *mechanism* through the cloud body of `respond()`,
   so ADR-0141 D2.2's letter was arguably met — but an edit to `_respond_local`
   alone would have tripped no test, and "the tests are the contract" is the
   point of the clause. Added `TestEgressGuardOnTheLocalRoute`: both layers,
   asserting `EgressBlockedError` at the caller and zero requests at the
   transport.

Also raised, and deliberately not changed: `generate_query_paraphrases` moving
from a bare local client to `get_llm_client("sub_agent")` makes it follow the
role binding and per-turn selection, so recall-query text can now reach a cloud
sub-agent where it previously could not. That is ADR-0141 D1 working as
designed — the old client honoured the selection too, but dispatched a cloud
model id at the local endpoint, which is FRE-1343 itself. Flagged for the owner
rather than pinned here, because pinning it is a design decision.

## 5. Steps

| # | Step | Verify |
|---|---|---|
| 1 | Write `tests/personal_agent/llm_client/test_local_via_litellm.py` — one class per criterion AC-a…AC-h | `make test-file FILE=...` — all fail |
| 2 | `litellm_client.py`: constructor, route classification, `_build_guarded_client` signature | mypy clean |
| 3 | `litellm_client.py`: `respond()` local branches (§3.3) + `_map_local_dispatch_error` | AC-a, AC-b, AC-e, AC-f, AC-g, AC-h pass |
| 4 | `litellm_client.py`: gate skip + telemetry fields | AC-c, AC-d pass |
| 5 | `factory.py`: branch collapse + docstrings | `test_factory_*` pass |
| 6 | Three `respond()` call sites → factory | their own tests pass |
| 7 | `reflection.py` / `reflection_dspy.py` rewrite + affected tests | `test_captains_log/` passes |
| 8 | Docs: `llm_client/AGENTS.md`, module docstrings | — |
| 9 | Gates | `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files` |

---

## 6. Acceptance criteria

| AC | Criterion | Proof |
|---|---|---|
| a | Wire shape through the real dispatch path | Capture the request body at `httpx.AsyncHTTPTransport.handle_async_request` under the guarded client. Budget-declaring deployment: `top_k`, `min_p`, `repetition_penalty`, `cache_prompt`, `thinking_budget` all top-level. Disabling deployment: `chat_template_kwargs == {"enable_thinking": False}` and no `thinking_budget`. Both: `"extra_body" not in body`. Catalog omits `max_tokens` → `"max_tokens" not in body` |
| b | Streaming parity | A recorded SSE stream (content + split tool-call argument fragments + usage-only final chunk) replayed through `respond()` yields the same `usage` block and tool-call set as the pre-cutover aggregation of the same chunks |
| c | No gate on the local hot path | `gate.reserve` / `commit` / `refund` mocks assert `not_called` for a local call; existing `test_litellm_gate_wiring.py` stays green for cloud |
| d | Telemetry parity | Captured `model_call_completed` carries `provider == "slm_local"`, `role`, and the four `prompt_*` identity fields |
| e | Reasoning, both shapes | `<think>…</think>` response and `reasoning_content` response each populate `reasoning_trace`; `content` carries no think-tags |
| f | Four propagation headers | Captured request headers contain `traceparent`, `X-Trace-Id`, `X-Span-Id`, `X-Session-Id` |
| g | Text tool-call fallback | A response with a textual tool call and no structured `tool_calls` yields parsed tool calls |
| h | Error taxonomy | Stub transport producing connection refusal / read timeout / 429 / 500 raises `LLMConnectionError` / `LLMTimeout` / `LLMRateLimit` / `LLMServerError` |

Every test dispatches through the real `litellm.acompletion()`; only the outbound transport is
replaced. AC-a's own wording forbids hand-building the payload.

---

## 7. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **Concurrency control is absent for local between T2 and T3.** The controller lives in `LocalLLMClient`; FRE-1366 re-homes it. Merging and deploying T2 alone removes the `max_concurrency: 1` GPU ceiling and the `InferencePriority` tiers. `priority` / `priority_timeout` become accepted-and-ignored, and `InferenceSlotTimeout` becomes unreachable on the local path. **Live effect:** `primary` and `sub_agent` are the two role bindings on local placement today, so the single-GPU ceiling that serialises them is what actually goes. **Latent effect:** `entity_extraction` and `session_summary` catch `InferenceSlotTimeout` for a fast 60 s / 120 s slot-wait bail; with the gate gone their worst case becomes the read timeout (90 s / 120 s). Both roles bind cloud today, so this is dormant until one is re-bound local | Not fixed here — FRE-1366 owns it, and duplicating it would collide with that ticket. **Flagged to master as a deploy-ordering constraint: do not deploy T2 without T3.** Recorded in the PR body, the handoff, and `concurrency.py`'s own module docstring, so the next reader of that module is not told a stale story |
| R2 | `top_k: 20` reaches the primary for the first time | ADR-0141 D4 accepts it — the model-card preset finally applying. FRE-1363 re-baselines |
| R3 | litellm upgrade changes `extra_body` flattening | AC-a pins it at the wire through the real dispatch path |
| R4 | Local `finish_reason` is absent from `LLMResponse` | Parity — `LocalLLMClient` never set it either. `session_summary._reject_if_truncated` checks `finish_reason` first and the `completion_tokens` ceiling second, so an absent stop reason degrades to the token check exactly as it does today. The aggregated stream *does* carry a real `finish_reason`, so surfacing it is a one-line follow-up — deliberately not taken here, because it would widen local behaviour beyond parity |
| R5 | Telemetry `model` field for local | Held byte-identical to today's bare catalog id (§3.1) |
