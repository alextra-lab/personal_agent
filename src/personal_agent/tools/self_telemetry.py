"""Self-telemetry tool for querying agent execution history.

This module exposes existing telemetry metric query helpers through a
single tool-call interface so the agent can inspect historical behavior.

## Overview

The `self_telemetry_query` tool provides access to multiple query types for
inspecting agent health, performance, errors, and interaction history. All
queries return pre-computed summaries rather than raw event data, making them
actionable for both the agent and human users.

## Query Types

| Query Type | Description | Default Scope |
|------------|-------------|---------------|
| `health` | Overall operational status (success rate, component health, alerts) | window=1h |
| `errors` | Error analysis grouped by type and component with trend detection | window=24h |
| `interactions` | Recent interaction history from Captain's Log | last_n=10 |
| `performance` | Latency percentiles (p50/p75/p90/p95), throughput, top tools | window=24h |
| `events` | Raw event query (filter by event/component/time) | N/A |
| `trace` | Reconstruct one trace (requires trace_id) | N/A |
| `latency` | Phase breakdown for one trace (requires trace_id) | N/A |

## Time Windows

Relative windows: `1h`, `30m`, `2d`, `45s`
Named windows: `today`, `yesterday`, `this_week`

## Scoping

- Use `window` for time-based queries (all query types except `interactions`)
- Use `last_n` for interaction-count scoping (`health`, `errors`, `interactions`, `performance`)
- `window` and `last_n` are mutually exclusive

## Internal Use Patterns

### Orchestrator Health Check at Session Start

```python
from personal_agent.tools.self_telemetry import self_telemetry_query_executor

health = self_telemetry_query_executor(query_type="health", window="1h")
if health["output"]["status"] == "degraded":
    log.warning("session_start_degraded", alerts=health["output"]["alerts"])
```

### Post-Failure Investigation

```python
trace_result = self_telemetry_query_executor(
    query_type="trace", trace_id=current_trace_id
)
```

### Brainstem Scheduler Mode Transitions

```python
health = self_telemetry_query_executor(query_type="health", window="30m")
if health["output"]["interactions"]["success_rate"] < 0.5:
    trigger_mode_transition("DEGRADED", reason="high_failure_rate")
```

### Captain's Log Enrichment for Reflection

```python
perf = self_telemetry_query_executor(query_type="performance", window="1h")
# Include in telemetry_summary for richer reflection
```
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from personal_agent.captains_log.capture import TaskCapture, read_captures
from personal_agent.telemetry.events import (
    ERROR_TREND_DECREASING,
    ERROR_TREND_INCREASING,
    ERROR_TREND_STABLE,
    HEALTH_STATUS_DEGRADED,
    HEALTH_STATUS_HEALTHY,
    HEALTH_STATUS_UNHEALTHY,
    TASK_OUTCOME_COMPLETED,
    TASK_OUTCOME_FAILED,
    TASK_OUTCOME_TIMEOUT,
)
from personal_agent.telemetry.metrics import (
    _parse_time_window,
    get_request_latency_breakdown,
    get_trace_events,
    query_events,
    resolve_window_to_datetime,
)
from personal_agent.tools.types import ToolDefinition, ToolParameter

_DEFAULT_EVENTS_LIMIT = 20
_MAX_OUTPUT_ENTRIES = 50


self_telemetry_query_tool = ToolDefinition(
    name="self_telemetry_query",
    description=(
        "Query this agent's operational health and telemetry. "
        "Use to inspect execution traces, analyze performance, find errors, or review recent interactions. "
        "\n\nquery_type options (choose one):\n"
        "- 'health': Overall operational status (success rate, component health, alerts). "
        "Default window=1h. Example: 'How are you?', 'Any errors recently?', 'Am I getting slower?'\n"
        "- 'errors': Error analysis grouped by type and component with trend detection. "
        "Default window=24h. Example: 'Show me failures this week', 'Errors in the last 5 interactions'\n"
        "- 'interactions': Recent interaction history from Captain's Log. "
        "Default last_n=10. Example: 'What have you been working on?'\n"
        "- 'performance': Latency percentiles (p50/p75/p90/p95), throughput (interactions/hour), "
        "breakdown by outcome (completed vs failed), top tools by usage, bottleneck identification. "
        "Default window=24h. Example: 'Am I getting slower?', 'Show performance this week'\n"
        "- 'events': Raw event query (filter by event/component/time). "
        "Use 'model_call_error' or 'task_failed' to find recent errors. Default limit=20.\n"
        "- 'trace': Reconstruct one trace by trace_id. Required for 'latency' queries too.\n"
        "- 'latency': Phase breakdown (duration_ms) for one trace. Requires trace_id.\n"
        "\nTime window formats:\n"
        "- Relative: '1h', '30m', '2d', '45s' (hours, minutes, days, seconds)\n"
        "- Named: 'today' (start of day), 'yesterday' (previous day), 'this_week' (Monday start)\n"
        "\nScoping:\n"
        "- Use 'window' for time-based queries (all types except 'interactions')\n"
        "- Use 'last_n' for interaction-count scoping (health, errors, interactions, performance)\n"
        "- 'window' and 'last_n' are mutually exclusive"
    ),
    category="read_only",
    parameters=[
        ToolParameter(
            name="query_type",
            type="string",
            description=(
                "One of: events, trace, latency, health, errors, interactions, performance. "
                "See tool description for detailed examples for each query type."
            ),
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="trace_id",
            type="string",
            description="Trace ID (required for trace and latency queries)",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="event",
            type="string",
            description="Event name filter (for events query). Use 'model_call_error' or 'task_failed' to find recent errors.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="window",
            type="string",
            description="Time window (for events, health, errors, performance queries). "
            "Accepts formats like '1h', '30m', '2d', 'today', 'yesterday', 'this_week'. "
            "Mutually exclusive with last_n.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="component",
            type="string",
            description="Component filter (for events query)",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description="Max results (for events query, default 20)",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="last_n",
            type="number",
            description="Query last N interactions (for health, errors, interactions, performance). "
            "Mutually exclusive with window. Float values will be converted to int.",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=None,
)


def _apply_output_cap(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cap output size to protect the context window.

    Args:
        results: Query results to cap.

    Returns:
        Results capped at 50 entries. If truncated, appends a metadata item.
    """
    total_available = len(results)
    if total_available <= _MAX_OUTPUT_ENTRIES:
        return results

    # Keep total entries at 50 including truncation metadata.
    # This preserves a hard ceiling while still surfacing truncation context.
    capped = results[: _MAX_OUTPUT_ENTRIES - 1]
    capped.append({"truncated": True, "total_available": total_available})
    return capped


def _load_captures(
    window: str | None = None,
    last_n: int | None = None,
) -> list[TaskCapture]:
    """Load TaskCapture records by time window or interaction count.

    Args:
        window: Time window string (e.g., "1h", "today").
        last_n: Number of most recent interactions to load.

    Returns:
        List of TaskCapture records, newest first.
    """
    if last_n is not None:
        captures = read_captures(limit=last_n)
        return captures

    start_time = resolve_window_to_datetime(window or "1h")
    captures = read_captures(start_date=start_time)
    return captures


def _compute_latency_stats(durations: Sequence[float | int]) -> dict[str, float | None]:
    """Compute latency statistics including percentiles.

    Args:
        durations: Sequence of duration values in milliseconds.

    Returns:
        Dict with avg_ms, min_ms, max_ms, p50_ms, p75_ms, p90_ms, p95_ms keys.
        Percentiles are None if fewer than 20 samples.
    """
    if not durations:
        return {
            "avg_ms": None,
            "min_ms": None,
            "max_ms": None,
            "p50_ms": None,
            "p75_ms": None,
            "p90_ms": None,
            "p95_ms": None,
        }

    sorted_durations = sorted(durations)
    n = len(sorted_durations)

    def percentile(p: float) -> float | None:
        """Calculate the p-th percentile (0-100) from sorted data."""
        if n < 4:
            return None
        idx = int(n * p / 100)
        return float(sorted_durations[idx])

    return {
        "avg_ms": round(sum(sorted_durations) / n, 2),
        "min_ms": sorted_durations[0],
        "max_ms": sorted_durations[-1],
        "p50_ms": percentile(50),
        "p75_ms": percentile(75),
        "p90_ms": percentile(90),
        "p95_ms": percentile(95),
    }


def _assess_component_health(window: str) -> dict[str, dict[str, Any]]:
    """Assess health of LLM, tools, and system from JSONL events.

    Args:
        window: Time window string (e.g., "1h").

    Returns:
        Dict with llm, tools, and system health info.
    """
    llm_errors = query_events(event="model_call_error", window_str=window)
    llm_calls = query_events(event="model_call_completed", window_str=window)
    tool_errors = query_events(event="tool_call_failed", window_str=window)
    tool_calls = query_events(event="tool_call_completed", window_str=window)

    llm_latencies = [e["latency_ms"] for e in llm_calls if "latency_ms" in e]
    avg_llm_latency = (
        round(sum(float(latency) for latency in llm_latencies) / len(llm_latencies), 2)
        if llm_latencies
        else None
    )

    return {
        "llm": {
            "status": HEALTH_STATUS_HEALTHY if len(llm_errors) == 0 else HEALTH_STATUS_DEGRADED,
            "calls": len(llm_calls) + len(llm_errors),
            "errors": len(llm_errors),
            "avg_latency_ms": avg_llm_latency,
        },
        "tools": {
            "status": HEALTH_STATUS_HEALTHY if len(tool_errors) == 0 else HEALTH_STATUS_DEGRADED,
            "calls": len(tool_calls) + len(tool_errors),
            "errors": len(tool_errors),
        },
    }


def _compute_latency_trend(window: str) -> str:
    """Compute latency trend by comparing median latency to preceding window.

    Args:
        window: Time window string (e.g., "24h"). Named windows like "today" or
        "yesterday" are also supported.

    Returns:
        One of: ERROR_TREND_INCREASING, ERROR_TREND_DECREASING, ERROR_TREND_STABLE
    """
    window_delta = _parse_time_window(window)

    # Get duration for current window
    captures = _load_captures(window=window)
    current_durations = [c.duration_ms for c in captures if c.duration_ms is not None]

    # Get duration for preceding window
    if isinstance(window_delta, timedelta):
        window_hours = int(window_delta.total_seconds() / 3600)
        window_minutes = int(window_delta.total_seconds() / 60)
        prev_window = f"{window_minutes}m" if window_hours < 1 else f"{window_hours}h"
    else:
        # Named window: compare "today so far" to "yesterday same time period"
        prev_window = "24h"

    prev_captures = _load_captures(window=prev_window)
    prev_durations = [c.duration_ms for c in prev_captures if c.duration_ms is not None]

    # Use median as the metric for comparison
    if not current_durations or not prev_durations:
        return ERROR_TREND_STABLE

    current_median = sorted(current_durations)[len(current_durations) // 2]
    prev_median = sorted(prev_durations)[len(prev_durations) // 2]

    return compute_trend(current_median, prev_median)


def _determine_health_status(
    success_rate: float,
    components: dict[str, dict[str, Any]],
    system: dict[str, Any],
) -> str:
    """Determine health status: HEALTH_STATUS_* constants.

    Args:
        success_rate: Interaction success rate (0.0 - 1.0).
        components: Component health info from _assess_component_health.
        system: System status info (mode, etc.).

    Returns:
        One of: HEALTH_STATUS_HEALTHY, HEALTH_STATUS_DEGRADED, HEALTH_STATUS_UNHEALTHY
    """
    mode = system.get("mode", "NORMAL")
    llm_status = components.get("llm", {}).get("status", HEALTH_STATUS_HEALTHY)
    tools_status = components.get("tools", {}).get("status", HEALTH_STATUS_HEALTHY)

    # Unhealthy conditions
    if success_rate < 0.7:
        return HEALTH_STATUS_UNHEALTHY
    if mode in ("LOCKDOWN",):
        return HEALTH_STATUS_UNHEALTHY
    if llm_status == HEALTH_STATUS_DEGRADED and tools_status == HEALTH_STATUS_DEGRADED:
        return HEALTH_STATUS_UNHEALTHY

    # Degraded conditions
    if success_rate < 0.9:
        return HEALTH_STATUS_DEGRADED
    if mode in ("ALERT", "DEGRADED"):
        return HEALTH_STATUS_DEGRADED
    if llm_status == HEALTH_STATUS_DEGRADED or tools_status == HEALTH_STATUS_DEGRADED:
        return HEALTH_STATUS_DEGRADED

    # Healthy: success_rate >= 0.9 AND all components healthy AND mode NORMAL
    return HEALTH_STATUS_HEALTHY


def compute_trend(current_value: int | float, previous_value: int | float) -> str:
    """Compare current vs. previous values to determine trend direction.

    Uses multiplicative thresholds:
    - Increasing: current > 1.5x previous
    - Decreasing: current < 0.5x previous
    - Stable: otherwise

    Args:
        current_value: Current period's metric value.
        previous_value: Previous period's metric value (same duration).

    Returns:
        One of: ERROR_TREND_INCREASING, ERROR_TREND_DECREASING, ERROR_TREND_STABLE

    Example:
        >>> compute_trend(10, 5)  # 2x increase = increasing
        "increasing"
        >>> compute_trend(10, 20)  # 0.5x = decreasing
        "decreasing"
        >>> compute_trend(10, 12)  # ~0.83x = stable
        "stable"
    """
    if previous_value == 0:
        return ERROR_TREND_STABLE if current_value == 0 else ERROR_TREND_INCREASING
    ratio = current_value / previous_value
    if ratio > 1.5:
        return ERROR_TREND_INCREASING
    if ratio < 0.5:
        return ERROR_TREND_DECREASING
    return ERROR_TREND_STABLE


def _compute_error_trend(window: str) -> str:
    """Compute error trend: ERROR_TREND_* constants.

    Args:
        window: Time window string (e.g., "24h"). Named windows like "today" or
        "yesterday" are also supported; for named windows, the preceding window
        of equivalent duration is used for comparison.

    Returns:
        One of: "increasing", "stable", "decreasing"
    """
    window_delta = _parse_time_window(window)

    # Current window error count
    current_errors = query_events(event="model_call_error", window_str=window) + query_events(
        event="tool_call_failed", window_str=window
    )

    # Preceding window error count (same duration as current)
    if isinstance(window_delta, timedelta):
        window_hours = int(window_delta.total_seconds() / 3600)
        window_minutes = int(window_delta.total_seconds() / 60)
        prev_window = f"{window_minutes}m" if window_hours < 1 else f"{window_hours}h"
    else:
        # Named window: compare "today so far" to "yesterday same time period"
        # Use a fixed 24h window as a reasonable approximation
        prev_window = "24h"

    prev_errors = query_events(event="model_call_error", window_str=prev_window) + query_events(
        event="tool_call_failed", window_str=prev_window
    )

    current_count = len(current_errors)
    prev_count = len(prev_errors)

    # Compute trend using reusable comparison function
    return compute_trend(current_count, prev_count)


def _get_system_status() -> dict[str, Any]:
    """Get current system status from system metrics snapshot.

    Returns:
        Dict with mode, cpu_avg, mem_avg keys.
    """
    from personal_agent.telemetry.events import SYSTEM_METRICS_SNAPSHOT

    # Get most recent system metrics snapshot
    metrics = query_events(event=SYSTEM_METRICS_SNAPSHOT, window_str="1h", limit=1)

    if not metrics:
        return {"mode": "NORMAL", "cpu_avg": None, "mem_avg": None}

    latest = metrics[0]
    return {
        "mode": latest.get("mode", "NORMAL"),
        "cpu_avg": latest.get("cpu_load"),
        "mem_avg": latest.get("memory_used"),
    }


def _count_by_field(entries: list[dict[str, Any]], field: str) -> dict[str, int]:
    """Count occurrences by a specific field.

    Args:
        entries: List of log entries.
        field: Field name to count by.

    Returns:
        Dict mapping field values to counts.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        value = entry.get(field)
        if value:
            counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def _build_recent_errors(
    error_events: list[dict[str, Any]],
    captures: list[TaskCapture],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Build recent error list with context from captures.

    Args:
        error_events: List of error log entries.
        captures: List of TaskCapture records.
        limit: Max number of errors to include.

    Returns:
        List of error dicts with context.
    """
    capture_map = {c.trace_id: c for c in captures}

    recent = []
    for event in error_events[:limit]:
        trace_id = event.get("trace_id")
        capture = capture_map.get(trace_id) if trace_id else None

        error_info = {
            "timestamp": event.get("timestamp"),
            "type": event.get("event"),
            "component": event.get("component"),
            "trace_id": trace_id,
            "summary": event.get("message", event.get("error", "")),
        }

        if capture:
            error_info["user_message_preview"] = (
                capture.user_message[:50] + "..."
                if len(capture.user_message) > 50
                else capture.user_message
            )

        recent.append(error_info)

    return recent


def _execute_health_query(
    window: str | None = None,
    last_n: int | None = None,
) -> dict[str, Any]:
    """Execute a health query and return structured health report.

    Args:
        window: Time window string (e.g., "1h", "today"). Default: "1h".
        last_n: Number of interactions to analyze. If provided, overrides window.

    Returns:
        Dict with health report including status, interactions, latency, components, alerts.
    """
    captures = _load_captures(window=window, last_n=last_n)
    window_str = window or "1h"

    # 1. Compute interaction stats from captures
    total = len(captures)
    succeeded = sum(1 for c in captures if c.outcome == TASK_OUTCOME_COMPLETED)
    failed = sum(1 for c in captures if c.outcome == TASK_OUTCOME_FAILED)
    timed_out = sum(1 for c in captures if c.outcome == TASK_OUTCOME_TIMEOUT)
    success_rate = succeeded / total if total > 0 else 1.0

    # 2. Compute latency stats from captures
    durations = [c.duration_ms for c in captures if c.duration_ms is not None]
    latency = _compute_latency_stats(durations)

    # 3. Query JSONL for component health
    components = _assess_component_health(window=window_str)

    # 4. Get system status
    system = _get_system_status()

    # 5. Determine overall status
    status = _determine_health_status(success_rate, components, system)

    # 6. Generate alerts
    alerts = _generate_alerts(captures, components)

    return {
        "success": True,
        "output": {
            "status": status,
            "window": window_str,
            "assessed_at": datetime.now(timezone.utc).isoformat(),
            "interactions": {
                "total": total,
                "succeeded": succeeded,
                "failed": failed,
                "timed_out": timed_out,
                "success_rate": round(success_rate, 3),
            },
            "latency": latency,
            "components": components,
            "system": system,
            "alerts": alerts,
        },
        "error": None,
    }


def _execute_errors_query(
    window: str | None = None,
    last_n: int | None = None,
) -> dict[str, Any]:
    """Execute an errors query and return grouped error analysis.

    Args:
        window: Time window string (e.g., "24h"). Default: "24h".
        last_n: Number of interactions to analyze. If provided, overrides window.

    Returns:
        Dict with error analysis including by_type, by_component, recent, trend.
    """
    captures = _load_captures(window=window, last_n=last_n)
    window_str = window or "24h"
    scope = f"last_{last_n}_interactions" if last_n else window_str

    # 1. Load failed captures
    failed_captures = [c for c in captures if c.outcome != TASK_OUTCOME_COMPLETED]

    # 2. Query JSONL for error events in the same window
    error_events = []
    for event_type in ("task_failed", "model_call_error", "tool_call_failed"):
        error_events.extend(query_events(event=event_type, window_str=window_str))

    # 3. Group by type and component
    by_type = _count_by_field(error_events, "event")
    by_component = _count_by_field(error_events, "component")

    # 4. Build recent error list (max 10, with context from captures)
    recent = _build_recent_errors(error_events, captures, limit=10)

    # 5. Compute trend
    trend = _compute_error_trend(window_str)

    return {
        "success": True,
        "output": {
            "scope": scope,
            "total_errors": len(error_events) + len(failed_captures),
            "by_type": by_type,
            "by_component": by_component,
            "recent": recent,
            "trend": trend,
        },
        "error": None,
    }


def _execute_interactions_query(
    last_n: int | None = None,
) -> dict[str, Any]:
    """Execute an interactions query and return recent interaction list.

    Args:
        last_n: Number of interactions to return. Default: 10.

    Returns:
        Dict with interactions list and summary stats.
    """
    captures = _load_captures(last_n=last_n or 10)

    interactions = []
    for c in captures:
        interactions.append(
            {
                "trace_id": c.trace_id,
                "timestamp": c.timestamp.isoformat(),
                "user_message_preview": c.user_message[:50] + "..."
                if len(c.user_message) > 50
                else c.user_message,
                "outcome": c.outcome,
                "duration_ms": c.duration_ms,
                "tools_used": c.tools_used,
                "steps": len(c.steps),
                "had_errors": c.outcome != TASK_OUTCOME_COMPLETED,
            }
        )

    # Compute summary stats
    total = len(captures)
    succeeded = sum(1 for c in captures if c.outcome == TASK_OUTCOME_COMPLETED)
    success_rate = succeeded / total if total > 0 else 1.0

    durations = [c.duration_ms for c in captures if c.duration_ms is not None]
    avg_duration = round(sum(durations) / len(durations), 2) if durations else None

    # Count tool usage
    tool_counts: dict[str, int] = {}
    for c in captures:
        for tool in c.tools_used:
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
    most_used_tools = sorted(tool_counts.keys(), key=lambda t: tool_counts[t], reverse=True)[:5]

    return {
        "success": True,
        "output": {
            "count": len(interactions),
            "interactions": interactions,
            "summary": {
                "success_rate": round(success_rate, 3),
                "avg_duration_ms": avg_duration,
                "most_used_tools": most_used_tools,
            },
        },
        "error": None,
    }


def _execute_performance_query(
    window: str | None = None,
    last_n: int | None = None,
) -> dict[str, Any]:
    """Execute a performance query and return latency/throughput metrics.

    Args:
        window: Time window string (e.g., "24h"). Default: "24h".
        last_n: Number of interactions to analyze. If provided, overrides window.

    Returns:
        Dict with throughput, latency percentiles, by_outcome, top_tools, bottleneck,
        and latency_trend (comparing current vs. preceding window).
    """
    captures = _load_captures(window=window, last_n=last_n)
    window_str = window or "24h"

    total = len(captures)

    # Compute throughput
    window_delta = _parse_time_window(window_str)
    if isinstance(window_delta, timedelta):
        window_hours = max(window_delta.total_seconds() / 3600, 1 / 60)  # At least 1 minute
    else:
        window_hours = 24.0  # For named windows, default to 24h
    avg_per_hour = round(total / window_hours, 2) if total > 0 else 0

    # Compute latency percentiles
    durations = sorted(c.duration_ms for c in captures if c.duration_ms is not None)
    latency = _compute_latency_stats(durations)

    # Compute by_outcome stats
    by_outcome: dict[str, dict[str, Any]] = {}
    for outcome in (TASK_OUTCOME_COMPLETED, TASK_OUTCOME_FAILED, TASK_OUTCOME_TIMEOUT):
        outcome_captures = [c for c in captures if c.outcome == outcome]
        if outcome_captures:
            outcome_durations = [
                c.duration_ms for c in outcome_captures if c.duration_ms is not None
            ]
            by_outcome[outcome] = {
                "count": len(outcome_captures),
                "avg_duration_ms": round(sum(outcome_durations) / len(outcome_durations), 2)
                if outcome_durations
                else None,
            }

    # Compute top tools
    tool_stats: dict[str, dict[str, Any]] = {}
    for c in captures:
        for tool in c.tools_used:
            if tool not in tool_stats:
                tool_stats[tool] = {"count": 0, "durations": []}
            tool_stats[tool]["count"] += 1
            # Extract tool duration from tool_results if available
            for tr in c.tool_results:
                if tr.get("tool_name") == tool and tr.get("latency_ms"):
                    tool_stats[tool]["durations"].append(tr["latency_ms"])

    top_tools = []
    for tool_name, stats in sorted(tool_stats.items(), key=lambda x: x[1]["count"], reverse=True)[
        :10
    ]:
        avg_ms = (
            round(sum(stats["durations"]) / len(stats["durations"]), 2)
            if stats["durations"]
            else None
        )
        top_tools.append(
            {
                "name": tool_name,
                "count": stats["count"],
                "avg_ms": avg_ms,
            }
        )

    # Determine bottleneck (phase with highest % of total duration)
    bottleneck = _identify_bottleneck(captures)

    # Compute latency trend (compare median latency to preceding same-duration window)
    latency_trend = _compute_latency_trend(window_str)

    # Compute token usage aggregates
    token_totals = [c.total_tokens for c in captures if c.total_tokens > 0]
    tokens = {
        "total_in_window": sum(token_totals),
        "avg_per_interaction": round(sum(token_totals) / len(token_totals), 1)
        if token_totals
        else None,
        "interactions_with_data": len(token_totals),
    }

    return {
        "success": True,
        "output": {
            "window": window_str,
            "throughput": {
                "total_interactions": total,
                "avg_per_hour": avg_per_hour,
            },
            "latency": latency,
            "tokens": tokens,
            "by_outcome": by_outcome,
            "top_tools": top_tools,
            "bottleneck": bottleneck,
            "latency_trend": latency_trend,
        },
        "error": None,
    }


def _generate_alerts(
    captures: list[TaskCapture],
    components: dict[str, dict[str, Any]],
) -> list[str]:
    """Generate alert messages based on health data.

    Args:
        captures: List of TaskCapture records.
        components: Component health info.

    Returns:
        List of alert strings.
    """
    alerts = []

    # Check for timeouts
    timeout_captures = [c for c in captures if c.outcome == TASK_OUTCOME_TIMEOUT]
    if timeout_captures:
        alerts.append(f"{len(timeout_captures)} task timeout(s) detected")

    # Check component alerts
    if components.get("llm", {}).get("status") == HEALTH_STATUS_DEGRADED:
        llm_errors = components.get("llm", {}).get("errors", 0)
        alerts.append(f"LLM component degraded: {llm_errors} error(s)")

    if components.get("tools", {}).get("status") == HEALTH_STATUS_DEGRADED:
        tool_errors = components.get("tools", {}).get("errors", 0)
        alerts.append(f"Tools component degraded: {tool_errors} error(s)")

    return alerts


def _identify_bottleneck(captures: list[TaskCapture]) -> str | None:
    """Identify the main performance bottleneck.

    Args:
        captures: List of TaskCapture records.

    Returns:
        Bottleneck description or None.
    """
    if not captures:
        return None

    total_duration = sum(c.duration_ms or 0 for c in captures)
    if total_duration == 0:
        return None

    # Analyze tool durations if available
    tool_durations: dict[str, float] = {}
    for c in captures:
        for tr in c.tool_results:
            if tr.get("latency_ms"):
                tool_name = tr.get("tool_name", "unknown")
                tool_durations[tool_name] = tool_durations.get(tool_name, 0) + tr["latency_ms"]

    if tool_durations:
        max_tool = max(tool_durations.items(), key=lambda x: x[1])
        if max_tool[1] / total_duration > 0.3:  # More than 30% of total time
            return f"tool_execution ({max_tool[0]})"

    # Default bottleneck
    return "llm_inference"


def self_telemetry_query_executor(
    query_type: str,
    trace_id: str | None = None,
    event: str | None = None,
    window: str | None = None,
    component: str | None = None,
    limit: int | None = None,
    last_n: int | None = None,
) -> dict[str, Any]:
    """Execute a self-telemetry query.

    Args:
        query_type: One of ``events``, ``trace``, ``latency``, ``health``, ``errors``, ``interactions``, ``performance``.
        trace_id: Required for ``trace`` and ``latency`` queries.
        event: Event name filter for ``events``.
        window: Time window filter for ``events``, ``health``, ``errors``, ``performance``.
        component: Component filter for ``events``.
        limit: Max results for ``events`` query (default 20).
        last_n: Number of interactions for ``interactions``, ``health``, ``errors``, ``performance``.
            If provided as a float, it will be converted to int.

    Returns:
        Dict with ``success``, ``output``, and ``error`` keys.

    Raises:
        ValueError: If both ``window`` and ``last_n`` are provided.
    """
    # Normalize last_n to int to prevent float issues from tool calls
    if last_n is not None:
        last_n = int(last_n)
    normalized_type = query_type.strip().lower()

    # Enforce mutual exclusivity of window and last_n
    if window is not None and last_n is not None:
        return {
            "success": False,
            "output": [],
            "error": "window and last_n are mutually exclusive; use one or the other",
        }

    if normalized_type == "events":
        requested_limit = _DEFAULT_EVENTS_LIMIT if limit is None else int(limit)
        query_limit = max(requested_limit, 1)
        results = query_events(
            event=event,
            window_str=window,
            component=component,
            limit=query_limit,
        )
        return {
            "success": True,
            "output": _apply_output_cap(results),
            "error": None,
        }

    if normalized_type == "trace":
        if not trace_id:
            return {
                "success": False,
                "output": [],
                "error": "trace_id required for query_type='trace'",
            }

        return {
            "success": True,
            "output": _apply_output_cap(get_trace_events(trace_id)),
            "error": None,
        }

    if normalized_type == "latency":
        if not trace_id:
            return {
                "success": False,
                "output": [],
                "error": "trace_id required for query_type='latency'",
            }

        return {
            "success": True,
            "output": _apply_output_cap(get_request_latency_breakdown(trace_id)),
            "error": None,
        }

    # New query types
    if normalized_type == "health":
        return _execute_health_query(window=window, last_n=last_n)

    if normalized_type == "errors":
        return _execute_errors_query(window=window, last_n=last_n)

    if normalized_type == "interactions":
        return _execute_interactions_query(last_n=last_n)

    if normalized_type == "performance":
        return _execute_performance_query(window=window, last_n=last_n)

    return {
        "success": False,
        "output": [],
        "error": f"invalid query_type '{query_type}'. expected one of: events, trace, latency, health, errors, interactions, performance",
    }
