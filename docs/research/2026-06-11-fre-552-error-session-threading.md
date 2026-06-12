# FRE-552 — Threading `session_id` onto error-level log events

**Date:** 2026-06-11 · **Project:** Telemetry Surface Audit · **Ticket:** FRE-552
**Refs:** FRE-539 (C4 dashboard — the finding), ADR-0074 (identity threading), FRE-533 (A1 reconciliation)

## Problem

Building FRE-539's session/trace E2E view, session-level error-rate was found *not honestly
buildable*. `level:ERROR` over `agent-logs-*` (7d): **161 events**, but `session_id` present on
only **5/161** — so an error-rate-per-session panel would be near-empty and misleading.

## Why it was cheap to fix

Measure-first established that the plumbing was already in place — only the emit sites were
missing the field:

- **`TraceContext` carries `session_id`** (`telemetry/trace.py:52`) and propagates it through
  `new_span()` / `create_child` / `SystemTraceContext.new()`.
- **The ES handler forwards `session_id` verbatim** as a pass-through custom field
  (`telemetry/es_handler.py:143-165`) — it is not in the excluded-keys set.
- **`session_id` and `trace_id` are already mapped `keyword`** in
  `docker/elasticsearch/index-template.json:59,61`. → **No ES template change.**

So the fix is purely adding `session_id=…` at error emit sites that already hold a session.

## Emit-site audit

| Event | Site | Session source | Outcome |
|---|---|---|---|
| `tool_call_failed` (warn, not-found) | `tools/executor.py:336` | `session_id` param (dispatch passes `ctx.session_id`) | threaded |
| `tool_call_failed` (error) | `tools/executor.py:472` | `session_id` param | threaded |
| `perplexity_query_http_error` | `tools/perplexity.py` | `ctx.session_id` | threaded |
| `perplexity_query_connect_failed` | `tools/perplexity.py` | `ctx.session_id` | threaded |
| `perplexity_query_timeout` | `tools/perplexity.py` | `ctx.session_id` | threaded |
| `perplexity_query_failed` | `tools/perplexity.py` | `ctx.session_id` | threaded |
| `litellm_refund_after_failure_failed` | `llm_client/litellm_client.py` | `trace_ctx.session_id` | threaded |
| `litellm_request_failed` | `llm_client/litellm_client.py` | `trace_ctx.session_id` | threaded |
| `litellm_commit_failed` | `llm_client/litellm_client.py` | `trace_ctx.session_id` | threaded |
| `unexpected_exception_in_respond` | `llm_client/client.py` | `trace_ctx.session_id` | threaded |
| `model_call_error` (client) | `llm_client/client.py` | `trace_ctx.session_id` | threaded |
| `model_call_error` (orchestrator) | `orchestrator/executor.py:2883` | `ctx.session_id` | already present (no change) |

Sibling ERROR logs in the same function/except-block were threaded alongside the ticket-named
sites: the module loggers carry no bound context, so each record needs `session_id` explicitly,
and a multi-line error sequence (e.g. a refund-failure + request-failure pair) would otherwise
leave a gap in any `terms(session_id)` aggregation.

### Null handling on system paths

`trace_ctx.session_id` is nullable on system-tagged paths (joinability probe, scheduler ticks via
`SystemTraceContext`). We log `session_id` **unconditionally** (value may be `None`), consistent
with the already-accepted emit at `orchestrator/executor.py:2883`. A `keyword`-mapped null does
not create a bogus `terms(session_id)` bucket; the FRE-539 panel filters `exists(session_id)`
(which it needs anyway to exclude the noise below).

## Genuinely unavailable — documented, no fix

**Generic `error` event_type (42/161 in window).** These are foreign/3rd-party logs reaching the
root logger via stdlib `logging`: when `record.msg` is not a structlog dict, the ES handler falls
back to `event_type = record.levelname.lower()` → `"error"` (`es_handler.py:121`). They carry no
structlog binding and no session. Every one of our own `log.exception(...)` calls uses a named
event string, so they are *not* the source. No session is recoverable here.

## Synthetic noise — documented + recommendation

**`test_error_with_context` (67/161).** Emitted by `tests/manual/test_elasticsearch_logging.py`,
which hardcodes `http://localhost:9200`. On the VPS that is **prod ES**, so the manual diagnostic
has polluted prod `agent-logs`. The `# fre-375-allow` comment on that line claims the test stack,
but the URL is the prod port. Recommendation: FRE-539 dashboards filter
`NOT event_type:(error OR test_error_with_context)`; a follow-up ticket guards the script's target.

## Outcome

- Error emit sites that have a session in scope now log `session_id` (+ `trace_id`).
- The only sessionless ERROR class (`event_type:error`) is foreign-log noise, documented.
- **FRE-539's session error-rate panel is now addable** once this deploys and data accrues
  (filter `level:ERROR AND exists(session_id) AND NOT event_type:(error OR test_error_with_context)`,
  agg `terms(session_id)`).

## Verification

Producer-side: `structlog.testing.capture_logs()` assertions per touched module
(`test_perplexity.py`, `test_executor.py`, `test_client.py`, `test_litellm_emit_payload.py`).
Pipeline: a handler-level test (`test_es_handler.py::test_emit_forwards_session_id_to_es_logger`)
confirms a `session_id`-bearing record reaches the `es_logger.log_event` payload — closing the
pass-through gap that `capture_logs` cannot see.

## Out of scope

- Building the Kibana panel = **FRE-539** (this only enables it).
- No ES template change (fields already `keyword`).
- Deploy is master's call.
