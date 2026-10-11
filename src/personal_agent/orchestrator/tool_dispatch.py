"""Shared per-call tool dispatch boundary.

`dispatch_tool_call` executes one validated, gate-allowed tool call and returns
its result payload. It is the single dispatch path invoked by both the primary
executor loop (`orchestrator.executor.step_tool_execution`) and the sub-agent
loop (`orchestrator.sub_agent.run_sub_agent`), so it does not re-implement
tool permissions, action-boundary governance (ADR-0063), and per-call
telemetry/``trace_id`` threading (ADR-0074) — all inherited from
`ToolExecutionLayer.execute_tool`; this function adds only parameter
validation, skill-load dedup, per-principal parameter clamping (FRE-1473), and
error formatting.

The function takes the few request primitives it needs (``trace_id``,
``session_id``, ``loaded_skills``) rather than an ``ExecutionContext`` so a
caller without a full primary context can still use it. The gate fields
(``gate_result``, ``loop_policy``, ``args_hash``) are optional: the primary
passes its real loop-gate state; a caller without a loop-gate FSM omits them
and they echo back as ``None`` in the contract dict.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from personal_agent.governance.sub_agent_tools import clamp_sub_agent_tool_params
from personal_agent.orchestrator.loop_gate import GateResult, ToolLoopPolicy, stable_hash
from personal_agent.telemetry import TOOL_CALL_COMPLETED, get_logger
from personal_agent.telemetry.events import SUB_AGENT_TOOL_PARAM_CLAMPED
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.contract import dispatch_error, failure_payload, render_content
from personal_agent.tools.types import RESULT_TOKEN_CEILING, ToolErrorDetail

if TYPE_CHECKING:
    from personal_agent.governance.worker_read_grant import ReadGrant
    from personal_agent.tools import ToolExecutionLayer

log = get_logger(__name__)

_tool_execution_layer: "ToolExecutionLayer | None" = None


def get_shared_tool_execution_layer() -> "ToolExecutionLayer":
    """Get or create the module-level tool execution layer for non-primary callers.

    A separate singleton from ``executor.py``'s own private
    ``_get_tool_execution_layer`` — that one is module-private to a file large
    enough that a sub-agent loop should not import from it. Both wrap the same
    underlying stateless registry/governance config, so having two instances
    changes no correctness guarantee.

    The layer carries an AG-UI transport, like the primary's, so that a worker's ask
    reaches the same approval card per call (ADR-0156 D8.3, D9.3).

    Returns:
        ToolExecutionLayer instance with the default tool registry.
    """
    global _tool_execution_layer
    if _tool_execution_layer is None:
        from personal_agent.tools import ToolExecutionLayer, get_default_registry  # noqa: PLC0415
        from personal_agent.transport.agui.transport import AGUITransport  # noqa: PLC0415

        _tool_execution_layer = ToolExecutionLayer(
            get_default_registry(), transport=AGUITransport()
        )
    return _tool_execution_layer


def undispatched_failure(tool_name: str, detail: ToolErrorDetail, *, trace_id: str) -> str:
    """Log a call that failed before the execution layer, and return its content.

    Covers the failures that dispatch and the tool loops build themselves: a missing
    parameter, malformed argument JSON, a tool outside a worker's set, and a loop-gate block.
    Each logs ``tool_call_completed`` with ``success: false`` (ADR-0156 D6.3).

    Args:
        tool_name: The tool the model called.
        detail: The model-facing failure.
        trace_id: Request trace identifier.

    Returns:
        The tool message content in the contract shape.
    """
    log.info(
        TOOL_CALL_COMPLETED,
        tool_name=tool_name,
        success=False,
        error_code=detail.code,
        retryable=detail.retryable,
        executed=False,
        trace_id=trace_id,
    )
    return json.dumps(failure_payload(detail))


async def dispatch_tool_call(
    *,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    tool_layer: "ToolExecutionLayer",
    trace_ctx: TraceContext,
    trace_id: str,
    session_id: str | None,
    loaded_skills: set[str],
    args_hash: str = "",
    gate_result: GateResult | None = None,
    loop_policy: ToolLoopPolicy | None = None,
    principal: Literal["primary", "sub_agent"] = "primary",
    worker: str | None = None,
    read_grant: ReadGrant | None = None,
) -> dict[str, Any]:
    """Execute one validated tool call and return its result payload.

    This coroutine handles param validation, skill dedup, tool execution (with full governance via ``execute_tool``), and error
    formatting. It does NOT mutate any loop-gate state — gate FSM mutations stay
    sequential in the primary's ``step_tool_execution``.

    Args:
        tool_call_id: Identifier of the tool call from the LLM response.
        tool_name: Name of the tool to execute.
        arguments: Parsed tool arguments.
        tool_layer: Tool execution layer (governance + execution).
        trace_ctx: Trace context for telemetry correlation.
        trace_id: Request trace identifier (for structured logs).
        session_id: Originating session id for cost attribution (ADR-0074).
        loaded_skills: Mutable set of already-loaded skill names (read_skill
            dedup); updated in place when ``read_skill`` loads a new skill.
        args_hash: Stable hash of ``arguments``; echoed back for the primary's
            Phase-3 gate record. Defaults to "" for callers without a gate.
        gate_result: Loop-gate decision from the primary, or ``None``.
        loop_policy: Loop policy from the primary, or ``None``.
        principal: Which governance principal issued this call (FRE-1473).
            ``"sub_agent"`` runs ``arguments`` through
            ``clamp_sub_agent_tool_params`` before anything else, reducing any
            parameter over that principal's declared ceiling and logging each
            reduction. Defaults to ``"primary"``, which never calls the clamp
            function at all — the primary's own tool policy is untouched by
            this parameter's existence, not merely by its value.
        worker: The worker type that makes the call, or ``None`` for the primary.
            An ask from a worker shows the same approval card, per call, with the
            worker named (ADR-0156 D8.3, D9.3).
        read_grant: The worker task's file grant (ADR-0156 D9.4), passed on to ``read``.
            A ``sub_agent`` call to ``read`` without one is refused before it runs.

    Returns:
        A dict with keys: ``tool_call_id``, ``tool_name``, ``content``,
        ``success``, ``latency_ms``, ``output_hash`` (None on error),
        ``gate_result``, ``args_hash``, ``loop_policy``, ``tool_layer_output``,
        ``tool_layer_error``, ``terminal``, ``terminal_reason``,
        ``terminal_next_step``.
    """
    if principal == "sub_agent":
        arguments, param_clamps = clamp_sub_agent_tool_params(
            tool_name, arguments, tool_layer.governance_config
        )
        for clamp in param_clamps:
            log.info(
                SUB_AGENT_TOOL_PARAM_CLAMPED,
                trace_id=trace_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                principal=principal,
                param=clamp.param,
                requested=clamp.requested,
                applied=clamp.applied,
            )

        # ADR-0156 D9.4: a worker reads only the files its task names. A worker path that
        # passes no grant cannot read at all, rather than read without limit.
        if tool_name == "read" and read_grant is None:
            detail = dispatch_error(
                "not_available_to_worker",
                "read is not available to this sub-agent: it holds no file grant.",
                retryable=False,
                next_action="Do not call read. Use the other tools this sub-agent holds.",
            )
            return {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "content": undispatched_failure(tool_name, detail, trace_id=trace_id),
                "success": False,
                "latency_ms": 0,
                "output_hash": None,
                "gate_result": gate_result,
                "args_hash": args_hash,
                "loop_policy": loop_policy,
                "tool_layer_output": None,
                "tool_layer_error": "no_read_grant",
            }

    # Validate required parameters against the tool definition.
    tool_info = tool_layer.registry.get_tool(tool_name)
    if tool_info:
        tool_def, _ = tool_info
        required_params = [p.name for p in tool_def.parameters if p.required]
        missing_params = [p for p in required_params if p not in arguments or arguments[p] is None]
        if missing_params:
            log.warning(
                "tool_call_missing_required_params",
                trace_id=trace_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                missing_params=missing_params,
                required_params=required_params,
            )
            detail = dispatch_error(
                "missing_parameter",
                f"{tool_name} is missing required parameters: {', '.join(missing_params)}.",
                retryable=True,
                next_action=f"Call {tool_name} again with {', '.join(missing_params)} included.",
            )
            return {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "content": undispatched_failure(tool_name, detail, trace_id=trace_id),
                "success": False,
                "latency_ms": 0,
                "output_hash": None,
                "gate_result": gate_result,
                "args_hash": args_hash,
                "loop_policy": loop_policy,
                "tool_layer_output": None,
                "tool_layer_error": "missing_required_params",
            }

    # Skill dedup: a body that read_skill already returned this turn stays in the context
    # (ADR-0156 D2.4), so a second body read returns a marker. A file read (``path``) is not
    # deduplicated.
    if tool_name == "read_skill" and not arguments.get("path"):
        _skill_name_arg = str(arguments.get("name", ""))
        if _skill_name_arg and _skill_name_arg in loaded_skills:
            log.debug(
                "read_skill_dedup",
                skill_name=_skill_name_arg,
                trace_id=trace_id,
            )
            return {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "content": render_content(
                    output={
                        "skill_name": _skill_name_arg,
                        "body": f"<skill: {_skill_name_arg} already loaded earlier in this turn>",
                    },
                    error_detail=None,
                ),
                "success": True,
                "latency_ms": 0,
                "output_hash": None,
                "gate_result": gate_result,
                "args_hash": args_hash,
                "loop_policy": loop_policy,
                "tool_layer_output": None,
                "tool_layer_error": None,
            }

    # Execute tool (governance + telemetry happen inside execute_tool).
    try:
        result = await tool_layer.execute_tool(
            tool_name,
            arguments,
            trace_ctx,
            session_id=session_id,
            worker=worker,
            read_grant=read_grant,
        )

        tool_info = tool_layer.registry.get_tool(tool_name)
        bound_def = tool_info[0] if tool_info else None
        output_hash: str | None = None
        if result.success:
            output_hash = stable_hash(result.output)

            # Track the skills whose body is loaded (ADR-0156 D2). The read_skill tool logs
            # read_skill_invoked itself; no other tool result carries a skill hint.
            if (
                tool_name == "read_skill"
                and isinstance(result.output, dict)
                and "body" in result.output
            ):
                _loaded_name = str(result.output.get("skill_name", ""))
                if _loaded_name:
                    loaded_skills.add(_loaded_name)

        # ADR-0156 D6: one shape for every result. A failure carries its full message
        # (bounded at 2,000 characters), never a clip; a success is bounded per tool.
        content = render_content(
            output=result.output,
            error_detail=None if result.success else result.error_detail,
            max_result_tokens=bound_def.max_result_tokens if bound_def else RESULT_TOKEN_CEILING,
            truncation_next_action=(
                bound_def.truncation_next_action
                if bound_def
                else "Narrow the request and call again."
            ),
        )

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
            # FRE-402: tools mark non-recoverable failures via ToolResult.metadata.
            "terminal": bool(result.metadata.get("terminal")),
            "terminal_reason": result.metadata.get("terminal_reason"),
            "terminal_next_step": result.metadata.get("terminal_next_step"),
        }

    except Exception as e:
        log.error(
            "tool_execution_exception",
            trace_id=trace_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            error=str(e),
            exc_info=True,
        )
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "content": undispatched_failure(
                tool_name,
                dispatch_error(
                    "tool_exception",
                    f"{tool_name} failed to execute: {type(e).__name__}: {e}",
                    retryable=False,
                    next_action="Do not repeat the same call. Use another tool.",
                ),
                trace_id=trace_id,
            ),
            "success": False,
            "latency_ms": 0,
            "output_hash": None,
            "gate_result": gate_result,
            "args_hash": args_hash,
            "loop_policy": loop_policy,
            "tool_layer_output": None,
            "tool_layer_error": str(e),
            "terminal": False,
            "terminal_reason": None,
            "terminal_next_step": None,
        }
