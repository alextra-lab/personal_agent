# FRE-1602 — Retry a local model call once after a mid-stream transport reset

**Ticket:** FRE-1602 (Approved, bugfix grant). **Tier:** Standard (touches `src/` logic on the hot path of every local turn).
**Backing design:** ADR-0144 (gap bound), ADR-0149 D3 move 5 (the streamed partial), `_respond_local` in `src/personal_agent/llm_client/litellm_client.py`.

## What the measurement showed

A throwaway probe drove the real `litellm.acompletion` path with the httpx transport patched. It counted transport requests per failure shape. Budget means `max_retries`.

| Shape | Budget 0 | Budget 1 | Surfaces as |
|---|---|---|---|
| Creation time: ConnectError, RemoteProtocolError, ReadError, WriteError | 1 | 3 | `LLMConnectionError` |
| Creation time: HTTP 502, 503 | 1 | 3 | `LLMServerError` |
| Creation time: HTTP 504 | 1 | 3 | `LLMTimeout` |
| Creation time: HTTP 500 | 1 | 3 | `LLMServerError` |
| Creation time: HTTP 400 | 1 | 2 | `LLMClientError` |
| Mid-stream: RemoteProtocolError or ReadError, with or without a partial chunk | 1 | **1** | `LLMConnectionError` |

Two facts follow.

1. litellm already retries a failure that happens before the response starts. It costs `2N + 1` requests. A second retry layer at that point would multiply the count to 6.
2. litellm never retries a failure that happens while the stream is read. This is the failure of 2026-10-13 12:01:40Z: `peer closed connection without sending complete message body`.

The primary turn passes `max_retries=1` with tools and `None` (setting default 3) without tools. Background producers pass `max_retries=0` and rely on "exactly one request" (`test_max_retries_zero_issues_exactly_one_request`).

## Design

Add one retry for a transport error that is raised **while the stream is read**. Leave creation-time failures to litellm's own budget.

- **Retry condition.** All three must hold:
  1. `litellm.acompletion()` returned a stream (so litellm had no chance to retry).
  2. The exception chain holds an `httpx.NetworkError` or an `httpx.RemoteProtocolError`. This covers a connection reset and an incomplete chunked body.
  3. `effective_max_retries >= 1`. A caller that passes `0` keeps "exactly one request".
- **Not retried.** A timeout (`httpx.TimeoutException`, the caller's wall-clock `TimeoutError`), a cancel (`CancelledError` is not an `Exception`), `EgressBlockedError` (a `RequestError`, not a `NetworkError`), any HTTP status, any other exception.
- **One retry.** `_LOCAL_STREAM_ATTEMPTS = 2`. The second failure goes to the existing `except Exception` arm and the turn gets today's error.
- **Same budget.** The loop sits inside the existing `asyncio.timeout(effective_timeout_s)`. The retry does not extend the wall-clock bound that callers sized their outer deadline against (FRE-1444).
- **Same request.** `litellm_kwargs` is not changed between attempts. The server holds the finished prompt in its cache, so the retry skips the prefill.
- **Partial not doubled.** Per attempt, `chunks` is a new list. On retry the loop resets `progress_sink.content` and `progress_sink.generation_started_monotonic`.
- **Visible.** On retry the loop logs `model_call_retried` (warning) with `trace_id`, `session_id`, `span_id`, `error_shape` (the httpx class name), `attempt` (the number of the attempt that starts), `max_attempts`. `model_call_completed` carries `attempts` (1 for a clean call, 2 for a retried call). The first attempt does not emit `model_call_error`. Only the terminal failure does.

### Interpretation to flag in the handoff

The ticket scope lists HTTP 502, 503, 504 as transport errors. An HTTP status cannot arrive after the response began, so no status can occur at the stream-read point. A status that arrives before the response is already retried by litellm (table above). The plan therefore adds no status handling. A test pins that this layer does not stack a second retry on a creation-time 502, 503 or 504.

## Files

| File | Change |
|---|---|
| `src/personal_agent/telemetry/events.py` | add `MODEL_CALL_RETRIED = "model_call_retried"` |
| `src/personal_agent/telemetry/__init__.py` | import and export `MODEL_CALL_RETRIED` |
| `src/personal_agent/llm_client/litellm_client.py` | classifier `_stream_transport_error_shape`, constant `_LOCAL_STREAM_ATTEMPTS`, attempt loop in `_respond_local`, `attempts` extra, docstring |
| `tests/personal_agent/llm_client/test_local_via_litellm.py` | `_dispatch` gains `streams` (one body per request) and records `log.warning` events. New class `TestMidStreamTransportRetry`. |

## Steps (TDD)

1. **Red.** Add the helper changes and the new test class. Run
   `uv run pytest tests/personal_agent/llm_client/test_local_via_litellm.py -k MidStreamTransportRetry -q`.
   Expected: the retry tests fail (1 request, `LLMConnectionError`). The tests that pin "not retried" pass.
2. **Events.** Add the constant and the export.
3. **Classifier.**
   ```python
   _LOCAL_STREAM_ATTEMPTS = 2

   def _stream_transport_error_shape(exc: BaseException) -> str | None:
       for link in _exception_chain(exc):
           if isinstance(link, (httpx.NetworkError, httpx.RemoteProtocolError)):
               return type(link).__name__
       return None
   ```
4. **Loop.** Replace the stream block inside `asyncio.timeout(...)` with an attempt loop. `stream` is `None` until `acompletion` returns. The retry test is `stream is not None`.
5. **Telemetry.** Pass `"attempts": attempts` in the `extra` of `emit_model_call_completed`.
6. **Green.** Re-run the new class, then the whole file, then `tests/personal_agent/llm_client/` and `tests/test_llm_client/`.
7. **Seeded negative.** Remove the `progress_sink` reset. Confirm the AC-5 test fails. Restore.
8. **Gates.** `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`, then `make test` (held until master clears the llama.cpp measurement window).

## Acceptance criteria → proof

| AC | Test (in `TestMidStreamTransportRetry`) | Fails if |
|---|---|---|
| AC-1 | `test_a_mid_stream_transport_break_is_retried_once` — RemoteProtocolError and ReadError, with and without a partial chunk, budgets `1` and `None`. Asserts 2 transport requests, equal bodies, the second stream's content. | a shape is not retried, or the bodies differ |
| AC-2 | `test_non_transport_failures_are_sent_once` — HTTP 400 and 500, a mid-stream ReadTimeout, the caller's wall-clock timeout, a caller cancel. Asserts 1 `litellm.acompletion` call. Also `test_classifier_*` for `EgressBlockedError`. | any is sent twice |
| AC-3 | `test_the_second_break_is_the_terminal_error` — both streams break. Asserts 2 transport requests, `LLMConnectionError`, one `model_call_retried`, one `model_call_error`. | a third request, or no terminal error |
| AC-4 | `test_the_retry_is_visible_in_telemetry` and `test_a_clean_call_records_one_attempt`. | the event lacks `trace_id`, `error_shape` or `attempt`, or `attempts` is missing |
| AC-5 | `test_the_partial_of_the_first_attempt_does_not_reach_the_reply_or_the_sink`. | the sink or the reply holds the first attempt's text |
| Budget contract | `test_budget_zero_sends_one_request_on_a_mid_stream_break`. | `max_retries=0` sends two requests |
| No stacking | `test_creation_time_failures_are_not_retried_again_by_this_layer` — 502, 503, 504, ConnectError at budget 1. Asserts 3 transport requests (litellm's `2N+1`), not 6. | the layer stacks on litellm |

## Out of scope

- The cloudflared upgrade (owner decision, slm_server side).
- Cloud placement. `_respond_local` only.
- A retry for creation-time failures (litellm's budget owns them).
