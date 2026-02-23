"""Orchestrator execution loop and state machine.

This module implements the core orchestrator state machine with step functions.
The executor coordinates task execution through explicit state transitions.
"""

import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

from personal_agent.config import settings
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.context_window import apply_context_window, estimate_messages_tokens
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import (
    ExecutionContext,
    OrchestratorResult,
    OrchestratorStep,
    RoutingDecision,
    RoutingResult,
    TaskState,
)
from personal_agent.security import sanitize_error_message
from personal_agent.telemetry import (
    MODEL_CALL_COMPLETED,
    MODEL_CALL_ERROR,
    MODEL_CALL_STARTED,
    ORCHESTRATOR_FATAL_ERROR,
    REPLY_READY,
    STATE_TRANSITION,
    STEP_EXECUTED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_STARTED,
    UNKNOWN_STATE,
    get_logger,
)
from personal_agent.telemetry.events import (
    ROUTING_DECISION,
    ROUTING_DELEGATION,
    ROUTING_HANDLED,
    ROUTING_PARSE_ERROR,
)
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools import ToolExecutionLayer, get_default_registry
from personal_agent.tools.registry import ToolRegistry

log = get_logger(__name__)

# Global tool registry instance (initialized on first use)
_tool_registry: ToolRegistry | None = None
_tool_execution_layer: ToolExecutionLayer | None = None

if TYPE_CHECKING:  # pragma: no cover
    from personal_agent.mcp.gateway import MCPGatewayAdapter

_mcp_adapter: "MCPGatewayAdapter | None" = None


def _router_response_format() -> dict[str, Any]:
    """Return strict JSON schema for router structured output."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "router_decision",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "routing_decision": {"type": "string", "enum": ["HANDLE", "DELEGATE"]},
                    "target_model": {"type": "string", "enum": ["STANDARD", "REASONING", "CODING"]},
                    "confidence": {"type": "number"},
                    "reasoning_depth": {"type": "integer"},
                    "reason": {"type": "string"},
                    "response": {"type": "string"},
                    "detected_format": {"type": "string"},
                    "format_confidence": {"type": "number"},
                    "format_keywords_matched": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "recommended_params": {"type": "object"},
                },
                "required": ["routing_decision", "confidence", "reasoning_depth", "reason"],
                "additionalProperties": True,
            },
        },
    }


def _normalize_no_think_suffix(suffix: str) -> str:
    """Normalize the no-think suffix to a single token-like string.

    Args:
        suffix: Raw configured suffix (e.g., "/no_think" or " /no_think").

    Returns:
        Normalized suffix string, without trailing whitespace.
    """
    return suffix.strip()


def _validate_and_fix_conversation_roles(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate conversation role alternation and fix if needed for strict models like Mistral.

    Mistral models require:
    - Optional system message at position 0
    - After system (or from start), strict user/assistant alternation
    - Tool messages don't break alternation

    This function:
    1. Preserves system message at start
    2. Ensures user/assistant alternation
    3. Merges consecutive messages of the same role (combines content)
    4. Preserves tool messages

    Args:
        messages: Original message list.

    Returns:
        Fixed message list with proper alternation.
    """
    if not messages:
        return messages

    fixed: list[dict[str, Any]] = []
    system_msg: dict[str, Any] | None = None
    last_non_tool_role: str | None = None

    # First pass: extract system message and build alternating sequence
    for msg in messages:
        role = msg.get("role")

        # Extract system message (only first one, keep at position 0)
        if role == "system":
            if system_msg is None:
                system_msg = msg
            continue

        # Tool messages: preserve them but don't affect alternation
        if role == "tool":
            fixed.append(msg)
            continue

        # For user/assistant: ensure alternation
        if role in ("user", "assistant"):
            # If same role as last non-tool message, merge content
            if role == last_non_tool_role and fixed:
                # Find the last message with this role
                for i in range(len(fixed) - 1, -1, -1):
                    if fixed[i].get("role") == role:
                        # Merge content
                        old_content = fixed[i].get("content", "")
                        new_content = msg.get("content", "")
                        if old_content and new_content:
                            fixed[i]["content"] = f"{old_content}\n\n{new_content}"
                        elif new_content:
                            fixed[i]["content"] = new_content
                        log.warning(
                            "conversation_role_duplicate_merged",
                            role=role,
                            trace_id=getattr(fixed[i], "trace_id", None),
                            message_preview=str(new_content)[:50],
                        )
                        break
            else:
                # Different role or no previous message: add it
                fixed.append(msg)
                last_non_tool_role = role

    # Rebuild with system at start
    result: list[dict[str, Any]] = []
    if system_msg:
        result.append(system_msg)
    result.extend(fixed)

    # Final validation: check that non-tool, non-system messages alternate
    roles_sequence = [
        msg.get("role") for msg in result if msg.get("role") not in ("system", "tool")
    ]

    # Verify alternation
    for i in range(1, len(roles_sequence)):
        if roles_sequence[i] == roles_sequence[i - 1]:
            log.error(
                "conversation_role_alternation_failed",
                position=i,
                role=roles_sequence[i],
                sequence=roles_sequence,
                message="Failed to fix conversation alternation - consecutive same roles remain",
            )

    return result


def _append_no_think_to_last_user_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append the configured no-think suffix to the last user message.

    This is used for tool-request prompts where the last message is typically the user request.
    The original message list is not mutated.
    """
    suffix = _normalize_no_think_suffix(settings.llm_no_think_suffix)
    if not settings.llm_append_no_think_to_tool_prompts or not suffix:
        return messages

    out = deepcopy(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        content = out[i].get("content")
        if not isinstance(content, str):
            continue
        trimmed = content.rstrip()
        if trimmed.endswith(suffix):
            return out
        # Append /no_think on a new line to clearly separate it from user query
        # This prevents models from misinterpreting it as a directory path
        out[i]["content"] = f"{trimmed}\n{suffix}"
        return out
    return out


def _append_no_think_synthesis_nudge(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure /no_think is the final suffix in post-tool synthesis prompts.

    In synthesis, the last message is often a tool output. To place the suffix at the end of the
    prompt, we append a short user nudge that ends with the suffix. The original message list is
    not mutated.

    Important: Only appends if the last message is NOT a user message, to avoid violating
    conversation alternation rules required by strict models.
    """
    suffix = _normalize_no_think_suffix(settings.llm_no_think_suffix)
    if not settings.llm_append_no_think_to_tool_prompts or not suffix:
        return messages

    out = deepcopy(messages)

    # Check last message role to avoid violating alternation
    if len(out) > 0 and out[-1].get("role") == "user":
        # Last message is already user - just append suffix to it
        content = out[-1].get("content", "")
        if isinstance(content, str) and not content.rstrip().endswith(suffix):
            out[-1]["content"] = f"{content.rstrip()}\n{suffix}"
        return out

    # Safe to append new user message (last was assistant or tool)
    out.append({"role": "user", "content": f"Return the final answer now. {suffix}"})
    return out


def _fallback_reply_from_tool_results(ctx: ExecutionContext) -> str:
    """Build a safe, user-facing reply when the model fails to synthesize after tools."""
    if not ctx.tool_results:
        return (
            "I attempted to use tools, but couldn't produce a final answer. "
            "Try rephrasing your request or specify the exact path to inspect."
        )

    last_results = ctx.tool_results[-3:]
    lines: list[str] = [
        "I attempted to use tools, but synthesis failed. Here are the latest tool results:"
    ]
    for r in last_results:
        tool_name = r.get("tool_name", "unknown_tool")
        success = r.get("success", False)
        if success:
            lines.append(f"- {tool_name}: success")
        else:
            err = r.get("error") or "Unknown error"
            lines.append(f"- {tool_name}: failed ({err})")
    lines.append(
        "If you'd like, try again with an explicit directory path, e.g. “List 3 non-hidden files in /path/to/directory”."
    )
    return "\n".join(lines)


def _unwrap_embedded_response_json(response_content: str) -> str:
    """Best-effort: unwrap models that emit router-style JSON with a `response` field."""
    candidate = response_content.strip()
    if not candidate:
        return response_content

    # Remove markdown code fences if present
    if candidate.startswith("```"):
        lines = candidate.split("\n")
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()

    if not (candidate.startswith("{") and candidate.endswith("}")):
        return response_content

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return response_content

    if isinstance(data, dict):
        embedded = data.get("response")
        if isinstance(embedded, str) and embedded.strip():
            return embedded.strip()

    return response_content


def _get_tool_execution_layer() -> ToolExecutionLayer:
    """Get or create the global tool execution layer.

    Returns:
        ToolExecutionLayer instance with MVP tools registered.
    """
    global _tool_execution_layer
    if _tool_execution_layer is None:
        global _tool_registry
        if _tool_registry is None:
            _tool_registry = get_default_registry()
        _tool_execution_layer = ToolExecutionLayer(_tool_registry)
    return _tool_execution_layer


async def _initialize_mcp_gateway() -> None:
    """Initialize MCP Gateway adapter if enabled.

    Called during orchestrator startup to discover and register MCP tools.
    If gateway fails to initialize, logs warning and continues (graceful degradation).
    """
    global _mcp_adapter

    # If already connected, don't re-initialize.
    # Note: If a previous init attempt failed, adapter.client will be None; allow retry.
    if _mcp_adapter is not None and getattr(_mcp_adapter, "client", None) is not None:
        return

    if not settings.mcp_gateway_enabled:
        log.debug("mcp_gateway_not_enabled")
        return

    try:
        from personal_agent.mcp.gateway import MCPGatewayAdapter

        # Get or create registry
        global _tool_registry
        if _tool_registry is None:
            _tool_registry = get_default_registry()

        _mcp_adapter = MCPGatewayAdapter(_tool_registry)
        await _mcp_adapter.initialize()

    except Exception as e:
        log.error(
            "mcp_gateway_init_failed", error=str(e), error_type=type(e).__name__, exc_info=True
        )
        # Graceful degradation: continue without MCP


async def _shutdown_mcp_gateway() -> None:
    """Shutdown MCP Gateway adapter."""
    global _mcp_adapter

    if _mcp_adapter:
        try:
            await _mcp_adapter.shutdown()
        except Exception as e:
            log.error("mcp_gateway_shutdown_failed", error=str(e), exc_info=True)
        finally:
            _mcp_adapter = None


# ============================================================================
# Helper Functions for Routing
# ============================================================================


def _determine_initial_model_role(ctx: ExecutionContext) -> ModelRole:
    """Determine initial model role based on channel.

    Args:
        ctx: Execution context.

    Returns:
        Initial model role to use.
    """
    if ctx.channel == Channel.CODE_TASK:
        return ModelRole.CODING
    elif ctx.channel == Channel.CHAT:
        return ModelRole.ROUTER  # CHAT always starts with router
    else:  # SYSTEM_HEALTH or default
        return ModelRole.REASONING


def _parse_routing_decision(response_content: str, ctx: ExecutionContext) -> RoutingResult | None:
    """Parse router's JSON response into RoutingResult.

    Args:
        response_content: Router's response text (should contain JSON).
        ctx: Execution context (for logging).

    Returns:
        RoutingResult if parsing succeeds, None otherwise.
    """
    try:
        # Check for empty response
        if not response_content or not response_content.strip():
            log.error(
                ROUTING_PARSE_ERROR,
                trace_id=ctx.trace_id,
                error="Empty response from router",
                response_preview="(empty)",
            )
            # Fallback to STANDARD
            log.warning(
                "routing_parse_fallback",
                trace_id=ctx.trace_id,
                fallback_model="STANDARD",
            )
            return {
                "decision": RoutingDecision.DELEGATE,
                "target_model": ModelRole.STANDARD,
                "confidence": 0.5,
                "reasoning_depth": 5,
                "reason": "Router returned empty response, defaulting to STANDARD",
                "detected_format": None,
                "format_confidence": None,
                "format_keywords_matched": None,
                "recommended_params": None,
                "response": None,
            }

        # Extract JSON from response (may be wrapped in markdown code blocks)
        json_str = response_content.strip()

        # Remove markdown code fences if present
        if json_str.startswith("```"):
            lines = json_str.split("\n")
            json_str = "\n".join(lines[1:-1])  # Remove first and last lines

        # Check if we still have content after stripping
        if not json_str:
            log.error(
                ROUTING_PARSE_ERROR,
                trace_id=ctx.trace_id,
                error="Empty JSON string after processing",
                response_preview=response_content[:200],
            )
            # Fallback to STANDARD
            log.warning(
                "routing_parse_fallback",
                trace_id=ctx.trace_id,
                fallback_model="STANDARD",
            )
            return {
                "decision": RoutingDecision.DELEGATE,
                "target_model": ModelRole.STANDARD,
                "confidence": 0.5,
                "reasoning_depth": 5,
                "reason": "Router response was empty after processing, defaulting to STANDARD",
                "detected_format": None,
                "format_confidence": None,
                "format_keywords_matched": None,
                "recommended_params": None,
                "response": None,
            }

        # Parse JSON
        data = json.loads(json_str)

        # Convert to RoutingResult
        result: RoutingResult = {
            "decision": RoutingDecision(data["routing_decision"]),
            "confidence": float(data["confidence"]),
            "reasoning_depth": int(data["reasoning_depth"]),
            "reason": data["reason"],
            "target_model": None,
            "detected_format": None,
            "format_confidence": None,
            "format_keywords_matched": None,
            "recommended_params": None,
            "response": None,
        }

        # Add optional fields
        if "target_model" in data and data["target_model"]:
            result["target_model"] = ModelRole.from_str(data["target_model"])

        # Phase 2 fields (not in MVP)
        if "detected_format" in data:
            result["detected_format"] = data["detected_format"]
        if "format_confidence" in data:
            result["format_confidence"] = float(data["format_confidence"])
        if "format_keywords_matched" in data:
            result["format_keywords_matched"] = data["format_keywords_matched"]
        if "recommended_params" in data:
            result["recommended_params"] = data["recommended_params"]

        # If router is handling directly, extract response
        if result["decision"] == RoutingDecision.HANDLE:
            result["response"] = data.get("response", response_content)

        return result

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.error(
            ROUTING_PARSE_ERROR,
            trace_id=ctx.trace_id,
            error=str(e),
            response_preview=response_content[:200],
        )

        # Fallback: If parsing fails, default to STANDARD model
        log.warning(
            "routing_parse_fallback",
            trace_id=ctx.trace_id,
            fallback_model="STANDARD",
        )

        return {
            "decision": RoutingDecision.DELEGATE,
            "target_model": ModelRole.STANDARD,
            "confidence": 0.5,
            "reasoning_depth": 5,
            "reason": "Router parse failed, defaulting to STANDARD",
            "detected_format": None,
            "format_confidence": None,
            "format_keywords_matched": None,
            "recommended_params": None,
            "response": None,
        }


async def _trigger_captains_log_reflection(ctx: ExecutionContext) -> None:
    """Trigger an LLM-based Captain's Log reflection after task completion.

    This is a non-blocking async function that creates a reflection entry
    with LLM-generated insights.

    Args:
        ctx: Execution context with task details.
    """
    try:
        from personal_agent.captains_log import CaptainLogManager
        from personal_agent.captains_log.reflection import generate_reflection_entry

        manager = CaptainLogManager()

        # Generate LLM-based reflection (with metrics summary from ADR-0012)
        entry = await generate_reflection_entry(
            user_message=ctx.user_message,
            trace_id=ctx.trace_id,
            steps_count=len(ctx.steps),
            final_state="COMPLETED",  # Task completed successfully if we're here
            reply_length=len(ctx.final_reply or ""),
            metrics_summary=ctx.metrics_summary,  # Request-scoped metrics (ADR-0012)
        )

        # Write entry to file
        manager.write_entry(entry)

        # Optionally commit to git (disabled in MVP)
        # manager.commit_to_git(entry.entry_id)

    except Exception as e:
        # Don't let Captain's Log failures break task completion
        log.warning(
            "captains_log_reflection_failed",
            trace_id=ctx.trace_id,
            error=str(e),
        )


async def execute_task(ctx: ExecutionContext, session_manager: SessionManager) -> ExecutionContext:
    """Main execution loop: iterate states until terminal.

    This is the core state machine that drives task execution. It transitions
    through states until reaching a terminal state (COMPLETED or FAILED).

    Includes request-scoped metrics monitoring (ADR-0012) for homeostasis
    control loops and Captain's Log enrichment.

    Args:
        ctx: Execution context containing task state and parameters.
        session_manager: Session manager for accessing session data.

    Returns:
        Updated execution context after state machine completion.
    """
    state = ctx.state
    trace_ctx = TraceContext(trace_id=ctx.trace_id)

    log.info(
        TASK_STARTED,
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        user_message=ctx.user_message,
        mode=ctx.mode.value,
        channel=ctx.channel.value,
    )

    # Start request-scoped metrics monitoring (ADR-0012)
    monitor = None
    if settings.request_monitoring_enabled:
        from personal_agent.brainstem.sensors.request_monitor import RequestMonitor

        monitor = RequestMonitor(
            trace_id=ctx.trace_id,
            interval_seconds=settings.request_monitoring_interval_seconds,
            include_gpu=settings.request_monitoring_include_gpu,
        )
        try:
            await monitor.start()
        except Exception as e:
            # Don't fail task if monitoring fails
            log.warning(
                "request_monitor_start_failed",
                trace_id=ctx.trace_id,
                error=str(e),
                component="executor",
            )
            monitor = None

    # Step function registry
    step_functions = {
        TaskState.INIT: step_init,
        TaskState.PLANNING: step_planning,
        TaskState.LLM_CALL: step_llm_call,
        TaskState.TOOL_EXECUTION: step_tool_execution,
        TaskState.SYNTHESIS: step_synthesis,
    }

    try:
        while state not in {TaskState.COMPLETED, TaskState.FAILED}:
            log.info(
                STATE_TRANSITION,
                trace_id=ctx.trace_id,
                from_state=state.value,
            )
            ctx.state = state

            step_func = step_functions.get(state)
            if not step_func:
                log.error(
                    UNKNOWN_STATE,
                    trace_id=ctx.trace_id,
                    state=state.value,
                )
                ctx.error = ValueError(f"Unknown state: {state}")
                state = TaskState.FAILED
                break

            # Execute step function
            state = await step_func(ctx, session_manager, trace_ctx)

        ctx.state = state

        # Stop request-scoped monitoring BEFORE Captain's Log (ADR-0012)
        # This ensures metrics_summary is available for reflection enrichment
        if monitor is not None:
            try:
                metrics_summary = await monitor.stop()
                ctx.metrics_summary = metrics_summary

                # Log summary for analysis
                log.info(
                    "request_metrics_summary",
                    trace_id=ctx.trace_id,
                    duration_seconds=metrics_summary.get("duration_seconds"),
                    samples_collected=metrics_summary.get("samples_collected"),
                    cpu_avg=metrics_summary.get("cpu_avg"),
                    memory_avg=metrics_summary.get("memory_avg"),
                    gpu_avg=metrics_summary.get("gpu_avg"),
                    threshold_violations=metrics_summary.get("threshold_violations"),
                    component="executor",
                )
            except Exception as e:
                # Don't fail task if monitoring cleanup fails
                log.warning(
                    "request_monitor_stop_failed",
                    trace_id=ctx.trace_id,
                    error=str(e),
                    component="executor",
                )

        if state == TaskState.COMPLETED:
            log.info(
                TASK_COMPLETED,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                reply_length=len(ctx.final_reply or ""),
                steps_count=len(ctx.steps),
            )

            # Fast capture (Phase 2.2): Write structured capture immediately (no LLM)
            try:
                from personal_agent.captains_log.capture import TaskCapture, write_capture

                # Calculate duration from metrics summary if available
                duration_ms = None
                if ctx.metrics_summary and "duration_seconds" in ctx.metrics_summary:
                    duration_ms = ctx.metrics_summary["duration_seconds"] * 1000

                # Extract tools used from steps
                tools_used = []
                for step in ctx.steps:
                    if step.get("type") == "tool_call":
                        tool_name = (step.get("metadata") or {}).get("tool_name")
                        if tool_name:
                            tools_used.append(tool_name)

                capture = TaskCapture(
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    timestamp=datetime.now(timezone.utc),
                    user_message=ctx.user_message,
                    assistant_response=ctx.final_reply,
                    steps=cast(list[dict[str, Any]], ctx.steps),
                    tools_used=list(set(tools_used)),  # Deduplicate
                    duration_ms=duration_ms,
                    metrics_summary=ctx.metrics_summary,
                    outcome="completed",
                    memory_context_used=bool(ctx.memory_context),
                    memory_conversations_found=len(ctx.memory_context) if ctx.memory_context else 0,
                )
                write_capture(capture)
            except Exception as e:
                # Don't fail task if capture fails
                log.warning(
                    "capture_write_failed",
                    trace_id=ctx.trace_id,
                    error=str(e),
                    exc_info=True,
                )

            # Trigger Captain's Log reflection (LLM-based, background)
            # Run in background to avoid blocking user response
            # Metrics summary is now available in ctx for reflection enrichment
            from personal_agent.captains_log.background import run_in_background

            run_in_background(_trigger_captains_log_reflection(ctx))
        else:
            log.warning(
                TASK_FAILED,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                error=str(ctx.error) if ctx.error else "Unknown error",
            )

    except Exception as e:
        log.error(
            ORCHESTRATOR_FATAL_ERROR,
            trace_id=ctx.trace_id,
            exc_info=True,
        )
        ctx.error = e
        ctx.state = TaskState.FAILED

        # Stop monitoring even on fatal error
        if monitor is not None and ctx.metrics_summary is None:
            try:
                metrics_summary = await monitor.stop()
                ctx.metrics_summary = metrics_summary

                # Log summary for analysis
                log.info(
                    "request_metrics_summary",
                    trace_id=ctx.trace_id,
                    duration_seconds=metrics_summary.get("duration_seconds"),
                    samples_collected=metrics_summary.get("samples_collected"),
                    cpu_avg=metrics_summary.get("cpu_avg"),
                    memory_avg=metrics_summary.get("memory_avg"),
                    gpu_avg=metrics_summary.get("gpu_avg"),
                    threshold_violations=metrics_summary.get("threshold_violations"),
                    component="executor",
                )
            except Exception as e:
                # Don't fail task if monitoring cleanup fails
                log.warning(
                    "request_monitor_stop_failed",
                    trace_id=ctx.trace_id,
                    error=str(e),
                    component="executor",
                )

    return ctx


async def step_init(
    ctx: ExecutionContext, session_manager: SessionManager, trace_ctx: TraceContext
) -> TaskState:
    """Initialize: determine intent and next action.

    For the skeleton implementation, this step:
    - Loads session message history
    - Adds the new user message
    - Queries memory graph for relevant context (Phase 2.2)
    - Determines if planning is needed (simple heuristic)
    - Transitions to PLANNING or LLM_CALL

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Next state (PLANNING or LLM_CALL).
    """
    # Load session and build message history
    session = session_manager.get_session(ctx.session_id)
    session_message_count = 0
    if session:
        ctx.messages = list(session.messages)
        session_message_count = len(ctx.messages)

    # Add new user message
    ctx.messages.append({"role": "user", "content": ctx.user_message})

    # Apply context window controls before LLM usage to prevent overflow.
    input_messages_count = len(ctx.messages)
    ctx.messages = apply_context_window(
        ctx.messages,
        max_tokens=settings.conversation_max_context_tokens,
        strategy=settings.conversation_context_strategy,
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
    )

    log.info(
        "conversation_context_loaded",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        total_messages_in_db=session_message_count,
        messages_loaded=len(ctx.messages),
        messages_truncated=max(0, input_messages_count - len(ctx.messages)),
        estimated_tokens=estimate_messages_tokens(ctx.messages),
    )

    # Query memory graph for relevant context (Phase 2.2)
    if settings.enable_memory_graph:
        try:
            from personal_agent.memory.models import MemoryQuery
            from personal_agent.memory.service import MemoryService

            # Get global memory service instance (initialized in FastAPI lifespan)
            # For CLI usage, create a temporary connection
            memory_service = None
            try:
                # Try to get from service if available
                from personal_agent.service.app import memory_service as global_memory_service

                if global_memory_service and global_memory_service.connected:
                    memory_service = global_memory_service
            except (ImportError, AttributeError):
                # CLI mode: create temporary connection
                memory_service = MemoryService()
                await memory_service.connect()

            if memory_service and memory_service.connected:
                # Extract potential entity names from user message (simple keyword extraction)
                # TODO: Use proper entity extraction/NLP here
                words = ctx.user_message.split()  # Don't lowercase - need capitals
                # Simple heuristic: look for capitalized words or quoted phrases
                potential_entities = [
                    w.strip('",.:;!?') for w in words if len(w) > 3 and w[0].isupper()
                ]

                if potential_entities:
                    query = MemoryQuery(
                        entity_names=potential_entities[:5],  # Limit to 5 entities
                        limit=5,  # Get top 5 related conversations
                        recency_days=30,  # Only recent conversations
                    )
                    result = await memory_service.query_memory(
                        query,
                        feedback_key=ctx.session_id,
                        query_text=ctx.user_message,
                    )

                    # Format memory context for LLM
                    ctx.memory_context = []
                    for conv in result.conversations:
                        ctx.memory_context.append(
                            {
                                "conversation_id": conv.conversation_id,
                                "timestamp": conv.timestamp.isoformat(),
                                "user_message": conv.user_message,
                                "summary": conv.summary or conv.user_message[:200],
                                "key_entities": conv.key_entities,
                            }
                        )

                    log.info(
                        "memory_enrichment_completed",
                        trace_id=ctx.trace_id,
                        conversations_found=len(ctx.memory_context),
                    )

                # Cleanup temporary connection if created
                if memory_service != global_memory_service:
                    await memory_service.disconnect()
        except Exception as e:
            # Don't fail task if memory query fails
            log.warning(
                "memory_enrichment_failed",
                trace_id=ctx.trace_id,
                error=str(e),
                exc_info=True,
            )

    # Simple heuristic: if message is short and simple, skip planning
    # For skeleton, always go to LLM_CALL (planning will be added later)
    needs_planning = False  # Placeholder: could check message complexity

    if needs_planning:
        return TaskState.PLANNING
    return TaskState.LLM_CALL


async def step_planning(
    ctx: ExecutionContext, session_manager: SessionManager, trace_ctx: TraceContext
) -> TaskState:
    """Use reasoning model to create an execution plan.

    This is a placeholder for future planning functionality.
    For skeleton, just transition to LLM_CALL.

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Next state (LLM_CALL).
    """
    # TODO: Call LLM with planning prompt
    # TODO: Parse plan, store in ctx.current_plan
    ctx.current_plan = {"status": "placeholder"}
    return TaskState.LLM_CALL


async def step_llm_call(
    ctx: ExecutionContext, session_manager: SessionManager, trace_ctx: TraceContext
) -> TaskState:
    """Execute LLM call with intelligent routing.

    This step implements multi-model coordination:
    1. If no model selected yet, determine initial model (usually ROUTER for CHAT)
    2. Call selected model
    3. If ROUTER, parse routing decision:
       - HANDLE: Router answered directly, proceed to SYNTHESIS
       - DELEGATE: Router wants to delegate, loop back to LLM_CALL with target model
    4. If non-ROUTER model, proceed to SYNTHESIS or TOOL_EXECUTION

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Next state (LLM_CALL for delegation, TOOL_EXECUTION, SYNTHESIS, or FAILED).
    """
    # Determine which model to call
    if ctx.selected_model_role is None:
        # First LLM call: determine initial model based on channel
        model_role = _determine_initial_model_role(ctx)
    else:
        # Routing decision was made, use selected model
        model_role = ctx.selected_model_role

        # Determine if we need router system prompt
    system_prompt: str | None = None
    if model_role == ModelRole.ROUTER and not ctx.routing_history:
        # First router call: add routing prompt
        from personal_agent.orchestrator.prompts import get_router_prompt

        system_prompt = get_router_prompt(include_format_detection=False)  # MVP: basic prompt
        # Note: User message already in ctx.messages, system prompt passed separately
    else:
        # Tool-use guidance for non-router calls (ADR-0008 hybrid tool calling)
        system_prompt = None

    # Create span for LLM call
    span_ctx, span_id = trace_ctx.new_span()

    step_start_time = time.time()
    log.info(
        MODEL_CALL_STARTED,
        trace_id=ctx.trace_id,
        span_id=span_id,
        model_role=model_role.value,
        channel=ctx.channel.value,
    )

    try:
        # Create LLM client instance
        llm_client = LocalLLMClient()

        # Get tools for this model role and mode
        # Per spec (ORCHESTRATOR_CORE_SPEC_v0.1.md): synthesis should use tools=None
        # Detect if we're synthesizing (last messages are tool results OR previous state was TOOL_EXECUTION)
        is_synthesizing = (len(ctx.messages) > 0 and ctx.messages[-1].get("role") == "tool") or (
            len(ctx.steps) > 0 and ctx.steps[-1].get("type") == "tool_call"
        )

        tools: list[dict[str, Any]] | None = None
        if not is_synthesizing and model_role != ModelRole.ROUTER:
            # Only pass tools if not synthesizing (per spec)
            global _tool_registry
            if _tool_registry is None:
                _tool_registry = get_default_registry()
            tools = _tool_registry.get_tool_definitions_for_llm(mode=ctx.mode)
            log.debug(
                "tools_passed_to_llm",
                trace_id=ctx.trace_id,
                model_role=model_role.value,
                tool_count=len(tools) if tools else 0,
                tool_names=[t.get("function", {}).get("name") for t in (tools or [])],
                mode=ctx.mode.value,
            )
        else:
            log.debug(
                "tools_not_passed_synthesizing",
                trace_id=ctx.trace_id,
                model_role=model_role.value,
            )

        # Add memory context to system prompt (Phase 2.2)
        if ctx.memory_context and len(ctx.memory_context) > 0:
            memory_section = "\n\n## Relevant Past Conversations\n"
            memory_section += (
                "The following past conversations may be relevant to the current request:\n\n"
            )
            for i, mem in enumerate(ctx.memory_context[:3], 1):  # Limit to top 3
                memory_section += (
                    f"{i}. {mem.get('summary', mem.get('user_message', ''))[:150]}...\n"
                )
                if mem.get("key_entities"):
                    memory_section += f"   Entities: {', '.join(mem['key_entities'][:5])}\n"
            memory_section += "\nYou can reference these past conversations to provide more context-aware responses."

            if system_prompt:
                system_prompt = f"{system_prompt}\n{memory_section}"
            else:
                system_prompt = memory_section

        # If we are passing tools, include tool-use guidance in the system prompt to reduce
        # malformed tool calls and looping.
        if tools:
            from personal_agent.orchestrator.prompts import (
                TOOL_USE_SYSTEM_PROMPT,
                get_tool_awareness_prompt,
            )

            # Add tool awareness so agent can answer questions about its capabilities
            tool_awareness = get_tool_awareness_prompt()

            if system_prompt:
                system_prompt = f"{tool_awareness}\n\n{system_prompt}\n\n{TOOL_USE_SYSTEM_PROMPT}"
            else:
                system_prompt = f"{tool_awareness}\n\n{TOOL_USE_SYSTEM_PROMPT}"

        # Call LocalLLMClient.respond()
        # Pass previous_response_id for stateful /v1/responses API
        max_retries_override: int | None = 1 if tools else None
        if is_synthesizing:
            max_retries_override = 0

        # /no_think injection for tool flow (per user preference):
        # - Tool-request call: append suffix to the last user message.
        # - Post-tool synthesis: append a short user nudge ending with the suffix (tool outputs are last).
        #   IMPORTANT: Skip synthesis nudge for Mistral models - they expect direct synthesis after tool results
        request_messages = ctx.messages
        if model_role != ModelRole.ROUTER:
            if tools:
                request_messages = _append_no_think_to_last_user_message(request_messages)
            elif is_synthesizing:
                # Check if we're using a Mistral model (strict alternation requirements)
                # Mistral models expect: user -> assistant (tool_call) -> tool -> assistant (synthesis)
                # They don't want a user nudge between tool results and synthesis
                model_config = llm_client.model_configs.get(model_role.value)
                is_mistral = model_config and "mistral" in model_config.id.lower()

                if not is_mistral:
                    request_messages = _append_no_think_synthesis_nudge(request_messages)
                else:
                    log.info(
                        "synthesis_nudge_skipped_for_mistral",
                        trace_id=ctx.trace_id,
                        model_id=model_config.id if model_config else None,
                        reason="Mistral models require direct synthesis after tool results",
                    )

        # Validate and fix conversation role alternation for strict models (e.g., Mistral)
        request_messages = _validate_and_fix_conversation_roles(request_messages)

        # Debug: log message roles for conversation validation
        message_roles = [msg.get("role", "unknown") for msg in request_messages]
        log.info(
            "llm_call_messages_debug",
            trace_id=ctx.trace_id,
            span_id=span_id,
            model_role=model_role.value,
            message_count=len(request_messages),
            message_roles=message_roles,
            messages_preview=[
                {
                    "role": msg.get("role"),
                    "content_preview": str(msg.get("content", ""))[:100]
                    if msg.get("content")
                    else None,
                    "has_tool_calls": bool(msg.get("tool_calls")),
                }
                for msg in request_messages
            ],
        )

        response_format: dict[str, Any] | None = None
        if model_role == ModelRole.ROUTER:
            response_format = _router_response_format()

        response = await llm_client.respond(
            role=model_role,
            messages=request_messages,
            system_prompt=system_prompt,
            tools=tools if tools else None,
            response_format=response_format,
            trace_ctx=span_ctx,
            previous_response_id=ctx.last_response_id,
            max_retries=max_retries_override,
        )

        # Extract response content and tool calls
        response_content = response["content"] or ""
        response_tool_calls = response["tool_calls"] or []

        # During synthesis, ignore tool calls (per spec: synthesis should not use tools)
        # This prevents the model from making additional tool calls after tool execution
        if is_synthesizing and response_tool_calls:
            log.warning(
                "tool_calls_ignored_during_synthesis",
                trace_id=ctx.trace_id,
                tool_count=len(response_tool_calls),
                message="Ignoring tool calls during synthesis phase",
            )
            response_tool_calls = []
            # If the model is still trying to call tools during synthesis, we should not
            # surface tool-call markup as the final user reply.
            if "[TOOL_REQUEST]" in response_content or "<tool_call>" in response_content:
                response_content = ""

        # Track response_id for stateful /v1/responses API
        if response.get("response_id"):
            ctx.last_response_id = response["response_id"]

        duration_ms = int((time.time() - step_start_time) * 1000)

        log.info(
            MODEL_CALL_COMPLETED,
            trace_id=ctx.trace_id,
            span_id=span_id,
            duration_ms=duration_ms,
            model_role=model_role.value,
            tokens=response.get("usage", {}).get("total_tokens", 0),
        )

        # Record step
        step: OrchestratorStep = {
            "type": "llm_call",
            "description": f"LLM call with {model_role.value} model",
            "metadata": {
                "model_role": model_role.value,
                "span_id": span_id,
                "duration_ms": duration_ms,
                "tokens": response.get("usage", {}).get("total_tokens", 0),
            },
        }
        ctx.steps.append(step)

        # Handle routing decision if this was a router call
        if model_role == ModelRole.ROUTER:
            routing_result = _parse_routing_decision(response_content, ctx)

            if routing_result:
                ctx.routing_history.append(routing_result)

                # Log routing decision
                log.info(
                    ROUTING_DECISION,
                    trace_id=ctx.trace_id,
                    decision=routing_result["decision"],
                    target_model=routing_result.get("target_model"),
                    confidence=routing_result["confidence"],
                    reasoning_depth=routing_result["reasoning_depth"],
                    reason=routing_result["reason"],
                )

                if routing_result["decision"] == "DELEGATE":
                    # Router wants to delegate to another model
                    target_model: ModelRole | None = routing_result.get("target_model")

                    # Validate target_model is not None (prevents infinite loop)
                    if target_model is None:
                        log.error(
                            ROUTING_PARSE_ERROR,
                            trace_id=ctx.trace_id,
                            error="Invalid or missing target_model in DELEGATE decision",
                            decision="DELEGATE",
                            target_model_raw=routing_result.get("target_model"),
                        )

                        # Fallback to STANDARD to prevent infinite loop
                        log.warning(
                            "routing_invalid_target_model_fallback",
                            trace_id=ctx.trace_id,
                            fallback_model="STANDARD",
                            reason="Invalid target_model in DELEGATE decision",
                        )
                        target_model = ModelRole.STANDARD
                    target_model_role: ModelRole = target_model

                    log.info(
                        ROUTING_DELEGATION,
                        trace_id=ctx.trace_id,
                        from_model="ROUTER",
                        to_model=target_model_role,
                    )

                    ctx.selected_model_role = target_model_role
                    # Note: Do NOT add routing marker to ctx.messages - it confuses
                    # the target model into thinking work has already been done,
                    # preventing it from calling tools properly.
                    return TaskState.LLM_CALL  # Loop back for delegated model

                else:  # HANDLE
                    # Router answered directly
                    log.info(
                        ROUTING_HANDLED,
                        trace_id=ctx.trace_id,
                        model="ROUTER",
                    )

                    # Use router's response if provided
                    router_response = routing_result.get("response")
                    if router_response:
                        response_content = router_response

        # Some reasoning models may emit router-style JSON with a `response` field.
        # Unwrap it to avoid returning JSON to the user.
        response_content = _unwrap_embedded_response_json(response_content)

        # Add assistant message to history (with tool calls if present)
        assistant_message: dict[str, Any] = {"role": "assistant", "content": response_content}
        if response_tool_calls:
            # Store tool calls in assistant message (OpenAI format)
            # MLX backend requires 'index' field per OpenAI API spec
            assistant_message["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    "index": idx,  # Required by MLX backend for validation
                }
                for idx, tc in enumerate(response_tool_calls)
            ]
        ctx.messages.append(assistant_message)

        # If tool calls present, transition to tool execution
        if response_tool_calls:
            return TaskState.TOOL_EXECUTION
        else:
            # No tools, set final reply and synthesize
            ctx.final_reply = response_content or _fallback_reply_from_tool_results(ctx)
            return TaskState.SYNTHESIS

    except Exception as e:
        duration_ms = int((time.time() - step_start_time) * 1000)
        log.error(
            MODEL_CALL_ERROR,
            trace_id=ctx.trace_id,
            span_id=span_id,
            duration_ms=duration_ms,
            error=str(e),
            error_type=type(e).__name__,
        )
        ctx.error = e
        sanitized_error = sanitize_error_message(e)
        error_step: OrchestratorStep = {
            "type": "warning",
            "description": f"LLM call failed: {sanitized_error}",
            "metadata": {"error": sanitized_error, "error_type": type(e).__name__, "span_id": span_id},
        }
        ctx.steps.append(error_step)
        return TaskState.FAILED


async def step_tool_execution(
    ctx: ExecutionContext, session_manager: SessionManager, trace_ctx: TraceContext
) -> TaskState:
    """Execute tool calls, append results to context.

    This step:
    1. Extracts tool calls from the last assistant message
    2. Executes each tool via ToolExecutionLayer
    3. Appends tool results to ctx.messages as tool role messages
    4. Adds tool execution steps to ctx.steps
    5. Transitions back to LLM_CALL for synthesis

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Next state (LLM_CALL for synthesis, or FAILED on error).
    """
    step_start_time = time.time()

    # Loop governance: prevent infinite tool execution cycles
    ctx.tool_iteration_count += 1
    if ctx.tool_iteration_count > settings.orchestrator_max_tool_iterations:
        log.warning(
            "tool_iteration_limit_reached",
            trace_id=ctx.trace_id,
            iteration=ctx.tool_iteration_count,
            max_iterations=settings.orchestrator_max_tool_iterations,
        )
        ctx.final_reply = _fallback_reply_from_tool_results(ctx)
        ctx.steps.append(
            {
                "type": "warning",
                "description": "Tool loop limit reached; returning best-effort response",
                "metadata": {
                    "iteration": ctx.tool_iteration_count,
                    "max_iterations": settings.orchestrator_max_tool_iterations,
                },
            }
        )
        return TaskState.SYNTHESIS

    # Get tool execution layer
    tool_layer = _get_tool_execution_layer()

    # Extract tool calls from the last assistant message
    if not ctx.messages:
        log.error(
            "no_messages_for_tool_execution",
            trace_id=ctx.trace_id,
            error="No messages in context to extract tool calls from",
        )
        ctx.error = ValueError("No messages in context to extract tool calls from")
        return TaskState.FAILED

    last_message = ctx.messages[-1]
    if last_message.get("role") != "assistant":
        log.error(
            "last_message_not_assistant",
            trace_id=ctx.trace_id,
            error="Last message is not from assistant",
        )
        ctx.error = ValueError("Last message is not from assistant")
        return TaskState.FAILED

    # Extract tool calls (OpenAI format)
    tool_calls = last_message.get("tool_calls", [])
    if not tool_calls:
        log.warning(
            "no_tool_calls_in_message",
            trace_id=ctx.trace_id,
            message="No tool calls found in assistant message, transitioning to synthesis",
        )
        return TaskState.SYNTHESIS

    log.info(
        STEP_EXECUTED,
        trace_id=ctx.trace_id,
        tool_count=len(tool_calls),
    )

    # Execute each tool call
    tool_results: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id", "")
        function_info = tool_call.get("function", {})
        tool_name = function_info.get("name", "")
        arguments_str = function_info.get("arguments", "{}")

        if not tool_name:
            log.warning(
                "tool_call_missing_name",
                trace_id=ctx.trace_id,
                tool_call_id=tool_call_id,
            )
            continue

        # Parse arguments JSON
        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError as e:
            log.error(
                "tool_call_invalid_arguments",
                trace_id=ctx.trace_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                error=str(e),
            )
            # Create error result with sanitized message
            sanitized_error = sanitize_error_message(e)
            tool_results.append(
                {
                    "tool_call_id": tool_call_id,
                    "role": "tool",
                    "name": tool_name,
                    "content": json.dumps({"error": f"Invalid arguments JSON: {sanitized_error}"}),
                }
            )
            continue

        # Repeat-call detection: prevent identical tool call signatures from looping
        try:
            args_signature = json.dumps(arguments, sort_keys=True)
        except TypeError:
            # Non-JSON-serializable args shouldn't happen; fall back to repr
            args_signature = repr(arguments)
        call_signature = f"{tool_name}:{args_signature}"
        repeats = ctx.tool_call_signatures.count(call_signature)
        if repeats >= settings.orchestrator_max_repeated_tool_calls:
            log.warning(
                "repeated_tool_call_blocked",
                trace_id=ctx.trace_id,
                tool_name=tool_name,
                repeats=repeats,
                max_repeats=settings.orchestrator_max_repeated_tool_calls,
            )
            tool_results.append(
                {
                    "tool_call_id": tool_call_id,
                    "role": "tool",
                    "name": tool_name,
                    "content": json.dumps(
                        {
                            "error": (
                                "Blocked repeated tool call to prevent a loop. "
                                "Use the previous tool result and provide a final answer."
                            )
                        }
                    ),
                }
            )
            continue
        ctx.tool_call_signatures.append(call_signature)

        # Validate required parameters before execution
        global _tool_registry
        if _tool_registry is None:
            _tool_registry = get_default_registry()
        tool_info = _tool_registry.get_tool(tool_name)
        if tool_info:
            tool_def, _ = tool_info
            required_params = [p.name for p in tool_def.parameters if p.required]
            missing_params = [
                p for p in required_params if p not in arguments or arguments[p] is None
            ]
            if missing_params:
                # Build detailed error with parameter descriptions and types
                param_details = []
                for param in tool_def.parameters:
                    if param.name in missing_params:
                        desc = param.description[:100] if param.description else "No description"
                        param_details.append(f"  - {param.name} ({param.type}): {desc}")
                error_msg = (
                    f"Missing required parameters for {tool_name}:\n"
                    + "\n".join(param_details)
                    + "\n\nPlease call this tool again with all required parameters."
                )
                log.warning(
                    "tool_call_missing_required_params",
                    trace_id=ctx.trace_id,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    missing_params=missing_params,
                    required_params=required_params,
                )
                tool_results.append(
                    {
                        "tool_call_id": tool_call_id,
                        "role": "tool",
                        "name": tool_name,
                        "content": json.dumps({"error": error_msg}),
                    }
                )
                continue

        # Execute tool
        try:
            result = await tool_layer.execute_tool(tool_name, arguments, trace_ctx)

            # Store result in tool_results list
            ctx.tool_results.append(
                {
                    "tool_name": tool_name,
                    "success": result.success,
                    "output": result.output,
                    "error": result.error,
                    "latency_ms": result.latency_ms,
                }
            )

            # Format result for LLM (OpenAI format)
            if result.success:
                content = (
                    json.dumps(result.output)
                    if isinstance(result.output, dict)
                    else str(result.output)
                )
            else:
                content = json.dumps({"error": result.error or "Tool execution failed"})

            tool_results.append(
                {
                    "tool_call_id": tool_call_id,
                    "role": "tool",
                    "name": tool_name,
                    "content": content,
                }
            )

            # Record step
            step: OrchestratorStep = {
                "type": "tool_call",
                "description": f"Executed tool: {tool_name}",
                "metadata": {
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "success": result.success,
                    "latency_ms": result.latency_ms,
                },
            }
            ctx.steps.append(step)

        except Exception as e:
            log.error(
                "tool_execution_exception",
                trace_id=ctx.trace_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                error=str(e),
                exc_info=True,
            )
            # Create error result with sanitized message
            sanitized_error = sanitize_error_message(e)
            tool_results.append(
                {
                    "tool_call_id": tool_call_id,
                    "role": "tool",
                    "name": tool_name,
                    "content": json.dumps({"error": sanitized_error}),
                }
            )

    # Append all tool results to messages
    ctx.messages.extend(tool_results)

    duration_ms = int((time.time() - step_start_time) * 1000)
    log.info(
        "tool_execution_completed",
        trace_id=ctx.trace_id,
        tool_count=len(tool_calls),
        duration_ms=duration_ms,
    )

    # Transition back to LLM_CALL for synthesis (use reasoning model for flexible analysis)
    # Synthesize using the same model that requested the tool(s) (fast for CODING, flexible for REASONING).
    # This avoids always paying the REASONING-model cost for simple tool-driven answers.
    last_llm_role: ModelRole | None = None
    for step in reversed(ctx.steps):
        if step.get("type") == "llm_call":
            role_str = (step.get("metadata") or {}).get("model_role")
            if isinstance(role_str, str):
                last_llm_role = ModelRole.from_str(role_str)
            break
    ctx.selected_model_role = last_llm_role or ModelRole.REASONING
    return TaskState.LLM_CALL


async def step_synthesis(
    ctx: ExecutionContext, session_manager: SessionManager, trace_ctx: TraceContext
) -> TaskState:
    """Finalize response.

    This step ensures the final reply is set and completes the task.

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Terminal state (COMPLETED).
    """
    # Ensure final reply is set (should already be set from LLM call)
    if not ctx.final_reply:
        ctx.final_reply = "Task completed"  # Fallback

    # Update session with new messages
    session_manager.update_session(ctx.session_id, messages=ctx.messages)

    return TaskState.COMPLETED


async def execute_task_safe(
    ctx: ExecutionContext, session_manager: SessionManager
) -> OrchestratorResult:
    """Wrapper with top-level error handling.

    This is the public entry point that ensures the orchestrator never
    raises exceptions. All errors are captured and returned as part of
    the OrchestratorResult.

    Args:
        ctx: Execution context.
        session_manager: Session manager.

    Returns:
        OrchestratorResult with reply, steps, and trace_id.
    """
    try:
        # Note: MCP initialization moved to CLI startup for singleton pattern
        ctx = await execute_task(ctx, session_manager)

        # Build result
        result: OrchestratorResult = {
            "reply": ctx.final_reply or "Task completed",
            "steps": ctx.steps,
            "trace_id": ctx.trace_id,
        }

        if ctx.error:
            sanitized_error = sanitize_error_message(ctx.error)
            result["reply"] = f"Error: {sanitized_error}"
            result["steps"].append(
                {
                    "type": "error",
                    "description": f"Task failed: {sanitized_error}",
                    "metadata": {"error_type": type(ctx.error).__name__},
                }
            )

        log.info(
            REPLY_READY,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            reply_length=len(result["reply"]),
        )
        return result

    except Exception as e:
        log.critical(
            ORCHESTRATOR_FATAL_ERROR,
            trace_id=ctx.trace_id,
            exc_info=True,
        )
        # Return error result with sanitized message
        sanitized_error = sanitize_error_message(e)
        log.info(
            REPLY_READY,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            reply_length=0,
            fatal_error=True,
        )
        return {
            "reply": "Critical error occurred. The agent is recovering.",
            "steps": [
                {
                    "type": "error",
                    "description": f"Fatal error: {sanitized_error}",
                    "metadata": {"error_type": type(e).__name__},
                }
            ],
            "trace_id": ctx.trace_id,
        }
