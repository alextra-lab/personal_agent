# FRE-1067 — ADR-0129 B3: the span tree (step / model-call / tool-call), gen_ai semconv, retiring RequestTimer

**Ticket:** FRE-1067 (Approved, In Progress) · **ADR:** ADR-0129 D2, D3 · **Tier:** Standard/Complex — codex plan-review required.

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
- **`RequestTimer`** (`telemetry/request_timer.py`, class at line 88) is still live. Callers:
  `service/app.py:411,1984`, `gateway/chat_api.py:98` (construction); `orchestrator/executor.py` uses
  `.start_span()`/`.end_span()` at 8 phase names, most relevantly `llm_span_name` (opened 4731, closed at
  4810/4875/4914/5096) and `tool_span_name` (opened 5224, closed at 5272/5297/5303/5315/5328/5632).
- **The orchestrator is a flat state machine**, not nested calls: `executor.py:2713-2740` runs
  `while state not in {COMPLETED, FAILED}: state = await step_functions[state](ctx, session_manager, trace_ctx)`.
  `TaskState.LLM_CALL` → `step_llm_call` (def at 4072) and `TaskState.TOOL_EXECUTION` → `step_tool_execution`
  (def at 5191) are **separate top-level functions**, not one nested inside the other. `step_llm_call`
  returns `TOOL_EXECUTION` when the response carries tool calls (line 5084), else `SYNTHESIS` (5091) or
  `FAILED` (5188). `step_tool_execution` always returns to `LLM_CALL` (or `FAILED`/`SYNTHESIS` on its own
  error/iteration-limit paths) after its tail emit at ~5628-5643:
  ```python
  duration_ms = int((time.time() - step_start_time) * 1000)
  tool_names = [...]
  if timer and tool_span_name:
      timer.end_span(tool_span_name, tool_count=len(tool_calls), tool_names=tool_names)
  log.info("tool_execution_completed", trace_id=ctx.trace_id, tool_count=len(tool_calls), duration_ms=duration_ms)
  ```
  This is the "parent with no `span_id`" ADR-0129's Context describes, and per AC-5 this log line must
  **stop being emitted** (its tool count moves onto the step span as an attribute).
- **Tool-call spans**: `tools/executor.py` mints a hand-rolled span id per call (`trace_ctx.new_span()` at
  line 428) then emits `TOOL_CALL_STARTED` (429), `TOOL_CALL_COMPLETED` (461-468, carries `latency_ms`),
  `TOOL_CALL_FAILED` (481-490, carries `latency_ms`). These three log records are **retained** (D3, AC-11)
  — only `latency_ms` drops (AC-9 names these exact lines), everything else about the log records is
  unchanged.
- **Model-call spans**: both clients mint a hand-rolled span id via `trace_ctx.new_span()`
  (`client.py:366`, `litellm_client.py:550`), call `emit_model_call_started`/`emit_model_call_completed`
  (`llm_client/telemetry.py`), and the actual provider call happens inside that same function body —
  `client.py::_do_request` (263-~680, httpx stream at 458) and `litellm_client.py::respond`
  (321-~800, `litellm.acompletion` at 566). `emit_model_call_completed`'s payload carries `latency_ms`
  (AC-9 requires dropping it from the **log payload only** — the Postgres `api_costs.latency_ms` column,
  written separately by `cost_tracker.record_api_call`, is untouched; ADR-0129 AC-4 depends on it).
- **`ModelRole`** (`llm_client/types.py:16-64`, str Enum: `PRIMARY, SUB_AGENT, COMPRESSOR,
  ARTIFACT_BUILDER, ENTITY_EXTRACTION, CAPTAINS_LOG, SESSION_SUMMARY, INSIGHTS, EMBEDDING, RERANKER,
  RERANKER_FALLBACK, VISION, SKILL_ROUTING, STUDY`) is the FRE-1037 "purpose" vocabulary AC-7 requires
  `gen_ai.operation.name` to draw from — both clients already pass `role.value` into the emit helpers.
- **`gen_ai` semconv**: zero usage in `src/` today. `opentelemetry-semantic-conventions` is installed
  transitively at `0.65b0` but not a declared dependency (`pyproject.toml:65-66` only lists
  `opentelemetry-sdk`, `opentelemetry-exporter-otlp`). The attribute name constants live at
  `opentelemetry.semconv._incubating.attributes.gen_ai_attributes` (`GEN_AI_OPERATION_NAME`,
  `GEN_AI_SYSTEM`, `GEN_AI_REQUEST_MODEL`, `GEN_AI_USAGE_INPUT_TOKENS`, `GEN_AI_USAGE_OUTPUT_TOKENS`) —
  using these constants (not hand-typed strings) means an incubating-module rename breaks the build
  loudly instead of drifting silently.
- **`CANONICAL_MODEL_CALL_STARTED_FIELDS` / `CANONICAL_MODEL_CALL_COMPLETED_FIELDS`**
  (`telemetry/events.py:54-82`) have exactly one consumer: `tests/personal_agent/llm_client/test_telemetry_parity.py`
  (plus a docstring mention in `llm_client/telemetry.py:10-11`). Safe to delete both frozensets once that
  test is re-pointed.
- **`InMemorySpanExporter` test scaffold** already exists twice (`tests/test_telemetry/test_trace_otel_bridge.py:24-46`,
  `tests/personal_agent/service/test_otel_root_span.py:20-34`) — own `TracerProvider` +
  `SimpleSpanProcessor(InMemorySpanExporter())`, never touching the process-global provider. Reuse this
  shape for every new test.

## Design

### Why a plain design choice matters here: explicit parent-passing, not "current span" attach/detach

The step span must stay valid as a parent across **two separate top-level async function calls**
(`step_llm_call` then `step_tool_execution`) driven by a flat `while` loop — no single `with` block can
lexically wrap both. Two ways to solve this:

1. OTel's own `context.attach()`/`context.detach()` primitives, manually paired.
2. A plain `contextvars.ContextVar` holding the step `Span` object, set by the driver loop, read
   explicitly by child-span creators via `context=trace.set_span_in_context(step_span)`.

**Chosen: (2).** It sidesteps the attach/detach pairing-on-every-exit-path hazard entirely (a leaked
token from an unhandled exception path corrupts the context stack; a plain contextvar has no such
failure mode — worst case a stale reference that gets overwritten next request), and it keeps the
step span deliberately **not** "current" — only explicitly-parented children reference it, exactly
matching the ADR's "explicit propagation over implicit convention" spirit (D1). Model-call and tool-call
spans, by contrast, **do** use `start_as_current_span` (a normal `with`, single function scope) because
(a) that's what makes `opentelemetry.propagate.inject()` pick them up for AC-14's `traceparent` header,
and (b) it's what makes the existing structlog processor auto-correlate `trace_id`/`span_id` onto the
log lines inside that scope for free.

### New module: `src/personal_agent/telemetry/spans.py`

```python
"""Span-tree helpers for ADR-0129 D3 (root → step → {model-call, tool-call})."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from opentelemetry import trace
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.trace import Span

_TRACER_NAME = "personal_agent"
_ATTR_NAMESPACE = "personal_agent"

_current_step_span: ContextVar[Span | None] = ContextVar("_current_step_span", default=None)


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


def namespaced(key: str) -> str:
    """Prefix a non-semconv attribute key with the project namespace (AC-8)."""
    return f"{_ATTR_NAMESPACE}.{key}"


def get_current_step_span() -> Span | None:
    return _current_step_span.get()


def open_step_span(*, iteration: int) -> Span:
    """Start the step span. Parent is whatever span is currently active
    (the request root span, or none for background entrypoints pre-FRE-1069).
    Does NOT make the span "current" — callers read it back via
    ``get_current_step_span()`` and pass it as an explicit parent.
    """
    span = get_tracer().start_span(
        "step",
        attributes={namespaced("step.iteration"): iteration},
    )
    _current_step_span.set(span)
    return span


def close_step_span(*, tool_count: int) -> None:
    span = _current_step_span.get()
    if span is not None:
        span.set_attribute(namespaced("step.tool_count"), tool_count)
        span.end()
    _current_step_span.set(None)


def _step_parent_context() -> trace.context.Context | None:  # type: ignore[name-defined]
    step_span = get_current_step_span()
    if step_span is None:
        return None
    return trace.set_span_in_context(step_span)


def model_call_span(*, role: str, model: str, provider: str):
    """Context manager: opens a model-call span as CURRENT, parented to the
    step span. gen_ai.request.* attributes are set at open; usage.* are set
    by the caller via ``span.set_attribute`` before the ``with`` exits.
    """
    return get_tracer().start_as_current_span(
        f"model_call {model}",
        context=_step_parent_context(),
        attributes={
            gen_ai.GEN_AI_OPERATION_NAME: role,  # FRE-1037 purpose vocabulary (AC-7)
            gen_ai.GEN_AI_SYSTEM: provider,
            gen_ai.GEN_AI_REQUEST_MODEL: model,
        },
    )


def tool_call_span(*, tool_name: str):
    return get_tracer().start_as_current_span(
        f"tool_call {tool_name}",
        context=_step_parent_context(),
        attributes={namespaced("tool.name"): tool_name},
    )
```

Notes for implementation, not final code:
- `close_step_span` must be safe to call when no step span is open (defensive `if span is not None`).
- `model_call_span`/`tool_call_span` return the context-manager object directly (not a generator) so
  callers do `with model_call_span(...) as span:` and set response attributes on `span` before exit.
- A helper `record_semconv_version()` or a simple constant module is needed for AC-13 — see Step 9.

### Step-span lifecycle: `orchestrator/executor.py` driver loop (~2713-2740)

Open the step span whenever entering `LLM_CALL` with no step span already open; close it whenever a
step function returns anything other than `TOOL_EXECUTION` (covers: no-tool-calls → SYNTHESIS, any
error → FAILED, and the normal tool-calls-ran → back-to-LLM_CALL path). One insertion point, ~6 lines:

```python
while state not in {TaskState.COMPLETED, TaskState.FAILED}:
    ...
    if state == TaskState.LLM_CALL and get_current_step_span() is None:
        open_step_span(iteration=ctx.tool_iteration_count)

    state = await step_func(ctx, session_manager, trace_ctx)

    if get_current_step_span() is not None and state != TaskState.TOOL_EXECUTION:
        close_step_span(tool_count=ctx.steps[-1].get("metadata", {}).get("tool_count", 0) if ...)
```

**Open question for codex review:** where does `close_step_span`'s `tool_count` come from cleanly? The
cleanest source is `step_tool_execution`'s own local `len(tool_calls)` at its tail (~line 5628) — so
prefer having `step_tool_execution` call `close_step_span(tool_count=len(tool_calls))` itself, at its own
tail (replacing the `tool_execution_completed` log emit and the `timer.end_span(tool_span_name, ...)`
call), and having `step_llm_call` call `close_step_span(tool_count=0)` on its own no-tool-calls exit path
(line ~5091) and its own error exit path (line ~5188). The driver loop then only needs the **open**
half (`if state == TaskState.LLM_CALL and get_current_step_span() is None: open_step_span(...)`) —
closing lives at each step function's own natural exit points, which is more explicit and avoids
threading `tool_count` back out through the driver loop. **This plan adopts that shape** — three close
call sites (one in `step_llm_call`'s no-tool branch, one in its error branch, one in
`step_tool_execution`'s tail), all trivial to audit against AC-5's "no `tool_execution_completed` record,
step span carries the tool count."

Also in this pass: delete the `timer.start_span(tool_span_name)`/`.end_span(...)` calls (5224 and its six
close sites) and the `timer.start_span(llm_span_name)`/`.end_span(...)` calls (4731 and its four close
sites) — these are `RequestTimer` retirement (AC-10). Do **not** touch the other `RequestTimer` phase
names in this file (`session_history_load`, `context_window`, `memory_query`, `synthesis`,
`session_update`) — ADR-0129's scope list names only `orchestrator/executor.py:5373` (the tool-execution
parent) and the model/tool spans; the other five phases are out of scope for this ticket and stay on
`RequestTimer` **unless** that leaves `RequestTimer` with remaining callers, which would fail AC-10's
zero-caller census. **This is the single biggest scope-boundary risk in this ticket — flag it explicitly
to codex.** Two honest options: (a) AC-10 is read literally and this ticket must also retire the other
five `RequestTimer` phases (session_history_load, context_window, memory_query, synthesis,
session_update) even though the ADR's file list doesn't name them, since "no callers left" is
unconditional; or (b) those five phases are understood to be a different, unticketed retirement and
AC-10 is scoped implicitly to the phases this ticket touches. Re-reading AC-10: *"`RequestTimer` has no
callers left. Proven by: an `ast-grep` census for `start_span` and `end_span` call sites returning zero,
with the methods removed rather than deprecated."* This is unconditional — it does not say "no callers
among the phases this ticket touches." **Decision: (a) — retire all `RequestTimer` usage in this ticket,
across all phase names**, since AC-10 is this ticket's own criterion and reads as absolute. That means
also touching `session_history_load` (3369/3377), `context_window` (3772/3801), `memory_query`
(3819/4002-4035), `synthesis` (5704-5735 area), `session_update`, plus removing `RequestTimer`
construction from `service/app.py:411,1984` and `gateway/chat_api.py:98`, and deleting
`telemetry/request_timer.py` and the `ExecutionContext.request_timer` field entirely. **These five extra
phases get no new OTel spans** — ADR-0129 D3 only mandates the step/model-call/tool-call tree; the other
phases' timing simply stops being recorded as a span or a `RequestTimer` interval (their `log.info`
completion events, where they exist independently of the timer, are untouched). Flag this reading to
codex explicitly — it is the one place this plan extends past the ADR's named file list, and it is being
extended because a stated AC leaves no honest narrower reading.

### Model-call spans: `llm_client/client.py::_do_request`, `llm_client/litellm_client.py::respond`

Wrap the existing body (from just after `start_time = time.time()` / `span_id = trace_ctx.new_span()`
through the `emit_model_call_completed` call and `return`) in:

```python
with model_call_span(role=role.value, model=model_id, provider=model_config.provider or "unknown") as span:
    ...  # existing body, unchanged, except:
    # - span_id passed to emit_model_call_started/completed becomes the real OTel span id:
    #   span_id = format(span.get_span_context().span_id, "016x")
    # - after usage is known, before emit_model_call_completed:
    span.set_attribute(gen_ai.GEN_AI_USAGE_INPUT_TOKENS, _pt)
    span.set_attribute(gen_ai.GEN_AI_USAGE_OUTPUT_TOKENS, _ct)
    emit_model_call_completed(..., span_id=span_id, ...)  # drop latency_ms from the log payload only
```

Both clients already have exactly one span_id mint (`client.py:366`, `litellm_client.py:550`) and one
completion emit (`client.py:554`, `litellm_client.py:768`) — replace the mint with reading the real span,
keep everything else. `emit_model_call_started`/`emit_model_call_completed` keep the `latency_ms`
**parameter** (needed nowhere inside `llm_client/telemetry.py` itself, but check whether removing it from
the payload dict only, vs. removing the parameter, is cleaner — **removing the payload key while keeping
the parameter unused would trip ruff's unused-argument-adjacent lints; simplest is to drop the parameter
from `emit_model_call_completed` entirely and have both call sites stop passing it**, since `latency_ms`
now lives nowhere in that function once the payload key is gone). Confirm no other caller of
`emit_model_call_completed` exists (`grep -rn "emit_model_call_completed(" src/`) before removing the
parameter.

**AC-14 (traceparent injection)** lands inside this same `with model_call_span(...)` block, in
`client.py` only (the ticket names `client.py:432`, the SLM outbound call — `litellm_client.py` calls
`litellm.acompletion` which is a different, non-SLM transport and is out of scope per the ticket's own
"Scope" section, which cites only `client.py:432`):

```python
from opentelemetry.propagate import inject

carrier: dict[str, str] = {}
inject(carrier)  # picks up the active span (the model-call span) via the global propagator
request_headers: dict[str, str] = {
    "X-Trace-Id": str(trace_ctx.trace_id),
    "X-Span-Id": span_id,
    **carrier,
}
```
`opentelemetry.propagate.inject` uses the SDK-configured global propagator, which defaults to W3C
`tracecontext` — no explicit propagator construction needed unless `configure_tracing()`
(`otel_bootstrap.py`) doesn't already set one; confirm this at implementation time (`grep -n "set_global_textmap\|propagate" src/personal_agent/telemetry/otel_bootstrap.py`) and set one explicitly if absent, since relying on an unconfigured default is exactly the kind of implicit behavior ADR-0129 D1 argues against.

### Tool-call spans: `tools/executor.py` (~420-495)

```python
with tool_call_span(tool_name=tool_name) as span:
    span_id = format(span.get_span_context().span_id, "016x")
    log.info(TOOL_CALL_STARTED, tool_name=tool_name, arguments=..., trace_id=trace_ctx.trace_id, span_id=span_id)
    start_time = time.time()
    try:
        result = await ...  # existing dispatch, unchanged
        log.info(TOOL_CALL_COMPLETED, tool_name=tool_name, success=True, trace_id=trace_ctx.trace_id, span_id=span_id)  # latency_ms dropped
    except Exception as e:
        log.error(TOOL_CALL_FAILED, tool_name=tool_name, error=str(e), trace_id=trace_ctx.trace_id, session_id=session_id, span_id=span_id, exc_info=True)  # latency_ms dropped
        raise
```
Replace `trace_ctx.new_span()`'s hand-minted id with the real span's id, same as the model-call sites.
Confirm `asyncio.gather`-dispatched parallel tool calls each get their own correctly-parented span — each
gathered coroutine, when scheduled, copies the contextvar state (including `_current_step_span`) at
`asyncio.ensure_future`/task-creation time, so this should just work; **write a test asserting it**
(two concurrent tool calls in one step, both must show the step span as parent — this is exactly AC-1's
own check).

### gen_ai.operation.name and the FRE-1037 vocabulary (AC-7)

`role` passed into `model_call_span(role=...)` is already `ModelRole.value` (a string) at both call
sites — no new mapping needed, `gen_ai.operation.name` becomes e.g. `"primary"`, `"sub_agent"`, matching
AC-7's requirement that the value be a member of the enum, not a free-form string.

### AC-13 — recorded semconv version matches installed

Add `opentelemetry-semantic-conventions>=0.65b0` as an explicit `pyproject.toml` dependency (pin the
floor to what's installed; do not pin exact, since it is a backwards-compatible incubating package that
moves fast). Add a small test (not a runtime check — AC-13 is a static/test-time criterion) asserting
`importlib.metadata.version("opentelemetry-semantic-conventions")` is non-empty and, if this ticket
records the version anywhere (a constant, a comment), that recorded value round-trips against the
installed one — simplest implementation: **do not hand-record a version string anywhere** (nothing to
drift), and instead have the AC-13 test itself read the installed version and assert it is what the test
expects to exercise against, e.g.:
```python
def test_semconv_version_matches_installed():
    from importlib.metadata import version
    installed = version("opentelemetry-semantic-conventions")
    assert installed == importlib.metadata.version("opentelemetry-semantic-conventions")  # trivially true — WRONG, revisit
```
**Flag to codex:** AC-13's exact mechanism needs a second look — "a test asserting the version recorded
in the repository equals the resolved version of the installed package" implies the repository *does*
record a version somewhere (e.g. a constant `SEMCONV_VERSION = "0.65b0"` in `spans.py`) which the test
then diffs against `importlib.metadata.version(...)`. That is the more literal reading and is what this
plan will implement: a `SEMCONV_VERSION` constant in `telemetry/spans.py`, plus a test that fails loudly
the moment `pyproject.toml`'s floor moves past what the constant says — cheap insurance against exactly
the kind of silent drift AC-13 exists to catch.

### AC-12 — no field registry

Nothing in this plan generates anything (no codegen, no CI diff-gate). `spans.py`'s `namespaced()` helper
and the `SEMCONV_VERSION` constant are plain declarations, not a registry — consistent with the
already-adjudicated AC-12 wording (struck clause aside, the surviving intent is "no generator, no CI
drift-gate over generated span-attribute output," which this plan does not introduce).

## Files touched

| File | Change |
|---|---|
| `src/personal_agent/telemetry/spans.py` | **New.** Tracer accessor, step-span contextvar + open/close, `model_call_span`/`tool_call_span` context managers, `namespaced()`, `SEMCONV_VERSION`. |
| `src/personal_agent/orchestrator/executor.py` | Driver loop: open step span on `LLM_CALL` entry. `step_llm_call`: close step span on no-tool-calls exit and on error exit; remove `llm_span_name` `RequestTimer` calls; remove `session_history_load`/`context_window`/`memory_query`/`synthesis`/`session_update` `RequestTimer` calls (AC-10). `step_tool_execution`: replace `tool_execution_completed` log emit + `tool_span_name` `RequestTimer` calls with `close_step_span(tool_count=...)`. |
| `src/personal_agent/orchestrator/types.py` | Remove `ExecutionContext.request_timer` field. |
| `src/personal_agent/telemetry/request_timer.py` | **Deleted.** |
| `src/personal_agent/tools/executor.py` | Wrap tool dispatch in `tool_call_span`; replace hand-minted `span_id`; drop `latency_ms` from `TOOL_CALL_COMPLETED`/`TOOL_CALL_FAILED` payloads. |
| `src/personal_agent/llm_client/client.py` | Wrap `_do_request` body in `model_call_span`; replace hand-minted `span_id`; set `gen_ai.usage.*` attrs; inject `traceparent` alongside `X-Trace-Id`/`X-Span-Id` (AC-14). |
| `src/personal_agent/llm_client/litellm_client.py` | Wrap `respond` body in `model_call_span`; replace hand-minted `span_id`; set `gen_ai.usage.*` attrs. |
| `src/personal_agent/llm_client/telemetry.py` | Drop `latency_ms` parameter from `emit_model_call_completed`; update docstring (drop `CANONICAL_MODEL_CALL_*_FIELDS` references). |
| `src/personal_agent/telemetry/events.py` | Delete `CANONICAL_MODEL_CALL_STARTED_FIELDS`, `CANONICAL_MODEL_CALL_COMPLETED_FIELDS`. |
| `src/personal_agent/service/app.py` | Remove `RequestTimer(` construction at 411, 1984 and whatever passes it into `ctx.request_timer`. |
| `src/personal_agent/gateway/chat_api.py` | Remove `RequestTimer(` construction at 98. |
| `pyproject.toml` | Add explicit `opentelemetry-semantic-conventions>=0.65b0` dependency. |
| `tests/personal_agent/llm_client/test_telemetry_parity.py` | Re-point from `CANONICAL_MODEL_CALL_*_FIELDS` assertions to span-attribute conformance (gen_ai.* + namespaced attrs present, no legacy `latency_ms` in payload). |
| `tests/test_telemetry/test_request_timer.py` | **Deleted** (tests the retired class). |
| `tests/observability/route_trace/test_assembler.py` | Update/remove `RequestTimer` usage at line 41 — check what it asserts before deciding delete vs. rewrite. |
| `tests/personal_agent/orchestrator/` (new or existing file) | New integration test(s) for AC-1 through AC-5, AC-9, AC-11: one exercised tool-using turn (≥2 tool calls) under `InMemorySpanExporter`, asserting shape, parent/child timing, tool-span-count parity with retained log records, no legacy duration fields, no `tool_execution_completed` record. |
| `tests/personal_agent/llm_client/` (new or existing file) | New test(s) for AC-6, AC-13, AC-14: gen_ai attribute correctness, semconv version assertion, `traceparent` header well-formedness + trace-id match + legacy headers retained. |

## Explicitly out of scope (per ticket text, confirmed against code)

- Background entrypoint root spans (scheduler, monitors) — FRE-1069 (B4).
- OTel Collector export — FRE-1070 (B5). No processor/exporter is attached to the `TracerProvider` today;
  this ticket does not add one. All new tests use their own `InMemorySpanExporter`, never the process
  global.
- `slm_server`-side OTLP export — FRE-1071 (B6, separate repo). This ticket only injects the outbound
  `traceparent` header from this repo's side (AC-14); it does not touch `slm_server`.
- The seam ticket's population-level, cross-store, 7-day-window acceptance criteria (AC-1 through AC-10 of
  the **ADR**, not of this ticket) — those belong to FRE-1073 per ADR-0130 D1/D2. This ticket proves its
  own 14 ACs via one exercised turn under an in-memory exporter, nothing population-level.

## Atomic implementation steps

1. `telemetry/spans.py` — write the module (tracer, contextvar, `open_step_span`/`close_step_span`,
   `model_call_span`/`tool_call_span`, `namespaced()`, `SEMCONV_VERSION`). No callers yet.
   Verify: `uv run python -c "from personal_agent.telemetry import spans"` imports clean.
2. `pyproject.toml` — add `opentelemetry-semantic-conventions` dependency; `uv sync`.
   Verify: `uv run python -c "from opentelemetry.semconv._incubating.attributes import gen_ai_attributes"`.
3. Write failing tests first (TDD) for the span-tree shape (AC-1, AC-2, AC-4) against a minimal harness
   that calls `open_step_span`/`close_step_span`/`model_call_span`/`tool_call_span` directly (not yet
   wired into the orchestrator) — confirms the primitives produce the right parent/child shape before
   wiring them into 5+ call sites.
   Verify: `make test-file FILE=tests/test_telemetry/test_spans.py` — new tests fail (functions exist,
   shape not yet proven wrong, but nothing calls them in production code yet — this step is really "prove
   the primitive is correct in isolation").
4. Wire `open_step_span`/close paths into `orchestrator/executor.py`'s driver loop + `step_llm_call` +
   `step_tool_execution`; remove `tool_execution_completed` emit; remove `llm_span_name`/`tool_span_name`
   `RequestTimer` calls.
   Verify: `make test-file FILE=tests/personal_agent/orchestrator/test_executor.py` (existing suite —
   must still pass; this is the regression net for the FSM change).
5. Retire the remaining `RequestTimer` phases in `executor.py` (`session_history_load`, `context_window`,
   `memory_query`, `synthesis`, `session_update`); delete `RequestTimer` construction in `app.py`,
   `chat_api.py`; delete `telemetry/request_timer.py`; remove `ExecutionContext.request_timer` field;
   delete `tests/test_telemetry/test_request_timer.py`; update/remove
   `tests/observability/route_trace/test_assembler.py:41`.
   Verify: `ast-grep run -p 'RequestTimer' -l py src/ tests/` returns zero; `ast-grep run -p '$X.start_span($$$)' -l py src/` and `-p '$X.end_span($$$)'` both return zero (AC-10's own census, run before writing the AC-10 test itself).
6. Wire `tool_call_span` into `tools/executor.py`; drop `latency_ms` from `TOOL_CALL_COMPLETED`/`FAILED`.
   Verify: `make test-file FILE=tests/personal_agent/tools/test_executor.py`.
7. Wire `model_call_span` into `client.py::_do_request` and `litellm_client.py::respond`; set
   `gen_ai.usage.*`; drop `latency_ms` parameter from `emit_model_call_completed`; delete
   `CANONICAL_MODEL_CALL_*_FIELDS` from `events.py`; re-point `test_telemetry_parity.py`.
   Verify: `make test-file FILE=tests/personal_agent/llm_client/test_telemetry_parity.py`.
8. AC-14 — inject `traceparent` in `client.py:432` alongside `X-Trace-Id`/`X-Span-Id`; confirm/configure
   the global W3C propagator in `otel_bootstrap.py` if not already set.
   Verify: new test in `tests/personal_agent/llm_client/` asserting header presence, well-formedness,
   trace-id match, and legacy headers still present (stub the httpx transport, don't hit the network).
9. Write the integration test(s) for AC-1 through AC-5, AC-9, AC-11 — one exercised turn, ≥2 tool calls,
   under `InMemorySpanExporter`, following the `test_trace_otel_bridge.py` fixture shape.
   Verify: `make test-file FILE=tests/personal_agent/orchestrator/test_span_tree.py` (or wherever this
   lands) — all pass, and manually inspect the exported spans once to sanity-check shape before trusting
   the assertions.
10. AC-13 test (semconv version) — `make test-file FILE=tests/test_telemetry/test_spans.py`.
11. AC-8 check — grep/ast-grep census confirming every attribute key set in this ticket's diff is either
    a `gen_ai.*` semconv name or starts with `personal_agent.`.
12. Full suite: `make test` (module-scoped first, then full — see quality gates).

## Test commands (exact)

```
make test-file FILE=tests/test_telemetry/test_spans.py
make test-file FILE=tests/personal_agent/orchestrator/test_executor.py
make test-file FILE=tests/personal_agent/orchestrator/test_span_tree.py
make test-file FILE=tests/personal_agent/tools/test_executor.py
make test-file FILE=tests/personal_agent/llm_client/test_telemetry_parity.py
make test-file FILE=tests/personal_agent/llm_client/test_traceparent_injection.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Risks flagged for codex review

1. **RequestTimer full retirement vs. ADR's named-file-list scope** — this plan reads AC-10 literally
   (zero callers anywhere) and therefore retires five `RequestTimer` phases the ADR's Implementation
   Notes never names. This is the largest scope judgment call in the plan; codex should sanity-check
   whether reading AC-10 non-literally (scoped only to the phases ADR-0129 names) is more defensible,
   given that a Standard/Complex ticket bounces at the gate on undisclosed scope creep just as readily as
   it does on an unmet criterion.
2. **`asyncio.gather`-parented tool spans** — relies on Python contextvars copying at task-creation time
   inside `asyncio.gather`. This is standard asyncio behavior but has not been verified against this
   codebase's actual dispatch mechanism (`dispatch_tool_call` via `asyncio.gather`,
   `executor.py:5442-5468`) — must be proven by a real test with ≥2 concurrent tool calls, not assumed.
3. **AC-13's literal mechanism** — plan guesses "a recorded `SEMCONV_VERSION` constant, diffed against
   installed" as the intended shape; this is inferred, not quoted from an unambiguous AC-13 sentence.
4. **W3C propagator configuration** — `otel_bootstrap.py`'s `configure_tracing()` does not set a global
   text-map propagator today; the OTel SDK's out-of-box default *should* be `tracecontext`, but this must
   be verified (not assumed) before relying on it for AC-14, and set explicitly if it's the SDK's default
   changes across versions or was already overridden by another dependency's side-effecting import.
