"""Tool execution layer with governance, validation, and telemetry.

This module provides the ToolExecutionLayer class that handles tool invocation
with permission checks, argument validation, execution, and telemetry.
"""

import os
import time
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from personal_agent.brainstem import ModeManager, get_mode_manager
from personal_agent.config import load_governance_config, settings
from personal_agent.governance.models import GovernanceConfig, Mode
from personal_agent.telemetry import (
    POLICY_VIOLATION,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_FAILED,
    TOOL_CALL_STARTED,
    TraceContext,
    get_logger,
)
from personal_agent.telemetry.events import TOOL_SCHEMA_VALIDATION_FAILED
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolResult

if TYPE_CHECKING:
    from personal_agent.transport.agui.transport import AGUITransport

log = get_logger(__name__)


class PermissionResult:
    """Result of a permission check."""

    def __init__(self, allowed: bool, reason: str = "") -> None:
        """Initialize permission result.

        Args:
            allowed: Whether permission is granted.
            reason: Reason for denial (if not allowed).
        """
        self.allowed = allowed
        self.reason = reason


class ToolExecutionError(Exception):
    """Raised when tool execution fails."""

    pass


def _expand_path(path: str) -> str:
    """Expand path with environment variables.

    Args:
        path: Path string that may contain environment variables.

    Returns:
        Expanded path string.
    """
    return os.path.expanduser(os.path.expandvars(path))


def _validate_path_against_patterns(path: str, patterns: list[str]) -> bool:
    """Check if path matches any pattern.

    Args:
        path: Path to check.
        patterns: List of glob patterns.

    Returns:
        True if path matches any pattern, False otherwise.
    """
    expanded_path = _expand_path(path)
    return any(fnmatch(expanded_path, _expand_path(pattern)) for pattern in patterns)


def _validate_tool_arguments(
    tool_name: str, arguments: dict[str, Any], governance_config: GovernanceConfig
) -> PermissionResult:
    """Validate tool arguments against governance policies.

    Args:
        tool_name: Name of the tool.
        arguments: Tool arguments to validate.
        governance_config: Governance configuration.

    Returns:
        PermissionResult indicating if validation passed.
    """
    tool_policy = governance_config.tools.get(tool_name)
    if not tool_policy:
        # No specific policy, allow (will be checked by mode check)
        return PermissionResult(allowed=True)

    # Check path allowlists/denylists for file operations
    if "path" in arguments:
        path = arguments["path"]
        if not isinstance(path, str):
            return PermissionResult(allowed=False, reason="Path must be a string")

        # Check forbidden paths first (more restrictive)
        if tool_policy.forbidden_paths:
            if _validate_path_against_patterns(path, tool_policy.forbidden_paths):
                return PermissionResult(allowed=False, reason=f"Path {path} is in forbidden paths")

        # Check allowed paths (if specified)
        if tool_policy.allowed_paths:
            if not _validate_path_against_patterns(path, tool_policy.allowed_paths):
                return PermissionResult(
                    allowed=False, reason=f"Path {path} is not in allowed paths"
                )

    # Check file size limits
    if "max_size_mb" in arguments and tool_policy.max_file_size_mb:
        max_size_mb = arguments.get("max_size_mb")
        if max_size_mb and max_size_mb > tool_policy.max_file_size_mb:
            return PermissionResult(
                allowed=False,
                reason=f"Requested max_size_mb {max_size_mb} exceeds limit {tool_policy.max_file_size_mb}",
            )

    return PermissionResult(allowed=True)


async def _check_permissions(
    tool_name: str,
    tool_def: Any,
    arguments: dict[str, Any],
    current_mode: Mode,
    governance_config: GovernanceConfig,
    transport: "AGUITransport | None" = None,
    session_id: str | None = None,
    trace_ctx: TraceContext | None = None,
) -> PermissionResult:
    """Check if tool execution is permitted, requesting UI approval if required.

    Args:
        tool_name: Name of the tool.
        tool_def: Tool definition.
        arguments: Tool arguments.
        current_mode: Current operational mode.
        governance_config: Governance configuration.
        transport: Optional AG-UI transport for interactive approval round-trips.
            When ``None``, approval-required tools log a warning and are allowed
            (legacy MVP behaviour, preserved until approval UI is deployed).
        session_id: Session identifier forwarded to the approval waiter.
        trace_ctx: Trace context for telemetry correlation in approval events.

    Returns:
        PermissionResult indicating if execution is allowed.
    """
    # 1. Mode check
    mode_str = current_mode.value
    if mode_str not in tool_def.allowed_modes:
        return PermissionResult(allowed=False, reason=f"Tool not allowed in {mode_str} mode")

    # Check tool policy for forbidden modes
    tool_policy = governance_config.tools.get(tool_name)
    if tool_policy and mode_str in tool_policy.forbidden_in_modes:
        return PermissionResult(allowed=False, reason=f"Tool forbidden in {mode_str} mode")

    # 2. Approval check
    if tool_policy and (
        mode_str in tool_policy.requires_approval_in_modes or tool_policy.requires_approval
    ):
        if settings.approval_ui_enabled and not session_id:
            # Guard: approval UI is enabled but no session_id was supplied.
            # Registering a waiter with an empty session_id can never be
            # correctly resolved by the endpoint (it compares caller_session_id
            # against the registered value).  Fall through to the warn-and-allow
            # path rather than creating an un-resolvable waiter.
            log.warning(
                "approval_skipped_no_session_id",
                tool_name=tool_name,
                mode=mode_str,
                message="Approval required but session_id is empty; skipping interactive approval",
            )
        elif settings.approval_ui_enabled and transport is not None:
            # Perform interactive approval round-trip via the PWA.
            # Import here to avoid circular imports at module load time.
            from personal_agent.transport.agui.approval_waiter import ApprovalDecision  # noqa: PLC0415, I001

            decision: ApprovalDecision = await transport.request_tool_approval(
                request_id=str(uuid4()),
                trace_id=trace_ctx.trace_id if trace_ctx else "",
                session_id=session_id or "",
                tool=tool_name,
                args=arguments,
                risk_level="high",  # primitives will pass the real level later
                reason=f"Tool '{tool_name}' requires approval in {mode_str} mode",
                timeout_seconds=settings.approval_timeout_seconds,
            )
            if decision.decision != "approve":
                return PermissionResult(
                    allowed=False,
                    reason=f"approval_{decision.decision}",
                )
        else:
            log.warning(
                "approval_ui_disabled_proceeding",
                tool_name=tool_name,
                mode=mode_str,
                message="Approval required but AGENT_APPROVAL_UI_ENABLED=false — proceeding without prompt",
            )
            # Allow the call when approval UI is explicitly disabled.

    # 3. Rate limit check (for MVP, we skip - Phase 2 will implement)
    # TODO: Implement rate limiting via telemetry query

    # 4. Argument validation
    validation_result = _validate_tool_arguments(tool_name, arguments, governance_config)
    if not validation_result.allowed:
        return validation_result

    return PermissionResult(allowed=True)


class ToolExecutionLayer:
    """Handles tool invocation with governance, validation, and telemetry."""

    def __init__(
        self,
        registry: ToolRegistry,
        governance_config: GovernanceConfig | None = None,
        mode_manager: ModeManager | None = None,
        transport: "AGUITransport | None" = None,
    ) -> None:
        """Initialize tool execution layer.

        Args:
            registry: Tool registry containing registered tools.
            governance_config: Governance configuration. If None, loads from default.
            mode_manager: Mode manager. If None, uses global instance.
            transport: Optional AG-UI transport used for interactive tool-approval
                round-trips (FRE-261).  When ``None``, approval-required tools
                fall back to the legacy warn-and-allow path.
        """
        self.registry = registry
        if governance_config is None:
            self.governance_config = load_governance_config()
        else:
            self.governance_config = governance_config

        if mode_manager is None:
            self.mode_manager = get_mode_manager()
        else:
            self.mode_manager = mode_manager

        self.transport = transport

        log.debug("tool_execution_layer_initialized")

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        trace_ctx: TraceContext,
        session_id: str | None = None,
    ) -> ToolResult:
        """Execute a tool with full governance and observability.

        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments (keyword arguments for executor).
            trace_ctx: Trace context for telemetry.
            session_id: Optional session identifier forwarded to the approval
                waiter so that approval requests are scoped to the originating
                session.

        Returns:
            ToolResult with execution outcome.
        """
        # 1. Retrieve tool
        tool_result = self.registry.get_tool(tool_name)
        if not tool_result:
            available_tools = self.registry.list_tool_names()
            error_msg = f"Tool '{tool_name}' not found. Available: {available_tools}"
            log.warning(
                TOOL_CALL_FAILED,
                tool_name=tool_name,
                error=error_msg,
                trace_id=trace_ctx.trace_id,
            )
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output={},
                error=error_msg,
                latency_ms=0.0,
            )

        tool_def, executor = tool_result

        # 2. Check permissions (async — may perform interactive approval round-trip)
        current_mode = self.mode_manager.get_current_mode()
        permission = await _check_permissions(
            tool_name,
            tool_def,
            arguments,
            current_mode,
            self.governance_config,
            transport=self.transport,
            session_id=session_id,
            trace_ctx=trace_ctx,
        )

        if not permission.allowed:
            log.warning(
                POLICY_VIOLATION,
                tool_name=tool_name,
                reason=permission.reason,
                mode=current_mode.value,
                trace_id=trace_ctx.trace_id,
            )
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output={},
                error=f"Permission denied: {permission.reason}",
                latency_ms=0.0,
            )

        # 3. Validate and filter arguments to match tool definition
        # This prevents LLM from sending extra/invalid parameters
        valid_param_names = {param.name for param in tool_def.parameters}
        filtered_arguments = {k: v for k, v in arguments.items() if k in valid_param_names}

        # Log if LLM sent invalid parameters
        invalid_params = set(arguments.keys()) - valid_param_names
        if invalid_params:
            log.warning(
                "tool_call_invalid_parameters_filtered",
                tool_name=tool_name,
                invalid_parameters=list(invalid_params),
                valid_parameters=list(valid_param_names),
                trace_id=trace_ctx.trace_id,
            )

        # 3b. Full schema validation (required fields, types, nested structures)
        from personal_agent.tools.schema_validator import validate_tool_arguments

        schema_errors = validate_tool_arguments(tool_def, filtered_arguments)
        if schema_errors:
            error_summary = "; ".join(schema_errors[:3])
            log.warning(
                TOOL_SCHEMA_VALIDATION_FAILED,
                tool_name=tool_name,
                errors=schema_errors,
                arguments_preview={k: repr(v)[:80] for k, v in filtered_arguments.items()},
                trace_id=trace_ctx.trace_id,
            )
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output={},
                error=f"Invalid arguments: {error_summary}. Retry with corrected params.",
                latency_ms=0.0,
            )

        # 4. Emit telemetry (start)
        span_ctx, span_id = trace_ctx.new_span()
        log.info(
            TOOL_CALL_STARTED,
            tool_name=tool_name,
            arguments=filtered_arguments,  # Log filtered arguments
            trace_id=trace_ctx.trace_id,
            span_id=span_id,
        )

        # 5. Execute (with timeout handling)
        start_time = time.time()

        try:
            # Execute tool (async or sync executor). Pass ctx to executors that accept it.
            import inspect

            sig = inspect.signature(executor)
            pass_ctx = "ctx" in sig.parameters
            if pass_ctx:
                filtered_arguments = {**filtered_arguments, "ctx": trace_ctx}

            if inspect.iscoroutinefunction(executor):
                result = await executor(**filtered_arguments)
            else:
                # Sync executor - run in thread pool to avoid blocking
                import asyncio

                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: executor(**filtered_arguments))

            latency_ms = (time.time() - start_time) * 1000

            # 6. Emit telemetry (complete)
            log.info(
                TOOL_CALL_COMPLETED,
                tool_name=tool_name,
                success=True,
                latency_ms=latency_ms,
                trace_id=trace_ctx.trace_id,
                span_id=span_id,
            )

            return ToolResult(
                tool_name=tool_name,
                success=True,
                output=result,
                error=None,
                latency_ms=latency_ms,
            )

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000

            log.error(
                TOOL_CALL_FAILED,
                tool_name=tool_name,
                error=str(e),
                latency_ms=latency_ms,
                trace_id=trace_ctx.trace_id,
                span_id=span_id,
                exc_info=True,
            )

            return ToolResult(
                tool_name=tool_name,
                success=False,
                output={},
                error=str(e),
                latency_ms=latency_ms,
            )
