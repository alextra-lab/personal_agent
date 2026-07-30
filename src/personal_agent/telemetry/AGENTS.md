# Telemetry

Observability infrastructure with structured logging and tracing.

**Spec**: `../../docs/architecture/system_architecture_v0.1.md` Section 5

## Responsibilities

- Structured logging (JSON lines format)
- Trace context propagation (trace_id, span_id, session_id)
- Log queries for metrics
- Event schema definitions

## Structure

```
telemetry/
├── __init__.py      # Exports: get_logger, TraceContext, TelemetryQueries
├── logger.py        # structlog configuration
├── trace.py         # TraceContext dataclass
├── events.py        # Event name constants
├── metrics.py       # Log query utilities (Phase 2)
└── queries.py       # ES analytics for threshold tuning (Phase 2.3, FRE-11)
```

## Get Logger

```python
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
```

## Structured Logging

```python
log.info(
    "tool_executed",       # Use events.TOOL_EXECUTED constant
    tool=name,
    success=True,
    duration_ms=123,
    trace_id=ctx.trace_id,
    session_id=ctx.session_id,
)
```

**Never** use string interpolation: `log.info(f"Tool {name} executed")`

## Trace Context

```python
from personal_agent.telemetry import TraceContext
from datetime import datetime, timezone

ctx = TraceContext(
    trace_id="trace-abc-123",
    span_id="span-def-456",
    session_id="session-xyz-789",
    timestamp=datetime.now(timezone.utc),
)

result = await execute_tool(name, args, ctx)  # Pass through chains
```

Frozen dataclass - **never** modify: `ctx.trace_id = "new"` will error.

## Event Constants

```python
# telemetry/events.py
TOOL_EXECUTED = "tool_executed"
MODE_TRANSITION = "mode_transition"
LLM_REQUEST = "llm_request"

# Use in code
from personal_agent.telemetry.events import TOOL_EXECUTED
log.info(TOOL_EXECUTED, tool=name, success=True)
```

## Log Levels

- `debug`: Verbose diagnostics
- `info`: Normal operations
- `warning`: Unexpected but recoverable
- `error`: Errors requiring attention
- `critical`: System stability threatened

## Search

```bash
rg -n "log\.(debug|info|warning|error|critical)" src/
rg -n "TraceContext" src/
rg -n "^[A-Z_]+ = \"" src/personal_agent/telemetry/events.py
```

## Critical

- Always pass TraceContext - **never** create new trace IDs mid-chain
- Use UTC timestamps: `datetime.now(timezone.utc)`
- **Never log PII/secrets** - redact before logging
- Use constants for events, not magic strings

## Delivery is not guaranteed — check it, don't assume it (FRE-1051)

ADR-0090's three corners (emit · mapping · dashboard) all presuppose that the event
*arrived*. None checks delivery, so **a zero from an Elasticsearch query has three
indistinguishable causes**: there is no such data, the field name is wrong, or the event
was emitted and lost. The third was not previously on that list.

**Measured, 2026-07-23..28:** the Postgres `api_costs` ledger held 1,303 rows while
`agent-logs-*` held 899 `api_cost_recorded` documents — 404 missing, with three of six
days losing 48–83% and three losing nothing.

**Why:** `add_elasticsearch_handler` is called from exactly one place —
`service/app.py`, inside the FastAPI lifespan, and only if `connect()` succeeds. So
**a process that does not attach the handler ships nothing to Elasticsearch**, silently.
That accounted for all 404 (a study harness and a captains-log backfill). The standalone
`gateway/app.py` builds a handler but never attaches it, so it has the same gap.

Before drawing a conclusion from an `agent-logs-*` count, run the delivery probe:

```bash
uv run python -m scripts.monitors.delivery_ratio_monitor --since 2026-07-23 --until 2026-07-28
uv run python -m scripts.monitors.delivery_ratio_monitor --json   # machine-readable
```

Exit codes are deliberately distinct, because "the probe is broken" and "delivery is
broken" need different triage: `0` passed · `1` breach **or** nothing verifiable · `2`
argparse rejected the arguments · `64` precondition failed · `70` could not measure
(substrate unreachable). An all-`UNVERIFIABLE` window exits non-zero — it proved nothing,
and silence must not read as green.

The probe attributes a zero rather than assuming it means loss. A family whose queried
field is missing from the mapping reports `FIELD-ABSENT`, not `0.0% delivered`: the query
is broken, so blaming the pipeline would send you to the wrong system. It still alarms.

**Two further hazards in `es_handler.emit()`, real but not the cause of the above:**

- **Off-loop emission is dropped.** `emit()` only ships when `loop.is_running()` in the
  *calling thread*, so anything under `asyncio.to_thread` is silently skipped. Emit such
  events from the main loop (see `reflection_dspy.emit_missing_skill_warnings`).
- **No drain on shutdown.** `disconnect()` closes the client without awaiting in-flight
  writes, and `create_task` results are discarded, so a task may be collected mid-flight.

## Elasticsearch Analytics (Phase 2.3)

`TelemetryQueries` provides async ES queries for adaptive threshold tuning:

```python
from personal_agent.telemetry import TelemetryQueries

queries = TelemetryQueries(es_client=optional_client)
percentiles = await queries.get_resource_percentiles("cpu", days=7)
transitions = await queries.get_mode_transitions(days=7)
triggers = await queries.get_consolidation_triggers(days=7)
patterns = await queries.get_task_patterns(days=7)
```

Typed models: `ModeTransition`, `ConsolidationEvent`, `TaskPatternReport`. Requires `elasticsearch[async]` when creating a client (lazy import).

## Testing

- Test logger configuration (JSON output, fields present)
- Test TraceContext immutability
- Verify trace_id propagates through call chains
- Test TelemetryQueries with mocked ES (`tests/test_telemetry/test_queries.py`)

## Pre-PR

```bash
pytest tests/test_telemetry/ -v
mypy src/personal_agent/telemetry/
ruff check src/personal_agent/telemetry/
```
