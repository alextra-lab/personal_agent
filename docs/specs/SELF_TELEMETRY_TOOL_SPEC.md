# Self-Telemetry Tool Spec

**Date**: 2026-03-12
**Status**: Implemented
**Phase**: 2.3 Homeostasis & Feedback
**Related**: `telemetry/metrics.py`, `telemetry/queries.py`, `tools/system_health.py`, FRE-53

---

## Problem

The agent has no way to introspect its own execution history at runtime. Three
telemetry query capabilities exist in `telemetry/metrics.py`:

| Function | What it does |
|----------|-------------|
| `query_events(event, window_str, component, limit)` | Filter events by name, time window, component |
| `get_trace_events(trace_id)` | Reconstruct all events for a single trace |
| `get_request_latency_breakdown(trace_id)` | Phase-by-phase timing for a request |

These are callable from Python but the agent has no tool-call access to them.
Today the only consumer is:

- **Captain's Log reflection**: calls `get_trace_events()` once, immediately
  after each request, for the current trace only.
- **`ui/cli.py`**: human-facing Rich tables (now telemetry-only after FRE-53).

The agent cannot:

- Query across traces ("show me my last 10 failures").
- Analyze latency trends ("am I getting slower?").
- Correlate events across time windows ("how often do tool calls fail?").
- Inspect its own trace breakdown proactively.

## Proposal

Register a single tool, `self_telemetry_query`, with a `query_type` parameter
that dispatches to the three existing functions. No new query logic is needed;
the tool is a thin adapter between the tool-call interface and
`telemetry/metrics.py`.

### Why one tool, not three

The LLM sees tool descriptions in the system prompt. One tool with a clear
`query_type` enum keeps the tool list short. Three separate tools would add
noise for capabilities the agent uses occasionally, not on every request.

---

## Design

### Tool definition

```python
# tools/self_telemetry.py

self_telemetry_query_tool = ToolDefinition(
    name="self_telemetry_query",
    description=(
        "Query this agent's own telemetry. Use to inspect execution traces, "
        "find errors, analyze latency, or review recent events. "
        "query_type: 'events' (filter by event/component/time), "
        "'trace' (reconstruct one trace), "
        "'latency' (phase-by-phase breakdown for one trace)."
    ),
    category="read_only",
    parameters=[
        ToolParameter(name="query_type", type="string",
                      description="One of: events, trace, latency", required=True),
        ToolParameter(name="trace_id", type="string",
                      description="Trace ID (required for trace and latency)", required=False),
        ToolParameter(name="event", type="string",
                      description="Event name filter (for events query)", required=False),
        ToolParameter(name="window", type="string",
                      description="Time window e.g. '1h', '30m', '2d' (for events query)",
                      required=False),
        ToolParameter(name="component", type="string",
                      description="Component filter (for events query)", required=False),
        ToolParameter(name="limit", type="number",
                      description="Max results (for events query, default 20)", required=False),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=None,
)
```

### Executor function

```python
def self_telemetry_query_executor(
    query_type: str,
    trace_id: str | None = None,
    event: str | None = None,
    window: str | None = None,
    component: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Execute a self-telemetry query.

    Args:
        query_type: One of 'events', 'trace', 'latency'.
        trace_id: Required for 'trace' and 'latency' queries.
        event: Event name filter (for 'events').
        window: Time window string (for 'events').
        component: Component filter (for 'events').
        limit: Max results (for 'events', default 20).

    Returns:
        {success: bool, output: list[dict] | str, error: str | None}
    """
```

Dispatch logic:

| `query_type` | Calls | Required params | Output |
|-------------|-------|-----------------|--------|
| `events` | `query_events(event, window, component, limit or 20)` | none (all optional) | List of event dicts |
| `trace` | `get_trace_events(trace_id)` | `trace_id` | List of event dicts (chronological) |
| `latency` | `get_request_latency_breakdown(trace_id)` | `trace_id` | List of phase dicts with `duration_ms` |

On missing `trace_id` for `trace`/`latency`, return `{success: False, error: "trace_id required"}`.

Output is JSON-serializable dicts (same as the underlying functions return).
No Rich formatting — the LLM interprets the raw data.

### Registration

In `tools/__init__.py`, add to `register_mvp_tools()`:

```python
from personal_agent.tools.self_telemetry import (
    self_telemetry_query_executor,
    self_telemetry_query_tool,
)

registry.register(self_telemetry_query_tool, self_telemetry_query_executor)
```

### Output size guard

`query_events` can return thousands of entries. The executor should:

1. Default `limit` to 20 if not provided.
2. Cap output to 50 entries max regardless of `limit` parameter.
3. If truncated, include a summary item: `{"truncated": true, "total_available": N}` while keeping total returned entries at 50.

This keeps tool-call responses within reasonable context window size.

---

## Impact on `ui/cli.py`

After this tool is registered, the telemetry CLI becomes redundant for the
agent. It remains useful for direct human debugging but is no longer the
primary way to query telemetry.

**No changes to `cli.py` in this issue.** It stays as a human convenience.
A follow-up could deprecate and remove it if the service-side approach
(Kibana, Elasticsearch) covers human needs too.

---

## Files to create/modify

| File | Action |
|------|--------|
| `src/personal_agent/tools/self_telemetry.py` | **Create** — tool def + executor |
| `src/personal_agent/tools/__init__.py` | **Modify** — import and register |
| `tests/test_tools/test_self_telemetry.py` | **Create** — unit tests |

---

## Acceptance criteria

- [x] `self_telemetry_query` appears in `registry.list_tools()`.
- [x] `query_type=events` with `event=model_call_completed`, `window=1h` returns matching entries.
- [x] `query_type=trace` with a valid `trace_id` returns chronological events.
- [x] `query_type=latency` with a valid `trace_id` returns phase breakdown with `duration_ms`.
- [x] Missing `trace_id` on `trace`/`latency` returns `{success: False, error: ...}`.
- [x] Output is capped at 50 entries; `truncated` flag set when applicable.
- [x] Invalid `query_type` returns `{success: False, error: ...}`.
- [x] Tool is `read_only`, `risk_level=low`, allowed in all modes.
- [x] Unit tests pass with synthetic JSONL data (no live telemetry required).

---

## Use cases (how the agent would call this)

**During conversation** — user asks "Why was your last response slow?":

```json
{"name": "self_telemetry_query", "arguments": {
  "query_type": "latency",
  "trace_id": "<last_trace_id>"
}}
```

**During reflection** — Captain's Log checks for recent failures:

```json
{"name": "self_telemetry_query", "arguments": {
  "query_type": "events",
  "event": "captains_log_reflection_failed",
  "window": "1h"
}}
```

**Proactive self-check** — agent notices an error and investigates:

```json
{"name": "self_telemetry_query", "arguments": {
  "query_type": "trace",
  "trace_id": "<failing_trace_id>"
}}
```

---

## Non-goals

- Elasticsearch queries (`TelemetryQueries`) — different data source, different use case (threshold tuning). Can be added as a separate tool later.
- Writing or modifying telemetry data.
- Aggregation or statistical analysis (the LLM can do that from raw events).
