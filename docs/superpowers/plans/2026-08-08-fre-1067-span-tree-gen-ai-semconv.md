# FRE-1067 — ADR-0129 B3: the span tree (step / model-call / tool-call), gen_ai semconv, retiring RequestTimer

**Ticket:** FRE-1067 (Approved, In Progress) · **ADR:** ADR-0129 D2, D3 · **Tier:** Standard/Complex —
codex plan-review complete, revised below. **Diff class: escalated** (Step 8) — this plan's `RequestTimer`
retirement now touches a production Elasticsearch write path (`request_trace` indexing) via its call
chain; flag for owner `/code-review ultra` before merge per the escalation rule.

## Revision history

**Rev 2 (this version)** — incorporates codex plan-review findings (13 total: 3 blocker, 4 major, 6
minor/nit) plus one owner decision (AskUserQuestion, 2026-08-08): AC-10 is read **literally** — full
`RequestTimer` retirement, including its downstream `RequestCompletedEvent`/Elasticsearch consumers, not
just the phases ADR-0129's Implementation Notes names. Every blocker/major finding is resolved below;
each subsection says which finding it closes.

## Current state (verified against this branch, not the ADR's line numbers, which have drifted)

- **FRE-1064 (B1) and FRE-1065 (B2) are already merged and deployed.** OTel SDK is bootstrapped
  (`telemetry/otel_bootstrap.py`, called from `service/app.py:653`), a request-boundary root span exists
  (`telemetry/otel_middleware.py::RequestRootSpanMiddleware`), the structlog processor injects
  `trace_id`/`span_id` from the active span (`telemetry/logger.py:127-153`), and `TraceContext`
  (`telemetry/trace.py`) reads its trace id from the active span rather than minting one (falls back to
  minting only when no span is active).
- **No span-creation helper exists anywhere.** `TraceContext.new_span()` (`trace.py:180-198`) mints a
  hand-rolled `uuid.uuid4()` string as a "span id" — this is not an OTel span. Every call site that wants
  a "span" today calls this and threads the string through log kwargs by hand.
- **`RequestTimer`** (`telemetry/request_timer.py`, class at line 88) is live and has a much larger
  footprint than its file alone: it is a public parameter on `Orchestrator.handle_user_request()`
  (`orchestrator/orchestrator.py:46,68-69,124`), constructed at `service/app.py:411,2201` and
  `gateway/chat_api.py:98` (a **second, independent** timer in `chat_api.py` timing
  `"llm_call:anthropic_stream"` — nothing to do with the orchestrator FSM), read by
  `observability/route_trace/assembler.py:264-269`, and its `.to_trace_summary()`/`.to_breakdown()`
  snapshot is carried on `RequestCompletedEvent.trace_summary`/`.trace_breakdown`
  (`events/models.py:246-247`), which `events/request_completed_handlers.py::build_request_trace_es_handler`
  (registered `service/app.py:1034,1062`) indexes into Elasticsearch as `request_trace`/`request_trace_step`
  documents via `es_logger.py::index_request_trace`/`index_request_trace_from_snapshot`. See
  **"RequestTimer full retirement"** below for the complete blast radius (codex finding #2).
- **The orchestrator is a flat state machine**, not nested calls: `executor.py:2713-2740` runs
  `while state not in {COMPLETED, FAILED}: state = await step_functions[state](ctx, session_manager, trace_ctx)`.
  `TaskState.LLM_CALL` → `step_llm_call` (def at 4072) and `TaskState.TOOL_EXECUTION` → `step_tool_execution`
  (def at 5191) are **separate top-level functions**, not one nested inside the other. Real exits from this
  pair are **not** limited to the three the first plan revision assumed — codex enumerated at least ten:
  deadline-exceeded before/mid inference (~4826, ~4891), hybrid-expansion re-entry into `LLM_CALL` (~5037),
  cancellation before tool dispatch (~5219), iteration-limit / malformed-state / no-tool-calls exits
  (~5290, ~5310, ~5322, ~5334), the terminal tool-tail return (~5673, ~5684), plus any exception escaping
  the driver loop entirely (caught by the turn-scoped `except`/`finally` at ~2929-2985). **A scattered
  three-call-site close design (rejected below) misses most of these** (codex finding #1).
- **Tool-call spans**: `tools/executor.py` mints a hand-rolled span id per call (`trace_ctx.new_span()` at
  line 428) then emits `TOOL_CALL_STARTED` (429), `TOOL_CALL_COMPLETED` (461-468, carries `latency_ms`),
  `TOOL_CALL_FAILED` (481-490, carries `latency_ms`). These three log records are **retained** (D3, AC-11)
  — only `latency_ms` drops (AC-9 names these exact lines). The real dispatcher catches tool exceptions
  and returns `ToolResult(success=False)` rather than letting them propagate — a naive `with
  tool_call_span(...): ... raise` design (rejected below) would never see the exception and the span would
  report `UNSET` status even for failed calls (codex finding #9).
- **Model-call spans**: `client.py::_do_request` mints `span_id` via `trace_ctx.new_span()` at line 366,
  emits `emit_model_call_started`/`completed` (367, 554-572); `litellm_client.py::respond` does the same at
  550/768-792. **A third production caller exists that the first plan revision missed**:
  `gateway/chat_api.py::_emit_gateway_model_call_completed` (382-437) calls
  `emit_model_call_completed(..., span_id=uuid4().hex, latency_ms=latency_ms, ...)` directly, for the
  gateway's own direct-Anthropic streaming path — dropping the `latency_ms` **parameter** from the shared
  helper without fixing this caller breaks it at runtime (codex finding #3).
- **`litellm_client.py`'s existing latency stopwatch ends at line ~607-608**, immediately after
  `litellm.acompletion()` returns — well before `emit_model_call_completed` at 768-792, which comes after
  budget-gate settlement, a durable Postgres cost-tracker write, and response parsing. Wrapping the *whole*
  function body in one span (the first plan revision's design) would make the span's duration include all
  of that extra work, which can exceed ADR-0129 AC-4's 10% duration-reconciliation tolerance against
  `api_costs.latency_ms` (codex finding #5).
- **`ModelRole`** (`llm_client/types.py:16-64`, str Enum: `PRIMARY, SUB_AGENT, COMPRESSOR,
  ARTIFACT_BUILDER, ENTITY_EXTRACTION, CAPTAINS_LOG, SESSION_SUMMARY, INSIGHTS, EMBEDDING, RERANKER,
  RERANKER_FALLBACK, VISION, SKILL_ROUTING, STUDY`) is the FRE-1037 "purpose" vocabulary AC-7 requires
  `gen_ai.operation.name` to draw from — both clients already pass `role.value` into the emit helpers.
- **`gen_ai` semconv**: zero usage in `src/` today. `opentelemetry-semantic-conventions` is installed
  transitively at `0.65b0` but not a declared dependency. The attribute name constants live at
  `opentelemetry.semconv._incubating.attributes.gen_ai_attributes` (`GEN_AI_OPERATION_NAME`,
  `GEN_AI_SYSTEM`, `GEN_AI_REQUEST_MODEL`, `GEN_AI_USAGE_INPUT_TOKENS`, `GEN_AI_USAGE_OUTPUT_TOKENS`).
- **`CANONICAL_MODEL_CALL_STARTED_FIELDS` / `CANONICAL_MODEL_CALL_COMPLETED_FIELDS`**
  (`telemetry/events.py:54-82`) have exactly one consumer: `tests/personal_agent/llm_client/test_telemetry_parity.py`
  (plus a docstring mention in `llm_client/telemetry.py:10-11`). Safe to delete both frozensets once that
  test is re-pointed.
- **`InMemorySpanExporter` test scaffold** already exists twice (`tests/test_telemetry/test_trace_otel_bridge.py:24-46`,
  `tests/personal_agent/service/test_otel_root_span.py:20-34`) — own `TracerProvider` +
  `SimpleSpanProcessor(InMemorySpanExporter())`, never touching the process-global provider. Reuse this
  shape for every new test.
- **ES `span_id`/`parent_span_id` mappings are `keyword`** in both `docker/elasticsearch/index-template.json`
  and `slm-requests-index-template.json` — a 16-lowercase-hex OTel span id is schema-safe to write
  alongside the legacy UUID-hex values already there; no template change needed (codex finding #13,
  resolved as a non-issue — noted for completeness).

## Design

### Step-span lifecycle: centralized in the driver loop with `try`/`finally`, span made genuinely current (resolves findings #1, #4, #7)

**Rejected (Rev 1): a plain `ContextVar` holding the step span, opened/closed at three scattered call
sites inside `step_llm_call`/`step_tool_execution`.** Two independent defects, both raised by codex:

- **Finding #1 (blocker):** three call sites cannot cover ten-plus real exit paths. Any exit not
  explicitly instrumented leaves an ended turn with a live, unclosed, unexported step span — and the next
  turn's first model-call span would incorrectly parent onto the stale leftover if the `ContextVar` value
  survived (it usually wouldn't, since a fresh request runs in a fresh asyncio context, but *within* one
  turn a missed close corrupts every subsequent span in that turn).
- **Finding #4 (major):** keeping the step span **not current** was sold as `D1`-aligned "explicit
  propagation," but it silently breaks the *log* side: `executor.py`'s existing hand-minted
  `trace_ctx.new_span()` calls (still present, still driving `STEP_PLANNING_COMPLETED`/`MODEL_CALL_ERROR`
  log `parent_span_id` fields) and the structlog `_add_span_context` processor (which reads
  `trace.get_current_span()`, i.e. whatever *is* current — the request root span, not the step span, since
  the step span was deliberately kept out of the context stack) disagree with the exported span tree. The
  spans would be correctly shaped; the Elasticsearch log records correlated to them would not be.
- **Finding #7 (major):** the plain `ContextVar.set()` was claimed to be "safer" than OTel's
  `context.attach()`/`.detach()` pairing, but it carries the identical lifecycle obligation (a value that
  should be restored, not just overwritten) with none of OTel's own protections — the safety claim in Rev 1
  was false.

**Adopted (Rev 2): the step span becomes genuinely current, opened/closed from exactly one place — the
driver loop — using a `try`/`finally` keyed on the state transition, not on which step function ran.**

```python
# orchestrator/executor.py, ~2713-2740
current_step_span: Span | None = None
current_step_token: object | None = None

async with observe_topology(ctx):
    try:
        while state not in {TaskState.COMPLETED, TaskState.FAILED}:
            ...
            if state == TaskState.LLM_CALL and current_step_span is None:
                current_step_span, current_step_token = open_step_span(
                    iteration=ctx.tool_iteration_count
                )

            step_func = step_functions.get(state)
            ...
            try:
                state = await step_func(ctx, session_manager, trace_ctx)
            finally:
                # Close on every exit that is not "continuing into TOOL_EXECUTION" —
                # this covers the happy path (LLM_CALL -> SYNTHESIS/FAILED with no
                # tools), the tool tail (TOOL_EXECUTION -> LLM_CALL/FAILED/SYNTHESIS),
                # and every early-return / deadline / cancellation exit, because
                # `state` at this point either holds the step function's real return
                # value or (if it raised) the pre-call value — TOOL_EXECUTION only
                # when step_llm_call legitimately asked for it.
                if current_step_span is not None and state != TaskState.TOOL_EXECUTION:
                    close_step_span(current_step_span, current_step_token, tool_count=...)
                    current_step_span, current_step_token = None, None
        ...
    finally:
        # Backstop: guarantee no span survives the turn even if the loop exits via
        # an exception path this try/finally didn't anticipate (e.g. a bug in the
        # loop itself, or asyncio.CancelledError racing the inner finally).
        if current_step_span is not None:
            close_step_span(current_step_span, current_step_token, tool_count=0)
```

`open_step_span`/`close_step_span` (in the new `telemetry/spans.py`) use OTel's own
`opentelemetry.context.attach()`/`.detach()` — **not** a bespoke `ContextVar** — so the step span is
genuinely the "current span" for its entire lifetime, and any log line or nested span created while it is
open (across both `step_llm_call` and `step_tool_execution`) correlates correctly through the existing
`_add_span_context` processor with **zero changes to that processor**. Model-call and tool-call spans are
then opened with plain `tracer.start_as_current_span(...)` and need **no explicit `context=` argument** —
they inherit the step span as parent automatically, because it is current. This also fixes finding #4's
specific complaint: `executor.py`'s pre-existing `trace_ctx.new_span()` mint (used for the *orthogonal*
`STEP_PLANNING_*` log family, not touched by this ticket) stays as-is and is explicitly out of scope — the
model-call span's own `parent_span_id`, wherever a log record needs one, is now sourced from the real
active parent (`trace.get_current_span()` before opening the child), never from that stale mint.

One remaining subtlety `close_step_span` must handle: the driver loop's `finally` runs on **every** step
function return, including the ones for `TaskState.INIT`, `TaskState.PLANNING`, and `TaskState.SYNTHESIS` —
states this design never opens a step span for. `close_step_span` is a no-op when `current_step_span is
None` (already true in Rev 1, kept), so this is safe by construction, not by care taken at each call site.

### `tool_count` on the step span

Resolved by having `close_step_span` accept it as a parameter, sourced by the driver loop from
`step_tool_execution`'s own return — the cleanest way to get an authoritative count without threading it
back out of a function whose contract is just "return the next `TaskState`." **Design: `step_tool_execution`
stores its own `len(tool_calls)` onto `ctx` (a private field, e.g. `ctx._last_tool_count`, or simpler:
reuse `ctx.tool_iteration_count`'s delta) before returning**, and the driver loop's `finally` reads it back.
Exact mechanism is an implementation-time judgment call (a dataclass field vs. a return-tuple change to
`step_tool_execution`'s signature) — **not** re-litigated here since it is a private, single-file plumbing
detail, not a design risk. **What `tool_count` means is a design decision, resolved here** (closes codex
finding #10): it is the count of tool calls that reached `dispatch_tool_call` (i.e., passed the loop-gate
and schema checks) — the same population that produces `TOOL_CALL_STARTED`/`COMPLETED`/`FAILED` log
records and therefore the same population AC-3/AC-5 reconcile against. Requested-but-blocked or
schema-invalid tool calls are **not** counted here (they never got a tool-call span either), and are not
separately surfaced as a span attribute — no AC requires it, and adding one would be undirected scope.

### New module: `src/personal_agent/telemetry/spans.py`

```python
"""Span-tree helpers for ADR-0129 D3 (root -> step -> {model-call, tool-call})."""

from __future__ import annotations

from importlib.metadata import version as _pkg_version

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.trace import Span

_TRACER_NAME = "personal_agent"
_ATTR_NAMESPACE = "personal_agent"

# AC-13: pinned exactly (not a floor) so a `pyproject.toml` bump that moves past
# this value fails the AC-13 test loudly instead of silently drifting. Bump this
# constant in the same commit as the pyproject.toml dependency bump.
SEMCONV_VERSION = "0.65b0"


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


def namespaced(key: str) -> str:
    """Prefix a non-semconv attribute key with the project namespace (AC-8)."""
    return f"{_ATTR_NAMESPACE}.{key}"


def open_step_span(*, iteration: int) -> tuple[Span, object]:
    """Start the step span and attach it as the current OTel context.

    Returns ``(span, token)``; the caller MUST pass both to
    :func:`close_step_span` — the token is what makes ``context.detach``
    restore the prior context correctly rather than clobbering it.
    """
    span = get_tracer().start_span(
        "step", attributes={namespaced("step.iteration"): iteration}
    )
    token = context_api.attach(trace.set_span_in_context(span))
    return span, token


def close_step_span(span: Span, token: object, *, tool_count: int) -> None:
    span.set_attribute(namespaced("step.tool_count"), tool_count)
    span.end()
    context_api.detach(token)


def model_call_span(*, role: str, model: str, provider: str):
    """Context manager: opens a model-call span as CURRENT. Parent is
    whatever span is current (the step span, via ``open_step_span``'s
    attach) — no explicit ``context=`` needed.
    """
    return get_tracer().start_as_current_span(
        f"model_call {model}",
        attributes={
            gen_ai.GEN_AI_OPERATION_NAME: role,  # FRE-1037 purpose vocabulary (AC-7)
            gen_ai.GEN_AI_SYSTEM: provider,
            gen_ai.GEN_AI_REQUEST_MODEL: model,
        },
    )


def tool_call_span(*, tool_name: str):
    return get_tracer().start_as_current_span(
        f"tool_call {tool_name}", attributes={namespaced("tool.name"): tool_name}
    )


def assert_semconv_version_pinned() -> None:
    """AC-13: the pinned constant above must equal what's actually installed."""
    installed = _pkg_version("opentelemetry-semantic-conventions")
    if installed != SEMCONV_VERSION:
        raise RuntimeError(
            f"SEMCONV_VERSION={SEMCONV_VERSION!r} does not match installed "
            f"opentelemetry-semantic-conventions=={installed!r}; bump both together."
        )
```

`assert_semconv_version_pinned()` is called from the AC-13 test, not at import time — a version mismatch
should fail CI, not crash the running service (closes codex finding #3 from the "AC-13's literal
mechanism" risk: **exact pin**, not a floor, resolving the Rev 1 inconsistency between
`>=0.65b0` and a hard-coded equality check).

### Model-call spans: narrow the boundary to match the existing stopwatch (resolves finding #5)

`litellm_client.py::respond` — wrap **only** from just before `litellm.acompletion(**litellm_kwargs)`
(line 566) through reading `usage`/building the response dict, i.e. the same boundary the existing
stopwatch (`start_time`/the value that becomes `latency_ms`, ending ~607-608) already uses. Budget-gate
settlement, the durable `cost_tracker.record_api_call` write, and response-object construction happen
**after** the `with model_call_span(...)` block exits, exactly mirroring where the stopwatch already ends.
`emit_model_call_completed` (called later, at 768-792) reads the span's id via
`format(span.get_span_context().span_id, "016x")` — the span object itself must be captured in a local
variable before the `with` block exits so it's available at the later emit call.

`client.py::_do_request` — the existing stopwatch (`start_time = time.time()` at 365) already spans
almost exactly the httpx call + response parsing with no interleaved unrelated work (unlike
`litellm_client.py`), so wrapping the whole function body from the mint site through the completion emit
is fine here, matching Rev 1's original design for this file specifically. Verify this at implementation
time by re-reading the function in full rather than trusting this summary — the risk `litellm_client.py`
had (interleaved unrelated work between the network call and the emit) is the thing to specifically rule
out here too.

### Third caller: `gateway/chat_api.py::_emit_gateway_model_call_completed` (resolves finding #3)

This function (382-437) is a fourth-ish call site of the shared `emit_model_call_completed` helper,
missed entirely in Rev 1. Since dropping the `latency_ms` **parameter** from that shared helper (see
below) is a hard compile-time break for any caller still passing it, this caller must be fixed regardless
of how minimal a touch is preferred. Rather than leave it as the one remaining hand-minted
`span_id=uuid4().hex` path (which would make it the *only* one of four model-call sites without a real
span — inconsistent, and exactly what codex finding #13 warns about for the transition period), give it a
real span too: wrap the `client.messages.stream(...)` call in `chat_api.py`'s own streaming section
(~104-118, the `with timer.span("llm_call:anthropic_stream")` block — see RequestTimer retirement below,
this wrapper is being replaced anyway) with `with model_call_span(role="primary", model=f"anthropic/{_CLOUD_MODEL}", provider="anthropic") as span:`,
and have `_emit_gateway_model_call_completed` read `span_id` from that real span instead of minting a
UUID. This is a small increment on top of work this ticket must do anyway (RequestTimer retirement
touches this exact function), not new unscoped work.

### Tool-call spans: explicit failure status (resolves finding #9)

The real dispatcher (`tools/executor.py`) catches the tool's own exception and returns
`ToolResult(success=False, ...)` — it does not re-raise past the tool-call span's `with` block, so a naive
design would leave every failed tool call's span silently `UNSET` (looks the same as success). Fix:
inside the existing `except Exception as e:` handler (currently emitting `TOOL_CALL_FAILED` at 481-490,
this ticket only drops its `latency_ms`), explicitly mark the **span**, not just the log record:

```python
from opentelemetry.trace import Status, StatusCode

with tool_call_span(tool_name=tool_name) as span:
    span_id = format(span.get_span_context().span_id, "016x")
    log.info(TOOL_CALL_STARTED, ..., span_id=span_id)
    start_time = time.time()
    try:
        result = await ...  # existing dispatch, unchanged
        log.info(TOOL_CALL_COMPLETED, ..., span_id=span_id)  # latency_ms dropped
    except Exception as e:
        span.record_exception(e)
        span.set_status(Status(StatusCode.ERROR, str(e)))
        log.error(TOOL_CALL_FAILED, ..., span_id=span_id, exc_info=True)  # latency_ms dropped
        result = ToolResult(success=False, ...)  # existing shape, unchanged
    # existing post-processing, unchanged
```

Exact existing control flow (does the current code re-raise after building the failure `ToolResult`, or
return it inline?) must be re-read at implementation time — this snippet illustrates the required
`record_exception`/`set_status` addition, not a full replacement of the surrounding logic. Verify with a
test asserting a deliberately-failing tool call's span has `status.status_code == StatusCode.ERROR`.

### AC-14: traceparent injection with an explicit propagator, corrected type reference (resolves findings #6, #11)

```python
from opentelemetry.propagate import inject

carrier: dict[str, str] = {}
inject(carrier)  # picks up the active span (the model-call span, via context_api's current context)
request_headers: dict[str, str] = {
    "X-Trace-Id": str(trace_ctx.trace_id),
    "X-Span-Id": span_id,
    **carrier,
}
```
`opentelemetry.propagate.inject()` does correctly read the current context set by `start_as_current_span`
— confirmed by codex. But relying on the SDK's *default* global propagator is exactly the kind of implicit
behavior ADR-0129 D1 argues against, and mutating global propagator state conditionally risks clobbering
deployment configuration set elsewhere. **Fix: `otel_bootstrap.py::configure_tracing()` sets the
propagator explicitly and unconditionally at bootstrap** (it already unconditionally sets the tracer
provider, so this is consistent with the existing pattern, not new precedent):
```python
from opentelemetry.propagate import set_global_textmap
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

set_global_textmap(TraceContextTextMapPropagator())
```
Also fixes a **type error** in Rev 1's draft: `opentelemetry.trace` has no `context` submodule/attribute —
the correct type for a `Context` object is `opentelemetry.context.Context` (imported as `context_api` in
`spans.py` above), not `opentelemetry.trace.context.Context`. This plan's `spans.py` code no longer needs
that annotation at all (no more explicit `context=` argument), so the bug is moot in Rev 2's design, but is
recorded here since it would have resurfaced anywhere else a `Context` type hint was needed.

### gen_ai.operation.name and the FRE-1037 vocabulary (AC-7)

Unchanged from Rev 1: `role` passed into `model_call_span(role=...)` is already `ModelRole.value` at
every call site — `gen_ai.operation.name` becomes e.g. `"primary"`, `"sub_agent"`, a member of the
enum, matching AC-7.

### AC-12 — no field registry

Unchanged: nothing in this plan generates anything. `namespaced()` and `SEMCONV_VERSION` are plain
declarations, not a registry.

## RequestTimer full retirement — the complete blast radius (resolves finding #2; owner-confirmed literal AC-10 reading)

Per the 2026-08-08 owner decision, AC-10 is read literally: **zero `RequestTimer` callers anywhere**,
including its downstream consumers. Full list of what this touches, beyond Rev 1's orchestrator-only scope:

1. **`orchestrator/executor.py`** — remove `RequestTimer`-derived `.start_span()`/`.end_span()` calls at
   all eight phase names (`session_history_load`, `context_window`, `memory_query`, `llm_span_name`,
   `tool_span_name`, `synthesis`, `session_update` — the full set, not just the two the step/model/tool
   spans replace). No replacement spans are added for the five phases outside step/model/tool — their
   timing simply stops being recorded via `RequestTimer`; their independent `log.info` completion events
   (where they exist separately from the timer call) are untouched.
2. **`orchestrator/types.py`** — remove `ExecutionContext.request_timer: RequestTimer | None` field.
3. **`orchestrator/orchestrator.py`** — remove the `request_timer: RequestTimer | None = None` parameter
   from `Orchestrator.handle_user_request()` (line 46, its docstring at 68-69, and its pass-through to
   `ExecutionContext(...)` at 124); remove the `from personal_agent.telemetry.request_timer import
   RequestTimer` import.
4. **`telemetry/request_timer.py`** — delete the file.
5. **`telemetry/__init__.py`** — remove the `RequestTimer` import and its `__all__` entry.
6. **`service/app.py`** — two call sites (~411, ~2201): remove `timer = RequestTimer(trace_id=trace_id)`
   and the `request_timer=timer` argument to `handle_user_request(...)`; remove the `REQUEST_TIMING`
   log-emit block (~2233-2245, reads `timer.to_breakdown()`/`.get_total_ms()` — no replacement, this was a
   RequestTimer-only diagnostic with no ADR-mandated successor in this ticket); remove
   `trace_summary=timer.to_trace_summary(), trace_breakdown=timer.to_breakdown()` from both
   `RequestCompletedEvent(...)` constructions (~509, ~2260s); remove the now-dead
   `es_handler.es_logger.index_request_trace(...)` calls (~541, ~2278).
7. **`gateway/chat_api.py`** — remove `timer = RequestTimer(trace_id=trace_id)` (~98) and its
   `with timer.span("llm_call:anthropic_stream"):` wrapper, replaced by the `model_call_span(...)` wrapper
   from the "Third caller" section above; remove `trace_summary`/`trace_breakdown` from its own
   `RequestCompletedEvent(...)` construction (~137-145).
8. **`events/models.py`** — remove `RequestCompletedEvent.trace_summary: dict[str, Any]` and
   `.trace_breakdown: list[dict[str, Any]]` fields, and their docstring lines (246-247). This is a
   Pydantic schema change on an event carried over Redis Streams — the event is ephemeral (not a durable
   store schema needing a migration), so this is a clean removal, not a versioned migration.
9. **`events/request_completed_handlers.py`** — delete `build_request_trace_es_handler` entirely (its
   only job was reading the two fields just removed). Leave `build_session_writer_handler` untouched (it
   doesn't reference `trace_summary`/`trace_breakdown`).
10. **`service/app.py`** (registration) — remove the `build_request_trace_es_handler` import (~1034) and
    its subscription registration (~1062).
11. **`telemetry/es_logger.py`** — delete `index_request_trace` (the `RequestTimer`-typed wrapper, 355-397)
    and `index_request_trace_from_snapshot` (400+) **together**, since after (6)/(9) above nothing in
    production calls either — leaving `index_request_trace_from_snapshot` in place uncalled would be dead
    code my own changes orphaned (CLAUDE.md: remove what your changes make unused). Verify with a fresh
    grep at implementation time that nothing else calls it (several tests do — see below, all updated in
    step).
12. **`observability/route_trace/assembler.py`** — remove the `request_timer = getattr(ctx,
    "request_timer", None)` block (264-269) and its `latency_total_ms`/`latency_breakdown` derivation.
    `RouteTraceRow.latency_total_ms`/`.latency_breakdown` (`route_trace/types.py:141-142`) stay in the
    dataclass/Postgres schema as-is — both already `| None = None` — they simply become always-`None` for
    rows assembled going forward. **No Postgres migration in this ticket**; this is an accepted, honest
    consequence of retiring the sole data source, not a schema change.
13. **`ui/cli.py::telemetry_trace_breakdown`** — no code change. It calls a separate
    `get_request_latency_breakdown(trace_id)` helper (reads `request_trace` ES docs by id, not
    `RequestCompletedEvent` fields directly) and already has a graceful empty-state message
    ("No latency breakdown for trace_id... run a request after this change"). Once (9)-(11) land, new
    turns simply produce no `request_trace` docs and the command reports its existing empty state — a
    documented, accepted consequence, not a bug this ticket must fix.
14. **Tests to update/delete**: `tests/test_telemetry/test_request_timer.py` (delete — tests the retired
    class); `tests/observability/route_trace/test_assembler.py` (constructs a `RequestTimer` in
    `_base_ctx()` — rewrite to not pass `request_timer=`, since the field/attribute is gone);
    `tests/test_events/test_request_completed_handlers.py` (tests
    `build_request_trace_es_handler`/`index_request_trace_from_snapshot` wiring — delete the handler-test
    cases, keep any covering `build_session_writer_handler`); `tests/test_telemetry/test_es_logger_redaction.py`
    and `tests/test_telemetry/test_es_logger.py` (call `index_request_trace_from_snapshot` directly —
    delete these cases since the method is deleted); `tests/test_orchestrator/test_eval_isolation.py`
    (asserts `index_request_trace_from_snapshot` called/not-called for eval-mode gating — this
    eval-mode-skip behavior has no replacement to test once the method is gone; delete these two
    assertions, and confirm nothing else in that test file depended on the surrounding scaffolding);
    `tests/personal_agent/events/test_request_completed_consumer_integration.py` (same — remove
    `request_trace`-handler-specific assertions, keep session-writer-handler coverage).

**Diff-class consequence**: item 9/11 above mean this ticket's diff removes a live Elasticsearch
write path (the `request_trace`/`request_trace_step` indexing). Per the build skill's Step 8 escalation
rule (trigger 1, "production write path... or sits directly in that write's call chain"), **this
ticket's diff class is escalated** — self-review still runs and still gets fixed on-branch, but the PR
body + ticket handoff must flag it for owner `/code-review ultra` before merge, per lifecycle-rules.

## Files touched (Rev 2, complete)

| File | Change |
|---|---|
| `src/personal_agent/telemetry/spans.py` | **New.** Tracer accessor, `open_step_span`/`close_step_span` (real OTel attach/detach), `model_call_span`/`tool_call_span` context managers (no explicit `context=` needed), `namespaced()`, `SEMCONV_VERSION` (exact pin), `assert_semconv_version_pinned()`. |
| `src/personal_agent/orchestrator/executor.py` | Driver loop: centralized `try`/`finally` open/close of the step span keyed on state transition, plus a turn-level backstop close. Remove **all eight** `RequestTimer` phase call sites. Remove `tool_execution_completed` log emit (AC-5). |
| `src/personal_agent/orchestrator/types.py` | Remove `ExecutionContext.request_timer` field. |
| `src/personal_agent/orchestrator/orchestrator.py` | Remove `request_timer` param from `handle_user_request()` + its docstring + `RequestTimer` import. |
| `src/personal_agent/telemetry/request_timer.py` | **Deleted.** |
| `src/personal_agent/telemetry/__init__.py` | Remove `RequestTimer` import/`__all__` entry. |
| `src/personal_agent/tools/executor.py` | Wrap tool dispatch in `tool_call_span`; real span id; `span.record_exception`/`set_status` on failure; drop `latency_ms` from `TOOL_CALL_COMPLETED`/`TOOL_CALL_FAILED` payloads. |
| `src/personal_agent/llm_client/client.py` | Wrap `_do_request` in `model_call_span` (verify boundary matches stopwatch); real span id; `gen_ai.usage.*` attrs; inject `traceparent` (AC-14). |
| `src/personal_agent/llm_client/litellm_client.py` | Wrap **only** the `litellm.acompletion()` call + minimal usage extraction in `model_call_span` (narrow boundary, matches existing stopwatch — codex finding #5); real span id; `gen_ai.usage.*` attrs. |
| `src/personal_agent/llm_client/telemetry.py` | Drop `latency_ms` **parameter** from `emit_model_call_completed`; update docstring (drop `CANONICAL_MODEL_CALL_*_FIELDS` references). |
| `src/personal_agent/telemetry/events.py` | Delete `CANONICAL_MODEL_CALL_STARTED_FIELDS`, `CANONICAL_MODEL_CALL_COMPLETED_FIELDS`. |
| `src/personal_agent/telemetry/otel_bootstrap.py` | Set the global W3C `TraceContextTextMapPropagator` explicitly (AC-14, codex finding #6/#11). |
| `src/personal_agent/service/app.py` | Remove both `RequestTimer` construction sites, the `REQUEST_TIMING` log block, `trace_summary`/`trace_breakdown` from both `RequestCompletedEvent` constructions, both `index_request_trace(...)` calls, and the `build_request_trace_es_handler` import + registration. |
| `src/personal_agent/gateway/chat_api.py` | Remove `RequestTimer` construction + `.span()` wrapper (replaced by `model_call_span`); fix `_emit_gateway_model_call_completed` to use the real span id; remove `trace_summary`/`trace_breakdown` from its `RequestCompletedEvent`. |
| `src/personal_agent/events/models.py` | Remove `RequestCompletedEvent.trace_summary`/`.trace_breakdown` fields. |
| `src/personal_agent/events/request_completed_handlers.py` | Delete `build_request_trace_es_handler`. |
| `src/personal_agent/telemetry/es_logger.py` | Delete `index_request_trace`, `index_request_trace_from_snapshot`. |
| `src/personal_agent/observability/route_trace/assembler.py` | Remove the `request_timer`-derived `latency_total_ms`/`latency_breakdown` block. |
| `pyproject.toml` | Add `opentelemetry-semantic-conventions==0.65b0` (exact pin, matching `spans.SEMCONV_VERSION`). |
| `tests/personal_agent/llm_client/test_telemetry_parity.py` | Re-point from `CANONICAL_MODEL_CALL_*_FIELDS` to span-attribute conformance. |
| `tests/test_telemetry/test_request_timer.py` | **Deleted.** |
| `tests/test_telemetry/test_es_logger.py`, `test_es_logger_redaction.py` | Remove `index_request_trace_from_snapshot` test cases. |
| `tests/test_events/test_request_completed_handlers.py` | Remove `build_request_trace_es_handler` test cases. |
| `tests/personal_agent/events/test_request_completed_consumer_integration.py` | Remove request-trace-handler assertions. |
| `tests/test_orchestrator/test_eval_isolation.py` | Remove the two `index_request_trace_from_snapshot` call/no-call assertions. |
| `tests/observability/route_trace/test_assembler.py` | Rewrite `_base_ctx()` to not construct/pass a `RequestTimer`. |
| `tests/personal_agent/orchestrator/` (new file, e.g. `test_span_tree.py`) | New integration test(s) for AC-1 through AC-5, AC-9, AC-11: one exercised tool-using turn (≥2 concurrent tool calls) under `InMemorySpanExporter`; assert shape, parent/child timing, tool-span-count parity, no legacy duration fields, no `tool_execution_completed` record, and step-span closure on at least one non-happy-path exit (e.g. a forced iteration-limit or a mocked tool exception) — not just the happy path. |
| `tests/personal_agent/tools/` | New test: a deliberately-failing tool call's span has `StatusCode.ERROR` (codex finding #9). |
| `tests/personal_agent/llm_client/` (new file) | New test(s) for AC-6, AC-13, AC-14: gen_ai attribute correctness, `assert_semconv_version_pinned()`, `traceparent` header well-formedness + trace-id match + legacy headers retained; a test for `gateway/chat_api.py`'s fixed third caller. |
| `tests/personal_agent/orchestrator/test_executor.py` | Existing suite — regression net; must still pass after the driver-loop rewrite. |

## Explicitly out of scope (per ticket text, confirmed against code)

- Background entrypoint root spans (scheduler, monitors) — FRE-1069 (B4).
- OTel Collector export — FRE-1070 (B5). No processor/exporter is attached to the `TracerProvider` today;
  this ticket does not add one. All new tests use their own `InMemorySpanExporter`, never the process
  global.
- `slm_server`-side OTLP export — FRE-1071 (B6, separate repo). This ticket only injects the outbound
  `traceparent` header from this repo's side (AC-14); it does not touch `slm_server`.
- The seam ticket's population-level, cross-store, 7-day-window acceptance criteria (AC-1 through AC-10 of
  the **ADR**, not of this ticket) — those belong to FRE-1073 per ADR-0130 D1/D2.
- Postgres migration for `RouteTraceRow.latency_total_ms`/`.latency_breakdown` — they stay `| None`,
  simply always-`None` going forward.
- `ui/cli.py::telemetry_trace_breakdown` code changes — its data source empties out, its existing
  empty-state message already covers this.

## Atomic implementation steps

1. `telemetry/spans.py` — write the module per the Design section above (real attach/detach, no
   `context=` param needed on child spans). `pyproject.toml` — add
   `opentelemetry-semantic-conventions==0.65b0`; `uv sync`.
   Verify: `uv run python -c "from personal_agent.telemetry import spans; spans.assert_semconv_version_pinned()"`.
2. `otel_bootstrap.py` — set the global W3C propagator explicitly.
   Verify: `uv run python -c "from opentelemetry.propagate import get_global_textmap; print(get_global_textmap())"` after calling `configure_tracing()`.
3. Failing-first tests for the span-tree primitives in isolation (`open_step_span`/`close_step_span`
   attach/detach correctness, `model_call_span`/`tool_call_span` auto-parenting via current context) —
   prove the primitive before wiring it into 6+ call sites.
   Verify: `make test-file FILE=tests/test_telemetry/test_spans.py`.
4. Rewrite the orchestrator driver loop's step-span lifecycle (centralized `try`/`finally` + backstop);
   remove `tool_execution_completed` emit; remove the two `RequestTimer` calls this replaces
   (`llm_span_name`, `tool_span_name`).
   Verify: `make test-file FILE=tests/personal_agent/orchestrator/test_executor.py` (existing suite must
   still pass — this is the regression net for the FSM rewrite).
5. Remove the remaining six `RequestTimer` phase call sites in `executor.py`.
   Verify: `ast-grep run -p 'RequestTimer' -l py src/personal_agent/orchestrator/` and
   `-p '$X.start_span($$$)'` / `-p '$X.end_span($$$)'` scoped to `executor.py` all return zero.
6. Full `RequestTimer` retirement per the "blast radius" section: `orchestrator.py`, `types.py`,
   `service/app.py` (both call sites + REQUEST_TIMING + index_request_trace calls + handler registration),
   `gateway/chat_api.py` (RequestTimer removal + third-caller span fix together, since both touch the same
   function), `events/models.py`, `events/request_completed_handlers.py`, `es_logger.py`, `route_trace/assembler.py`,
   delete `telemetry/request_timer.py`, update `telemetry/__init__.py`. Update/delete every test in the
   "Tests to update/delete" list above.
   Verify: `ast-grep run -p 'RequestTimer' -l py src/ tests/` returns zero (AC-10's own census, run before
   writing the AC-10-satisfying test); `make test-file FILE=tests/observability/route_trace/test_assembler.py`;
   `make test-file FILE=tests/test_events/test_request_completed_handlers.py`.
7. Wire `tool_call_span` into `tools/executor.py`, including the `record_exception`/`set_status` failure
   path; drop `latency_ms` from `TOOL_CALL_COMPLETED`/`FAILED`.
   Verify: `make test-file FILE=tests/personal_agent/tools/test_executor.py` plus the new failure-status test.
8. Wire `model_call_span` into `client.py::_do_request` (verify existing boundary is already tight) and
   `litellm_client.py::respond` (narrow boundary to match the existing stopwatch, per Design); drop
   `latency_ms` parameter from `emit_model_call_completed`; fix the `gateway/chat_api.py` third caller;
   delete `CANONICAL_MODEL_CALL_*_FIELDS` from `events.py`; re-point `test_telemetry_parity.py`.
   Verify: `make test-file FILE=tests/personal_agent/llm_client/test_telemetry_parity.py`.
9. AC-14 — inject `traceparent` in `client.py:432`.
   Verify: new test asserting header presence, well-formedness, trace-id match, legacy headers retained.
10. Integration test(s) for AC-1 through AC-5, AC-9, AC-11 — one exercised turn, ≥2 concurrent tool calls,
    under `InMemorySpanExporter`; **plus at least one non-happy-path exit test** (step span still closes
    correctly on a forced error/iteration-limit path — this is the test that would have caught finding #1
    had it existed in Rev 1).
    Verify: `make test-file FILE=tests/personal_agent/orchestrator/test_span_tree.py` — inspect exported
    spans manually once before trusting the assertions.
11. AC-13 test; AC-8 census (every attribute key in this ticket's diff is `gen_ai.*` or `personal_agent.*`).
12. Full suite: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Test commands (exact)

```
make test-file FILE=tests/test_telemetry/test_spans.py
make test-file FILE=tests/personal_agent/orchestrator/test_executor.py
make test-file FILE=tests/personal_agent/orchestrator/test_span_tree.py
make test-file FILE=tests/personal_agent/tools/test_executor.py
make test-file FILE=tests/personal_agent/llm_client/test_telemetry_parity.py
make test-file FILE=tests/personal_agent/llm_client/test_traceparent_injection.py
make test-file FILE=tests/observability/route_trace/test_assembler.py
make test-file FILE=tests/test_events/test_request_completed_handlers.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Residual risks (all Rev 1 risks resolved above; carried forward for implementation-time verification, not design gaps)

1. **`litellm_client.py`'s narrowed span boundary** — the exact line range to wrap must be re-verified
   against the live function at implementation time (line numbers drift); the invariant to preserve is
   "span duration ≈ existing stopwatch duration," not a specific line range.
2. **`step_tool_execution`'s exact mechanism for surfacing `tool_count`** back to the driver loop's
   `finally` is left as an implementation-time judgment call (see "tool_count on the step span" above) —
   not a design risk, a plumbing detail.
3. **Test coverage for the non-happy-path step-span-closure exits** (deadline timeout, cancellation,
   hybrid-expansion re-entry) should ideally cover more than the one extra test step 10 adds — if time
   allows, add one test per exit family rather than one representative test, since this is exactly the
   class of bug (finding #1) this revision exists to prevent from recurring.
