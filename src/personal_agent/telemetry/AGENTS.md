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
