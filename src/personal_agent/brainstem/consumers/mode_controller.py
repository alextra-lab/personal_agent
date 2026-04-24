"""Mode controller consumer for ADR-0055 (System Health & Homeostasis).

Subscribes to two streams via ``cg:mode-controller``:

1. ``stream:metrics.sampled`` — maintains a rolling ``deque`` of recent
   ``MetricsSampledEvent`` samples (window = ``mode_window_size`` × sample
   interval ≈ 60 s at default settings).  Every
   ``mode_evaluation_interval_seconds`` (default 30 s) it aggregates the
   window into a sensor snapshot and calls
   ``ModeManager.evaluate_transitions()``.

2. ``stream:mode.transition`` — tracks per-(from, to) edge transition
   timestamps in a 10-minute rolling window.  When the count for any edge
   reaches or exceeds ``mode_calibration_anomaly_threshold`` (default 3), it
   emits a fingerprinted ``CONFIG_PROPOSAL`` Captain's Log entry and logs a
   warning.  Subsequent events for the same edge are de-duplicated in-memory
   to avoid proposal flooding.
"""

from __future__ import annotations

import hashlib
import statistics
import time
from collections import deque
from typing import TYPE_CHECKING

from personal_agent.captains_log.manager import CaptainLogManager
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    ChangeCategory,
    ChangeScope,
    ProposedChange,
)
from personal_agent.config import settings
from personal_agent.events.models import (
    EventBase,
    MetricsSampledEvent,
    ModeTransitionEvent,
)
from personal_agent.governance.models import Mode
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.brainstem.mode_manager import ModeManager

log = get_logger(__name__)

# Rolling window for cadence tracking — 10 minutes in seconds.
_CADENCE_WINDOW_SECONDS: float = 600.0


def _compute_calibration_fingerprint(from_mode: Mode, to_mode: Mode) -> str:
    """Compute a short SHA-256 fingerprint for a mode calibration proposal.

    Args:
        from_mode: Source mode of the frequently-occurring transition.
        to_mode: Target mode of the frequently-occurring transition.

    Returns:
        First 16 hex characters of the SHA-256 digest — used as the Captain's
        Log proposal fingerprint for deduplication.
    """
    raw = f"mode_calibration|{from_mode.value}->{to_mode.value}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


class ModeControllerConsumer:
    """Event-bus consumer for cg:mode-controller (ADR-0055).

    Handles ``MetricsSampledEvent`` and ``ModeTransitionEvent`` events
    dispatched by the ``ConsumerRunner`` read loop.

    Two handlers share state through instance attributes so the consumer can
    be registered for both streams using the same object:

    - ``handle`` — dispatches to ``_on_metrics_sampled`` or
      ``_on_mode_transition`` based on event type; non-matching events are
      silently ignored.

    Args:
        mode_manager: ``ModeManager`` instance to call
            ``evaluate_transitions`` on.
        captain_log_manager: Optional ``CaptainLogManager`` for writing
            calibration proposals.  When ``None``, a default manager is
            created lazily on first proposal.
        evaluation_interval_seconds: Override for
            ``settings.mode_evaluation_interval_seconds``.
        window_size: Override for ``settings.mode_window_size``.
        calibration_anomaly_threshold: Override for
            ``settings.mode_calibration_anomaly_threshold``.
    """

    def __init__(
        self,
        mode_manager: ModeManager,
        captain_log_manager: CaptainLogManager | None = None,
        evaluation_interval_seconds: float | None = None,
        window_size: int | None = None,
        calibration_anomaly_threshold: int | None = None,
    ) -> None:
        """Initialise the consumer with dependencies and config overrides."""
        self._mode_manager = mode_manager
        self._captain_log_manager = captain_log_manager

        self._eval_interval: float = (
            evaluation_interval_seconds
            if evaluation_interval_seconds is not None
            else settings.mode_evaluation_interval_seconds
        )
        _window_size: int = (
            window_size
            if window_size is not None
            else settings.mode_window_size
        )
        self._calibration_threshold: int = (
            calibration_anomaly_threshold
            if calibration_anomaly_threshold is not None
            else settings.mode_calibration_anomaly_threshold
        )

        # Rolling window of recent MetricsSampledEvent instances.
        self._window: deque[MetricsSampledEvent] = deque(maxlen=_window_size)

        # Monotonic timestamp of the last evaluate_transitions call.
        self._last_evaluation: float = time.monotonic()

        # Per-(from, to) edge: list of monotonic timestamps within cadence window.
        self._cadence: dict[tuple[Mode, Mode], list[float]] = {}

        # Already-proposed fingerprints — prevents flooding the log with
        # repeated proposals within a single process lifetime.
        self._proposed_fingerprints: set[str] = set()

        log.info(
            "mode_controller_consumer_initialized",
            eval_interval_seconds=self._eval_interval,
            window_size=_window_size,
            calibration_threshold=self._calibration_threshold,
        )

    # ------------------------------------------------------------------
    # Public entry point — dispatches to type-specific handlers
    # ------------------------------------------------------------------

    async def handle(self, event: EventBase) -> None:
        """Dispatch an event from the consumer runner to the correct handler.

        Silently ignores events that are neither ``MetricsSampledEvent`` nor
        ``ModeTransitionEvent``.

        Args:
            event: Incoming event from the Redis Streams bus.
        """
        if isinstance(event, MetricsSampledEvent):
            await self._on_metrics_sampled(event)
        elif isinstance(event, ModeTransitionEvent):
            await self._on_mode_transition(event)

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    async def _on_metrics_sampled(self, event: MetricsSampledEvent) -> None:
        """Accumulate sample and fire evaluate_transitions on schedule.

        Args:
            event: Incoming metrics-sampled event.
        """
        try:
            self._window.append(event)

            now = time.monotonic()
            if now - self._last_evaluation < self._eval_interval:
                return

            # Aggregate window into a single sensor snapshot.
            sensor_data = self._aggregate_window()
            self._mode_manager.evaluate_transitions(sensor_data)
            self._last_evaluation = now

            log.debug(
                "mode_controller_evaluated",
                window_samples=len(self._window),
                sensor_cpu=sensor_data.get("perf_system_cpu_load"),
                sensor_mem=sensor_data.get("perf_system_mem_used"),
            )

        except Exception as exc:
            log.warning(
                "mode_controller_metrics_handler_error",
                error=str(exc),
                exc_info=True,
            )

    async def _on_mode_transition(self, event: ModeTransitionEvent) -> None:
        """Track transition cadence and emit proposal on anomaly.

        Args:
            event: Incoming mode-transition event.
        """
        try:
            edge: tuple[Mode, Mode] = (event.from_mode, event.to_mode)
            now = time.monotonic()

            # Initialise or retrieve edge timestamp list.
            timestamps = self._cadence.setdefault(edge, [])
            timestamps.append(now)

            # Prune stale entries outside the 10-minute rolling window.
            cutoff = now - _CADENCE_WINDOW_SECONDS
            self._cadence[edge] = [t for t in timestamps if t >= cutoff]
            count = len(self._cadence[edge])

            log.debug(
                "mode_controller_cadence_updated",
                from_mode=event.from_mode.value,
                to_mode=event.to_mode.value,
                edge_count_10min=count,
                threshold=self._calibration_threshold,
            )

            if count >= self._calibration_threshold:
                await self._emit_calibration_proposal(event, count)

        except Exception as exc:
            log.warning(
                "mode_controller_transition_handler_error",
                error=str(exc),
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _aggregate_window(self) -> dict[str, float]:
        """Collapse the current window into aggregated sensor values.

        Aggregation rules (matching ADR-0055 spec):
        - CPU load: mean across window samples.
        - Memory used: mean across window samples.
        - GPU load: max across window samples (peaks matter most).
        - Disk used: last sample value (disk changes slowly).
        - Safety counters: always 0 (not aggregated from MetricsDaemon).

        Returns:
            Dictionary of aggregated sensor values keyed by metric name.
        """
        if not self._window:
            return {
                "perf_system_cpu_load": 0.0,
                "perf_system_mem_used": 0.0,
                "perf_system_gpu_load": 0.0,
                "perf_system_disk_used": 0.0,
                "safety_tool_high_risk_calls": 0,
                "safety_policy_violations": 0,
            }

        cpu_vals = [s.metrics.get("perf_system_cpu_load", 0.0) for s in self._window]
        mem_vals = [s.metrics.get("perf_system_mem_used", 0.0) for s in self._window]
        gpu_vals = [s.metrics.get("perf_system_gpu_load", 0.0) for s in self._window]
        disk_vals = [s.metrics.get("perf_system_disk_used", 0.0) for s in self._window]

        return {
            "perf_system_cpu_load": statistics.mean(cpu_vals) if cpu_vals else 0.0,
            "perf_system_mem_used": statistics.mean(mem_vals) if mem_vals else 0.0,
            "perf_system_gpu_load": max(gpu_vals) if gpu_vals else 0.0,
            "perf_system_disk_used": disk_vals[-1] if disk_vals else 0.0,
            "safety_tool_high_risk_calls": 0,
            "safety_policy_violations": 0,
        }

    async def _emit_calibration_proposal(
        self, event: ModeTransitionEvent, count: int
    ) -> None:
        """Write a Captain's Log CONFIG_PROPOSAL for anomalous edge cadence.

        De-duplicates within the process lifetime via the
        ``_proposed_fingerprints`` set — only the first breach for each edge
        creates a new proposal; subsequent ones are logged but skipped.

        Args:
            event: The transition event that triggered the threshold.
            count: Current edge count within the 10-minute rolling window.
        """
        fingerprint = _compute_calibration_fingerprint(event.from_mode, event.to_mode)

        if fingerprint in self._proposed_fingerprints:
            log.debug(
                "mode_calibration_proposal_already_emitted",
                from_mode=event.from_mode.value,
                to_mode=event.to_mode.value,
                fingerprint=fingerprint,
            )
            return

        description = (
            f"Mode calibration: {event.from_mode.value}→{event.to_mode.value} "
            f"triggered {count} times in 10 min — threshold may need tuning"
        )

        entry = CaptainLogEntry(
            entry_id="",
            type=CaptainLogEntryType.CONFIG_PROPOSAL,
            title=description,
            rationale=(
                f"The cg:mode-controller consumer observed the "
                f"{event.from_mode.value}→{event.to_mode.value} transition "
                f"{count} times within 10 minutes (anomaly threshold: "
                f"{self._calibration_threshold}). Frequent oscillation between "
                "modes suggests the transition rule thresholds need calibration."
            ),
            proposed_change=ProposedChange(
                what=(
                    f"Tune the {event.from_mode.value}→{event.to_mode.value} "
                    "transition rule thresholds in config/governance/"
                ),
                why=(
                    f"Edge triggered {count}x in 10 min, exceeding anomaly threshold "
                    f"of {self._calibration_threshold}. Oscillation wastes resources "
                    "and may destabilise dependent components."
                ),
                how=(
                    "Review the transition rule conditions in "
                    "config/governance/governance.yaml and adjust thresholds so the "
                    "edge fires at most once per 10-minute window under normal load."
                ),
                category=ChangeCategory.RELIABILITY,
                scope=ChangeScope.BRAINSTEM,
                fingerprint=fingerprint,
            ),
            supporting_metrics=[
                f"edge: {event.from_mode.value}→{event.to_mode.value}",
                f"count_10min: {count}",
                f"threshold: {self._calibration_threshold}",
                f"reason: {event.reason}",
            ],
        )

        manager = self._captain_log_manager or CaptainLogManager()
        manager.save_entry(entry)

        self._proposed_fingerprints.add(fingerprint)

        log.warning(
            "mode_calibration_proposal_emitted",
            from_mode=event.from_mode.value,
            to_mode=event.to_mode.value,
            count_10min=count,
            threshold=self._calibration_threshold,
            fingerprint=fingerprint,
        )
