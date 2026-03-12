"""Self-telemetry tool for querying agent execution history.

This module exposes existing telemetry metric query helpers through a
single tool-call interface so the agent can inspect historical behavior.
"""

from __future__ import annotations

from typing import Any

from personal_agent.telemetry.metrics import (
    get_request_latency_breakdown,
    get_trace_events,
    query_events,
)
from personal_agent.tools.types import ToolDefinition, ToolParameter

_DEFAULT_EVENTS_LIMIT = 20
_MAX_OUTPUT_ENTRIES = 50


self_telemetry_query_tool = ToolDefinition(
    name="self_telemetry_query",
    description=(
        "Query this agent's own telemetry. Use to inspect execution traces, "
        "find errors, analyze latency, or review recent events. "
        "query_type: 'events' (filter by event/component/time), "
        "'trace' (reconstruct one trace), "
        "'latency' (phase-by-phase breakdown for one trace). "
        "To find recent errors: use query_type='events', event='model_call_error' or 'task_failed', "
        "window e.g. '1h' or '24h', and limit (e.g. 10)."
    ),
    category="read_only",
    parameters=[
        ToolParameter(
            name="query_type",
            type="string",
            description="One of: events, trace, latency",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="trace_id",
            type="string",
            description="Trace ID (required for trace and latency)",
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
            description="Time window e.g. '1h', '30m', '2d' (for events query)",
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
        query_type: One of ``events``, ``trace``, or ``latency``.
        trace_id: Required for ``trace`` and ``latency`` queries.
        event: Event name filter for ``events``.
        window: Time window filter for ``events``.
        component: Component filter for ``events``.
        limit: Optional max results for ``events`` (defaults to 20).

    Returns:
        Dict with ``success``, ``output``, and ``error`` keys.
    """
    normalized_type = query_type.strip().lower()

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

    return {
        "success": False,
        "output": [],
        "error": f"invalid query_type '{query_type}'. expected one of: events, trace, latency",
    }
