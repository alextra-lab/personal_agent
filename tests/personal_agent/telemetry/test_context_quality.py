"""Tests for telemetry/context_quality.py (ADR-0059, FRE-249)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from personal_agent.events.models import (
    STREAM_CONTEXT_COMPACTION_QUALITY_POOR,
    CompactionQualityIncidentEvent,
)
from personal_agent.telemetry.context_quality import (
    CompactionQualityIncident,
    IncidentTracker,
    fingerprint_incident,
    get_incident_tracker,
    record_incident,
    reset_incident_tracker,
    schedule_record_incident,
)


def _make_incident(
    *,
    fingerprint: str = "fp0123456789abcd",
    trace_id: str = "trace-1",
    session_id: str = "session-1",
    noun_phrase: str = "caching system",
    dropped_entity: str = "redis-config",
    recall_cue: str = "what was our caching system again",
    tier_affected: str = "episodic",
    tokens_removed: int = 412,
    detected_at: datetime | None = None,
) -> CompactionQualityIncident:
    return CompactionQualityIncident(
        fingerprint=fingerprint,
        trace_id=trace_id,
        session_id=session_id,
        noun_phrase=noun_phrase,
        dropped_entity=dropped_entity,
        recall_cue=recall_cue,
        tier_affected=tier_affected,
        tokens_removed=tokens_removed,
        detected_at=detected_at or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# fingerprint_incident
# ---------------------------------------------------------------------------


class TestFingerprintIncident:
    def test_deterministic(self) -> None:
        a = fingerprint_incident("caching system", "redis-config", "x.y")
        b = fingerprint_incident("caching system", "redis-config", "x.y")
        assert a == b
        assert len(a) == 16

    def test_distinct_inputs_produce_distinct_fingerprints(self) -> None:
        a = fingerprint_incident("a", "b", "c")
        b = fingerprint_incident("a", "b", "d")
        c = fingerprint_incident("a", "x", "c")
        d = fingerprint_incident("y", "b", "c")
        assert len({a, b, c, d}) == 4


# ---------------------------------------------------------------------------
# record_incident — dual-write ordering and resilience
# ---------------------------------------------------------------------------


class TestRecordIncident:
    @pytest.mark.asyncio
    async def test_writes_jsonl_then_publishes(self, tmp_path: Path) -> None:
        bus = AsyncMock()
        incident = _make_incident()
        await record_incident(incident, bus, output_dir=tmp_path)

        day = incident.detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        fp = tmp_path / f"CQ-{day}.jsonl"
        assert fp.exists()
        line = fp.read_text().strip()
        payload = json.loads(line)
        assert payload["fingerprint"] == incident.fingerprint
        assert payload["noun_phrase"] == incident.noun_phrase
        assert payload["session_id"] == incident.session_id

        bus.publish.assert_awaited_once()
        call_args = bus.publish.call_args
        assert call_args.args[0] == STREAM_CONTEXT_COMPACTION_QUALITY_POOR
        published_event: Any = call_args.args[1]
        assert isinstance(published_event, CompactionQualityIncidentEvent)
        assert published_event.fingerprint == incident.fingerprint
        assert published_event.session_id == incident.session_id

    @pytest.mark.asyncio
    async def test_appends_multiple_lines(self, tmp_path: Path) -> None:
        bus = AsyncMock()
        ts = datetime.now(timezone.utc)
        a = _make_incident(fingerprint="aaa0000000000000", detected_at=ts)
        b = _make_incident(fingerprint="bbb0000000000000", detected_at=ts)
        await record_incident(a, bus, output_dir=tmp_path)
        await record_incident(b, bus, output_dir=tmp_path)

        day = ts.strftime("%Y-%m-%d")
        fp = tmp_path / f"CQ-{day}.jsonl"
        lines = [json.loads(line) for line in fp.read_text().splitlines()]
        assert {ln["fingerprint"] for ln in lines} == {a.fingerprint, b.fingerprint}

    @pytest.mark.asyncio
    async def test_bus_failure_is_swallowed(self, tmp_path: Path) -> None:
        bus = AsyncMock()
        bus.publish.side_effect = RuntimeError("redis down")
        incident = _make_incident()
        await record_incident(incident, bus, output_dir=tmp_path)

        day = incident.detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        fp = tmp_path / f"CQ-{day}.jsonl"
        assert fp.exists()
        bus.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_none_bus_skips_publish(self, tmp_path: Path) -> None:
        incident = _make_incident()
        await record_incident(incident, None, output_dir=tmp_path)
        day = incident.detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        fp = tmp_path / f"CQ-{day}.jsonl"
        assert fp.exists()

    @pytest.mark.asyncio
    async def test_registers_with_global_tracker(self, tmp_path: Path) -> None:
        reset_incident_tracker()
        bus = AsyncMock()
        incident = _make_incident(session_id="session-tracker-1")
        await record_incident(incident, bus, output_dir=tmp_path)
        tracker = get_incident_tracker()
        assert tracker.count_in_window("session-tracker-1", hours=24) == 1


class TestScheduleRecordIncident:
    def test_runs_synchronously_when_no_loop(self, tmp_path: Path) -> None:
        reset_incident_tracker()
        incident = _make_incident(session_id="sync-1")

        from personal_agent.telemetry import context_quality as cq

        original_default_dir = cq._default_output_dir

        try:
            cq._default_output_dir = lambda: tmp_path  # type: ignore[assignment]
            schedule_record_incident(incident, None)
        finally:
            cq._default_output_dir = original_default_dir  # type: ignore[assignment]

        day = incident.detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        fp = tmp_path / f"CQ-{day}.jsonl"
        assert fp.exists()
        assert get_incident_tracker().count_in_window("sync-1", hours=24) == 1

    @pytest.mark.asyncio
    async def test_schedules_task_when_loop_running(self, tmp_path: Path) -> None:
        reset_incident_tracker()
        bus = AsyncMock()
        incident = _make_incident(session_id="async-1")

        from personal_agent.telemetry import context_quality as cq

        original_default_dir = cq._default_output_dir
        try:
            cq._default_output_dir = lambda: tmp_path  # type: ignore[assignment]
            schedule_record_incident(incident, bus)
            for _ in range(50):
                await asyncio.sleep(0.01)
                if (tmp_path / f"CQ-{incident.detected_at.strftime('%Y-%m-%d')}.jsonl").exists():
                    break
        finally:
            cq._default_output_dir = original_default_dir  # type: ignore[assignment]

        bus.publish.assert_awaited()


# ---------------------------------------------------------------------------
# IncidentTracker
# ---------------------------------------------------------------------------


class TestIncidentTracker:
    def test_register_and_count(self) -> None:
        tracker = IncidentTracker()
        tracker.register("s1")
        tracker.register("s1")
        assert tracker.count_in_window("s1", hours=24) == 2

    def test_unknown_session_returns_zero(self) -> None:
        tracker = IncidentTracker()
        assert tracker.count_in_window("never-seen", hours=24) == 0

    def test_old_entries_evicted_on_register(self) -> None:
        tracker = IncidentTracker(retention_hours=1)
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        recent = datetime.now(timezone.utc)
        tracker.register("s1", when=old)
        tracker.register("s1", when=recent)
        assert tracker.count_in_window("s1", hours=24) == 1

    def test_count_window_filters_to_window(self) -> None:
        tracker = IncidentTracker(retention_hours=48)
        a = datetime.now(timezone.utc) - timedelta(hours=30)
        b = datetime.now(timezone.utc) - timedelta(hours=2)
        tracker.register("s1", when=a)
        tracker.register("s1", when=b)
        assert tracker.count_in_window("s1", hours=24) == 1
        assert tracker.count_in_window("s1", hours=48) == 2

    def test_lru_capacity_evicts_oldest(self) -> None:
        tracker = IncidentTracker(capacity=3)
        tracker.register("s1")
        tracker.register("s2")
        tracker.register("s3")
        tracker.register("s4")
        assert tracker.count_in_window("s1", hours=24) == 0
        assert tracker.count_in_window("s4", hours=24) == 1

    def test_register_empty_session_id_is_noop(self) -> None:
        tracker = IncidentTracker()
        tracker.register("")
        assert tracker.count_in_window("", hours=24) == 0
