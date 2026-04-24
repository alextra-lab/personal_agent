"""Unit tests for ModeControllerConsumer (ADR-0055, FRE-246 Task 5).

Tests verify:
1. MetricsSampledEvent accumulation is capped by window deque maxlen.
2. evaluate_transitions is throttled to once per eval interval.
3. ModeTransitionEvent increments per-edge cadence counters.
4. Captain's Log proposal is emitted when threshold is reached (default 3).
5. No proposal is emitted when event count is below threshold.
6. Stale cadence entries (> 600 s) are pruned before threshold check.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from personal_agent.brainstem.consumers.mode_controller import (
    ModeControllerConsumer,
    _compute_calibration_fingerprint,
)
from personal_agent.events.models import MetricsSampledEvent, ModeTransitionEvent
from personal_agent.governance.models import Mode


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_metrics_event(
    cpu: float = 10.0,
    mem: float = 40.0,
    gpu: float = 5.0,
    disk: float = 60.0,
) -> MetricsSampledEvent:
    return MetricsSampledEvent(
        source_component="brainstem.sensors.metrics_daemon",
        sample_timestamp=datetime.now(timezone.utc),
        metrics={
            "perf_system_cpu_load": cpu,
            "perf_system_mem_used": mem,
            "perf_system_gpu_load": gpu,
            "perf_system_disk_used": disk,
        },
        sample_interval_seconds=5.0,
    )


def _make_transition_event(
    from_mode: Mode = Mode.NORMAL,
    to_mode: Mode = Mode.ALERT,
    reason: str = "test_rule",
    index: int = 1,
) -> ModeTransitionEvent:
    return ModeTransitionEvent(
        source_component="brainstem.mode_manager",
        from_mode=from_mode,
        to_mode=to_mode,
        reason=reason,
        sensor_snapshot={},
        transition_index=index,
    )


def _make_consumer(
    window_size: int = 5,
    eval_interval: float = 30.0,
    threshold: int = 3,
    captain_log_manager: MagicMock | None = None,
) -> tuple[ModeControllerConsumer, MagicMock]:
    """Return (consumer, mock_mode_manager)."""
    mock_mm = MagicMock()
    consumer = ModeControllerConsumer(
        mode_manager=mock_mm,
        captain_log_manager=captain_log_manager,
        evaluation_interval_seconds=eval_interval,
        window_size=window_size,
        calibration_anomaly_threshold=threshold,
    )
    return consumer, mock_mm


# ---------------------------------------------------------------------------
# Test 1: window accumulation capped at maxlen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_samples_accumulate_in_window() -> None:
    """Feed N events where N < maxlen; verify all are stored."""
    consumer, _ = _make_consumer(window_size=10, eval_interval=9999.0)
    for i in range(7):
        await consumer.handle(_make_metrics_event(cpu=float(i)))
    assert len(consumer._window) == 7


@pytest.mark.asyncio
async def test_metrics_samples_cap_at_maxlen() -> None:
    """Feed N events where N > maxlen; deque should cap at maxlen."""
    consumer, _ = _make_consumer(window_size=5, eval_interval=9999.0)
    for i in range(20):
        await consumer.handle(_make_metrics_event(cpu=float(i)))
    assert len(consumer._window) == 5


# ---------------------------------------------------------------------------
# Test 2: evaluation throttle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluation_throttle() -> None:
    """Rapid event delivery should only trigger evaluate_transitions once."""
    consumer, mock_mm = _make_consumer(window_size=20, eval_interval=30.0)

    # Force last_evaluation far in the past so the first call fires.
    consumer._last_evaluation = time.monotonic() - 60.0

    # Send 10 events in rapid succession.
    for _ in range(10):
        await consumer.handle(_make_metrics_event())

    # Should have been called exactly once (first event crossed the interval).
    assert mock_mm.evaluate_transitions.call_count == 1


@pytest.mark.asyncio
async def test_evaluation_not_called_before_interval() -> None:
    """No evaluation should fire when the interval has not elapsed."""
    consumer, mock_mm = _make_consumer(window_size=10, eval_interval=9999.0)
    # _last_evaluation is set to time.monotonic() at construction time,
    # so the interval has definitely not elapsed.
    for _ in range(5):
        await consumer.handle(_make_metrics_event())
    mock_mm.evaluate_transitions.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: transition event increments cadence counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_event_increments_cadence_counter() -> None:
    """Single ModeTransitionEvent should create an entry in cadence dict."""
    consumer, _ = _make_consumer(threshold=99)  # high threshold — no proposal
    event = _make_transition_event(from_mode=Mode.NORMAL, to_mode=Mode.ALERT)
    await consumer.handle(event)

    edge = (Mode.NORMAL, Mode.ALERT)
    assert edge in consumer._cadence
    assert len(consumer._cadence[edge]) == 1


@pytest.mark.asyncio
async def test_transition_event_accumulates_multiple_counts() -> None:
    """Three events for the same edge should register three timestamps."""
    consumer, _ = _make_consumer(threshold=99)
    for i in range(3):
        await consumer.handle(_make_transition_event(index=i + 1))

    edge = (Mode.NORMAL, Mode.ALERT)
    assert len(consumer._cadence[edge]) == 3


# ---------------------------------------------------------------------------
# Test 4: Captain's Log proposal emitted on threshold breach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_captain_log_proposal_on_threshold_breach() -> None:
    """Sending threshold events should call captain_log_manager.save_entry."""
    mock_cl = MagicMock()
    consumer, _ = _make_consumer(threshold=3, captain_log_manager=mock_cl)

    for i in range(3):
        await consumer.handle(_make_transition_event(index=i + 1))

    mock_cl.save_entry.assert_called_once()
    # Verify the entry passed to save_entry is a CONFIG_PROPOSAL.
    from personal_agent.captains_log.models import CaptainLogEntryType

    entry_arg = mock_cl.save_entry.call_args[0][0]
    assert entry_arg.type == CaptainLogEntryType.CONFIG_PROPOSAL


@pytest.mark.asyncio
async def test_captain_log_proposal_fingerprint_is_correct() -> None:
    """The emitted proposal fingerprint should match the expected SHA-256 prefix."""
    mock_cl = MagicMock()
    consumer, _ = _make_consumer(threshold=3, captain_log_manager=mock_cl)

    for i in range(3):
        await consumer.handle(_make_transition_event(from_mode=Mode.NORMAL, to_mode=Mode.ALERT, index=i + 1))

    expected_fp = _compute_calibration_fingerprint(Mode.NORMAL, Mode.ALERT)
    entry_arg = mock_cl.save_entry.call_args[0][0]
    assert entry_arg.proposed_change is not None
    assert entry_arg.proposed_change.fingerprint == expected_fp


@pytest.mark.asyncio
async def test_captain_log_not_duplicated_on_repeated_breach() -> None:
    """After the first proposal, subsequent threshold events should not create more."""
    mock_cl = MagicMock()
    consumer, _ = _make_consumer(threshold=3, captain_log_manager=mock_cl)

    for i in range(6):  # Double the threshold
        await consumer.handle(_make_transition_event(index=i + 1))

    # save_entry should only be called once (dedup via _proposed_fingerprints).
    assert mock_cl.save_entry.call_count == 1


# ---------------------------------------------------------------------------
# Test 5: no proposal below threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_captain_log_not_called_below_threshold() -> None:
    """Sending fewer events than threshold should not trigger any proposal."""
    mock_cl = MagicMock()
    consumer, _ = _make_consumer(threshold=3, captain_log_manager=mock_cl)

    for i in range(2):  # One below threshold
        await consumer.handle(_make_transition_event(index=i + 1))

    mock_cl.save_entry.assert_not_called()


@pytest.mark.asyncio
async def test_captain_log_not_called_at_threshold_minus_one() -> None:
    """Edge case: threshold-1 events — no proposal."""
    mock_cl = MagicMock()
    consumer, _ = _make_consumer(threshold=5, captain_log_manager=mock_cl)

    for i in range(4):
        await consumer.handle(_make_transition_event(index=i + 1))

    mock_cl.save_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: stale cadence entries are pruned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_cadence_entries_are_pruned() -> None:
    """Timestamps older than 600 s should not count toward the threshold."""
    mock_cl = MagicMock()
    consumer, _ = _make_consumer(threshold=3, captain_log_manager=mock_cl)

    edge = (Mode.NORMAL, Mode.ALERT)
    # Pre-populate cadence with 10 timestamps that are all > 600 s old.
    old_time = time.monotonic() - 700.0
    consumer._cadence[edge] = [old_time] * 10

    # Send a single fresh event — after pruning only 1 remains.
    await consumer.handle(_make_transition_event(index=1))

    assert len(consumer._cadence[edge]) == 1
    # Threshold is 3, count is 1 — no proposal.
    mock_cl.save_entry.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_entries_survive_pruning() -> None:
    """Recent timestamps should survive pruning."""
    mock_cl = MagicMock()
    consumer, _ = _make_consumer(threshold=99, captain_log_manager=mock_cl)

    edge = (Mode.NORMAL, Mode.ALERT)
    # Pre-populate with 5 fresh timestamps (within 600 s).
    now = time.monotonic()
    consumer._cadence[edge] = [now - 100.0, now - 200.0, now - 300.0]

    await consumer.handle(_make_transition_event(index=1))

    # 3 old fresh + 1 new = 4 entries; all within window.
    assert len(consumer._cadence[edge]) == 4


# ---------------------------------------------------------------------------
# Test 7: window aggregation correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_window_mean_cpu() -> None:
    """CPU aggregation should be the mean of all window samples."""
    consumer, mock_mm = _make_consumer(window_size=5, eval_interval=0.0)
    # Force last evaluation far enough in the past.
    consumer._last_evaluation = time.monotonic() - 100.0

    cpu_values = [10.0, 20.0, 30.0, 40.0, 50.0]
    for cpu in cpu_values:
        await consumer.handle(_make_metrics_event(cpu=cpu))

    # evaluate_transitions should have been called; inspect sensor_data argument.
    assert mock_mm.evaluate_transitions.call_count >= 1
    sensor_data = mock_mm.evaluate_transitions.call_args[0][0]
    expected_cpu = sum(cpu_values) / len(cpu_values)
    assert abs(sensor_data["perf_system_cpu_load"] - expected_cpu) < 0.01


@pytest.mark.asyncio
async def test_aggregate_window_max_gpu() -> None:
    """GPU aggregation should use the max value across the window."""
    consumer, mock_mm = _make_consumer(window_size=5, eval_interval=0.0)
    consumer._last_evaluation = time.monotonic() - 100.0

    gpu_values = [5.0, 15.0, 90.0, 30.0, 10.0]
    for gpu in gpu_values:
        await consumer.handle(_make_metrics_event(gpu=gpu))

    sensor_data = mock_mm.evaluate_transitions.call_args[0][0]
    assert sensor_data["perf_system_gpu_load"] == max(gpu_values)


@pytest.mark.asyncio
async def test_aggregate_window_last_disk() -> None:
    """Disk aggregation should return the last sample value."""
    consumer, mock_mm = _make_consumer(window_size=3, eval_interval=0.0)
    consumer._last_evaluation = time.monotonic() - 100.0

    disk_values = [50.0, 60.0, 70.0]
    for disk in disk_values:
        await consumer.handle(_make_metrics_event(disk=disk))

    sensor_data = mock_mm.evaluate_transitions.call_args[0][0]
    assert sensor_data["perf_system_disk_used"] == disk_values[-1]


@pytest.mark.asyncio
async def test_aggregate_empty_window_returns_zeros() -> None:
    """_aggregate_window on an empty deque should return zeros."""
    consumer, _ = _make_consumer(eval_interval=9999.0)
    result = consumer._aggregate_window()
    assert result["perf_system_cpu_load"] == 0.0
    assert result["perf_system_gpu_load"] == 0.0
    assert result["safety_tool_high_risk_calls"] == 0


# ---------------------------------------------------------------------------
# Test 8: fingerprint helper
# ---------------------------------------------------------------------------


def test_compute_calibration_fingerprint_length() -> None:
    """Fingerprint should be exactly 16 hex characters."""
    fp = _compute_calibration_fingerprint(Mode.NORMAL, Mode.ALERT)
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_compute_calibration_fingerprint_deterministic() -> None:
    """Same edge should always produce the same fingerprint."""
    fp1 = _compute_calibration_fingerprint(Mode.NORMAL, Mode.ALERT)
    fp2 = _compute_calibration_fingerprint(Mode.NORMAL, Mode.ALERT)
    assert fp1 == fp2


def test_compute_calibration_fingerprint_different_edges() -> None:
    """Different edges should produce different fingerprints."""
    fp_na = _compute_calibration_fingerprint(Mode.NORMAL, Mode.ALERT)
    fp_an = _compute_calibration_fingerprint(Mode.ALERT, Mode.NORMAL)
    assert fp_na != fp_an


# ---------------------------------------------------------------------------
# Test 9: non-matching events are silently ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_ignores_unknown_event_type() -> None:
    """Non-MetricsSampled, non-ModeTransition events should be silently ignored."""
    from personal_agent.events.models import SystemIdleEvent

    consumer, mock_mm = _make_consumer()
    idle_event = SystemIdleEvent(
        source_component="brainstem.scheduler",
        idle_seconds=300.0,
    )
    # Should not raise and should not call evaluate_transitions.
    await consumer.handle(idle_event)
    mock_mm.evaluate_transitions.assert_not_called()
    assert len(consumer._window) == 0
