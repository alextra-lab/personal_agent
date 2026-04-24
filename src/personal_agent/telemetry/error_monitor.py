"""Error pattern monitor — Level 3 self-observability (ADR-0056).

Triggered by ``ConsolidationCompletedEvent``; scans Elasticsearch for recurring
error clusters, dual-writes durable JSON files and bus events, and chains into
the Captain's Log → promotion → Linear loop via ``stream:errors.pattern_detected``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from personal_agent.events.models import (
    STREAM_ERRORS_PATTERN_DETECTED,
    ErrorPatternCluster,
    ErrorPatternDetectedEvent,
)
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.events.bus import EventBus
    from personal_agent.telemetry.queries import TelemetryQueries

log = get_logger(__name__)

# WARNING events that signal broken loops or quality degradation (ADR-0056 D1).
# Adding an event name here is a one-line change; criterion: "this WARNING
# indicates a broken loop, not a benign degradation."
WARNING_EVENT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "compaction_quality.poor",
        "history_sanitised_orphans_removed",
        "chat.stream_failed",
        "gateway_pipeline_failed",
        "expansion_budget_computation_failed",
        "dspy_reflection_failed_fallback_manual",
        "feedback_llm_failed",
        "insights_cost_query_failed",
        "captains_log_backfill_failed",
        "dead_letter_routed",
        # mcp timeout/failure family
        "mcp_tool_call_failed",
        "mcp_tool_timeout",
        "mcp_gateway_unavailable",
        # freshness degradation
        "freshness_review_skipped_no_driver",
        "freshness_review_skipped_empty",
        "freshness_review_skipped_error",
    }
)

_SCAN_HISTORY_CAP = 30


class ErrorMonitor:
    """Orchestrates one error-pattern scan per ``ConsolidationCompletedEvent``.

    On each scan:
    1. Calls ``queries.get_error_patterns()`` to cluster ES error events.
    2. For each cluster: upserts ``EP-<fingerprint>.json`` (file write first,
       per ADR-0054 D4).
    3. Publishes ``ErrorPatternDetectedEvent`` to ``stream:errors.pattern_detected``.

    Resilience:
    - ES down → logs ``error_monitor_scan_failed`` at WARNING, returns ``[]``.
    - Redis down → file is already written; publish error is swallowed with WARNING.

    Args:
        queries: ``TelemetryQueries`` instance for ES access.
        bus: ``EventBus`` instance for event publication.
        output_dir: Directory for ``EP-<fingerprint>.json`` files.
        window_hours: Trailing look-back window passed to ``get_error_patterns``.
        min_occurrences: Minimum cluster size passed to ``get_error_patterns``.
        max_patterns_per_scan: Hard cap on bus emissions per scan.
    """

    def __init__(
        self,
        queries: TelemetryQueries,
        bus: EventBus,
        output_dir: Path = Path("telemetry/error_patterns"),
        window_hours: int = 24,
        min_occurrences: int = 5,
        max_patterns_per_scan: int = 50,
    ) -> None:
        """Initialise with query backend, event bus, and scan configuration."""
        self._queries = queries
        self._bus = bus
        self._output_dir = output_dir
        self._window_hours = window_hours
        self._min_occurrences = min_occurrences
        self._max_patterns_per_scan = max_patterns_per_scan

    async def scan(self) -> list[ErrorPatternCluster]:
        """Run one error-pattern scan and dual-write results.

        Returns:
            List of ``ErrorPatternCluster`` records processed this scan.
            Empty list on ES failure or when no patterns qualify.
        """
        log.info("error_monitor_scan_started", window_hours=self._window_hours)
        try:
            clusters = await self._queries.get_error_patterns(
                window_hours=self._window_hours,
                min_occurrences=self._min_occurrences,
            )
        except Exception as exc:
            log.warning(
                "error_monitor_scan_failed",
                error=str(exc),
                window_hours=self._window_hours,
            )
            return []

        emitted: list[ErrorPatternCluster] = []
        for cluster in clusters[: self._max_patterns_per_scan]:
            self._upsert_pattern_file(cluster)
            await self._publish(cluster)
            emitted.append(cluster)

        log.info(
            "error_monitor_scan_completed",
            clusters_found=len(clusters),
            clusters_emitted=len(emitted),
        )
        return emitted

    # -- Internal ------------------------------------------------------------

    def _upsert_pattern_file(self, cluster: ErrorPatternCluster) -> None:
        """Write or update ``EP-<fingerprint>.json`` (ADR-0056 Layer C).

        File write always precedes bus publish (ADR-0054 D4).
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        fp_path = self._output_dir / f"EP-{cluster.fingerprint}.json"
        now_iso = datetime.now(timezone.utc).isoformat()

        if fp_path.exists():
            try:
                data: dict[str, Any] = json.loads(fp_path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}

        scan_history: list[dict[str, Any]] = data.get("scan_history", [])
        scan_history.append(
            {
                "scan_at": now_iso,
                "window_hours": cluster.window_hours,
                "occurrences_in_window": cluster.occurrences,
            }
        )
        if len(scan_history) > _SCAN_HISTORY_CAP:
            scan_history = scan_history[-_SCAN_HISTORY_CAP:]

        data.update(
            {
                "fingerprint": cluster.fingerprint,
                "component": cluster.component,
                "event_name": cluster.event_name,
                "error_type": cluster.error_type,
                "level": cluster.level,
                "first_seen": cluster.first_seen.isoformat(),
                "last_seen": cluster.last_seen.isoformat(),
                "total_occurrences": cluster.occurrences,
                "sample_trace_ids": list(cluster.sample_trace_ids),
                "sample_messages": list(cluster.sample_messages),
                "scan_history": scan_history,
            }
        )
        fp_path.write_text(json.dumps(data, indent=2))

    async def _publish(self, cluster: ErrorPatternCluster) -> None:
        """Publish ``ErrorPatternDetectedEvent`` — best-effort (Redis may be down)."""
        event = ErrorPatternDetectedEvent(
            source_component="telemetry.error_monitor",
            trace_id=None,
            fingerprint=cluster.fingerprint,
            component=cluster.component,
            event_name=cluster.event_name,
            error_type=cluster.error_type,
            level=cluster.level,
            occurrences=cluster.occurrences,
            first_seen=cluster.first_seen,
            last_seen=cluster.last_seen,
            window_hours=cluster.window_hours,
            sample_trace_ids=list(cluster.sample_trace_ids),
            sample_messages=list(cluster.sample_messages),
        )
        try:
            await self._bus.publish(STREAM_ERRORS_PATTERN_DETECTED, event)
        except Exception as exc:
            log.warning(
                "error_monitor_publish_failed",
                fingerprint=cluster.fingerprint,
                error=str(exc),
            )
