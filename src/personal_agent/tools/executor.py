"""Tool execution layer with governance, validation, and telemetry.

This module provides the ToolExecutionLayer class that handles tool invocation
with permission checks, argument validation, execution, and telemetry.
"""

import asyncio
import os
import time
from collections.abc import Mapping
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from opentelemetry.trace import Status, StatusCode

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
from personal_agent.telemetry.spans import tool_call_span
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolResult

if TYPE_CHECKING:
    from personal_agent.transport.agui.transport import AGUITransport
    from personal_agent.transport.agui.ws_endpoint import ApprovalDecision

log = get_logger(__name__)

# Name of the executor parameter that receives the injected approval callable
# (ADR-0153 D7). Reserved: the layer removes any model-supplied value of this name
# and sets the real callable, so a tool definition cannot expose it to the model.
_APPROVE_PARAM = "approve"


class ApproveFn(Protocol):
    """The approval callable injected into executors that declare ``approve``."""

    async def __call__(self, *, args: Mapping[str, Any], reason: str) -> "ApprovalDecision":
        """Send one approval request and return the owner's decision.

        Args:
            args: The trusted values the owner sees on the approval card.
            reason: Why the tool asks for approval.

        Returns:
            The decision. ``deny`` when no approval channel exists.
        """
        ...


class PermissionResult:
    """Result of a permission check."""

    def __init__(
        self, allowed: bool, reason: str = "", error: dict[str, object] | None = None
    ) -> None:
        """Initialize permission result.

        Args:
            allowed: Whether permission is granted.
            reason: Reason for denial (if not allowed).
            error: For a refusal by the permission rules (ADR-0156 D8), the error the model
                gets: ``code``, ``message``, ``retryable`` and ``next_action`` (the D6 shape).
        """
        self.allowed = allowed
        self.reason = reason
        self.error = error


#: One approval card at a time per session. The PWA shows a single card, and a round's tool
#: calls (and a fan-out's workers) can ask at the same moment: without this a second card
#: would replace the first, which would then time out unanswered (FRE-1574 review).
_APPROVAL_LOCKS: dict[str, asyncio.Lock] = {}

#: Longest model-facing permission message (ADR-0156 D6.2 bound).
_PERMISSION_MESSAGE_LIMIT = 2000


def _permission_refusal(
    *,
    tool_name: str,
    reason: str,
    code: str,
    message: str,
    next_action: str,
) -> PermissionResult:
    """Build a refusal whose error tells the model what was refused and what to do next.

    Args:
        tool_name: The refused tool.
        reason: Short machine-readable reason, logged and kept in ``ToolResult.error``.
        code: Stable error code for the model.
        message: What was refused, by which rule, and why. Bounded at 2,000 characters.
        next_action: The next call that can work, or that none exists.

    Returns:
        A refused :class:`PermissionResult`.
    """
    text = f"{tool_name}: {message}"
    if len(text) > _PERMISSION_MESSAGE_LIMIT:
        text = text[: _PERMISSION_MESSAGE_LIMIT - 15] + " [truncated]"
    return PermissionResult(
        allowed=False,
        reason=reason,
        error={"code": code, "message": text, "retryable": False, "next_action": next_action},
    )


def _quote(text: str | None, limit: int = 600) -> str:
    """Quote a refused subcommand or argument set for a message.

    Args:
        text: The refused text.
        limit: Longest text kept.

    Returns:
        The text in backticks, cut to ``limit`` characters.
    """
    value = text or ""
    if len(value) > limit:
        value = value[:limit] + "…"
    return f"`{value}`"


class ToolExecutionError(Exception):
    """Raised when tool execution fails."""

    pass


class TerminalToolError(ToolExecutionError):
    """A tool failure the model cannot recover from — short-circuit the turn.

    Tools raise this (instead of :class:`ToolExecutionError`) when a failure is
    non-recoverable, so the orchestrator can surface a classified error without a
    recovery LLM round-trip (FRE-402). The ``reason`` and ``next_step`` are
    user-facing strings carried into the :class:`ClassifiedError` shown in the PWA.
    """

    def __init__(self, message: str, *, reason: str, next_step: str) -> None:
        """Initialize a terminal tool error.

        Args:
            message: Internal error message (used for logging and ``ToolResult.error``).
            reason: User-facing explanation of what happened (no internals).
            next_step: Concrete guidance for what the user can do next.
        """
        super().__init__(message)
        self.reason = reason
        self.next_step = next_step


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


async def _request_approval(
    *,
    transport: "AGUITransport",
    tool_name: str,
    args: Mapping[str, Any],
    reason: str,
    session_id: str,
    trace_ctx: TraceContext,
    worker: str | None = None,
    session_option: bool = False,
) -> "ApprovalDecision":
    """Send one approval request through the AG-UI transport and wait for the decision.

    Shared by the generic gate in :func:`_check_permissions` and the ``approve``
    callable injected into executors, so both send the same request.

    Args:
        transport: AG-UI transport for the round trip.
        tool_name: Tool awaiting approval.
        args: Values shown on the approval card.
        reason: Human-readable reason shown on the card.
        session_id: Session the request belongs to.
        trace_ctx: Trace context of this invocation.
        worker: The worker type that asks, or ``None`` for the primary (ADR-0156 D8.3).
        session_option: Whether the card offers "allow for this session". Only a call that
            matched no rule offers it (D8.3).

    Returns:
        The owner's decision, or a timeout or connection-lost decision.
    """
    # Import here to avoid circular imports at module load time.
    from personal_agent.transport.agui.transport import phase_span  # noqa: PLC0415
    from personal_agent.transport.events import Phase  # noqa: PLC0415

    # ADR-0123 §1 (FRE-934): the turn is blocked on the user's approval —
    # an explicit WAITING_FOR_CHOICE phase (excluded from the AC-2 clock).
    lock = _APPROVAL_LOCKS.setdefault(session_id, asyncio.Lock())
    async with (
        lock,
        phase_span(session_id=session_id, phase=Phase.WAITING_FOR_CHOICE, detail=tool_name),
    ):
        return await transport.request_tool_approval(
            request_id=str(uuid4()),
            trace_id=trace_ctx.trace_id,
            session_id=session_id,
            tool=tool_name,
            args=args,
            risk_level="high",  # primitives will pass the real level later
            reason=reason,
            timeout_seconds=settings.approval_timeout_seconds,
            worker=worker,
            session_option=session_option,
        )


async def _check_permissions(
    tool_name: str,
    tool_def: Any,
    arguments: dict[str, Any],
    current_mode: Mode,
    governance_config: GovernanceConfig,
    transport: "AGUITransport | None" = None,
    session_id: str | None = None,
    *,
    trace_ctx: TraceContext,
    worker: str | None = None,
) -> PermissionResult:
    """Check whether a call may run: mode, arguments, then the permission rules.

    The permission rules (ADR-0156 D8, ``config/governance/permissions.yaml``) decide deny,
    ask or allow. An ask shows the approval card and runs only on the user's yes. With no
    approval surface (``approval_ui_enabled`` false, no session id, no transport, no PWA
    client) an ask is denied (D8.4, ADR-0063 Amendment B). A rule-load error or an evaluation
    error denies (D8.1). Every refusal carries an error that names the refused subcommand and
    the rule, so the model can choose its next call.

    Args:
        tool_name: Name of the tool.
        tool_def: Tool definition.
        arguments: Tool arguments.
        current_mode: Current operational mode.
        governance_config: Governance configuration.
        transport: AG-UI transport for the approval card, or ``None``.
        session_id: Session identifier forwarded to the approval waiter.
        trace_ctx: Trace context for telemetry correlation in approval events.
        worker: The worker type that makes the call, or ``None`` for the primary. Shown on
            the card (D8.3).

    Returns:
        PermissionResult indicating if execution is allowed.
    """
    # Imported here: the rules import the bash matcher, which imports a tools module, and
    # this module is imported while ``personal_agent.tools`` initialises.
    from personal_agent.governance.permissions import decide_permission  # noqa: PLC0415
    from personal_agent.governance.session_rules import (  # noqa: PLC0415
        add_session_rule,
        session_rules,
    )

    # 1. Mode check. The governance policy's allowed_in_modes is authoritative when a
    # policy is configured for this tool (ADR-0063 §D1, FRE-1358) — an empty list means
    # "allowed nowhere", not "unset". tool_def.allowed_modes is the fallback for tools
    # with no governance entry.
    mode_str = current_mode.value
    tool_policy = governance_config.tools.get(tool_name)
    if tool_policy is not None:
        if mode_str not in tool_policy.allowed_in_modes:
            return PermissionResult(allowed=False, reason=f"Tool not allowed in {mode_str} mode")
    elif mode_str not in tool_def.allowed_modes:
        return PermissionResult(allowed=False, reason=f"Tool not allowed in {mode_str} mode")

    # Check tool policy for forbidden modes
    if tool_policy and mode_str in tool_policy.forbidden_in_modes:
        return PermissionResult(allowed=False, reason=f"Tool forbidden in {mode_str} mode")

    # 2. Argument validation, before the card: never ask the user about a call that a path
    # rule refuses anyway.
    validation_result = _validate_tool_arguments(tool_name, arguments, governance_config)
    if not validation_result.allowed:
        return validation_result

    # 3. The permission rules (ADR-0156 D8).
    decision = decide_permission(
        tool_name, arguments, mode_str, session_rules=session_rules(session_id)
    )
    log.info(
        "permission_evaluated",
        tool_name=tool_name,
        mode=mode_str,
        decision=decision.decision,
        rule=decision.rule,
        refused=decision.refused,
        reason=decision.reason,
        explicit_ask=decision.explicit,
        worker=worker,
        trace_id=trace_ctx.trace_id,
        session_id=session_id,
    )
    if decision.decision == "allow":
        return PermissionResult(allowed=True)
    if decision.decision == "deny":
        return _permission_refusal(
            tool_name=tool_name,
            reason="permission_deny_rule" if decision.rule else "permission_rules_error",
            code="permission_denied" if decision.rule else "permission_rules_unavailable",
            message=(
                f"refused {_quote(decision.refused)}. Rule: {decision.rule or 'none'}. "
                f"{decision.reason}"
            ),
            next_action=(
                "Do not retry this call. Use another tool or a different command."
                if decision.rule
                else "No call can run until the permission rules load. Tell the user."
            ),
        )

    # 4. Ask: the card, or a denial when there is no approval surface (D8.4).
    asked = (
        f"{_quote(decision.refused)} needs the user's approval "
        f"({decision.reason}; rule: {decision.rule or 'no rule allows it'})"
    )
    unanswered_next = (
        "Do not retry the same call. Use a command or tool that the allow rules cover, "
        "or tell the user what you need to run and why."
    )

    def unanswered(cause: str, event: str, reason: str) -> PermissionResult:
        log.warning(
            event,
            tool_name=tool_name,
            mode=mode_str,
            refused=decision.refused,
            rule=decision.rule,
            worker=worker,
            trace_id=trace_ctx.trace_id,
            session_id=session_id,
        )
        return _permission_refusal(
            tool_name=tool_name,
            reason=reason,
            code="approval_unavailable",
            message=f"{asked}, and {cause}. It did not run.",
            next_action=unanswered_next,
        )

    if not settings.approval_ui_enabled:
        return unanswered(
            "the approval UI is disabled on this deployment",
            "approval_ui_disabled_denied",
            "approval_ui_disabled",
        )
    if not session_id:
        return unanswered(
            "this call has no session to ask in",
            "approval_denied_no_session_id",
            "approval_no_session_id",
        )
    if transport is None:
        return unanswered(
            "no approval channel is attached",
            "approval_denied_no_transport",
            "approval_no_transport",
        )

    answer = await _request_approval(
        transport=transport,
        tool_name=tool_name,
        args=arguments,
        reason=decision.reason if decision.explicit else f"{decision.reason}: {decision.refused}",
        session_id=session_id,
        trace_ctx=trace_ctx,
        worker=worker,
        session_option=not decision.explicit,
    )
    if answer.decision == "approve":
        log.info(
            "approval_granted",
            tool_name=tool_name,
            scope="once",
            worker=worker,
            trace_id=trace_ctx.trace_id,
            session_id=session_id,
        )
        return PermissionResult(allowed=True)
    if answer.decision == "approve_session":
        # Only an unmatched call may add a session rule; an explicit ask is never
        # overridden (D8.3). A session answer to an explicit ask runs this call once.
        scope = "once"
        if not decision.explicit:
            rule_text = add_session_rule(session_id, tool_name, arguments)
            scope = "session"
            log.info(
                "approval_session_rule_added",
                tool_name=tool_name,
                rule=rule_text,
                worker=worker,
                trace_id=trace_ctx.trace_id,
                session_id=session_id,
            )
        log.info(
            "approval_granted",
            tool_name=tool_name,
            scope=scope,
            worker=worker,
            trace_id=trace_ctx.trace_id,
            session_id=session_id,
        )
        return PermissionResult(allowed=True)

    outcome = {
        "deny": ("the user denied it", "approval_denied_by_user", "denied_by_user"),
        "timeout": (
            "the card timed out with no answer",
            "approval_timeout",
            "approval_timed_out",
        ),
        "connection_lost": (
            "no PWA client is connected to answer",
            "approval_no_client",
            "approval_unavailable",
        ),
    }.get(
        answer.decision,
        ("the answer was not an approval", "approval_not_approved", "denied_by_user"),
    )
    log.warning(
        "approval_denied",
        tool_name=tool_name,
        mode=mode_str,
        decision=answer.decision,
        refused=decision.refused,
        worker=worker,
        trace_id=trace_ctx.trace_id,
        session_id=session_id,
    )
    return _permission_refusal(
        tool_name=tool_name,
        reason=outcome[1],
        code=outcome[2],
        message=f"{asked}, and {outcome[0]}. It did not run.",
        next_action=unanswered_next,
    )


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
                round-trips (FRE-261).  When ``None`` and the approval UI is
                enabled, approval-required tools are denied (FRE-1535).
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

    def _build_approve(
        self, tool_name: str, session_id: str | None, trace_ctx: TraceContext
    ) -> ApproveFn:
        """Build the approval callable for one invocation (ADR-0153 D7).

        The callable sends one request through this layer's transport.  It fails
        closed: with the approval UI disabled, no session id, or no transport it
        returns ``deny`` and sends nothing.

        Args:
            tool_name: Tool the request is made for.
            session_id: Session of this invocation.
            trace_ctx: Trace context of this invocation.

        Returns:
            The callable bound to this invocation.
        """
        transport = self.transport

        async def approve(*, args: Mapping[str, Any], reason: str) -> "ApprovalDecision":
            from personal_agent.transport.agui.ws_endpoint import (  # noqa: PLC0415
                ApprovalDecision,
            )

            if not settings.approval_ui_enabled:
                cause = "ui_disabled"
            elif not session_id:
                cause = "no_session_id"
            elif transport is None:
                cause = "no_transport"
            else:
                return await _request_approval(
                    transport=transport,
                    tool_name=tool_name,
                    args=args,
                    reason=reason,
                    session_id=session_id,
                    trace_ctx=trace_ctx,
                )
            log.warning(
                "approval_callable_denied",
                tool_name=tool_name,
                cause=cause,
                trace_id=trace_ctx.trace_id,
            )
            return ApprovalDecision(decision="deny", reason=cause)

        return approve

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        trace_ctx: TraceContext,
        session_id: str | None = None,
        worker: str | None = None,
    ) -> ToolResult:
        """Execute a tool with full governance and observability.

        An executor that declares an ``approve`` parameter receives an
        :class:`ApproveFn` bound to this call (ADR-0153 D7).  The name is
        reserved: a model-supplied ``approve`` argument is always dropped.  Only
        async executors may declare it.

        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments (keyword arguments for executor).
            trace_ctx: Trace context for telemetry.
            session_id: Optional session identifier forwarded to the approval
                waiter so that approval requests are scoped to the originating
                session.
            worker: The worker type that makes the call, or ``None`` for the
                primary.  An ask from a worker shows the same card, per call,
                with the worker named (ADR-0156 D8.3, D9.3).

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
                session_id=session_id,
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
            worker=worker,
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
                metadata=(
                    {"permission_error": permission.error} if permission.error is not None else {}
                ),
            )

        # 3. Validate and filter arguments to match tool definition
        # This prevents LLM from sending extra/invalid parameters
        valid_param_names = {param.name for param in tool_def.parameters}
        filtered_arguments = {
            k: v for k, v in arguments.items() if k in valid_param_names and k != _APPROVE_PARAM
        }

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

        # 4. Emit telemetry (start) — ADR-0129 D3 / FRE-1067: a real OTel span,
        # child of the currently open step span. span_id used in the retained
        # log records below (D3, AC-11) is the span's own id, not a hand-minted one.
        with tool_call_span(tool_name=tool_name) as span:
            span_id = format(span.get_span_context().span_id, "016x")
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
                if _APPROVE_PARAM in sig.parameters:
                    if not inspect.iscoroutinefunction(executor):
                        raise ToolExecutionError(
                            f"Tool '{tool_name}' declares '{_APPROVE_PARAM}' but its executor "
                            "is not async; it cannot await the approval"
                        )
                    filtered_arguments = {
                        **filtered_arguments,
                        _APPROVE_PARAM: self._build_approve(tool_name, session_id, trace_ctx),
                    }

                if inspect.iscoroutinefunction(executor):
                    result = await executor(**filtered_arguments)
                else:
                    # Sync executor - run in thread pool to avoid blocking
                    import asyncio

                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(
                        None, lambda: executor(**filtered_arguments)
                    )

                latency_ms = (time.time() - start_time) * 1000

                # 6. Emit telemetry (complete)
                log.info(
                    TOOL_CALL_COMPLETED,
                    tool_name=tool_name,
                    success=True,
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
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))

                log.error(
                    TOOL_CALL_FAILED,
                    tool_name=tool_name,
                    error=str(e),
                    trace_id=trace_ctx.trace_id,
                    session_id=session_id,
                    span_id=span_id,
                    exc_info=True,
                )

                # FRE-402: preserve terminality (and its user-facing guidance) through
                # the flatten so the orchestrator can short-circuit the reasoning loop.
                metadata: dict[str, Any] = {}
                if isinstance(e, TerminalToolError):
                    metadata = {
                        "terminal": True,
                        "terminal_reason": e.reason,
                        "terminal_next_step": e.next_step,
                    }

                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    output={},
                    error=str(e),
                    latency_ms=latency_ms,
                    metadata=metadata,
                )
