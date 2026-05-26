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
- Configured with PRIMARY model
- Telemetry-integrated (trace_id, latency, parse failures)
- Fallback to manual approach if DSPy unavailable (handled by caller)
- Deterministic metrics extraction (ADR-0014) - no LLM for metrics formatting
"""

import inspect
import re
from datetime import datetime, timezone
from typing import Any, cast

from personal_agent.captains_log.dedup import compute_proposal_fingerprint
from personal_agent.captains_log.metrics_extraction import (
    extract_metrics_from_summary,
    format_metrics_string,
)
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposedChange,
    TelemetryRef,
)
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


def _ensure_str(value: Any, default: str = "") -> str:
    """Coerce value to str; use default if it is a coroutine or not a string.

    DSPy prediction can contain coroutines when the LM backend is async and
    reflection runs in asyncio.to_thread (no event loop to await). Passing
    a coroutine to re or fingerprinting causes "expected string or bytes-like
    object, got 'coroutine'".
    """
    if value is None:
        return default
    if inspect.iscoroutine(value):
        value.close()  # suppress "RuntimeWarning: coroutine was never awaited"
        log.warning(
            "reflection_field_was_coroutine",
            field_type=type(value).__name__,
            component="reflection_dspy",
        )
        return default
    if isinstance(value, str):
        return value
    return str(value)


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
        user_message: str = dspy.InputField(desc="The user's original message")
        trace_id: str = dspy.InputField(desc="Trace ID for the task execution")
        steps_count: int = dspy.InputField(desc="Number of orchestrator steps executed")
        final_state: str = dspy.InputField(desc="Final task state (e.g., COMPLETED, FAILED)")
        reply_length: int = dspy.InputField(desc="Length of agent's reply in characters")
        telemetry_summary: str = dspy.InputField(
            desc="Summarized telemetry events showing LLM calls, tool executions, errors"
        )
        metrics_summary: str = dspy.InputField(
            desc="Pre-formatted system metrics from RequestMonitor (e.g., 'cpu: 9.3%, duration: 5.4s')"
        )
        # ADR-0056 Phase 2 — failure-path reflection (GEPA-inspired)
        failure_excerpt: str = dspy.InputField(
            desc=(
                "JSON-serialized FailureExcerpt with failed tool calls, error summary, "
                "and recovery actions. Empty string when had_errors is False."
            )
        )
        had_errors: bool = dspy.InputField(
            desc="True when the trace contained at least one tool call failure or error event."
        )

        # Output fields (metrics removed - now deterministically extracted)
        rationale: str = dspy.OutputField(
            desc="Analysis of what happened and key observations about the execution"
        )
        proposed_change_what: str = dspy.OutputField(
            desc="What to change (empty string if no change proposed)"
        )
        proposed_change_why: str = dspy.OutputField(
            desc="Why this change would help (empty string if no change proposed)"
        )
        proposed_change_how: str = dspy.OutputField(
            desc="How to implement this change (empty string if no change proposed)"
        )
        proposed_change_category: str = dspy.OutputField(
            desc=(
                "Category of proposed change. One of: performance, reliability, "
                "concurrency, knowledge, cost, ux, observability, architecture, safety. "
                "Empty string if no change proposed."
            )
        )
        proposed_change_scope: str = dspy.OutputField(
            desc=(
                "Target subsystem of proposed change. One of: llm_client, orchestrator, "
                "second_brain, captains_log, brainstem, tools, telemetry, governance, "
                "insights, config, cross_cutting. Empty string if no change proposed."
            )
        )
        impact_assessment: str = dspy.OutputField(
            desc="Expected benefits if change is implemented (empty string if none)"
        )
        # ADR-0056 Phase 2 — failure-path fix suggestion
        failure_path_fix_what: str = dspy.OutputField(
            desc=(
                "Surgical fix (≤ 80 chars) that would have prevented this exact failure. "
                "Example: 'Add retry-with-scope-reduction note to query_elasticsearch tool description.' "
                "Return empty string if had_errors is False or no specific fix is identifiable."
            )
        )
        failure_path_fix_location: str = dspy.OutputField(
            desc=(
                "File path + symbol of the text to edit, if known. "
                "Example: 'src/personal_agent/tools/fetch_url.py::DESCRIPTION' or "
                "'docs/skills/fetch_url.md'. Empty string if had_errors is False or unknown."
            )
        )
        # FRE-328 follow-up — capability gap recognition during reflection
        missing_skill_names: str = dspy.OutputField(
            desc=(
                "Skills you needed but didn't have. "
                "Format: {domain}-{noun}. "
                "Nouns: fetcher, runner, sender, writer, monitor, checker, scanner, "
                "analyzer, summarizer, generator, creator, tracker, detector, validator, notifier. "
                "Same gap = same name every time. "
                "Max 3, comma-separated. Empty string if nothing was missing."
            )
        )

    DSPY_AVAILABLE = True
except ImportError:
    dspy = None  # type: ignore[assignment,unused-ignore]
    GenerateReflection = None  # type: ignore[assignment,misc]
    DSPY_AVAILABLE = False


def _parse_enum(enum_cls: type, raw: str) -> object | None:
    """Safely parse an LLM-produced string into an enum value.

    Returns None if the string doesn't match any member.
    """
    raw = raw.strip().lower()
    if not raw:
        return None
    try:
        return cast(object, enum_cls(raw))
    except ValueError:
        return None


_MISSING_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
_MISSING_SKILL_MAX = 3


def parse_missing_skill_names(raw: str, trace_id: str = "") -> list[str]:
    """Parse the ``missing_skill_names`` DSPy field into a clean, capped list.

    Pure validation step: lowercases, dedupes (case-insensitively), rejects
    anything that doesn't match the kebab-case regex, and caps at
    ``_MISSING_SKILL_MAX`` names.

    Emission of the ``missing_skill_requested`` warning event is deliberately
    NOT done here — it must happen on the main asyncio event loop so the
    ``ElasticsearchHandler`` can forward it.  DSPy reflection runs via
    ``asyncio.to_thread``, where ``loop.is_running()`` is False and ES emission
    is silently skipped (see ``telemetry/es_handler.py``).  Callers should
    invoke ``emit_missing_skill_warnings`` on the main loop after the thread
    returns.

    Args:
        raw: The DSPy ``missing_skill_names`` output (comma-separated names).
        trace_id: Trace ID, used only for debug logging on rejection.

    Returns:
        Deduped, validated list of kebab-case skill names (max ``_MISSING_SKILL_MAX``).
    """
    if not raw or not raw.strip():
        return []
    seen: set[str] = set()
    accepted: list[str] = []
    for token in raw.split(","):
        name = token.strip().lower()
        if not name or name in seen:
            continue
        if not _MISSING_SKILL_NAME_RE.match(name):
            log.debug(
                "missing_skill_name_rejected_by_validator",
                requested_name=name,
                trace_id=trace_id,
                component="reflection_dspy",
            )
            continue
        seen.add(name)
        accepted.append(name)
        if len(accepted) >= _MISSING_SKILL_MAX:
            break
    return accepted


def emit_missing_skill_warnings(
    names: list[str], trace_id: str, session_id: str | None = None
) -> None:
    """Emit one ``missing_skill_requested`` warning per name (main-loop only).

    Must be called from a coroutine running on the main asyncio event loop so
    the ``ElasticsearchHandler`` forwards the events to agent-logs-* — that is
    the index ``InsightsEngine.detect_missing_skill_patterns`` aggregates over.

    Args:
        names: Validated names from ``parse_missing_skill_names``.
        trace_id: Trace ID of the reflection's source task.
        session_id: Session ID of the reflection's source task.  Required for
            the ≥2-distinct-sessions threshold in
            ``InsightsEngine.detect_missing_skill_patterns``.
    """
    for name in names:
        log.warning(
            "missing_skill_requested",
            trace_id=trace_id,
            session_id=session_id,
            requested_name=name,
            source="reflection",
            component="reflection_dspy",
        )


def generate_reflection_dspy(
    user_message: str,
    trace_id: str,
    steps_count: int,
    final_state: str,
    reply_length: int,
    telemetry_summary: str,
    llm_client: LocalLLMClient,
    metrics_summary: dict[str, Any] | None = None,
    captains_log_role: str | None = None,
    failure_excerpt_json: str = "",
    had_errors: bool = False,
    hit_iteration_limit: bool = False,
    task_type: str = "",
    iteration_count: int = 0,
    max_iterations: int = 0,
) -> tuple[CaptainLogEntry, list[str]]:
    """Generate reflection using DSPy ChainOfThought with deterministic metrics extraction.

    Returns:
        A tuple of ``(entry, missing_skill_names)``.  The names list is
        validated by ``parse_missing_skill_names`` but NOT emitted as warnings
        here — DSPy runs in a worker thread where ES emission is silently
        skipped.  The main-loop caller must invoke
        ``emit_missing_skill_warnings(names, trace_id)`` to surface the events.

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
        llm_client: LocalLLMClient instance (used as fallback when
            captains_log_role is None).
        metrics_summary: Dict from RequestMonitor with system metrics (ADR-0014).
            Metrics are extracted deterministically, not generated by LLM.
        captains_log_role: Optional role name from models.yaml (e.g.
            ``"gpt-5.4-nano"``, ``"claude_sonnet"``). When provided, DSPy is
            configured with this model — supporting both local and cloud
            endpoints. When None, falls back to ``llm_client`` PRIMARY model.
        failure_excerpt_json: JSON-serialized ``FailureExcerpt`` from
            ``_extract_failure_excerpt()``. Empty string when Phase 2 is
            disabled or no failures were found (ADR-0056 §D6).
        had_errors: ``True`` when the trace contained at least one failure
            event. Controls whether failure-path output fields are populated.
        hit_iteration_limit: True when agent was forced to stop by the iteration cap.
            Prepended to telemetry_summary to nudge a cap-raise proposal.
        task_type: TaskType value for the capped request (e.g. "analysis").
        iteration_count: Tool iterations consumed before the cap fired.
        max_iterations: Effective iteration cap that was applied.

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
        ...     captains_log_role="gpt-5.4-nano",
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

    # Prepend iteration-limit signal to telemetry_summary so the reflection
    # model is nudged to propose a cap-raise for the exhausted TaskType.
    if hit_iteration_limit:
        limit_note = (
            f"[ITERATION LIMIT HIT] task_type={task_type or 'unknown'} "
            f"used {iteration_count}/{max_iterations} iterations. "
            "Agent was forced to stop before completing analysis. "
            "Consider proposing a cap-raise for this TaskType."
        )
        telemetry_summary = f"{limit_note}\n{telemetry_summary}"

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
        # Configure DSPy with the captains_log model (local or cloud).
        # Use dspy.context() instead of dspy.configure() for background tasks
        # (Captain's Log reflection runs in a different async task).
        if captains_log_role is not None:
            from personal_agent.llm_client.dspy_adapter import configure_dspy_lm

            lm = configure_dspy_lm(role=captains_log_role)
        else:
            lm = llm_client.get_dspy_lm(role=ModelRole.PRIMARY)

        log.info(
            "dspy_configured_for_reflection",
            model_role="primary",
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
                failure_excerpt=failure_excerpt_json,
                had_errors=had_errors,
            )

            log.info(
                "dspy_reflection_generated",
                has_rationale=bool(_ensure_str(getattr(result, "rationale", ""))),
                has_proposed_change=bool(
                    _ensure_str(getattr(result, "proposed_change_what", "")).strip()
                ),
                metrics_count=len(string_metrics),
                trace_id=trace_id,
                component="reflection_dspy",
            )

        # Convert DSPy result to CaptainLogEntry (outside context manager)
        # Coerce to str in case DSPy/async LM left coroutines in result fields.
        proposed_change_what = _ensure_str(getattr(result, "proposed_change_what", ""))
        proposed_change = None
        if proposed_change_what and proposed_change_what.strip():
            category = _parse_enum(
                ChangeCategory, _ensure_str(getattr(result, "proposed_change_category", ""))
            )
            scope = _parse_enum(
                ChangeScope, _ensure_str(getattr(result, "proposed_change_scope", ""))
            )

            fingerprint = None
            if category and scope:
                fingerprint = compute_proposal_fingerprint(
                    cast(ChangeCategory, category),
                    cast(ChangeScope, scope),
                    proposed_change_what,
                )

            proposed_change = ProposedChange(
                what=proposed_change_what,
                why=_ensure_str(getattr(result, "proposed_change_why", ""), ""),
                how=_ensure_str(getattr(result, "proposed_change_how", ""), ""),
                category=cast(ChangeCategory | None, category),
                scope=cast(ChangeScope | None, scope),
                fingerprint=fingerprint,
                first_seen=datetime.now(timezone.utc),
            )

        # Parse impact_assessment (empty string if none)
        impact_raw = _ensure_str(getattr(result, "impact_assessment", ""), "")
        impact_assessment = impact_raw.strip() or None

        # Create title
        title = f"Task: {user_message[:50]}" if len(user_message) > 50 else f"Task: {user_message}"

        # Phase 2: extract surgical fix suggestion (ADR-0056 §D6)
        fix_what = _ensure_str(getattr(result, "failure_path_fix_what", ""), "").strip()
        fix_location = _ensure_str(getattr(result, "failure_path_fix_location", ""), "").strip()
        potential_impl: list[str] | None = None
        if fix_what and had_errors:
            potential_impl = [fix_what]
            if fix_location:
                potential_impl.append(f"Location: {fix_location}")

        # Create entry with BOTH metric formats (ADR-0014)
        # - string_metrics: Human-readable (deterministic extraction)
        # - structured_metrics: Typed values for analytics
        rationale_str = _ensure_str(getattr(result, "rationale", ""), "No rationale")
        entry = CaptainLogEntry(
            entry_id="",  # Will be generated by manager
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            status=CaptainLogStatus.AWAITING_APPROVAL,
            title=title,
            rationale=rationale_str,
            proposed_change=proposed_change,
            supporting_metrics=string_metrics,  # Deterministic extraction (ADR-0014)
            metrics_structured=structured_metrics if structured_metrics else None,  # ADR-0014
            impact_assessment=impact_assessment,
            related_adrs=[],  # Could be enhanced with LLM extraction
            related_experiments=[],  # Could be enhanced with LLM extraction
            telemetry_refs=[TelemetryRef(trace_id=trace_id)] if trace_id else [],
            potential_implementation=potential_impl,
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

        # FRE-328 follow-up — capability-gap recognition during reflection.
        # Pure parse only — the main-loop caller is responsible for emitting
        # the missing_skill_requested warnings so the ES handler can see them.
        missing_skills_raw = _ensure_str(getattr(result, "missing_skill_names", ""), "")
        missing_skill_names = parse_missing_skill_names(missing_skills_raw, trace_id=trace_id)
        if missing_skill_names:
            log.info(
                "reflection_missing_skills_parsed",
                trace_id=trace_id,
                count=len(missing_skill_names),
                names=missing_skill_names,
                component="reflection_dspy",
            )

        return entry, missing_skill_names

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
