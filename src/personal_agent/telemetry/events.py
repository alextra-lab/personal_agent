"""Semantic event constants for structured logging.

All log events should use these constants rather than magic strings to ensure
consistency and enable reliable querying and analysis.
"""

# Task outcomes (for self-telemetry health/error tracking)
TASK_OUTCOME_COMPLETED = "completed"
TASK_OUTCOME_FAILED = "failed"
TASK_OUTCOME_TIMEOUT = "timeout"

# Health status constants (for self-telemetry health queries)
HEALTH_STATUS_HEALTHY = "healthy"
HEALTH_STATUS_DEGRADED = "degraded"
HEALTH_STATUS_UNHEALTHY = "unhealthy"

# Error trend constants
ERROR_TREND_INCREASING = "increasing"
ERROR_TREND_STABLE = "stable"
ERROR_TREND_DECREASING = "decreasing"

# Orchestrator events
REQUEST_RECEIVED = "request_received"
REPLY_READY = "reply_ready"
TASK_STARTED = "task_started"
TASK_COMPLETED = "task_completed"
TASK_FAILED = "task_failed"
STEP_EXECUTED = "step_executed"
STATE_TRANSITION = "state_transition"
ORCHESTRATOR_FATAL_ERROR = "orchestrator_fatal_error"
UNKNOWN_STATE = "unknown_state"

# LLM Client events
MODEL_CALL_STARTED = "model_call_started"
MODEL_CALL_COMPLETED = "model_call_completed"
MODEL_CALL_ERROR = "model_call_error"
HISTORY_SANITISED = "history_sanitised"

# ADR-0074 / FRE-376 Phase 3: orchestrator step-planning boundary.
# Distinct from MODEL_CALL_* (which the model clients emit with the full
# canonical shape). Before Phase 3, the orchestrator also emitted
# MODEL_CALL_STARTED with a thinner (model_role, channel) shape — same event
# name, two different payloads, ambiguous Kibana queries. The split makes
# model_call_* exclusively client-side and step_planning_* exclusively
# orchestrator-side.
STEP_PLANNING_STARTED = "step_planning_started"
STEP_PLANNING_COMPLETED = "step_planning_completed"

# ADR-0074 / FRE-376 Phase 2 (I2): both LocalLLMClient and LiteLLMClient emit
# the canonical `model_call_started` / `model_call_completed` events with the
# field sets below. These frozensets are imported by the parity test as the
# single source of truth — adding a required field here forces both clients
# (and any future model client) to emit it.
CANONICAL_MODEL_CALL_STARTED_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "provider",
        "role",
        "endpoint",
        "trace_id",
        "session_id",
        "span_id",
        "parent_span_id",
    }
)
CANONICAL_MODEL_CALL_COMPLETED_FIELDS: frozenset[str] = (
    CANONICAL_MODEL_CALL_STARTED_FIELDS
    | frozenset(
        {
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            # Prompt identity (ADR-0078 D1/D4, FRE-405). Stamped on every call so
            # cost/cache/quality are attributable to a named prompt composition.
            "prompt_callsite",
            "prompt_component_ids",
            "prompt_static_prefix_hash",
            "prompt_dynamic_hash",
        }
    )
)

# Orchestrator step events (distinct from LLM client events above)
# FRE-352: step-level emit uses llm_step_completed to avoid conflating with
# the richer client-level model_call_completed payload in ES consumers.
LLM_STEP_COMPLETED = "llm_step_completed"

# Tool execution events
TOOL_CALL_STARTED = "tool_call_started"
TOOL_CALL_COMPLETED = "tool_call_completed"
TOOL_CALL_FAILED = "tool_call_failed"
TOOL_EXECUTED = "tool_executed"
TOOL_SCHEMA_VALIDATION_FAILED = "tool_schema_validation_failed"

# Brainstem events
MODE_TRANSITION = "mode_transition"
SENSOR_POLL = "sensor_poll"
SYSTEM_METRICS_SNAPSHOT = "system_metrics_snapshot"

# Safety and governance events
POLICY_VIOLATION = "policy_violation"
APPROVAL_REQUIRED = "approval_required"
APPROVAL_GRANTED = "approval_granted"
APPROVAL_DENIED = "approval_denied"

# Session events
SESSION_CREATED = "session_created"
SESSION_CLOSED = "session_closed"

# Captain's Log events (Day 24-25)
CAPTAINS_LOG_ENTRY_CREATED = "captains_log_entry_created"
CAPTAINS_LOG_ENTRY_COMMITTED = "captains_log_entry_committed"

# Captain's Log ES backfill (FRE-30)
CAPTAINS_LOG_BACKFILL_STARTED = "captains_log_backfill_started"
CAPTAINS_LOG_BACKFILL_COMPLETED = "captains_log_backfill_completed"
CAPTAINS_LOG_BACKFILL_FILE_FAILED = "captains_log_backfill_file_failed"
CAPTAINS_LOG_BACKFILL_CHECKPOINT_UPDATED = "captains_log_backfill_checkpoint_updated"

# MCP Gateway events
MCP_GATEWAY_STARTED = "mcp_gateway_started"
MCP_GATEWAY_STOPPED = "mcp_gateway_stopped"
MCP_GATEWAY_INIT_FAILED = "mcp_gateway_init_failed"
MCP_TOOL_DISCOVERED = "mcp_tool_discovered"
MCP_TOOL_GOVERNANCE_ADDED = "mcp_tool_governance_added"

# Request timing events (FRE-37)
REQUEST_TIMING = "request_timing"

# Data lifecycle events (Phase 2.3)
LIFECYCLE_DISK_CHECK = "lifecycle_disk_check"
LIFECYCLE_ARCHIVE = "lifecycle_archive"
LIFECYCLE_PURGE = "lifecycle_purge"
LIFECYCLE_ES_CLEANUP = "lifecycle_es_cleanup"
LIFECYCLE_REPORT = "lifecycle_report"
LIFECYCLE_DISK_ALERT = "lifecycle_disk_alert"
