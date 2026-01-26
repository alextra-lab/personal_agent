"""Semantic event constants for structured logging.

All log events should use these constants rather than magic strings to ensure
consistency and enable reliable querying and analysis.
"""

# Orchestrator events
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

# Tool execution events
TOOL_CALL_STARTED = "tool_call_started"
TOOL_CALL_COMPLETED = "tool_call_completed"
TOOL_CALL_FAILED = "tool_call_failed"
TOOL_EXECUTED = "tool_executed"

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

# Routing events (Day 11.5)
ROUTING_DECISION = "routing_decision"
ROUTING_DELEGATION = "routing_delegation"
ROUTING_HANDLED = "routing_handled"
ROUTING_PARSE_ERROR = "routing_parse_error"

# Captain's Log events (Day 24-25)
CAPTAINS_LOG_ENTRY_CREATED = "captains_log_entry_created"
CAPTAINS_LOG_ENTRY_COMMITTED = "captains_log_entry_committed"

# MCP Gateway events
MCP_GATEWAY_STARTED = "mcp_gateway_started"
MCP_GATEWAY_STOPPED = "mcp_gateway_stopped"
MCP_GATEWAY_INIT_FAILED = "mcp_gateway_init_failed"
MCP_TOOL_DISCOVERED = "mcp_tool_discovered"
MCP_TOOL_GOVERNANCE_ADDED = "mcp_tool_governance_added"
