"""LLM-based reflection generation for Captain's Log.

This module generates reflection entries using DSPy ChainOfThought for structured
outputs, with automatic fallback to manual JSON parsing if DSPy fails.

Based on:
- ADR-0010: Structured LLM Outputs (DSPy adoption)
- ADR-0014: Structured Metrics (deterministic extraction, no LLM for metrics)
- E-008 Test Case A evaluation (100% reliability, ~30-40% code reduction)
"""

import json
from datetime import datetime, timezone
from typing import Any

from personal_agent.captains_log.metrics_extraction import (
    extract_metrics_from_summary,
)
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ProposedChange,
)
from personal_agent.config import settings
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.metrics import get_trace_events
from personal_agent.telemetry.trace import TraceContext

log = get_logger(__name__)

# Import DSPy implementation (may not be available)
try:
    from personal_agent.captains_log.reflection_dspy import generate_reflection_dspy

    DSPY_AVAILABLE = True
except ImportError:
    DSPY_AVAILABLE = False
    log.warning(
        "dspy_reflection_unavailable",
        message="DSPy not available, will use manual JSON parsing fallback",
        component="reflection",
    )


REFLECTION_PROMPT = """You are a personal AI agent analyzing your own task execution to generate insights and improvement proposals.

## Task Context
- **User Message**: {user_message}
- **Trace ID**: {trace_id}
- **Steps Completed**: {steps_count}
- **Final State**: {final_state}
- **Reply Length**: {reply_length} characters

## Telemetry Events
{telemetry_summary}

## Your Task
Analyze this task execution and generate a structured reflection entry with:

1. **Rationale**: What happened? Key observations about the execution.
2. **Supporting Metrics**: Specific metrics that stand out (e.g., "llm_call_duration: 2.3s", "tool_executions: 3")
3. **Proposed Change** (if any): Concrete, actionable improvement suggestion based on evidence
   - What to change
   - Why it would help
   - How to implement it
4. **Impact Assessment**: Expected benefits if the change is implemented

Focus on:
- Performance patterns (slow operations, repeated calls)
- Error patterns (failures, retries)
- Tool usage patterns (which tools, success rates, unnecessary calls, permission issues)
- Tool effectiveness (did tool calls provide value or could LLM handle directly?)
- Mode/governance interactions
- Opportunities for optimization (caching, parallelization, reduced tool calls)

If this was a simple, successful task with no issues, keep the reflection lightweight.
If there were errors, inefficiencies, or interesting patterns, provide deeper analysis.

Respond with ONLY valid JSON in this exact format:
{{
  "rationale": "string",
  "proposed_change": {{
    "what": "string",
    "why": "string",
    "how": "string"
  }} | null,
  "supporting_metrics": ["metric1: value1", "metric2: value2"],
  "impact_assessment": "string" | null,
  "related_adrs": [],
  "related_experiments": []
}}

Do not include markdown formatting, explanations, or any text outside the JSON object."""


async def generate_reflection_entry(
    user_message: str,
    trace_id: str,
    steps_count: int,
    final_state: str | None,
    reply_length: int,
    metrics_summary: dict[str, Any] | None = None,
) -> CaptainLogEntry:
    """Generate an LLM-based reflection entry analyzing task execution.

    Uses DSPy ChainOfThought for structured output generation (ADR-0010).
    Falls back to manual JSON parsing if DSPy is unavailable or fails.

    Includes request-scoped metrics summary (ADR-0012) for performance context.

    Args:
        user_message: The user's original message.
        trace_id: Trace ID for the task execution.
        steps_count: Number of orchestrator steps executed.
        final_state: Final task state (e.g., "COMPLETED", "FAILED"), or None if not available.
        reply_length: Length of the agent's reply.
        metrics_summary: Optional request-scoped metrics summary from RequestMonitor (ADR-0012).

    Returns:
        A rich CaptainLogEntry with LLM-generated insights.

    Notes:
        - Prefers DSPy ChainOfThought (E-008: 0% parse failures)
        - Falls back to manual approach if DSPy unavailable/fails
        - Final fallback: basic reflection with task metadata only
        - Includes performance metrics for richer context (ADR-0012)
    """
    # Query telemetry for this trace (needed for both DSPy and manual)
    trace_events = get_trace_events(trace_id)
    telemetry_summary = _summarize_telemetry(trace_events, metrics_summary)

    # Ensure final_state is a string (DSPy expects string, not None)
    effective_final_state = final_state or "UNKNOWN"

    # Create LLM client
    llm_client = LocalLLMClient(
        base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )

    # Try DSPy approach first (if available)
    if DSPY_AVAILABLE:
        try:
            log.info(
                "attempting_dspy_reflection",
                trace_id=trace_id,
                component="reflection",
            )
            entry = generate_reflection_dspy(
                user_message=user_message,
                trace_id=trace_id,
                steps_count=steps_count,
                final_state=effective_final_state,
                reply_length=reply_length,
                telemetry_summary=telemetry_summary,
                llm_client=llm_client,
                metrics_summary=metrics_summary,  # ADR-0014: Deterministic extraction
            )
            log.info(
                "dspy_reflection_succeeded",
                trace_id=trace_id,
                has_proposal=entry.proposed_change is not None,
                metrics_count=len(entry.supporting_metrics),
                component="reflection",
            )
            return entry
        except Exception as e:
            # DSPy failed, fallback to manual
            log.warning(
                "dspy_reflection_failed_fallback_manual",
                trace_id=trace_id,
                error_type=type(e).__name__,
                error_message=str(e),
                component="reflection",
            )
            # Continue to manual approach below

    # Manual approach (fallback or DSPy not available)
    try:
        log.info(
            "attempting_manual_reflection",
            trace_id=trace_id,
            dspy_available=DSPY_AVAILABLE,
            component="reflection",
        )

        # Manual approach: Create reflection prompt
        prompt = REFLECTION_PROMPT.format(
            user_message=user_message[:200],  # Truncate for prompt
            trace_id=trace_id,
            steps_count=steps_count,
            final_state=effective_final_state,
            reply_length=reply_length,
            telemetry_summary=telemetry_summary,
        )

        # Call LLM with manual prompt (reasoning model)
        response = await llm_client.respond(
            role=ModelRole.REASONING,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,  # Lower temperature for structured output
            max_tokens=3000,  # Increased for reasoning models with thinking process
            reasoning_effort="medium",  # LM Studio /v1/responses: minimal/low/medium/high
            trace_ctx=TraceContext.new_trace(),  # New trace for reflection
        )

        # Parse LLM response (manual JSON parsing)
        # Log the response structure for debugging
        raw_response = response.get("raw", {})
        log.info(
            "reflection_llm_raw_response",
            response_keys=list(response.keys()),
            content_type=type(response.get("content")).__name__,
            content_value=response.get("content")[:100] if response.get("content") else None,
            raw_keys=list(raw_response.keys()) if raw_response else [],
            trace_id=trace_id,
        )

        content = response.get("content", "")
        reasoning_trace = response.get("reasoning_trace", "")

        # DeepSeek R1 and similar models put actual content in raw.choices[0].message.reasoning_content
        raw_response = response.get("raw", {})
        reasoning_content = None
        if raw_response and "choices" in raw_response:
            first_choice = raw_response["choices"][0] if raw_response["choices"] else {}
            message = first_choice.get("message", {})
            reasoning_content = message.get("reasoning_content")

        log.info(
            "reflection_llm_response_received",
            content_length=len(content) if content else 0,
            reasoning_length=len(reasoning_trace) if reasoning_trace else 0,
            reasoning_content_length=len(reasoning_content) if reasoning_content else 0,
            has_content=bool(content),
            has_reasoning=bool(reasoning_trace),
            has_reasoning_content=bool(reasoning_content),
            trace_id=trace_id,
        )

        # Priority: content (actual response) > reasoning_trace > reasoning_content (thinking process)
        if content:
            # Content already set, this is the actual response
            pass
        elif reasoning_trace:
            content = reasoning_trace
            log.info("using_reasoning_trace_as_content", trace_id=trace_id)
        elif reasoning_content:
            content = reasoning_content
            log.info("using_reasoning_content", trace_id=trace_id)

        if not content:
            raise ValueError(
                "LLM returned empty response (no content, reasoning_trace, or reasoning_content)"
            )

        reflection_data = _parse_reflection_response(content)

        # Extract metrics deterministically (ADR-0014) - NO LLM INVOLVED
        # This overrides any metrics the LLM might have generated in the JSON
        string_metrics, structured_metrics = extract_metrics_from_summary(metrics_summary)

        # Create entry with BOTH metric formats (ADR-0014)
        title = f"Task: {user_message[:50]}" if len(user_message) > 50 else f"Task: {user_message}"

        entry = CaptainLogEntry(
            entry_id="",  # Will be generated by manager
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            title=title,
            rationale=reflection_data["rationale"],
            proposed_change=(
                ProposedChange(**reflection_data["proposed_change"])
                if reflection_data.get("proposed_change")
                else None
            ),
            supporting_metrics=string_metrics,  # Deterministic extraction (ADR-0014)
            metrics_structured=structured_metrics if structured_metrics else None,  # ADR-0014
            impact_assessment=reflection_data.get("impact_assessment"),
            status=CaptainLogStatus.AWAITING_APPROVAL,
            related_adrs=reflection_data.get("related_adrs", []),
            related_experiments=reflection_data.get("related_experiments", []),
            telemetry_refs=[
                {
                    "trace_id": trace_id,
                    "metric_name": None,
                    "value": None,
                }
            ],
        )

        log.info(
            "manual_reflection_generated",
            trace_id=trace_id,
            has_proposal=entry.proposed_change is not None,
            metrics_count=len(entry.supporting_metrics),
            metrics_structured_count=len(entry.metrics_structured)
            if entry.metrics_structured
            else 0,
            deterministic_metrics=True,  # ADR-0014
            component="reflection",
        )

        return entry

    except Exception as e:
        # Final fallback to basic reflection if both DSPy and manual fail
        log.warning(
            "all_reflection_methods_failed_fallback_basic",
            trace_id=trace_id,
            error_type=type(e).__name__,
            error_message=str(e),
            component="reflection",
        )
        return _create_basic_reflection_entry(
            user_message, trace_id, steps_count, effective_final_state, reply_length
        )


def _summarize_telemetry(events: list[dict], metrics_summary: dict[str, Any] | None = None) -> str:
    """Summarize telemetry events and metrics for inclusion in LLM prompt.

    Combines traditional telemetry events with request-scoped metrics summary
    (ADR-0012) to provide comprehensive performance context.

    Args:
        events: List of telemetry events.
        metrics_summary: Optional request-scoped metrics from RequestMonitor.

    Returns:
        Human-readable summary string.
    """
    if not events:
        summary_parts = ["No telemetry events found for this trace."]
    else:
        # Extract key events
        event_types = {}
        for event in events:
            event_name = event.get("event", "unknown")
            event_types[event_name] = event_types.get(event_name, 0) + 1

        # Extract timing info
        model_calls = [e for e in events if e.get("event") == "model_call_completed"]
        tool_calls = [e for e in events if e.get("event") == "tool_executed"]
        errors = [e for e in events if "error" in e.get("event", "").lower()]

        summary_parts = [
            f"**Event Counts**: {json.dumps(event_types, indent=2)}",
        ]

        if model_calls:
            avg_duration = sum(e.get("duration_ms", 0) for e in model_calls) / len(model_calls)
            summary_parts.append(
                f"**LLM Calls**: {len(model_calls)} calls, avg duration: {avg_duration:.0f}ms"
            )

        if tool_calls:
            tool_names = [e.get("tool") for e in tool_calls]
            tool_failures = [e for e in tool_calls if not e.get("success")]

            summary_parts.append(
                f"**Tools Used**: {len(tool_calls)} calls - {', '.join(set(tool_names))}"
            )

            if tool_failures:
                failed_tools = [e.get("tool") for e in tool_failures]
                summary_parts.append(
                    f"**Tool Failures**: {len(tool_failures)} failures - {', '.join(failed_tools)}"
                )

            # Extract tool durations
            tool_durations = [
                (e.get("tool"), e.get("duration_ms", 0)) for e in tool_calls if e.get("duration_ms")
            ]
            if tool_durations:
                avg_tool_duration = sum(d for _, d in tool_durations) / len(tool_durations)
                summary_parts.append(f"**Tool Avg Duration**: {avg_tool_duration:.0f}ms")

        if errors:
            error_messages = [e.get("message", "Unknown error") for e in errors[:3]]
            summary_parts.append(f"**Errors**: {len(errors)} errors - {'; '.join(error_messages)}")

    # Add request-scoped metrics summary (ADR-0012)
    if metrics_summary:
        summary_parts.append("\n**System Performance (Request-Scoped)**:")
        summary_parts.append(f"- Duration: {metrics_summary.get('duration_seconds', 0):.1f}s")
        summary_parts.append(
            f"- Samples: {metrics_summary.get('samples_collected', 0)} metric snapshots"
        )

        if "cpu_avg" in metrics_summary:
            summary_parts.append(
                f"- CPU: avg={metrics_summary['cpu_avg']:.1f}%, "
                f"min={metrics_summary.get('cpu_min', 0):.1f}%, "
                f"max={metrics_summary.get('cpu_max', 0):.1f}%"
            )

        if "memory_avg" in metrics_summary:
            summary_parts.append(
                f"- Memory: avg={metrics_summary['memory_avg']:.1f}%, "
                f"min={metrics_summary.get('memory_min', 0):.1f}%, "
                f"max={metrics_summary.get('memory_max', 0):.1f}%"
            )

        if "gpu_avg" in metrics_summary:
            summary_parts.append(
                f"- GPU: avg={metrics_summary['gpu_avg']:.1f}%, "
                f"min={metrics_summary.get('gpu_min', 0):.1f}%, "
                f"max={metrics_summary.get('gpu_max', 0):.1f}%"
            )

        violations = metrics_summary.get("threshold_violations", [])
        if violations:
            summary_parts.append(f"- **Threshold Violations**: {', '.join(violations)}")

    return "\n".join(summary_parts)


def _parse_reflection_response(content: str) -> dict:
    """Parse LLM reflection response.

    Args:
        content: LLM response content (should be JSON).

    Returns:
        Parsed reflection data dictionary.

    Raises:
        ValueError: If response cannot be parsed.
    """
    try:
        # Try to extract JSON from markdown code blocks
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        data = json.loads(content)
        return data
    except (json.JSONDecodeError, IndexError) as e:
        log.warning("reflection_parse_failed", error=str(e), content=content[:200])
        raise ValueError(f"Failed to parse reflection response: {e}") from e


def _create_basic_reflection_entry(
    user_message: str,
    trace_id: str,
    steps_count: int,
    final_state: str,
    reply_length: int,
) -> CaptainLogEntry:
    """Create a basic reflection entry without LLM analysis (fallback).

    Args:
        user_message: The user's original message.
        trace_id: Trace ID for the task execution.
        steps_count: Number of orchestrator steps executed.
        final_state: Final task state.
        reply_length: Length of the agent's reply.

    Returns:
        Basic CaptainLogEntry.
    """
    title = f"Task: {user_message[:50]}" if len(user_message) > 50 else f"Task: {user_message}"

    return CaptainLogEntry(
        entry_id="",  # Will be generated by manager
        timestamp=datetime.now(timezone.utc),
        type=CaptainLogEntryType.REFLECTION,
        title=title,
        rationale=f"Completed task with {steps_count} steps. Trace ID: {trace_id}",
        proposed_change=None,
        supporting_metrics=[
            f"steps_count: {steps_count}",
            f"reply_length: {reply_length}",
            f"final_state: {final_state}",
        ],
        impact_assessment=None,
        status=CaptainLogStatus.AWAITING_APPROVAL,
        telemetry_refs=[
            {
                "trace_id": trace_id,
                "metric_name": None,
                "value": None,
            }
        ],
    )
