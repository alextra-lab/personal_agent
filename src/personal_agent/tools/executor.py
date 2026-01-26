"""Tool execution layer with governance, validation, and telemetry.

This module provides the ToolExecutionLayer class that handles tool invocation
with permission checks, argument validation, execution, and telemetry.
"""

import os
import time
from fnmatch import fnmatch
from typing import Any

from personal_agent.brainstem import ModeManager, get_mode_manager
from personal_agent.config import load_governance_config
from personal_agent.governance.models import GovernanceConfig, Mode
from personal_agent.telemetry import (
    POLICY_VIOLATION,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_FAILED,
    TOOL_CALL_STARTED,
    TraceContext,
    get_logger,
)
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolResult

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


def _check_permissions(
    tool_name: str,
    tool_def: Any,
    arguments: dict[str, Any],
    current_mode: Mode,
    governance_config: GovernanceConfig,
) -> PermissionResult:
    """Check if tool execution is permitted.

    Args:
        tool_name: Name of the tool.
        tool_def: Tool definition.
        arguments: Tool arguments.
        current_mode: Current operational mode.
        governance_config: Governance configuration.

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

    # 2. Approval check (for MVP, we skip interactive approval - just log)
    # TODO: Implement interactive approval workflow in Phase 2
    if tool_policy:
        if mode_str in tool_policy.requires_approval_in_modes or tool_policy.requires_approval:
            log.warning(
                "approval_required_but_not_implemented",
                tool_name=tool_name,
                mode=mode_str,
                message="Approval required but interactive approval not yet implemented",
            )
            # For MVP, we allow with warning (Phase 2 will implement approval)

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
    ) -> None:
        """Initialize tool execution layer.

        Args:
            registry: Tool registry containing registered tools.
            governance_config: Governance configuration. If None, loads from default.
            mode_manager: Mode manager. If None, uses global instance.
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

        log.debug("tool_execution_layer_initialized")

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        trace_ctx: TraceContext,
    ) -> ToolResult:
        """Execute a tool with full governance and observability.

        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments (keyword arguments for executor).
            trace_ctx: Trace context for telemetry.

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

        # 2. Check permissions
        current_mode = self.mode_manager.get_current_mode()
        permission = _check_permissions(
            tool_name, tool_def, arguments, current_mode, self.governance_config
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
            # Execute tool (async or sync executor)
            import inspect

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
