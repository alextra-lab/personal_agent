"""Telemetry module for structured logging and trace correlation.

This module provides:
- TraceContext for distributed tracing
- Structured logging via structlog
- Semantic event constants
"""

from personal_agent.telemetry.events import (
    APPROVAL_DENIED,
    APPROVAL_GRANTED,
    APPROVAL_REQUIRED,
    CAPTAINS_LOG_ENTRY_COMMITTED,
    CAPTAINS_LOG_ENTRY_CREATED,
    LIFECYCLE_ARCHIVE,
    LIFECYCLE_DISK_ALERT,
    LIFECYCLE_DISK_CHECK,
    LIFECYCLE_ES_CLEANUP,
    LIFECYCLE_PURGE,
    LIFECYCLE_REPORT,
    MODE_TRANSITION,
    MODEL_CALL_COMPLETED,
    MODEL_CALL_ERROR,
    MODEL_CALL_STARTED,
    ORCHESTRATOR_FATAL_ERROR,
    POLICY_VIOLATION,
    REPLY_READY,
    REQUEST_RECEIVED,
    SENSOR_POLL,
    SESSION_CLOSED,
    SESSION_CREATED,
    STATE_TRANSITION,
    STEP_EXECUTED,
    SYSTEM_METRICS_SNAPSHOT,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_STARTED,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_FAILED,
    TOOL_CALL_STARTED,
    TOOL_EXECUTED,
    UNKNOWN_STATE,
)
from personal_agent.telemetry.logger import add_elasticsearch_handler, configure_logging, get_logger
from personal_agent.telemetry.metrics import (
    get_recent_cpu_load,
    get_recent_event_count,
    get_request_latency_breakdown,
    get_trace_events,
    query_events,
)
from personal_agent.telemetry.queries import (
    ConsolidationEvent,
    ModeTransition,
    TaskPatternReport,
    TelemetryQueries,
)
from personal_agent.telemetry.trace import TraceContext

__all__ = [
    # Core exports
    "TraceContext",
    "get_logger",
    "configure_logging",
    "add_elasticsearch_handler",
    # Metrics and queries
    "get_recent_event_count",
    "get_recent_cpu_load",
    "get_request_latency_breakdown",
    "get_trace_events",
    "query_events",
    "TelemetryQueries",
    "ModeTransition",
    "ConsolidationEvent",
    "TaskPatternReport",
    # Event constants
    "REQUEST_RECEIVED",
    "REPLY_READY",
    "TASK_STARTED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "STEP_EXECUTED",
    "STATE_TRANSITION",
    "ORCHESTRATOR_FATAL_ERROR",
    "UNKNOWN_STATE",
    "MODEL_CALL_STARTED",
    "MODEL_CALL_COMPLETED",
    "MODEL_CALL_ERROR",
    "TOOL_CALL_STARTED",
    "TOOL_CALL_COMPLETED",
    "TOOL_CALL_FAILED",
    "TOOL_EXECUTED",
    "MODE_TRANSITION",
    "SENSOR_POLL",
    "SYSTEM_METRICS_SNAPSHOT",
    "POLICY_VIOLATION",
    "APPROVAL_REQUIRED",
    "APPROVAL_GRANTED",
    "APPROVAL_DENIED",
    "SESSION_CREATED",
    "SESSION_CLOSED",
    "CAPTAINS_LOG_ENTRY_CREATED",
    "CAPTAINS_LOG_ENTRY_COMMITTED",
    "LIFECYCLE_ARCHIVE",
    "LIFECYCLE_DISK_ALERT",
    "LIFECYCLE_DISK_CHECK",
    "LIFECYCLE_ES_CLEANUP",
    "LIFECYCLE_PURGE",
    "LIFECYCLE_REPORT",
]
