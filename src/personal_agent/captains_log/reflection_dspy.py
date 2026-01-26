"""DSPy-based reflection generation for Captain's Log.

This module implements structured reflection generation using DSPy ChainOfThought,
replacing manual JSON parsing with type-safe structured outputs.

Based on:
- E-008 Test Case A (100% reliability, ~30-40% code reduction)
- ADR-0010: Structured LLM Outputs (selective adoption for Captain's Log)
- ADR-0014: Structured Metrics (deterministic extraction, no LLM for metrics)
- experiments/dspy_prototype/test_case_a_reflection.py

Performance (from E-008):
- Parse failure rate: 0% (5/5 successful)
- Latency overhead: +21% vs manual (acceptable: 11.8s → 14.3s)
- Code reduction: ~30-40%

Design:
- Uses DSPy ChainOfThought for reasoning + structured output
- Configured with REASONING model (qwen/qwen3-8b)
- Telemetry-integrated (trace_id, latency, parse failures)
- Fallback to manual approach if DSPy unavailable (handled by caller)
- Deterministic metrics extraction (ADR-0014) - no LLM for metrics formatting
"""

from datetime import datetime, timezone
from typing import Any

from personal_agent.captains_log.metrics_extraction import (
    extract_metrics_from_summary,
    format_metrics_string,
)
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ProposedChange,
    TelemetryRef,
)
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

try:
    import dspy  # type: ignore[import-untyped]

    class GenerateReflection(dspy.Signature):  # type: ignore[misc]
        """Generate structured reflection on task execution to propose improvements.

        Analyzes task execution telemetry to identify patterns, issues, and opportunities
        for optimization. Proposes concrete, actionable improvements based on evidence.

        Focus areas:
        - Performance patterns (slow operations, repeated calls)
        - Error patterns (failures, retries)
        - Tool usage patterns (effectiveness, unnecessary calls)
        - Mode/governance interactions
        - Optimization opportunities (caching, parallelization)
        """

        # Input fields
        user_message: str = dspy.InputField(desc="The user's original message")  # type: ignore[misc]
        trace_id: str = dspy.InputField(desc="Trace ID for the task execution")  # type: ignore[misc]
        steps_count: int = dspy.InputField(desc="Number of orchestrator steps executed")  # type: ignore[misc]
        final_state: str = dspy.InputField(desc="Final task state (e.g., COMPLETED, FAILED)")  # type: ignore[misc]
        reply_length: int = dspy.InputField(desc="Length of agent's reply in characters")  # type: ignore[misc]
        telemetry_summary: str = dspy.InputField(  # type: ignore[misc]
            desc="Summarized telemetry events showing LLM calls, tool executions, errors"
        )
        metrics_summary: str = dspy.InputField(  # type: ignore[misc]
            desc="Pre-formatted system metrics from RequestMonitor (e.g., 'cpu: 9.3%, duration: 5.4s')"
        )

        # Output fields (metrics removed - now deterministically extracted)
        rationale: str = dspy.OutputField(  # type: ignore[misc]
            desc="Analysis of what happened and key observations about the execution"
        )
        proposed_change_what: str = dspy.OutputField(  # type: ignore[misc]
            desc="What to change (empty string if no change proposed)"
        )
        proposed_change_why: str = dspy.OutputField(  # type: ignore[misc]
            desc="Why this change would help (empty string if no change proposed)"
        )
        proposed_change_how: str = dspy.OutputField(  # type: ignore[misc]
            desc="How to implement this change (empty string if no change proposed)"
        )
        impact_assessment: str = dspy.OutputField(  # type: ignore[misc]
            desc="Expected benefits if change is implemented (empty string if none)"
        )

    DSPY_AVAILABLE = True
except ImportError:
    dspy = None  # type: ignore[assignment,unused-ignore]
    GenerateReflection = None  # type: ignore[assignment,misc]
    DSPY_AVAILABLE = False


def generate_reflection_dspy(
    user_message: str,
    trace_id: str,
    steps_count: int,
    final_state: str,
    reply_length: int,
    telemetry_summary: str,
    llm_client: LocalLLMClient,
    metrics_summary: dict[str, Any] | None = None,
) -> CaptainLogEntry:
    """Generate reflection using DSPy ChainOfThought with deterministic metrics extraction.

    Raises:
        ImportError: If dspy is not installed.

    This function uses DSPy's ChainOfThought module to generate structured
    reflections with built-in reasoning. Metrics are extracted deterministically
    (no LLM formatting) for 100% reliability and consistency (ADR-0014).

    Args:
        user_message: The user's original message (will be truncated to 200 chars).
        trace_id: Trace ID for telemetry correlation.
        steps_count: Number of orchestrator steps executed.
        final_state: Final task state (e.g., "COMPLETED", "FAILED").
        reply_length: Length of agent's reply in characters.
        telemetry_summary: Summarized telemetry events for analysis.
        llm_client: LocalLLMClient instance for DSPy configuration.
        metrics_summary: Dict from RequestMonitor with system metrics (ADR-0014).
            Metrics are extracted deterministically, not generated by LLM.

    Returns:
        CaptainLogEntry with DSPy-generated reflection and deterministic metrics.

    Raises:
        ImportError: If dspy is not installed.
        Exception: If DSPy generation fails (caller should fallback to manual).

    Example:
        >>> llm_client = LocalLLMClient()
        >>> metrics = {"duration_seconds": 5.4, "cpu_avg": 9.3}
        >>> entry = generate_reflection_dspy(
        ...     user_message="What is Python?",
        ...     trace_id="trace-123",
        ...     steps_count=3,
        ...     final_state="COMPLETED",
        ...     reply_length=150,
        ...     telemetry_summary="4 LLM calls, 0 tool calls, 0 errors",
        ...     llm_client=llm_client,
        ...     metrics_summary=metrics,
        ... )
        >>> assert entry.rationale != ""
        >>> assert len(entry.supporting_metrics) > 0  # Deterministic extraction
        >>> assert entry.metrics_structured is not None  # Structured format
    """
    if dspy is None:
        raise ImportError(
            "dspy package is required for DSPy-based reflection. Install with: uv add dspy>=3.1.0"
        )

    # Extract metrics deterministically (ADR-0014) - NO LLM INVOLVED
    string_metrics, structured_metrics = extract_metrics_from_summary(metrics_summary)
    metrics_string = format_metrics_string(string_metrics)

    # Log DSPy reflection attempt
    log.info(
        "dspy_reflection_started",
        user_message_length=len(user_message),
        trace_id=trace_id,
        steps_count=steps_count,
        final_state=final_state,
        metrics_extracted=len(string_metrics),
        component="reflection_dspy",
    )

    try:
        # Configure DSPy with REASONING model
        # Use dspy.context() instead of dspy.configure() for background tasks
        # (Captain's Log reflection runs in a different async task)
        lm = llm_client.get_dspy_lm(role=ModelRole.REASONING)

        log.info(
            "dspy_configured_for_reflection",
            model_role="reasoning",
            trace_id=trace_id,
            component="reflection_dspy",
        )

        # Use context manager to avoid "can only be called from same async task" error
        with dspy.context(lm=lm):
            # Create ChainOfThought predictor
            reflection_generator = dspy.ChainOfThought(GenerateReflection)

            # Generate reflection
            # Metrics are pre-formatted (deterministic), LLM only generates insights
            result = reflection_generator(
                user_message=user_message[:200],
                trace_id=trace_id,
                steps_count=steps_count,
                final_state=final_state,
                reply_length=reply_length,
                telemetry_summary=telemetry_summary,
                metrics_summary=metrics_string,  # Pre-formatted, deterministic
            )

            log.info(
                "dspy_reflection_generated",
                has_rationale=bool(result.rationale),
                has_proposed_change=bool(result.proposed_change_what.strip()),
                metrics_count=len(string_metrics),
                trace_id=trace_id,
                component="reflection_dspy",
            )

        # Convert DSPy result to CaptainLogEntry (outside context manager)
        # Parse proposed_change
        proposed_change = None
        if result.proposed_change_what and result.proposed_change_what.strip():
            proposed_change = ProposedChange(
                what=result.proposed_change_what,
                why=result.proposed_change_why or "",
                how=result.proposed_change_how or "",
            )

        # Parse impact_assessment (empty string if none)
        impact_assessment = (
            result.impact_assessment
            if result.impact_assessment and result.impact_assessment.strip()
            else None
        )

        # Create title
        title = f"Task: {user_message[:50]}" if len(user_message) > 50 else f"Task: {user_message}"

        # Create entry with BOTH metric formats (ADR-0014)
        # - string_metrics: Human-readable (deterministic extraction)
        # - structured_metrics: Typed values for analytics
        entry = CaptainLogEntry(
            entry_id="",  # Will be generated by manager
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            status=CaptainLogStatus.AWAITING_APPROVAL,
            title=title,
            rationale=result.rationale,
            proposed_change=proposed_change,
            supporting_metrics=string_metrics,  # Deterministic extraction (ADR-0014)
            metrics_structured=structured_metrics if structured_metrics else None,  # ADR-0014
            impact_assessment=impact_assessment,
            related_adrs=[],  # Could be enhanced with LLM extraction
            related_experiments=[],  # Could be enhanced with LLM extraction
            telemetry_refs=[TelemetryRef(trace_id=trace_id)] if trace_id else [],
        )

        log.info(
            "dspy_reflection_entry_created",
            entry_type=entry.type.value,
            has_proposed_change=entry.proposed_change is not None,
            metrics_count=len(entry.supporting_metrics),
            metrics_structured_count=len(entry.metrics_structured)
            if entry.metrics_structured
            else 0,
            deterministic_metrics=True,  # ADR-0014
            trace_id=trace_id,
            component="reflection_dspy",
        )

        return entry

    except Exception as e:
        # Log DSPy failure (caller should fallback to manual)
        log.error(
            "dspy_reflection_failed",
            error_type=type(e).__name__,
            error_message=str(e),
            trace_id=trace_id,
            component="reflection_dspy",
        )
        raise  # Caller will catch and fallback to manual
