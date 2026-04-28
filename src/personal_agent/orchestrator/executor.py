"""Orchestrator execution loop and state machine.

This module implements the core orchestrator state machine with step functions.
The executor coordinates task execution through explicit state transitions.
"""

import asyncio
import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

from personal_agent.config import settings
from personal_agent.config.env_loader import Environment
from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator import compression_manager
from personal_agent.orchestrator.context_window import (
    apply_context_window,
    estimate_messages_tokens,
)
from personal_agent.orchestrator.loop_gate import (
    GateDecision,
    GateResult,
    ToolLoopPolicy,
    stable_hash,
)
from personal_agent.orchestrator.routing import is_memory_recall_query
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import (
    ExecutionContext,
    OrchestratorResult,
    OrchestratorStep,
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
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools import ToolExecutionLayer, get_default_registry
from personal_agent.tools.registry import ToolRegistry

log = get_logger(__name__)

# ── Tool loop gate helpers ─────────────────────────────────────────────────

_cached_governance_config: object = None


def _get_cached_governance_config() -> object:
    """Module-level governance config cache. TODO: replace with @lru_cache after config singleton."""
    global _cached_governance_config
    if _cached_governance_config is None:
        from personal_agent.config import load_governance_config  # noqa: PLC0415

        _cached_governance_config = load_governance_config()
    return _cached_governance_config


def _get_tool_loop_policy(tool_name: str) -> ToolLoopPolicy:
    """Returns loop policy for tool_name, or ToolLoopPolicy() defaults if not configured.

    Args:
        tool_name: The name of the tool to look up in governance config.

    Returns:
        ToolLoopPolicy with values from governance config, or defaults if not found.
    """
    try:
        gov_config = _get_cached_governance_config()
        tool_policy = gov_config.tools.get(tool_name)  # type: ignore[union-attr]
        if tool_policy is None:
            return ToolLoopPolicy()
        return ToolLoopPolicy(
            loop_max_per_signature=tool_policy.loop_max_per_signature,
            loop_max_consecutive=tool_policy.loop_max_consecutive,
            loop_output_sensitive=tool_policy.loop_output_sensitive,
            loop_consecutive_terminal=tool_policy.loop_consecutive_terminal,
        )
    except Exception:  # noqa: BLE001
        return ToolLoopPolicy()


def _resolve_max_iterations(ctx: "ExecutionContext") -> int:
    """Return the effective max-tool-iterations ceiling for this request.

    Uses the per-TaskType limit from settings when the gateway classified a
    task type, falling back to the global orchestrator_max_tool_iterations.
    Always respects the global ceiling as a hard upper bound.
    """
    global_max = settings.orchestrator_max_tool_iterations
    if ctx.gateway_output is not None:
        task_type_val = ctx.gateway_output.intent.task_type.value
        by_type = settings.orchestrator_max_tool_iterations_by_task_type
        if task_type_val in by_type:
            return min(by_type[task_type_val], global_max)
    return global_max


def _gate_blocked_result(
    tool_call_id: str,
    tool_name: str,
    gate_result: GateResult,
) -> dict[str, Any]:
    """Formats a tool result dict for gate-blocked calls.

    Args:
        tool_call_id: The tool call ID from the LLM response.
        tool_name: The name of the blocked tool.
        gate_result: The GateResult that triggered the block.

    Returns:
        A tool result dict suitable for appending to ctx.messages.
    """
    hints: dict[GateDecision, str] = {
        GateDecision.BLOCK_IDENTITY: (
            "Already retrieved these results. Use the previous tool output to answer."
        ),
        GateDecision.BLOCK_OUTPUT: (
            "Retrieved the same result before. Use the previous tool output to answer."
        ),
        GateDecision.BLOCK_CONSECUTIVE: (
            "Same tool called too many times consecutively without converging. "
            "Stop and synthesize from results gathered so far, or report what is missing."
        ),
    }
    return {
        "tool_call_id": tool_call_id,
        "role": "tool",
        "name": tool_name,
        "content": json.dumps(
            {
                "status": "done",
                "hint": hints.get(gate_result.decision, "Tool call blocked by loop gate."),
                "gate_decision": gate_result.decision.value,
            }
        ),
    }


# Entity type keywords for recall intent (ADR-0025) — map words to graph entity_type
_ENTITY_TYPE_KEYWORDS: dict[str, str] = {
    "location": "Location",
    "locations": "Location",
    "place": "Location",
    "places": "Location",
    "city": "Location",
    "cities": "Location",
    "country": "Location",
    "countries": "Location",
    "person": "Person",
    "people": "Person",
    "someone": "Person",
    "organization": "Organization",
    "org": "Organization",
    "company": "Organization",
    "companies": "Organization",
    "tool": "Technology",
    "tools": "Technology",
    "technology": "Technology",
    "topic": "Topic",
    "topics": "Topic",
    "concept": "Concept",
    "concepts": "Concept",
}


def _extract_entity_type_hints(user_message: str) -> list[str]:
    """Map words in the query to entity_type values (ADR-0025).

    e.g. "What Greek locations" -> ["Location"]
         "What tools have I used" -> ["Technology"]
         "What have I discussed" -> []
    """
    words = (user_message or "").lower().split()
    types: set[str] = set()
    for w in words:
        clean = w.strip('",.:;!?')
        if clean in _ENTITY_TYPE_KEYWORDS:
            types.add(_ENTITY_TYPE_KEYWORDS[clean])
    return list(types)


def _format_broad_recall(broad: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert query_memory_broad result to memory_context format (ADR-0025).

    The list is injected into the system prompt; keep it concise.
    """
    items: list[dict[str, Any]] = []
    for e in broad.get("entities", []):
        items.append(
            {
                "type": "entity",
                "name": e.get("name", ""),
                "entity_type": e.get("type", ""),
                "mentions": e.get("mentions", 0),
                "description": e.get("description") or "",
            }
        )
    for s in broad.get("sessions", []):
        items.append(
            {
                "type": "session",
                "session_id": s.get("session_id", ""),
                "dominant_entities": s.get("dominant_entities") or [],
                "turn_count": s.get("turn_count", 0),
            }
        )
    return items


# Global tool registry instance (initialized on first use)
_tool_registry: ToolRegistry | None = None
_tool_execution_layer: ToolExecutionLayer | None = None

if TYPE_CHECKING:  # pragma: no cover
    from personal_agent.mcp.gateway import MCPGatewayAdapter

_mcp_adapter: "MCPGatewayAdapter | None" = None


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
            "I couldn't produce a final answer. Try rephrasing your request or being more specific. "
            "For questions about recent errors or failures, I can query my telemetry using the "
            "self_telemetry_query tool with query_type='events', event='model_call_error' or "
            "'task_failed', and a time window (e.g. window='1h')."
        )

    last_results = ctx.tool_results[-3:]
    lines: list[str] = [
        "I reached my tool-use limit before completing a synthesis. Here are the latest tool results:"
    ]
    for r in last_results:
        tool_name = r.get("tool_name", "unknown_tool")
        success = r.get("success", False)
        if success:
            lines.append(f"- {tool_name}: success")
        else:
            err = r.get("error") or "Unknown error"
            lines.append(f"- {tool_name}: failed ({err})")
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
        from personal_agent.mcp.gateway import (
            MCPGatewayAdapter,
            get_active_mcp_gateway_adapter,
        )

        # Get or create registry
        global _tool_registry
        if _tool_registry is None:
            _tool_registry = get_default_registry()

        existing = get_active_mcp_gateway_adapter()
        if existing is not None and getattr(existing, "client", None) is not None:
            _mcp_adapter = existing
            log.info(
                "mcp_gateway_reusing_existing_adapter",
                tools_count=len(existing._mcp_tool_names),
            )
            return

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

    All channels route to PRIMARY (ADR-0033). Coding tasks no longer have a
    dedicated local model role — the primary agent decides whether to handle
    directly or delegate via DelegationPackage (Slice 3).

    Args:
        ctx: Execution context.

    Returns:
        Initial model role to use.
    """
    return ModelRole.PRIMARY


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
        from personal_agent.brainstem.sensors.metrics_daemon import get_global_metrics_daemon
        from personal_agent.brainstem.sensors.request_monitor import RequestMonitor

        daemon = get_global_metrics_daemon()
        if daemon is None:
            log.warning("request_monitor_skipped_no_metrics_daemon", trace_id=ctx.trace_id)
        else:
            monitor = RequestMonitor(trace_id=ctx.trace_id, daemon=daemon)
        try:
            if monitor is not None:
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

    previous_state: TaskState | None = None
    try:
        while state not in {TaskState.COMPLETED, TaskState.FAILED}:
            log.info(
                STATE_TRANSITION,
                trace_id=ctx.trace_id,
                from_state=(previous_state.value if previous_state is not None else state.value),
                to_state=state.value,
                component="executor",
            )
            ctx.state = state
            previous_state = state

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

                # Extract tools used and accumulate token counts from steps
                tools_used = []
                cap_prompt_tokens = 0
                cap_completion_tokens = 0
                cap_total_tokens = 0
                for step in ctx.steps:
                    if step.get("type") == "tool_call":
                        tool_name = (step.get("metadata") or {}).get("tool_name")
                        if tool_name:
                            tools_used.append(tool_name)
                    elif step.get("type") == "llm_call":
                        meta = step.get("metadata") or {}
                        cap_prompt_tokens += meta.get("prompt_tokens", 0)
                        cap_completion_tokens += meta.get("completion_tokens", 0)
                        cap_total_tokens += meta.get("tokens", 0)

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
                    prompt_tokens=cap_prompt_tokens,
                    completion_tokens=cap_completion_tokens,
                    total_tokens=cap_total_tokens,
                    tool_results=ctx.tool_results,
                    user_id=getattr(ctx, "user_id", None),
                )
                write_capture(capture)

                # Publish request.captured event (ADR-0041)
                from personal_agent.captains_log.background import (
                    run_in_background as _run_bg,
                )
                from personal_agent.events.bus import get_event_bus
                from personal_agent.events.models import (
                    STREAM_REQUEST_CAPTURED,
                    RequestCapturedEvent,
                )

                event = RequestCapturedEvent(
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    source_component="orchestrator.executor",
                )
                _run_bg(get_event_bus().publish(STREAM_REQUEST_CAPTURED, event))
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
            error=str(e),
            error_type=type(e).__name__,
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
    timer = ctx.request_timer

    # Load session and build message history
    session_message_count = 0
    if timer:
        timer.start_span("session_history_load")
    try:
        session = session_manager.get_session(ctx.session_id)
        if session:
            ctx.messages = list(session.messages)
            session_message_count = len(ctx.messages)
    finally:
        if timer:
            timer.end_span("session_history_load", message_count=session_message_count)

    # Add new user message
    ctx.messages.append({"role": "user", "content": ctx.user_message})

    # --- Gateway-driven path: skip inline routing and memory ---
    if ctx.gateway_output is not None:
        gw = ctx.gateway_output
        # Use pre-assembled memory context
        if gw.context.memory_context:
            ctx.memory_context = gw.context.memory_context
            log.info(
                "memory_enrichment_completed",
                trace_id=ctx.trace_id,
                conversations_found=len(gw.context.memory_context),
            )
        log.info(
            "step_init_gateway_path",
            trace_id=ctx.trace_id,
            task_type=gw.intent.task_type.value,
            complexity=gw.intent.complexity.value,
            has_memory=gw.context.memory_context is not None,
        )
        if gw.intent.task_type.value == "memory_recall":
            # Gateway path returns early, so emit broad-recall telemetry here.
            # This keeps CP-26 observable even when inline memory query is skipped.
            log.info(
                "memory_recall_broad_query",
                trace_id=ctx.trace_id,
                entity_type_hints=_extract_entity_type_hints(ctx.user_message),
                entities_found=len(gw.context.memory_context or []),
                source="gateway_context",
            )
        from personal_agent.request_gateway.types import DecompositionStrategy

        if gw.decomposition.strategy == DecompositionStrategy.DELEGATE:
            from personal_agent.request_gateway.delegation import compose_delegation_package

            # Build memory excerpt and pitfalls from gateway context
            mem_items = gw.context.memory_context or []
            memory_excerpt: list[dict[str, str | float]] = [
                {
                    "type": str(item.get("type", "episode")),
                    "summary": str(
                        item.get("summary") or item.get("description") or item.get("name", "")
                    ),
                }
                for item in mem_items[:5]
            ]
            known_pitfalls: list[str] = [
                str(item.get("summary") or item.get("description") or "")
                for item in mem_items
                if item.get("type") == "episode"
            ][:3]

            # Extract acceptance criteria from user message using "with X, Y, Z" split
            raw = ctx.user_message
            acceptance_criteria: list[str] = []
            if " with " in raw.lower():
                after_with = raw[raw.lower().index(" with ") + 6 :]
                parts = [
                    p.strip().rstrip(".,;") for p in after_with.replace(" and ", ",").split(",")
                ]
                acceptance_criteria = [p for p in parts if len(p) > 3][:5]
            if not acceptance_criteria:
                acceptance_criteria = ["Implementation meets requirements described in the task"]

            relevant_files: list[str] = []
            for word in raw.split():
                stripped = word.strip('",.:;!?()')
                if "/" in stripped and stripped.startswith("src/"):
                    relevant_files.append(stripped)

            compose_delegation_package(
                task_description=ctx.user_message,
                trace_id=ctx.trace_id,
                acceptance_criteria=acceptance_criteria,
                known_pitfalls=known_pitfalls or None,
                memory_excerpt=memory_excerpt or None,
                relevant_files=relevant_files or None,
            )
            # Fall through to LLM call — primary agent responds with delegation package

        elif gw.decomposition.strategy in (
            DecompositionStrategy.HYBRID,
            DecompositionStrategy.DECOMPOSE,
        ):
            ctx.expansion_strategy = gw.decomposition.strategy.value
            ctx.expansion_constraints = gw.decomposition.constraints or {}

            if settings.orchestration_mode == "enforced":
                from personal_agent.llm_client.factory import get_llm_client
                from personal_agent.orchestrator.expansion_controller import (
                    ExpansionController,
                )

                llm_client = get_llm_client(role_name=ModelRole.PRIMARY.value)
                controller = ExpansionController()
                expansion_result = await controller.execute(
                    query=ctx.messages[-1].get("content", "") if ctx.messages else "",
                    strategy=gw.decomposition.strategy.value.upper(),
                    llm_client=llm_client,
                    trace_id=ctx.trace_id,
                    messages=ctx.messages,
                    constraints=ctx.expansion_constraints,
                )

                ctx.expansion_plan = expansion_result.plan
                ctx.sub_agent_results = expansion_result.sub_agent_results
                ctx.expansion_phase_results = expansion_result.phase_results

                # Build synthesis context and append to messages
                if expansion_result.sub_agent_results:
                    synthesis_msg = {
                        "role": "user",
                        "content": (
                            f"{expansion_result.synthesis_context}\n"
                            "The sub-tasks above have been completed. "
                            "Synthesize the results into a coherent response "
                            "for the user's original question."
                        ),
                    }
                    ctx.messages.append(synthesis_msg)

                log.info(
                    "expansion_controller_complete",
                    mode="enforced",
                    plan_is_fallback=expansion_result.plan.is_fallback
                    if expansion_result.plan
                    else None,
                    sub_agent_count=len(expansion_result.sub_agent_results),
                    successful=expansion_result.successful_count,
                    degraded=expansion_result.degraded,
                    trace_id=ctx.trace_id,
                )

                # Go directly to synthesis LLM call
                return TaskState.LLM_CALL

            # Autonomous mode — existing behavior
            log.info(
                "step_init_expansion_flagged",
                mode="autonomous",
                strategy=gw.decomposition.strategy.value,
                constraints=gw.decomposition.constraints,
                trace_id=ctx.trace_id,
            )
        return TaskState.LLM_CALL

    # Apply context window controls before LLM usage to prevent overflow.
    input_messages_count = len(ctx.messages)
    estimated_tokens = 0
    if timer:
        timer.start_span("context_window")
    try:
        # Retrieve pre-computed compression summary if available (ADR-0038).
        _summary = compression_manager.get_summary(ctx.session_id) if ctx.session_id else None

        ctx.messages = apply_context_window(
            ctx.messages,
            max_tokens=settings.context_window_max_tokens,
            strategy=settings.conversation_context_strategy,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            compressed_summary=_summary,
        )
        estimated_tokens = estimate_messages_tokens(ctx.messages)
    finally:
        if timer:
            timer.end_span(
                "context_window",
                messages_in=input_messages_count,
                messages_out=len(ctx.messages),
                estimated_tokens=estimated_tokens,
            )

    log.info(
        "conversation_context_loaded",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        total_messages_in_db=session_message_count,
        messages_loaded=len(ctx.messages),
        messages_truncated=max(0, input_messages_count - len(ctx.messages)),
        estimated_tokens=estimated_tokens,
    )

    # Query memory graph for relevant context (Phase 2.2)
    if settings.enable_memory_graph:
        if timer:
            timer.start_span("memory_query")
        try:
            from personal_agent.memory.models import MemoryQuery
            from personal_agent.memory.service import MemoryService

            memory_service = None
            global_memory_service = None
            try:
                from personal_agent.service.app import memory_service as global_memory_service

                if global_memory_service and global_memory_service.connected:
                    memory_service = global_memory_service
            except (ImportError, AttributeError):
                memory_service = MemoryService()
                await memory_service.connect()

            if memory_service and memory_service.connected:
                conversations_found = 0

                potential_entities: list[str] = []
                if is_memory_recall_query(ctx.user_message):
                    # Broad recall path (ADR-0025): no entity names to match
                    entity_type_hints = _extract_entity_type_hints(ctx.user_message)
                    try:
                        broad = await memory_service.query_memory_broad(
                            entity_types=entity_type_hints or None,
                            recency_days=90,
                            limit=20,
                        )
                        ctx.memory_context = _format_broad_recall(broad)
                        conversations_found = len(ctx.memory_context)
                        log.info(
                            "memory_recall_broad_query",
                            trace_id=ctx.trace_id,
                            entity_type_hints=entity_type_hints,
                            entities_found=len(broad.get("entities", [])),
                        )
                    except Exception as broad_err:
                        log.warning(
                            "memory_recall_broad_query_failed",
                            trace_id=ctx.trace_id,
                            error=str(broad_err),
                        )
                        log.info(
                            "memory_recall_broad_query",
                            trace_id=ctx.trace_id,
                            entity_type_hints=entity_type_hints,
                            entities_found=0,
                            query_error=str(broad_err),
                        )
                else:
                    # Entity-name match path (existing)
                    words = ctx.user_message.split()
                    potential_entities = [
                        w.strip('",.:;!?') for w in words if len(w) > 3 and w[0].isupper()
                    ]
                    if potential_entities:
                        query = MemoryQuery(
                            entity_names=potential_entities[:5],
                            limit=5,
                            recency_days=30,
                        )
                        result = await memory_service.query_memory(
                            query,
                            feedback_key=ctx.session_id,
                            query_text=ctx.user_message,
                        )
                        ctx.memory_context = [
                            {
                                "conversation_id": conv.turn_id,
                                "timestamp": conv.timestamp.isoformat(),
                                "user_message": conv.user_message,
                                "summary": conv.summary or conv.user_message[:200],
                                "key_entities": conv.key_entities,
                            }
                            for conv in result.conversations
                        ]
                        conversations_found = len(ctx.memory_context)
                        log.info(
                            "memory_enrichment_completed",
                            trace_id=ctx.trace_id,
                            conversations_found=conversations_found,
                        )

                if memory_service != global_memory_service:
                    await memory_service.disconnect()

                if timer:
                    timer.end_span(
                        "memory_query",
                        entities_searched=len(potential_entities) if potential_entities else 0,
                        conversations_found=conversations_found,
                    )
            elif is_memory_recall_query(ctx.user_message):
                # Broad recall intent without a connected MemoryService (e.g. Neo4j
                # used only by second_brain). Still emit telemetry so eval/harness
                # can observe the recall path (ADR-0025).
                log.info(
                    "memory_recall_broad_query",
                    trace_id=ctx.trace_id,
                    entity_type_hints=_extract_entity_type_hints(ctx.user_message),
                    entities_found=0,
                    skipped_reason="memory_service_unavailable",
                )
                if timer:
                    timer.end_span(
                        "memory_query",
                        entities_searched=0,
                        conversations_found=0,
                    )
            else:
                # Memory graph enabled but service not connected and not a recall-only path.
                if timer:
                    timer.end_span(
                        "memory_query",
                        entities_searched=0,
                        conversations_found=0,
                        skipped_reason="memory_service_unavailable",
                    )
        except Exception as e:
            if timer:
                timer.end_span("memory_query", error=str(e))
            log.warning(
                "memory_enrichment_failed",
                trace_id=ctx.trace_id,
                error=str(e),
                exc_info=True,
            )

    needs_planning = False

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
    """Execute LLM call with the primary model.

    All requests use the PRIMARY model (ADR-0033 two-tier taxonomy).
    Intent classification is handled by the Pre-LLM Gateway; this step
    executes the call and proceeds to TOOL_EXECUTION or SYNTHESIS.

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Next state (TOOL_EXECUTION, SYNTHESIS, or FAILED).
    """
    timer = ctx.request_timer
    llm_span_name: str | None = None  # set once span is started; used to close span on exception

    # Determine which model to call
    if ctx.gateway_output is not None and ctx.selected_model_role is None:
        # Gateway-driven path: always use PRIMARY role (ADR-0033)
        model_role = ModelRole.PRIMARY
        ctx.selected_model_role = model_role
        log.info(
            "step_llm_call_gateway_model",
            trace_id=ctx.trace_id,
            model_role=model_role.value,
            task_type=ctx.gateway_output.intent.task_type.value,
        )
    elif ctx.selected_model_role is None:
        # First LLM call: always PRIMARY (ADR-0033)
        model_role = _determine_initial_model_role(ctx)
    else:
        # Continuation — use previously selected role
        model_role = ctx.selected_model_role

    system_prompt: str | None = None

    # Inject deployment context so the model doesn't try to access host-only paths.
    # Tool-name hints are appended later, only when tools are actually being passed
    # — otherwise the model sees named tools it can't call and hallucinates pseudo-code.
    if settings.environment == Environment.PRODUCTION:
        system_prompt = (
            "## Deployment Context\n"
            "You are running inside a Docker container on a cloud VPS.\n"
            "- App code is at `/app` — the host path `/opt/seshat` is the host mount point and is NOT accessible from here\n"
            "- Configuration is injected as environment variables at startup; there is no `.env` file inside the container\n"
            "- Do NOT search for files at `/opt/seshat`, `/home/debian`, or other host paths — they do not exist inside the container\n"
            "- All backend services are reachable via Docker internal DNS:\n"
            "    postgres:5432  |  neo4j:7687 (bolt) / neo4j:7474 (HTTP)  |  elasticsearch:9200\n"
            "    redis:6379  |  embeddings:8503  |  reranker:8504"
        )

    # Inject skill library docs when prefer_primitives_enabled is set (ADR-0063 §D7).
    # Placed before dynamic content (memory/decomposition) to stay in the cached prefix.
    from personal_agent.orchestrator.skills import get_skill_block

    skill_block = get_skill_block()
    if skill_block:
        if system_prompt:
            system_prompt = f"{system_prompt}\n\n{skill_block}"
        else:
            system_prompt = skill_block

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
        # Create LLM client — dispatches to LocalLLMClient or LiteLLMClient based on provider_type
        from personal_agent.llm_client.factory import get_llm_client

        llm_client = get_llm_client(role_name=model_role.value)

        # Get tools for this model role and mode
        # ReAct loop: always offer tools so the model can chain calls until it
        # decides to synthesize on its own.  Bounded by orchestrator_max_tool_iterations
        # in step_tool_execution, which forces TaskState.SYNTHESIS when the limit is hit.
        is_synthesizing = False

        # ── Strategy-aware tool setup (ADR-0032) ──────────────────────
        from personal_agent.config.profile import resolve_model_key
        from personal_agent.llm_client.models import ToolCallingStrategy

        model_config = llm_client.model_configs.get(resolve_model_key(model_role.value))
        tool_strategy = (
            model_config.effective_tool_strategy if model_config else ToolCallingStrategy.NATIVE
        )

        tools: list[dict[str, Any]] | None = None
        _prompt_injected_tool_text: str | None = None  # filled for PROMPT_INJECTED only

        # Forced synthesis: iteration limit fired — disable tools and inject a synthesis prompt
        # so the LLM produces a real answer from gathered results instead of a useless fallback.
        if ctx.force_synthesis_from_limit:
            ctx.force_synthesis_from_limit = False
            is_synthesizing = True
            ctx.messages.append(
                {
                    "role": "user",
                    "content": (
                        "You have reached the tool call limit. "
                        "Do NOT call any more tools. "
                        "Using only the tool results already in this conversation, "
                        "synthesize a complete, helpful answer to the user's original request."
                    ),
                }
            )
            log.info(
                "force_synthesis_injected",
                trace_id=ctx.trace_id,
                iteration=ctx.tool_iteration_count,
            )

        # Budget warning: when 2 calls from the per-TaskType limit, ask the LLM to wrap up
        elif not is_synthesizing and ctx.tool_iteration_count >= _resolve_max_iterations(ctx) - 2:
            _effective_max = _resolve_max_iterations(ctx)
            ctx.messages.append(
                {
                    "role": "user",
                    "content": (
                        f"⚠️ Tool budget: {_effective_max - ctx.tool_iteration_count} "
                        "tool call(s) remaining. Prioritize synthesis — only make additional tool calls "
                        "if they are strictly necessary to answer the user's question."
                    ),
                }
            )
            log.info(
                "tool_budget_warning_injected",
                trace_id=ctx.trace_id,
                remaining=_effective_max - ctx.tool_iteration_count,
            )

        if not is_synthesizing and tool_strategy != ToolCallingStrategy.DISABLED:
            # Load tool definitions from registry
            global _tool_registry
            if _tool_registry is None:
                _tool_registry = get_default_registry()

            # Per ADR-0063 §D1 (FRE-260), governance is mode-only — the
            # TaskType→tool-filter wire is severed. Every turn sees every tool
            # the active mode allows.
            tool_defs = _tool_registry.get_tool_definitions_for_llm(mode=ctx.mode)

            if tool_strategy == ToolCallingStrategy.NATIVE:
                # Pass tools in the API request — model uses native function calling
                tools = tool_defs if tool_defs else None
            elif tool_strategy == ToolCallingStrategy.PROMPT_INJECTED:
                # Render tools as text for the system prompt instead of the API parameter.
                # The model's chat template doesn't support the tools array.
                from personal_agent.llm_client.tool_prompt_renderer import render_tools_for_prompt

                _prompt_injected_tool_text = render_tools_for_prompt(tool_defs)
                tools = None  # do NOT send tools array in the API request

            log.debug(
                "tools_passed_to_llm",
                trace_id=ctx.trace_id,
                model_role=model_role.value,
                tool_strategy=tool_strategy.value,
                tool_count=len(tool_defs) if tool_defs else 0,
                tool_names=[t.get("function", {}).get("name") for t in (tool_defs or [])],
                mode=ctx.mode.value,
                prompt_injected=(_prompt_injected_tool_text is not None),
            )
        else:
            log.debug(
                "tools_not_passed",
                trace_id=ctx.trace_id,
                model_role=model_role.value,
                tool_strategy=tool_strategy.value,
                reason="synthesizing" if is_synthesizing else "disabled",
            )

        # Add memory context to system prompt (Phase 2.2, ADR-0025 broad recall)
        if ctx.memory_context and len(ctx.memory_context) > 0:
            if ctx.memory_context[0].get("type") in ("entity", "session"):
                # Broad recall path — format as direct knowledge summary
                entity_items = [m for m in ctx.memory_context if m.get("type") == "entity"]
                entity_lines = [
                    f"- [{m.get('entity_type', '')}] {m.get('name', '')}: {m.get('description', '')} "
                    f"(mentioned {m.get('mentions', 1)}x)"
                    for m in entity_items[:15]
                ]
                memory_section = "\n\n## Your Memory Graph — Known Entities\n"
                memory_section += "\n".join(entity_lines)
                memory_section += (
                    "\n\nUse this list to directly answer questions about what the user "
                    "has previously discussed. Do NOT say you have no memory."
                )
            else:
                # Task-assist path — inject conversation summaries
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

        # If we are passing tools (native or prompt-injected), include tool-use guidance
        # in the system prompt to reduce malformed tool calls and looping (ADR-0032).
        if tools or _prompt_injected_tool_text:
            from personal_agent.orchestrator.prompts import (
                TOOL_USE_NATIVE_PROMPT,
                TOOL_USE_PROMPT_INJECTED,
                get_tool_awareness_prompt,
            )

            # Select the prompt variant that matches the strategy
            if tool_strategy == ToolCallingStrategy.PROMPT_INJECTED:
                tool_prompt = TOOL_USE_PROMPT_INJECTED
                # Append the rendered tool definitions after the behavioural prompt
                tool_prompt = f"{tool_prompt}\n{_prompt_injected_tool_text}"
            else:
                tool_prompt = TOOL_USE_NATIVE_PROMPT

            # Add tool awareness so agent can answer questions about its capabilities
            tool_awareness = get_tool_awareness_prompt()

            # Production-only: append deployment-specific tool hints, filtered to
            # only mention tools that are actually available this turn. Naming a
            # tool the model can't call teaches it to hallucinate pseudo-code.
            deployment_tool_hints = ""
            if settings.environment == Environment.PRODUCTION:
                _available_tool_names = {
                    (t.get("function", {}).get("name") or "") for t in (tools or [])
                }
                _hint_map = {
                    "run_sysdiag": "- Use `run_sysdiag` to inspect the container filesystem starting at `/app`",
                    "infra_health": "- Use `infra_health` to check connectivity and health of all backend services at once",
                    "self_telemetry_query": "- Use `self_telemetry_query` to inspect logs, errors, and execution history",
                    "search_memory": "- Use `search_memory` to query the knowledge graph",
                    "query_elasticsearch": "- Use `query_elasticsearch` to query trace data",
                }
                _hint_lines = [
                    hint for name, hint in _hint_map.items() if name in _available_tool_names
                ]
                if _hint_lines:
                    deployment_tool_hints = "\n\n## Deployment Tools\n" + "\n".join(_hint_lines)

            if system_prompt:
                system_prompt = (
                    f"{tool_awareness}\n\n{system_prompt}{deployment_tool_hints}\n\n{tool_prompt}"
                )
            else:
                system_prompt = f"{tool_awareness}{deployment_tool_hints}\n\n{tool_prompt}"

        # HYBRID decomposition prompt (autonomous mode only — enforced mode
        # uses the expansion controller which has already run by this point).
        if (
            ctx.expansion_strategy is not None
            and ctx.sub_agent_results is None
            and settings.orchestration_mode == "autonomous"
        ):
            hybrid_prompt = (
                "\n\n## Decomposition Instructions\n"
                "Break your response into a numbered list of independent sub-tasks "
                "(1. ..., 2. ..., 3. ...). Each item should be a self-contained "
                "task that can be researched or answered independently. "
                "Keep to 2-4 sub-tasks. After the sub-tasks complete, you will "
                "synthesize their results into a final answer."
            )
            if system_prompt:
                system_prompt = f"{system_prompt}{hybrid_prompt}"
            else:
                system_prompt = hybrid_prompt.strip()

        # Call LocalLLMClient.respond()
        # Pass previous_response_id for stateful /v1/responses API
        max_retries_override: int | None = 1 if tools else None

        # /no_think injection for tool flow (per user preference):
        # - Tool-request call: append suffix to the last user message.
        # - Post-tool synthesis: append a short user nudge ending with the suffix (tool outputs are last).
        #   IMPORTANT: Skip synthesis nudge for Mistral models - they expect direct synthesis after tool results
        #   Note: We always inject the suffix when tools are present. LM Studio ignores extra_body
        #   chat_template_kwargs, so the suffix is the only working thinking control for Qwen3.5.
        request_messages = ctx.messages

        if tools:
            request_messages = _append_no_think_to_last_user_message(request_messages)

        # Validate and fix conversation role alternation for strict models (e.g., Mistral).
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
        # Timer span
        llm_span_name = f"llm_call:{model_role.value}"
        if timer:
            timer.start_span(llm_span_name)

        from personal_agent.llm_client.concurrency import InferencePriority

        response = await llm_client.respond(
            role=model_role,
            messages=request_messages,
            system_prompt=system_prompt,
            tools=tools if tools else None,
            trace_ctx=span_ctx,
            previous_response_id=ctx.last_response_id,
            max_retries=max_retries_override,
            priority=InferencePriority.USER_FACING,
        )

        # Extract response content and tool calls
        response_content = response["content"] or ""
        response_tool_calls = response["tool_calls"] or []

        # Track response_id for stateful /v1/responses API
        if response.get("response_id"):
            ctx.last_response_id = response["response_id"]

        duration_ms = int((time.time() - step_start_time) * 1000)
        total_tokens = response.get("usage", {}).get("total_tokens", 0)
        prompt_tokens = response.get("usage", {}).get("prompt_tokens", 0)
        completion_tokens = response.get("usage", {}).get("completion_tokens", 0)

        if timer:
            timer.end_span(
                llm_span_name,
                model_role=model_role.value,
                tokens=total_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        log.info(
            MODEL_CALL_COMPLETED,
            trace_id=ctx.trace_id,
            span_id=span_id,
            duration_ms=duration_ms,
            model_role=model_role.value,
            tokens=total_tokens,
        )
        # Record step
        step: OrchestratorStep = {
            "type": "llm_call",
            "description": f"LLM call with {model_role.value} model",
            "metadata": {
                "model_role": model_role.value,
                "span_id": span_id,
                "duration_ms": duration_ms,
                "tokens": total_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        }
        ctx.steps.append(step)

        # Some reasoning models may emit router-style JSON with a `response` field.
        # Unwrap it to avoid returning JSON to the user.
        response_content = _unwrap_embedded_response_json(response_content)

        # --- HYBRID expansion hook (autonomous mode only) ---
        if (
            ctx.expansion_strategy is not None
            and ctx.sub_agent_results is None
            and settings.orchestration_mode == "autonomous"
        ):
            from personal_agent.orchestrator.expansion import (
                execute_hybrid,
                parse_decomposition_plan,
            )

            max_sub = (ctx.expansion_constraints or {}).get("max_sub_agents", 3)
            specs = parse_decomposition_plan(
                plan_text=response_content,
                max_sub_agents=max_sub,
            )

            if specs:
                results = await execute_hybrid(
                    specs=specs,
                    trace_id=ctx.trace_id,
                    max_concurrent=max_sub,
                )
                ctx.sub_agent_results = results

                # Build synthesis context and append to messages
                synthesis_parts = ["Sub-agent results:\n"]
                for r in results:
                    status = "OK" if r.success else f"FAILED: {r.error}"
                    synthesis_parts.append(f"- {r.spec_task}: [{status}] {r.summary}\n")
                synthesis_context = "".join(synthesis_parts)

                synthesis_msg = {
                    "role": "user",
                    "content": (
                        f"{synthesis_context}\n"
                        "The sub-tasks above have been completed. "
                        "Synthesize the results into a coherent response "
                        "for the user's original question."
                    ),
                }
                ctx.messages.append({"role": "assistant", "content": response_content})
                ctx.messages.append(synthesis_msg)

                log.info(
                    "expansion_phase1_complete",
                    sub_agent_count=len(results),
                    successful=sum(1 for r in results if r.success),
                    trace_id=ctx.trace_id,
                )

                # Re-enter LLM_CALL for synthesis (phase 2)
                return TaskState.LLM_CALL

            # No parseable specs — fall through to normal response path
            log.warning(
                "expansion_no_specs_parsed",
                strategy=ctx.expansion_strategy,
                trace_id=ctx.trace_id,
            )
        # --- End HYBRID expansion hook ---

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
        if timer and llm_span_name:
            timer.end_span(
                llm_span_name,
                model_role=model_role.value,
                error=str(e),
                error_type=type(e).__name__,
            )
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
            "metadata": {
                "error": sanitized_error,
                "error_type": type(e).__name__,
                "span_id": span_id,
            },
        }
        ctx.steps.append(error_step)
        return TaskState.FAILED


async def _dispatch_tool_call(
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    args_hash: str,
    gate_result: "GateResult",
    loop_policy: "ToolLoopPolicy",
    tool_layer: "ToolExecutionLayer",
    ctx: "ExecutionContext",
    trace_ctx: "TraceContext",
) -> dict[str, Any]:
    """Execute one validated, gate-allowed tool call and return its result payload.

    This coroutine is the Phase-2 body for the asyncio.gather dispatch in
    step_tool_execution. It handles param validation, tool execution, and error
    formatting, but does NOT mutate ctx or the loop gate — those mutations happen
    sequentially in Phase 3 to preserve gate-FSM and ordering invariants.

    Returns a plain dict with keys:
        tool_call_id, tool_name, content, success, latency_ms,
        output_hash (None on error), gate_result, args_hash, loop_policy,
        tool_layer_output, tool_layer_error
    """
    # Validate required parameters
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = get_default_registry()
    tool_info = _tool_registry.get_tool(tool_name)
    if tool_info:
        tool_def, _ = tool_info
        required_params = [p.name for p in tool_def.parameters if p.required]
        missing_params = [p for p in required_params if p not in arguments or arguments[p] is None]
        if missing_params:
            log.warning(
                "tool_call_missing_required_params",
                trace_id=ctx.trace_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                missing_params=missing_params,
                required_params=required_params,
            )
            return {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "content": json.dumps(
                    {
                        "status": "retry",
                        "hint": f"Missing required params: {', '.join(missing_params)}. Retry with these included.",
                    }
                ),
                "success": False,
                "latency_ms": 0,
                "output_hash": None,
                "gate_result": gate_result,
                "args_hash": args_hash,
                "loop_policy": loop_policy,
                "tool_layer_output": None,
                "tool_layer_error": "missing_required_params",
            }

    # Execute tool
    try:
        result = await tool_layer.execute_tool(
            tool_name, arguments, trace_ctx, session_id=ctx.session_id
        )

        if result.success:
            content = (
                json.dumps(result.output) if isinstance(result.output, dict) else str(result.output)
            )
            output_hash: str | None = stable_hash(result.output)
        else:
            short_error = (result.error or "execution failed")[:150]
            content = json.dumps({"status": "error", "hint": f"{tool_name}: {short_error}"})
            output_hash = None

        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "content": content,
            "success": result.success,
            "latency_ms": result.latency_ms,
            "output_hash": output_hash,
            "gate_result": gate_result,
            "args_hash": args_hash,
            "loop_policy": loop_policy,
            "tool_layer_output": result.output,
            "tool_layer_error": result.error,
        }

    except Exception as e:
        log.error(
            "tool_execution_exception",
            trace_id=ctx.trace_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            error=str(e),
            exc_info=True,
        )
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "content": json.dumps(
                {
                    "status": "error",
                    "hint": f"{tool_name} failed to execute. Try a different approach or tool.",
                }
            ),
            "success": False,
            "latency_ms": 0,
            "output_hash": None,
            "gate_result": gate_result,
            "args_hash": args_hash,
            "loop_policy": loop_policy,
            "tool_layer_output": None,
            "tool_layer_error": str(e),
        }


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
    timer = ctx.request_timer
    step_start_time = time.time()

    tool_span_name: str | None = None
    if timer:
        tool_span_name = f"tool_execution:{ctx.tool_iteration_count + 1}"
        timer.start_span(tool_span_name)

    # Loop governance: prevent infinite tool execution cycles
    ctx.tool_iteration_count += 1
    _max_iters = _resolve_max_iterations(ctx)
    if ctx.tool_iteration_count > _max_iters:
        if timer and tool_span_name:
            timer.end_span(
                tool_span_name,
                reason="iteration_limit",
                iteration=ctx.tool_iteration_count,
            )
        log.warning(
            "tool_iteration_limit_reached",
            trace_id=ctx.trace_id,
            iteration=ctx.tool_iteration_count,
            max_iterations=_max_iters,
        )
        ctx.steps.append(
            {
                "type": "warning",
                "description": "Tool loop limit reached; forcing LLM synthesis pass",
                "metadata": {
                    "iteration": ctx.tool_iteration_count,
                    "max_iterations": _max_iters,
                },
            }
        )
        # Route back to LLM_CALL with tools disabled so the model synthesizes
        # from all gathered results rather than returning a useless fallback.
        ctx.force_synthesis_from_limit = True
        return TaskState.LLM_CALL

    # Get tool execution layer
    try:
        tool_layer = _get_tool_execution_layer()
    except Exception as e:
        if timer and tool_span_name:
            timer.end_span(tool_span_name, error=str(e), error_type=type(e).__name__)
        raise

    # Extract tool calls from the last assistant message
    if not ctx.messages:
        if timer and tool_span_name:
            timer.end_span(tool_span_name, error="no_messages_for_tool_execution")
        log.error(
            "no_messages_for_tool_execution",
            trace_id=ctx.trace_id,
            error="No messages in context to extract tool calls from",
        )
        ctx.error = ValueError("No messages in context to extract tool calls from")
        return TaskState.FAILED

    last_message = ctx.messages[-1]
    if last_message.get("role") != "assistant":
        if timer and tool_span_name:
            timer.end_span(tool_span_name, error="last_message_not_assistant")
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
        if timer and tool_span_name:
            timer.end_span(tool_span_name, reason="no_tool_calls_in_message")
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

    # ── Phase 1: Sequential gate check ────────────────────────────────────────
    # Gate FSM mutations must be sequential so call-count and consecutive-count
    # thresholds are correct before any I/O is dispatched (ADR-0062).
    tool_results: list[dict[str, Any]] = []  # blocked + error results (immediate)
    allowed_plans: list[dict[str, Any]] = []  # tool calls cleared for async dispatch

    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id", "")
        function_info = tool_call.get("function", {})
        tool_name = function_info.get("name", "")
        arguments_str = function_info.get("arguments", "{}")

        if not tool_name:
            log.warning("tool_call_missing_name", trace_id=ctx.trace_id, tool_call_id=tool_call_id)
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
            # Concise, neutral error — avoids poisoning the model's confidence
            # in tool use on subsequent turns (ADR-0032 §3.1).
            tool_results.append(
                {
                    "tool_call_id": tool_call_id,
                    "role": "tool",
                    "name": tool_name,
                    "content": json.dumps(
                        {
                            "status": "retry",
                            "hint": f"Arguments for {tool_name} were malformed JSON. Retry with valid JSON.",
                        }
                    ),
                }
            )
            continue

        # Gate pre-check (sequential — FSM state mutations happen here)
        args_hash = stable_hash(arguments)
        loop_policy = _get_tool_loop_policy(tool_name)
        gate_result = ctx.loop_gate.check_before(tool_name, args_hash, loop_policy)
        log.info(
            "tool_loop_gate",
            trace_id=ctx.trace_id,
            decision=gate_result.decision.value,
            tool_name=gate_result.tool_name,
            state_before=gate_result.state_before.value,
            state_after=gate_result.state_after.value,
            reason=gate_result.reason,
            consecutive_count=gate_result.consecutive_count,
            total_calls=gate_result.total_calls,
        )
        if gate_result.decision in (
            GateDecision.BLOCK_IDENTITY,
            GateDecision.BLOCK_OUTPUT,
            GateDecision.BLOCK_CONSECUTIVE,
        ):
            tool_results.append(_gate_blocked_result(tool_call_id, tool_name, gate_result))
            continue

        allowed_plans.append(
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "args_hash": args_hash,
                "loop_policy": loop_policy,
                "gate_result": gate_result,
            }
        )

    # ── Phase 2: Parallel async dispatch ──────────────────────────────────────
    # I/O-bound tool executions (network, ES, Neo4j) run concurrently; the gate
    # FSM has already been updated sequentially in Phase 1.
    _phase2_start = time.time()
    raw_dispatch: list[Any] = []
    if allowed_plans:
        raw_dispatch = list(
            await asyncio.gather(
                *[
                    _dispatch_tool_call(
                        p["tool_call_id"],
                        p["tool_name"],
                        p["arguments"],
                        p["args_hash"],
                        p["gate_result"],
                        p["loop_policy"],
                        tool_layer,
                        ctx,
                        trace_ctx,
                    )
                    for p in allowed_plans
                ],
                return_exceptions=True,
            )
        )

    # ── Phase 3: Sequential record + result assembly ───────────────────────────
    # gate.record_output and ctx mutations are sequential to preserve gate-FSM
    # invariants and ordering guarantees. Results are appended in allowed_plans order.
    _total_serial_ms = 0
    _max_dispatch_ms = 0
    for i, raw in enumerate(raw_dispatch):
        plan = allowed_plans[i]

        if isinstance(raw, BaseException):
            # Unexpected exception escaped _dispatch_tool_call's internal handler
            log.error(
                "tool_dispatch_unexpected_exception",
                trace_id=ctx.trace_id,
                tool_name=plan["tool_name"],
                error=str(raw),
            )
            tool_results.append(
                {
                    "tool_call_id": plan["tool_call_id"],
                    "role": "tool",
                    "name": plan["tool_name"],
                    "content": json.dumps(
                        {
                            "status": "error",
                            "hint": f"{plan['tool_name']} failed to execute. Try a different approach or tool.",
                        }
                    ),
                }
            )
            continue

        dr: dict[str, Any] = raw

        # Gate: record output for output-identity detection (success only)
        if dr["success"] and dr["output_hash"] is not None:
            ctx.loop_gate.record_output(
                dr["tool_name"], dr["args_hash"], dr["output_hash"], dr["loop_policy"]
            )

        content: str = dr["content"]

        # Inject gate advisory hint into content for advisory decisions
        _ADVISORY_DECISIONS = frozenset(
            {GateDecision.WARN_CONSECUTIVE, GateDecision.ADVISE_IDENTITY}
        )
        if dr["gate_result"].decision in _ADVISORY_DECISIONS:
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    if dr["gate_result"].decision == GateDecision.WARN_CONSECUTIVE:
                        parsed["_gate_warning"] = (
                            f"{dr['tool_name']} called {dr['gate_result'].consecutive_count} times "
                            "consecutively. Consider synthesizing from gathered results."
                        )
                    else:  # ADVISE_IDENTITY
                        parsed["_gate_warning"] = (
                            f"{dr['tool_name']} called with the same args "
                            f"{dr['gate_result'].total_calls}x. "
                            "Consider whether the result is stable or use prior output."
                        )
                    content = json.dumps(parsed)
            except (json.JSONDecodeError, TypeError):
                pass

        # Persist in ctx.tool_results and ctx.steps (sequential — shared state)
        ctx.tool_results.append(
            {
                "tool_name": dr["tool_name"],
                "success": dr["success"],
                "output": dr["tool_layer_output"],
                "error": dr["tool_layer_error"],
                "latency_ms": dr["latency_ms"],
            }
        )
        ctx.steps.append(
            {
                "type": "tool_call",
                "description": f"Executed tool: {dr['tool_name']}",
                "metadata": {
                    "tool_name": dr["tool_name"],
                    "tool_call_id": dr["tool_call_id"],
                    "success": dr["success"],
                    "latency_ms": dr["latency_ms"],
                },
            }
        )

        _total_serial_ms += dr["latency_ms"]
        _max_dispatch_ms = max(_max_dispatch_ms, dr["latency_ms"])

        tool_results.append(
            {
                "tool_call_id": dr["tool_call_id"],
                "role": "tool",
                "name": dr["tool_name"],
                "content": content,
            }
        )

    # Emit parallel-dispatch telemetry for Kibana efficiency tracking
    if allowed_plans:
        _actual_wall_ms = int((time.time() - _phase2_start) * 1000)
        log.info(
            "tools_dispatched_parallel",
            trace_id=ctx.trace_id,
            count=len(allowed_plans),
            blocked_count=len(tool_calls) - len(allowed_plans),
            max_latency_ms=_max_dispatch_ms,
            total_serial_equivalent_ms=_total_serial_ms,
            actual_wall_ms=_actual_wall_ms,
        )

    # Append all tool results to messages
    ctx.messages.extend(tool_results)

    duration_ms = int((time.time() - step_start_time) * 1000)

    tool_names = [tc.get("function", {}).get("name", "unknown") for tc in tool_calls]
    if timer and tool_span_name:
        timer.end_span(
            tool_span_name,
            tool_count=len(tool_calls),
            tool_names=tool_names,
        )

    log.info(
        "tool_execution_completed",
        trace_id=ctx.trace_id,
        tool_count=len(tool_calls),
        duration_ms=duration_ms,
    )

    # Transition back to LLM_CALL for synthesis using the same model that made the tool call.
    last_llm_role: ModelRole | None = None
    for step in reversed(ctx.steps):
        if step.get("type") == "llm_call":
            role_str = (step.get("metadata") or {}).get("model_role")
            if isinstance(role_str, str):
                last_llm_role = ModelRole.from_str(role_str)
            break
    ctx.selected_model_role = last_llm_role or ModelRole.PRIMARY
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
    timer = ctx.request_timer
    if timer:
        timer.start_span("synthesis")

    try:
        # Ensure final reply is set (should already be set from LLM call)
        if not ctx.final_reply:
            ctx.final_reply = "Task completed"  # Fallback

        # Update session with new messages
        if timer:
            timer.start_span("session_update")
        try:
            session_manager.update_session(ctx.session_id, messages=ctx.messages)
        finally:
            if timer:
                timer.end_span("session_update")
    finally:
        if timer:
            reply = ctx.final_reply or ""
            timer.end_span("synthesis", reply_length=len(reply))

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
            log.warning(
                TASK_FAILED,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                error=sanitized_error,
                error_type=type(ctx.error).__name__,
            )
            result["reply"] = "An error occurred while processing your request. Please try again."
            result["steps"].append(
                {
                    "type": "error",
                    "description": "Task failed due to an internal error.",
                    "metadata": {"error_type": type(ctx.error).__name__},
                }
            )

        # Trigger async context compression if threshold crossed (ADR-0038).
        if ctx.session_id:
            compression_manager.maybe_trigger_compression(
                session_id=ctx.session_id,
                messages=ctx.messages,
                trace_id=ctx.trace_id,
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
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        # Keep details in logs, return only generic user-facing error content.
        sanitized_error = sanitize_error_message(e)
        log.error(
            TASK_FAILED,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            error=sanitized_error,
            error_type=type(e).__name__,
        )
        log.info(
            REPLY_READY,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            reply_length=0,
            fatal_error=True,
        )
        return {
            "reply": "An internal error occurred. Please try again.",
            "steps": [
                {
                    "type": "error",
                    "description": "Task failed due to an internal error.",
                    "metadata": {"error_type": type(e).__name__},
                }
            ],
            "trace_id": ctx.trace_id,
        }
