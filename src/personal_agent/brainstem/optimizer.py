"""Adaptive threshold optimizer for brainstem scheduling.

FRE-11 implementation: analyze telemetry patterns, detect false positives, and
generate data-backed threshold proposals with shadow A/B evaluation.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from personal_agent.config.settings import AppConfig, get_settings
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.queries import (
    TaskPatternReport,
    TelemetryQueries,
)

log = get_logger(__name__)

HIGH_RISK_MODES = {"alert", "degraded", "lockdown"}


@dataclass(frozen=True)
class ResourceAnalysis:
    """Summary of resource and behavior patterns over a time window."""

    days: int
    cpu_percentiles: dict[str, float]
    memory_percentiles: dict[str, float]
    transition_count: int
    consolidation_count: int
    task_patterns: TaskPatternReport


@dataclass(frozen=True)
class FalsePositiveReport:
    """Result of detecting likely unnecessary consolidations."""

    total_consolidations: int
    suspected_false_positives: int
    false_positive_rate: float
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ThresholdProposal:
    """Proposal to adjust a scheduler threshold."""

    metric: str
    current_value: float
    proposed_value: float
    confidence: float
    reason: str
    supporting_metrics: dict[str, float | int]
    captains_log_payload: dict[str, Any]


@dataclass(frozen=True)
class ABTestResult:
    """Shadow-mode comparison of current vs proposed threshold."""

    metric: str
    baseline_value: float
    candidate_value: float
    baseline_false_positive_rate: float
    candidate_false_positive_rate: float
    recommended: bool
    summary: str


class ThresholdOptimizer:
    """Analyzes usage patterns and proposes threshold adjustments."""

    def __init__(
        self,
        telemetry_queries: TelemetryQueries | None = None,
        config: AppConfig | None = None,
    ) -> None:
        """Initialize optimizer with telemetry query adapter and config.

        Args:
            telemetry_queries: Optional query adapter.
            config: Optional app configuration override.
        """
        self._queries = telemetry_queries or TelemetryQueries()
        self._settings = config or get_settings()

    async def analyze_resource_patterns(self, days: int = 7) -> ResourceAnalysis:
        """Query telemetry for resource and task patterns.

        Args:
            days: Analysis window in days.

        Returns:
            Consolidated pattern report with resource and behavior metrics.
        """
        cpu_percentiles = await self._queries.get_resource_percentiles("cpu", days)
        memory_percentiles = await self._queries.get_resource_percentiles("memory", days)
        transitions = await self._queries.get_mode_transitions(days)
        consolidations = await self._queries.get_consolidation_triggers(days)
        task_patterns = await self._queries.get_task_patterns(days)

        return ResourceAnalysis(
            days=days,
            cpu_percentiles=cpu_percentiles,
            memory_percentiles=memory_percentiles,
            transition_count=len(transitions),
            consolidation_count=len(consolidations),
            task_patterns=task_patterns,
        )

    async def detect_false_positives(self) -> FalsePositiveReport:
        """Find likely unnecessary consolidation triggers.

        Returns:
            False-positive report based on low-resource consolidation timing and
            lack of nearby high-risk mode transitions.
        """
        consolidations = await self._queries.get_consolidation_triggers(days=7)
        transitions = await self._queries.get_mode_transitions(days=7)

        if not consolidations:
            return FalsePositiveReport(
                total_consolidations=0,
                suspected_false_positives=0,
                false_positive_rate=0.0,
            )

        evidence: list[dict[str, Any]] = []
        suspect_count = 0
        transition_window = timedelta(minutes=5)

        for event in consolidations:
            near_risk_transition = any(
                transition.to_mode.lower() in HIGH_RISK_MODES
                and abs(transition.timestamp - event.timestamp) <= transition_window
                for transition in transitions
            )
            cpu_ok = (
                event.cpu_percent is not None
                and event.cpu_percent <= self._settings.second_brain_cpu_threshold * 0.5
            )
            memory_ok = (
                event.memory_percent is not None
                and event.memory_percent <= self._settings.second_brain_memory_threshold * 0.5
            )
            if not near_risk_transition and (cpu_ok or memory_ok):
                suspect_count += 1
                evidence.append(
                    {
                        "timestamp": event.timestamp.isoformat(),
                        "cpu_percent": event.cpu_percent,
                        "memory_percent": event.memory_percent,
                        "reason": "low_resource_consolidation_without_risk_transition",
                    }
                )

        return FalsePositiveReport(
            total_consolidations=len(consolidations),
            suspected_false_positives=suspect_count,
            false_positive_rate=suspect_count / len(consolidations),
            evidence=evidence,
        )

    async def propose_threshold_adjustment(self, metric: str) -> ThresholdProposal:
        """Generate data-backed threshold proposal for one metric.

        Args:
            metric: One of cpu_threshold, memory_threshold, idle_time_seconds,
                or min_consolidation_interval_seconds.

        Returns:
            Threshold proposal with supporting telemetry and Captain's Log payload.

        Raises:
            ValueError: If metric is unsupported.
        """
        normalized_metric = metric.strip().lower()
        supported_metrics = {
            "cpu_threshold",
            "memory_threshold",
            "idle_time_seconds",
            "min_consolidation_interval_seconds",
        }
        if normalized_metric not in supported_metrics:
            raise ValueError(f"Unsupported metric '{metric}'. Supported: {sorted(supported_metrics)}")

        analysis = await self.analyze_resource_patterns(days=7)
        false_positives = await self.detect_false_positives()

        if normalized_metric == "cpu_threshold":
            current = float(self._settings.second_brain_cpu_threshold)
            p90 = float(analysis.cpu_percentiles.get("p90", current))
            proposed = _clamp_float((p90 + current) / 2.0, 20.0, 95.0)
            reason = "Align CPU threshold with observed p90 resource usage."
        elif normalized_metric == "memory_threshold":
            current = float(self._settings.second_brain_memory_threshold)
            p90 = float(analysis.memory_percentiles.get("p90", current))
            proposed = _clamp_float((p90 + current) / 2.0, 30.0, 98.0)
            reason = "Align memory threshold with observed p90 resource usage."
        elif normalized_metric == "idle_time_seconds":
            current = float(self._settings.second_brain_idle_time_seconds)
            avg_duration_s = analysis.task_patterns.avg_duration_ms / 1000.0
            proposed = _clamp_float(max(avg_duration_s * 3.0, 120.0), 60.0, 1800.0)
            reason = "Base idle threshold on observed task durations."
        else:
            current = float(self._settings.second_brain_min_interval_seconds)
            daily_consolidations = analysis.consolidation_count / max(analysis.days, 1)
            if daily_consolidations > 12:
                proposed = _clamp_float(current * 1.25, 900.0, 86400.0)
                reason = "Reduce consolidation churn by increasing minimum interval."
            elif daily_consolidations < 1:
                proposed = _clamp_float(current * 0.8, 300.0, 86400.0)
                reason = "Increase consolidation opportunities for sparse activity."
            else:
                proposed = current
                reason = "Current minimum interval is consistent with observed consolidation volume."

        confidence = _clamp_float(
            0.4 + min(analysis.task_patterns.total_tasks, 100) / 200.0,
            0.4,
            0.9,
        )
        supporting_metrics: dict[str, float | int] = {
            "cpu_p90": round(analysis.cpu_percentiles.get("p90", 0.0), 2),
            "memory_p90": round(analysis.memory_percentiles.get("p90", 0.0), 2),
            "consolidations_7d": analysis.consolidation_count,
            "false_positive_rate_7d": round(false_positives.false_positive_rate, 3),
            "tasks_7d": analysis.task_patterns.total_tasks,
        }
        payload = self._build_captains_log_payload(
            metric=normalized_metric,
            current_value=current,
            proposed_value=proposed,
            confidence=confidence,
            reason=reason,
            supporting_metrics=supporting_metrics,
        )
        return ThresholdProposal(
            metric=normalized_metric,
            current_value=current,
            proposed_value=proposed,
            confidence=confidence,
            reason=reason,
            supporting_metrics=supporting_metrics,
            captains_log_payload=payload,
        )

    async def run_ab_test(self, proposal: ThresholdProposal) -> ABTestResult:
        """Evaluate threshold proposal in shadow mode using telemetry heuristics.

        Args:
            proposal: Proposal to evaluate.

        Returns:
            A/B comparison result and recommendation.
        """
        baseline_report = await self.detect_false_positives()
        analysis = await self.analyze_resource_patterns(days=7)
        baseline_rate = baseline_report.false_positive_rate

        projected_rate = baseline_rate
        if proposal.metric in {"cpu_threshold", "memory_threshold"}:
            current = max(proposal.current_value, 1.0)
            relative_delta = (proposal.proposed_value - proposal.current_value) / current
            projected_rate = _clamp_float(baseline_rate + (relative_delta * 0.1), 0.0, 1.0)
        elif proposal.metric == "idle_time_seconds":
            current = max(proposal.current_value, 1.0)
            relative_delta = (proposal.proposed_value - proposal.current_value) / current
            projected_rate = _clamp_float(baseline_rate - (relative_delta * 0.08), 0.0, 1.0)
        elif proposal.metric == "min_consolidation_interval_seconds":
            current = max(proposal.current_value, 1.0)
            relative_delta = (proposal.proposed_value - proposal.current_value) / current
            projected_rate = _clamp_float(baseline_rate - (relative_delta * 0.12), 0.0, 1.0)

        recommended = projected_rate <= baseline_rate - 0.01
        summary = (
            f"Shadow evaluation over {analysis.days}d suggests false-positive rate "
            f"{baseline_rate:.2%} -> {projected_rate:.2%} for {proposal.metric}."
        )
        return ABTestResult(
            metric=proposal.metric,
            baseline_value=proposal.current_value,
            candidate_value=proposal.proposed_value,
            baseline_false_positive_rate=baseline_rate,
            candidate_false_positive_rate=projected_rate,
            recommended=recommended,
            summary=summary,
        )

    def _build_captains_log_payload(
        self,
        metric: str,
        current_value: float,
        proposed_value: float,
        confidence: float,
        reason: str,
        supporting_metrics: dict[str, float | int],
    ) -> dict[str, Any]:
        """Build Captain's Log-ready payload for proposal review."""
        return {
            "type": "config_proposal",
            "title": f"Threshold proposal: {metric}",
            "rationale": reason,
            "proposed_change": {
                "what": f"Update {metric} from {current_value:.2f} to {proposed_value:.2f}",
                "why": reason,
                "how": "Apply in scheduler configuration and monitor in shadow mode for 7 days.",
            },
            "supporting_metrics": [
                f"{name}: {value}" for name, value in supporting_metrics.items()
            ],
            "impact_assessment": (
                f"Estimated confidence {confidence:.0%}. Lower false positives expected while "
                "maintaining consolidation reliability."
            ),
        }


def _clamp_float(value: float, minimum: float, maximum: float) -> float:
    """Clamp float value to an inclusive range."""
    return max(minimum, min(maximum, value))
