"""Shared per-call tool dispatch boundary.

`dispatch_tool_call` executes one validated, gate-allowed tool call and returns
its result payload. It is the single dispatch path invoked by the primary
executor loop (`orchestrator.executor.step_tool_execution`), designed so any
future caller (e.g. a sub-agent loop) can reuse it without re-implementing
tool permissions, action-boundary governance (ADR-0063), and per-call
telemetry/``trace_id`` threading (ADR-0074) — all inherited from
`ToolExecutionLayer.execute_tool`; this function adds only parameter
validation, known-bad-pattern pre-checks, skill-load dedup, and error
formatting.

The function takes the few request primitives it needs (``trace_id``,
``session_id``, ``loaded_skills``) rather than an ``ExecutionContext`` so a
caller without a full primary context can still use it. The gate fields
(``gate_result``, ``loop_policy``, ``args_hash``) are optional: the primary
passes its real loop-gate state; a caller without a loop-gate FSM omits them
and they echo back as ``None`` in the contract dict.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from personal_agent.config import settings
from personal_agent.orchestrator.loop_gate import GateResult, ToolLoopPolicy, stable_hash
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.trace import TraceContext

if TYPE_CHECKING:
    from personal_agent.tools import ToolExecutionLayer

log = get_logger(__name__)


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
) -> dict[str, Any]:
    """Execute one validated tool call and return its result payload.

    This coroutine handles param validation, known-bad-pattern guards, skill
    dedup, tool execution (with full governance via ``execute_tool``), and error
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

    Returns:
        A dict with keys: ``tool_call_id``, ``tool_name``, ``content``,
        ``success``, ``latency_ms``, ``output_hash`` (None on error),
        ``gate_result``, ``args_hash``, ``loop_policy``, ``tool_layer_output``,
        ``tool_layer_error``, ``terminal``, ``terminal_reason``,
        ``terminal_next_step``.
    """
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

    # Pre-execution guard — check known_bad_patterns across all linked skills.
    # Data-driven from docs/skills/*.md frontmatter; new failure modes require
    # only a frontmatter edit, no code changes here. Multiple skills can declare
    # the same tool, so we check all of them.
    from personal_agent.orchestrator.skills import find_skills_for_tool  # noqa: PLC0415

    for linked_skill in find_skills_for_tool(tool_name):
        for bad in linked_skill.known_bad_patterns:
            pattern = str(bad.get("pattern", ""))
            if not pattern:
                continue
            applies_to: dict[str, Any] = bad.get("applies_to") or {}
            applies_tool: str | None = applies_to.get("tool") or None
            applies_fields: list[str] = list(applies_to.get("fields") or [])

            # Only fire for the declared applies_to.tool (defaults to all tools in skill.tools)
            if applies_tool is not None and applies_tool != tool_name:
                continue

            # Search declared fields; fall back to all string-valued arguments
            search_fields = applies_fields or [
                k for k, v in arguments.items() if isinstance(v, str)
            ]

            matched_field: str | None = None
            for fname in search_fields:
                val = arguments.get(fname)
                if isinstance(val, str) and pattern in val:
                    matched_field = fname
                    break

            if matched_field is not None:
                reason = str(bad.get("reason", "Known bad pattern."))
                suggestion = str(bad.get("suggestion", ""))
                error_msg = f"{reason} {suggestion}".strip()
                log.warning(
                    "tool_call_blocked_known_bad_pattern",
                    trace_id=trace_id,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    pattern=pattern,
                    field=matched_field,
                    linked_skill=linked_skill.name,
                )
                return {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "content": json.dumps({"status": "error", "hint": error_msg}),
                    "success": False,
                    "latency_ms": 0,
                    "output_hash": None,
                    "gate_result": gate_result,
                    "args_hash": args_hash,
                    "loop_policy": loop_policy,
                    "tool_layer_output": None,
                    "tool_layer_error": "known_bad_pattern",
                }

    # Skill dedup: if read_skill has already loaded this skill, return marker.
    if tool_name == "read_skill":
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
                "content": json.dumps(
                    {
                        "status": "ok",
                        "body": f"<skill: {_skill_name_arg} already loaded earlier this conversation>",
                    }
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
            tool_name, arguments, trace_ctx, session_id=session_id
        )

        if result.success:
            content = (
                json.dumps(result.output) if isinstance(result.output, dict) else str(result.output)
            )
            output_hash: str | None = stable_hash(result.output)

            # Track loaded skills + post-execution hint for hybrid skill routing.
            if tool_name == "read_skill" and isinstance(result.output, dict):
                _loaded_name = str(result.output.get("skill_name", ""))
                if _loaded_name:
                    loaded_skills.add(_loaded_name)
                    log.info(
                        "read_skill_invoked",
                        skill_name=_loaded_name,
                        trace_id=trace_id,
                    )
            elif result.success and settings.skill_routing_mode == "hybrid":
                # For non-read_skill tools in hybrid mode: hint if linked skill not loaded
                from personal_agent.orchestrator.skills import find_skills_for_tool  # noqa: PLC0415

                _hint_skill = next(
                    (s for s in find_skills_for_tool(tool_name) if s.name not in loaded_skills),
                    None,
                )
                if _hint_skill:
                    content = (
                        content
                        + f"\n\n[hint: skill {_hint_skill.name!r} is available — call read_skill to load full guidance]"
                    )
                    log.debug(
                        "tool_result_skill_hint_appended",
                        tool_name=tool_name,
                        linked_skill=_hint_skill.name,
                        trace_id=trace_id,
                    )
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
            "terminal": False,
            "terminal_reason": None,
            "terminal_next_step": None,
        }
