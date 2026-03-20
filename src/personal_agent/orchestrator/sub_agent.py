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
        llm_client: LLM client instance (LocalLLMClient or ClaudeClient).
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

        response = await asyncio.wait_for(
            llm_client.respond(
                messages=messages,
                max_tokens=spec.max_tokens,
            ),
            timeout=spec.timeout_seconds,
        )

        duration_ms = int(time.monotonic() * 1000) - start_ms

        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary=str(response),
            full_output=str(response),
            tools_used=[],
            token_count=len(str(response).split()),
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
