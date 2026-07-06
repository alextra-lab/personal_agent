"""LLM-based reflection generation for Captain's Log.

This module generates reflection entries using DSPy ChainOfThought for structured
outputs, with automatic fallback to manual JSON parsing if DSPy fails.

Based on:
- ADR-0010: Structured LLM Outputs (DSPy adoption)
- ADR-0014: Structured Metrics (deterministic extraction, no LLM for metrics)
- E-008 Test Case A evaluation (100% reliability, ~30-40% code reduction)
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, cast

from personal_agent.captains_log.dedup import compute_proposal_fingerprint
from personal_agent.captains_log.metrics_extraction import (
    extract_metrics_from_summary,
)
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposalSource,
    ProposedChange,
    TelemetryRef,
)
from personal_agent.captains_log.prompt_manifest import (
    build_prompt_manifest,
    load_mean_rating_lookup,
)
from personal_agent.config import settings
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.sysgraph import SysgraphRepository, get_default_sysgraph_repo
from personal_agent.sysgraph.dedup import ReadBeforeEmitDecision, check_before_emit
from personal_agent.sysgraph.repository import ProposalRecord
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.metrics import get_trace_events
from personal_agent.telemetry.trace import SystemTraceContext

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


# ---------------------------------------------------------------------------
# Phase 2 — Failure-path reflection types (ADR-0056 §D6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailedToolCall:
    """One failed tool call extracted from a trace (ADR-0056 Phase 2).

    Attributes:
        name: Tool name (e.g. ``"fetch_url"``).
        arguments: Tool arguments at the time of the failure.
        error_message: Exception message or status description.
        trace_id: Trace ID for ES correlation.
    """

    name: str
    arguments: Mapping[str, Any]
    error_message: str
    trace_id: str


@dataclass
class FailureExcerpt:
    """Failure-path summary extracted from a single trace (ADR-0056 Phase 2).

    Attributes:
        failed_tool_calls: Tool calls that ended in error or timeout.
        error_summary: Short human-readable summary of the failure (≤ 200 chars).
        recovery_actions: What the agent did after each failure (retry, fallback, etc.).
    """

    failed_tool_calls: list[FailedToolCall] = field(default_factory=list)
    error_summary: str = ""
    recovery_actions: list[str] = field(default_factory=list)


def _extract_failure_excerpt(trace_events: Sequence[dict[str, Any]]) -> FailureExcerpt | None:
    """Extract a failure-path excerpt from a trace's raw events.

    Walks ``trace_events`` looking for events whose ``event`` name or ``status``
    field signals a tool failure (``"tool_call_failed"``, ``status in {"error",
    "timeout"}``). For each failure, checks the next event to detect retries or
    fallback strategies.

    Returns ``None`` when the trace has no error events (Phase 2 should skip).

    Args:
        trace_events: List of structlog event dicts from ``get_trace_events()``.

    Returns:
        ``FailureExcerpt`` when at least one failure is present, else ``None``.
    """
    failed_calls: list[FailedToolCall] = []
    recovery_actions: list[str] = []
    last_error: str = ""

    for i, event in enumerate(trace_events):
        ev_name = str(event.get("event", ""))
        status = str(event.get("status", ""))
        is_failure = ev_name in {
            "tool_call_failed",
            "tool_execution_failed",
            "tool_timeout",
        } or status in {"error", "timeout", "failed"}

        if not is_failure:
            continue

        tool_name = str(event.get("tool_name", event.get("tool", "unknown")))
        error_msg = str(event.get("error", event.get("error_message", status or "unknown error")))
        trace_id = str(event.get("trace_id", ""))
        arguments: Mapping[str, Any] = event.get("arguments", {}) or {}

        failed_calls.append(
            FailedToolCall(
                name=tool_name,
                arguments=arguments,
                error_message=error_msg,
                trace_id=trace_id,
            )
        )
        last_error = error_msg

        # Check for recovery in the next event
        if i + 1 < len(trace_events):
            next_ev = trace_events[i + 1]
            next_tool = str(next_ev.get("tool_name", next_ev.get("tool", "")))
            if next_tool == tool_name:
                recovery_actions.append(f"Retried {tool_name} with same arguments")
            elif next_tool:
                recovery_actions.append(f"Switched from {tool_name} to {next_tool}")

    if not failed_calls:
        return None

    error_summary = last_error[:200] if last_error else f"{len(failed_calls)} tool failure(s)"
    return FailureExcerpt(
        failed_tool_calls=failed_calls,
        error_summary=error_summary,
        recovery_actions=recovery_actions,
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

## Prompt Composition (this turn)
{prompt_manifest}

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
- Prompt composition patterns (unused components, unstable static prefix hash, low/declining
  callsite ratings); when proposing a prompt change, name the specific component_id from the
  taxonomy and set scope to llm_client or orchestrator

If this was a simple, successful task with no issues, keep the reflection lightweight.
If there were errors, inefficiencies, or interesting patterns, provide deeper analysis.

Respond with ONLY valid JSON in this exact format:
{{
  "rationale": "string",
  "proposed_change": {{
    "what": "string",
    "why": "string",
    "how": "string",
    "category": "performance|reliability|concurrency|knowledge|cost|ux|observability|architecture|safety",
    "scope": "llm_client|orchestrator|second_brain|captains_log|brainstem|tools|telemetry|governance|insights|config|cross_cutting"
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
    hit_iteration_limit: bool = False,
    task_type: str = "",
    iteration_count: int = 0,
    max_iterations: int = 0,
    session_id: str | None = None,
    eval_mode: bool = False,
    sysgraph_repo: SysgraphRepository | None = None,
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
        hit_iteration_limit: True when the agent was forced to stop by the tool iteration cap.
            When True, the reflection model is nudged to propose raising the per-TaskType cap.
        task_type: TaskType value string (e.g. "analysis") for cap-raise proposals.
        iteration_count: Actual tool iterations consumed this request.
        max_iterations: Effective cap that was applied.
        session_id: Optional session ID for the task; threaded through to the
            ``missing_skill_requested`` warnings emitted on capability-gap
            recognition (FRE-328) so the ≥2-distinct-sessions clustering
            threshold can be evaluated.
        eval_mode: True when the task originated from an eval run (FRE-523).
            Stamped onto the returned entry so the promotion pipeline skips it
            (no Linear issues filed off eval prompts).
        sysgraph_repo: Optional connected SysgraphRepository for the ADR-0105
            D9/FRE-721 generation-time read-before-emit check. When ``None``
            (the default), falls back to the process-level shared repository
            via ``get_default_sysgraph_repo()`` — never blocks reflection
            generation when sysgraph is unwired or unreachable.

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

    # FRE-409: Build prompt-composition manifest from already-fetched trace events.
    # The mean-rating lookup is best-effort; reflection proceeds even if ES is down.
    mean_rating_lookup = await load_mean_rating_lookup(days=7, trace_id=trace_id)
    prompt_manifest = build_prompt_manifest(trace_events, mean_rating_lookup=mean_rating_lookup)
    log.info(
        "reflection_prompt_manifest_built",
        trace_id=trace_id,
        manifest_available=prompt_manifest != "Prompt manifest: unavailable",
        component="reflection",
    )

    # Ensure final_state is a string (DSPy expects string, not None)
    effective_final_state = final_state or "UNKNOWN"

    # Resolve the configured model for Captain's Log (ADR-0031).
    # DSPy handles both local and cloud models via configure_dspy_lm() — no
    # separate cloud branch needed (FRE-253).
    from personal_agent.config import resolve_role_model_key

    _captains_log_role = resolve_role_model_key("captains_log")

    # ── DSPy → manual JSON → basic ───────────────────────────────────────────
    llm_client = LocalLLMClient(
        base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )

    # Phase 2: failure-path excerpt (ADR-0056 §D6, default False until validated)
    failure_excerpt_json = ""
    had_errors = False
    if settings.failure_path_reflection_enabled:
        excerpt = _extract_failure_excerpt(trace_events)
        if excerpt is not None:
            had_errors = True
            failure_excerpt_json = json.dumps(
                {
                    "failed_tool_calls": [
                        {
                            "name": fc.name,
                            "error_message": fc.error_message,
                            "trace_id": fc.trace_id,
                        }
                        for fc in excerpt.failed_tool_calls
                    ],
                    "error_summary": excerpt.error_summary,
                    "recovery_actions": excerpt.recovery_actions,
                },
                ensure_ascii=False,
            )

    # Try DSPy approach first (if available)
    if DSPY_AVAILABLE:
        try:
            log.info(
                "attempting_dspy_reflection",
                trace_id=trace_id,
                had_errors=had_errors,
                failure_path_enabled=settings.failure_path_reflection_enabled,
                component="reflection",
            )
            entry, missing_skill_names = await asyncio.to_thread(
                generate_reflection_dspy,
                user_message=user_message,
                trace_id=trace_id,
                steps_count=steps_count,
                final_state=effective_final_state,
                reply_length=reply_length,
                telemetry_summary=telemetry_summary,
                llm_client=llm_client,
                metrics_summary=metrics_summary,  # ADR-0014: Deterministic extraction
                captains_log_role=_captains_log_role,
                failure_excerpt_json=failure_excerpt_json,
                had_errors=had_errors,
                hit_iteration_limit=hit_iteration_limit,
                task_type=task_type,
                iteration_count=iteration_count,
                max_iterations=max_iterations,
                prompt_manifest=prompt_manifest,
            )
            log.info(
                "dspy_reflection_succeeded",
                trace_id=trace_id,
                has_proposal=entry.proposed_change is not None,
                metrics_count=len(entry.supporting_metrics),
                component="reflection",
            )
            # FRE-328 follow-up — emit gap-recognition warnings from the main
            # loop so ElasticsearchHandler forwards them to agent-logs-*.
            # session_id is required by the ≥2-distinct-sessions clustering
            # threshold in InsightsEngine.detect_missing_skill_patterns.
            if missing_skill_names:
                from personal_agent.captains_log.reflection_dspy import (
                    emit_missing_skill_warnings,
                )

                emit_missing_skill_warnings(
                    missing_skill_names,
                    trace_id=trace_id,
                    session_id=session_id,
                )
            entry.eval_mode = eval_mode  # FRE-523 provenance
            await _apply_read_before_emit(entry, sysgraph_repo, trace_id=trace_id)
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
            prompt_manifest=prompt_manifest,
        )

        from personal_agent.llm_client.concurrency import InferencePriority

        # Call LLM with manual prompt (reasoning model)
        response = await llm_client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,  # Lower temperature for structured output
            max_tokens=3000,  # Increased for reasoning models with thinking process
            reasoning_effort="medium",  # LM Studio /v1/responses: minimal/low/medium/high
            trace_ctx=SystemTraceContext.new("captains_log_reflection", session_id=session_id),
            priority=InferencePriority.BACKGROUND,
            priority_timeout=30.0,
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

        reflection_data = _parse_reflection_response(content, trace_id=trace_id)

        # Extract metrics deterministically (ADR-0014) - NO LLM INVOLVED
        # This overrides any metrics the LLM might have generated in the JSON
        string_metrics, structured_metrics = extract_metrics_from_summary(metrics_summary)

        # Create entry with BOTH metric formats (ADR-0014)
        title = f"Task: {user_message[:50]}" if len(user_message) > 50 else f"Task: {user_message}"

        proposed_change = _build_proposed_change(reflection_data.get("proposed_change"))

        entry = CaptainLogEntry(
            entry_id="",  # Will be generated by manager
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            title=title,
            rationale=reflection_data["rationale"],
            proposed_change=proposed_change,
            supporting_metrics=string_metrics,  # Deterministic extraction (ADR-0014)
            metrics_structured=structured_metrics if structured_metrics else None,  # ADR-0014
            impact_assessment=reflection_data.get("impact_assessment"),
            status=CaptainLogStatus.AWAITING_APPROVAL,
            related_adrs=reflection_data.get("related_adrs", []),
            related_experiments=reflection_data.get("related_experiments", []),
            telemetry_refs=[TelemetryRef(trace_id=trace_id, metric_name=None, value=None)],
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

        entry.eval_mode = eval_mode  # FRE-523 provenance
        await _apply_read_before_emit(entry, sysgraph_repo, trace_id=trace_id)
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
            user_message,
            trace_id,
            steps_count,
            effective_final_state,
            reply_length,
            eval_mode=eval_mode,
        )


def _build_proposed_change(raw: dict[str, str] | None) -> ProposedChange | None:
    """Build a ProposedChange with ADR-0030 category/scope/fingerprint from parsed JSON.

    Gracefully handles missing or invalid category/scope values.

    Args:
        raw: Dict with at least what/why/how keys, optionally category and scope.

    Returns:
        ProposedChange with fingerprint if category and scope are valid, else None.
    """
    if not raw:
        return None

    category: ChangeCategory | None = None
    scope: ChangeScope | None = None
    try:
        category = ChangeCategory(raw.get("category", ""))
    except ValueError:
        pass
    try:
        scope = ChangeScope(raw.get("scope", ""))
    except ValueError:
        pass

    fingerprint = None
    if category and scope:
        fingerprint = compute_proposal_fingerprint(category, scope, raw["what"])

    return ProposedChange(
        what=raw["what"],
        why=raw["why"],
        how=raw["how"],
        category=category,
        scope=scope,
        source=ProposalSource.REFLECTION,
        fingerprint=fingerprint,
        first_seen=datetime.now(timezone.utc),
    )


async def _apply_read_before_emit(
    entry: CaptainLogEntry,
    sysgraph_repo: SysgraphRepository | None,
    *,
    trace_id: str | None,
) -> None:
    """ADR-0105 D9/FRE-721: suppress `entry.proposed_change` for a decided/awaiting equivalent.

    Mutates `entry` in place — sets `proposed_change` to `None` when an equivalent
    idea is already decided or still awaiting (the rationale/metrics still flow
    through, "at most an annotation" per AC-9). No-op when the entry carries no
    proposal, or its category/scope don't resolve to known enums.

    Args:
        entry: The reflection entry about to be returned to the caller.
        sysgraph_repo: Explicit repo override, or None to use the shared
            process-level singleton (``get_default_sysgraph_repo()``).
        trace_id: Originating request trace_id for log correlation (ADR-0074 §I3).
    """
    pc = entry.proposed_change
    if pc is None or pc.category is None or pc.scope is None or not pc.fingerprint:
        return

    repo = sysgraph_repo if sysgraph_repo is not None else get_default_sysgraph_repo()
    result = await check_before_emit(
        repo,
        source=ProposalSource.REFLECTION.value,
        category=pc.category.value,
        scope=pc.scope.value,
        proposal=ProposalRecord(
            source=ProposalSource.REFLECTION.value,
            category=pc.category.value,
            fingerprint=pc.fingerprint,
            what=pc.what,
            why=pc.why,
            how=pc.how,
            seen_count=1,
            scope=pc.scope.value,
        ),
        trace_id=trace_id,
    )
    if result.decision in (ReadBeforeEmitDecision.DECIDED_SKIP, ReadBeforeEmitDecision.REINFORCED):
        log.info(
            "reflection_proposal_suppressed_by_read_before_emit",
            decision=result.decision.value,
            trace_id=trace_id,
        )
        entry.proposed_change = None


def _summarize_telemetry(
    events: list[dict[str, Any]], metrics_summary: dict[str, Any] | None = None
) -> str:
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
        event_types: dict[str, int] = {}
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
                f"**Tools Used**: {len(tool_calls)} calls - {', '.join(set(x for x in tool_names if x is not None))}"
            )

            if tool_failures:
                failed_tools = [e.get("tool") for e in tool_failures]
                summary_parts.append(
                    f"**Tool Failures**: {len(tool_failures)} failures - {', '.join([x for x in failed_tools if x is not None])}"
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


def _parse_reflection_response(content: str, *, trace_id: str | None = None) -> dict[str, Any]:
    """Parse LLM reflection response.

    Args:
        content: LLM response content (should be JSON).
        trace_id: Originating request trace_id for log correlation (ADR-0074 §I3).

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
        return cast(dict[str, Any], data)
    except (json.JSONDecodeError, IndexError) as e:
        log.warning(
            "reflection_parse_failed",
            error=str(e),
            content=content[:200],
            trace_id=trace_id,
        )
        raise ValueError(f"Failed to parse reflection response: {e}") from e


def _create_basic_reflection_entry(
    user_message: str,
    trace_id: str,
    steps_count: int,
    final_state: str,
    reply_length: int,
    eval_mode: bool = False,
) -> CaptainLogEntry:
    """Create a basic reflection entry without LLM analysis (fallback).

    Args:
        user_message: The user's original message.
        trace_id: Trace ID for the task execution.
        steps_count: Number of orchestrator steps executed.
        final_state: Final task state.
        reply_length: Length of the agent's reply.
        eval_mode: True when the task originated from an eval run (FRE-523).

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
        telemetry_refs=[TelemetryRef(trace_id=trace_id, metric_name=None, value=None)],
        eval_mode=eval_mode,
    )
