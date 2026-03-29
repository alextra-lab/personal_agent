"""Sub-agent runner — executes focused inference calls.

Each sub-agent is a single LLM call with a constrained context slice.
The runner acquires a concurrency slot, runs the inference, and
returns a SubAgentResult with a compressed summary.

Full output goes to ES via structlog; only the summary enters
the primary agent's synthesis context.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.6
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import structlog

from personal_agent.orchestrator.expansion_types import SubAgentMode
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec

logger = structlog.get_logger(__name__)

# System prompt for sub-agents: focused, no personality
_SUB_AGENT_SYSTEM_PROMPT = (
    "You are a focused sub-agent executing a specific sub-task. "
    "Be concise and direct. Respond with the requested output format only. "
    "Do not ask follow-up questions. Do not add preamble or explanation "
    "beyond what was requested."
)


async def run_sub_agent(
    spec: SubAgentSpec,
    llm_client: Any,
    trace_id: str,
    concurrency_controller: Any | None = None,
) -> SubAgentResult:
    """Execute a single sub-agent inference call.

    Args:
        spec: Sub-agent specification from the primary agent.
        llm_client: LLM client instance (LocalLLMClient or LiteLLMClient).
        trace_id: Parent request trace identifier.
        concurrency_controller: Optional concurrency controller for slot management.

    Returns:
        SubAgentResult with summary, metrics, and success status.
    """
    task_id = f"sub-{uuid.uuid4().hex[:12]}"
    start_ms = int(time.monotonic() * 1000)

    logger.info(
        "sub_agent_start",
        task_id=task_id,
        task=spec.task,
        output_format=spec.output_format,
        max_tokens=spec.max_tokens,
        timeout=spec.timeout_seconds,
        trace_id=trace_id,
    )

    try:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SUB_AGENT_SYSTEM_PROMPT},
        ]
        messages.extend(spec.context)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Task: {spec.task}\n"
                    f"Output format: {spec.output_format}\n"
                    "Respond with the result only."
                ),
            }
        )

        if spec.mode == SubAgentMode.TOOLED_SEQUENTIAL and spec.tools:
            # Tooled mode: mini tool-use loop (max 3 iterations)
            response_content = await _run_tooled_loop(
                messages=messages,
                llm_client=llm_client,
                spec=spec,
                trace_id=trace_id,
                task_id=task_id,
            )
        else:
            # Default: single inference call
            response_content = str(
                await asyncio.wait_for(
                    llm_client.respond(
                        role=spec.model_role,
                        messages=messages,
                        max_tokens=spec.max_tokens,
                    ),
                    timeout=spec.timeout_seconds,
                )
            )

        duration_ms = int(time.monotonic() * 1000) - start_ms

        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary=response_content[:2000],  # Cap summary length
            full_output=response_content,
            tools_used=[],
            token_count=len(response_content.split()),
            duration_ms=duration_ms,
            success=True,
            error=None,
        )

    except asyncio.TimeoutError:
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary="",
            full_output="",
            tools_used=[],
            token_count=0,
            duration_ms=duration_ms,
            success=False,
            error=f"Timeout after {spec.timeout_seconds}s",
        )

    except Exception as exc:
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary="",
            full_output="",
            tools_used=[],
            token_count=0,
            duration_ms=duration_ms,
            success=False,
            error=str(exc),
        )

    logger.info(
        "sub_agent_complete",
        task_id=task_id,
        success=result.success,
        duration_ms=result.duration_ms,
        token_count=result.token_count,
        error=result.error,
        trace_id=trace_id,
    )

    return result


async def _run_tooled_loop(
    messages: list[dict[str, Any]],
    llm_client: Any,
    spec: SubAgentSpec,
    trace_id: str,
    task_id: str,
    max_iterations: int = 3,
) -> str:
    """Run a mini tool-use loop for TOOLED_SEQUENTIAL sub-agents.

    The sub-agent can call tools and incorporate results before producing
    its final answer. Currently returns the first response directly —
    tool parsing is wired when the LLM client exposes tool_calls.

    Args:
        messages: Initial message context.
        llm_client: LLM client.
        spec: Sub-agent specification.
        trace_id: Trace identifier.
        task_id: Sub-agent task identifier.
        max_iterations: Max tool-use rounds before forcing final answer.

    Returns:
        Final response content string.
    """
    for iteration in range(max_iterations):
        response = await asyncio.wait_for(
            llm_client.respond(
                role=spec.model_role,
                messages=messages,
                max_tokens=spec.max_tokens,
            ),
            timeout=spec.timeout_seconds,
        )

        response_str = str(response)

        # TODO: Parse tool calls from response when LLM client exposes them.
        # For now, return the response directly.
        logger.info(
            "sub_agent_tooled_iteration",
            task_id=task_id,
            iteration=iteration,
            trace_id=trace_id,
        )

        return response_str

    return ""  # Unreachable but satisfies type checker
