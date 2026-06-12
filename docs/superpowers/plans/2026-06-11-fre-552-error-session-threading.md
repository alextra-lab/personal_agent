# FRE-552 — Thread `session_id` onto error-level log events

**Project:** Telemetry Surface Audit · **Refs:** FRE-539 (C4 dashboard, the finding), ADR-0074 (identity threading), FRE-533 (A1 reconciliation)

## Goal

Make session-level error-rate honestly buildable by threading the ADR-0074 identity
tuple — specifically `session_id` — onto ERROR-level emit sites that have a session in
scope. Then FRE-539's deferred session error-rate panel becomes addable.

## Audit (measure-first — done during scoping)

`TraceContext` already carries `session_id` (`telemetry/trace.py:52`, propagated through
`new_span()` / `create_child`). The ES handler passes `session_id` through as a custom
field (`es_handler.py:144-165`). **`session_id` and `trace_id` are already mapped as
`keyword`** in `docker/elasticsearch/index-template.json:59,61` → **no ES template change.**

| Event | Site | Session source | Action |
|---|---|---|---|
| `tool_call_failed` (warn, not-found) | `tools/executor.py:336` | `session_id` param | add `session_id=session_id` |
| `tool_call_failed` (error) | `tools/executor.py:471` | `session_id` param | add `session_id=session_id` |
| `perplexity_query_http_error` | `tools/perplexity.py:144` | `ctx.session_id` | add `session_id=session_id` |
| `perplexity_query_connect_failed` | `tools/perplexity.py:153` | `ctx.session_id` | add `session_id=session_id` |
| `perplexity_query_timeout` | `tools/perplexity.py:157` | `ctx.session_id` | add `session_id=session_id` |
| `perplexity_query_failed` | `tools/perplexity.py:160` | `ctx.session_id` | add `session_id=session_id` |
| `litellm_refund_after_failure_failed` | `llm_client/litellm_client.py:509` | `trace_ctx.session_id` | add `session_id=trace_ctx.session_id` |
| `litellm_request_failed` | `llm_client/litellm_client.py:515` | `trace_ctx.session_id` | add `session_id=trace_ctx.session_id` |
| `litellm_commit_failed` | `llm_client/litellm_client.py:590` | `trace_ctx.session_id` | add `session_id=trace_ctx.session_id` |
| `unexpected_exception_in_respond` | `llm_client/client.py:596` | `trace_ctx.session_id` | add `session_id=trace_ctx.session_id` |
| `model_call_error` (client) | `llm_client/client.py:609` | `trace_ctx.session_id` | add `session_id=trace_ctx.session_id` |
| `model_call_error` (orchestrator) | `orchestrator/executor.py:2880` | `ctx.session_id` | **already done — no change** |

Sites scoped to one function each; siblings in the same except-block are threaded too
(same in-scope session, same goal — consistent session attribution on the whole hot path).

**Null handling (codex review Q2):** `trace_ctx.session_id` is nullable on system-tagged
paths (probes/scheduler via `SystemTraceContext`). We log `session_id` **unconditionally**
(value may be `None`) to stay consistent with the already-accepted emit at
`orchestrator/executor.py:2880`. A `keyword`-mapped null does not create a bogus
`terms(session_id)` bucket; FRE-539's panel filters `exists(session_id)` (which it already
needs to drop the foreign-`error` noise). Considered conditional-include — rejected for
consistency + smaller diff.

### Genuinely unavailable (documented, no fix)

- **Generic `error` (42/161 in window):** foreign/3rd-party logs hitting the root logger
  via stdlib `logging` (record.msg not a dict → `es_handler.py:121` falls back to
  `levelname.lower() == "error"`). No structlog binding, no session context. All our own
  `log.exception(...)` calls carry named events, so they are *not* the source.

### Synthetic noise (item 3)

- **`test_error_with_context` (67/161):** emitted by `tests/manual/test_elasticsearch_logging.py`,
  which hardcodes `http://localhost:9200` — on the VPS that's **prod ES**, so the manual
  diagnostic has polluted prod `agent-logs`. Resolution: FRE-539 dashboards filter
  `NOT event_type:(error OR test_error_with_context)`. Follow-up ticket proposed to fix the
  manual script's target.

## Steps (TDD — failing test first each time)

1. **perplexity** — `tests/test_tools/test_perplexity.py`: add `test_*_logs_session_id` using
   `TraceContext.new_trace(session_id="sess-xyz")` + `structlog.testing.capture_logs()`;
   assert the timeout (+ connect/http/query-failed) events carry `session_id == "sess-xyz"`.
   Confirm fail → add `session_id = ctx.session_id` local + `session_id=session_id` on the 4
   `log.error` calls in `tools/perplexity.py` → confirm pass.
   - `make test-file FILE=tests/test_tools/test_perplexity.py`

2. **tool executor** — `tests/test_tools/test_executor.py`: add a test that a failing tool
   emits `tool_call_failed` with `session_id` (call `execute_tool(..., session_id="sess-x")`,
   capture_logs, assert). Confirm fail → add `session_id=session_id` at executor.py:336,471 →
   pass.
   - `make test-file FILE=tests/test_tools/test_executor.py`

3. **client.py** — `tests/test_llm_client/test_client.py`: extend the retry-exhaustion path
   to assert `model_call_error` carries `session_id` (use `new_trace(session_id="sess-abc")`).
   Confirm fail → add `session_id=trace_ctx.session_id` at client.py:596,609 → pass.
   - `make test-file FILE=tests/test_llm_client/test_client.py`

4. **litellm_client** — `tests/personal_agent/llm_client/test_litellm_gate_wiring.py` (or a new
   focused `test_litellm_client_errors.py`): mock `litellm.acompletion` to raise; assert
   `litellm_request_failed` carries `session_id`. Confirm fail → add
   `session_id=trace_ctx.session_id` at litellm_client.py:509,515,590 → pass.
   - `make test-file FILE=tests/personal_agent/llm_client/test_litellm_gate_wiring.py`

5. **ES pipeline test (codex review Q3)** — `tests/personal_agent/telemetry/test_es_handler.py`
   (or nearest existing): build a `LogRecord` whose `msg` dict carries `session_id`; mock
   `es_logger.log_event`; assert `session_id` is present in the forwarded payload. Closes the
   producer→ES pass-through gap that `capture_logs` cannot see.
   - `make test-file FILE=tests/personal_agent/telemetry/test_es_handler.py`

6. **Docs** — `docs/research/2026-06-11-fre-552-error-session-threading.md`: the audit table
   above + foreign-`error` finding + synthetic-noise finding + dashboard-filter recommendation
   + "FRE-539 session error-rate panel now addable" note.

7. **Follow-up ticket** (Needs Approval, Telemetry Surface Audit): redirect/guard
   `tests/manual/test_elasticsearch_logging.py` so it cannot write to prod `:9200`.

## Quality gates

`make test` (touched modules then full) · `make mypy` · `make ruff-check` + `make ruff-format`
· `pre-commit run --all-files`.

## Out of scope (surfaced)

- Building the Kibana panel = **FRE-539** (this only enables it).
- No ES template change (fields already `keyword`).
- No deploy (master's call).
