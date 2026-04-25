"""Tests for CaptainLogManager bus dual-write (ADR-0058)."""

import asyncio
import pathlib
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.manager import CaptainLogManager
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    ProposedChange,
    TelemetryRef,
)
from personal_agent.events.models import (
    CaptainLogEntryCreatedEvent,
    parse_stream_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    tmp_path: pathlib.Path,
    entry_id: str = "CL-20260425-120000-001",
    title: str = "Test reflection",
    entry_type: CaptainLogEntryType = CaptainLogEntryType.REFLECTION,
    fingerprint: str | None = None,
    trace_id: str | None = "abc123",
) -> CaptainLogEntry:
    pc = None
    if fingerprint is not None:
        from personal_agent.captains_log.models import ChangeCategory, ChangeScope

        pc = ProposedChange(
            what="Do X",
            why="Because Y",
            how="By Z",
            fingerprint=fingerprint,
            category=ChangeCategory.PERFORMANCE,
            scope=ChangeScope.ORCHESTRATOR,
        )
    refs = [TelemetryRef(trace_id=trace_id)] if trace_id else []
    return CaptainLogEntry(
        entry_id=entry_id,
        type=entry_type,
        title=title,
        rationale="Test rationale",
        proposed_change=pc,
        telemetry_refs=refs,
    )


def _manager(tmp_path: pathlib.Path) -> CaptainLogManager:
    return CaptainLogManager(log_dir=tmp_path / "captains_log")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSaveEntryBusPublish:
    """save_entry() fires the bus event after a successful durable write."""

    def test_save_entry_publishes_event(self, tmp_path: pathlib.Path) -> None:
        """First write fires one event with is_merge=False, seen_count=1."""
        published: list[tuple[str, Any]] = []

        async def run() -> None:
            entry = _make_entry(tmp_path, fingerprint=None)
            mgr = _manager(tmp_path)

            with patch("personal_agent.captains_log.manager.schedule_es_index"):
                with patch("personal_agent.events.bus.get_event_bus") as mock_bus_factory:
                    mock_bus = AsyncMock()
                    mock_bus.publish.side_effect = lambda stream, evt: published.append(
                        (stream, evt)
                    )
                    mock_bus_factory.return_value = mock_bus
                    mgr.save_entry(entry)
                    await asyncio.sleep(0)  # yield to let create_task run

        asyncio.run(run())

        assert len(published) == 1
        stream, evt = published[0]
        assert stream == "stream:captain_log.entry_created"
        assert isinstance(evt, CaptainLogEntryCreatedEvent)
        assert evt.is_merge is False
        assert evt.seen_count == 1
        assert evt.entry_id == "CL-20260425-120000-001"
        assert evt.trace_id == "abc123"

    def test_save_entry_event_fields(self, tmp_path: pathlib.Path) -> None:
        """Event carries category, scope, fingerprint when ProposedChange is present."""
        published: list[tuple[str, Any]] = []

        async def run() -> None:
            entry = _make_entry(tmp_path, fingerprint="fp1234567890abcd")
            mgr = _manager(tmp_path)

            with patch("personal_agent.captains_log.manager.schedule_es_index"):
                with patch("personal_agent.events.bus.get_event_bus") as mock_bus_factory:
                    mock_bus = AsyncMock()
                    mock_bus.publish.side_effect = lambda s, e: published.append((s, e))
                    mock_bus_factory.return_value = mock_bus
                    mgr.save_entry(entry)
                    await asyncio.sleep(0)

        asyncio.run(run())

        assert len(published) == 1
        evt = published[0][1]
        assert evt.fingerprint == "fp1234567890abcd"
        assert evt.category == "performance"
        assert evt.scope == "orchestrator"
        assert evt.source_component == "captains_log.manager"
        assert evt.schema_version == 1

    def test_merge_fires_event_with_is_merge_true(self, tmp_path: pathlib.Path) -> None:
        """Two saves with the same fingerprint → 1 file, 2 events; second has is_merge=True."""
        published: list[tuple[str, Any]] = []

        async def run() -> None:
            mgr = _manager(tmp_path)

            with patch("personal_agent.captains_log.manager.schedule_es_index"):
                with patch("personal_agent.events.bus.get_event_bus") as mock_bus_factory:
                    mock_bus = AsyncMock()
                    mock_bus.publish.side_effect = lambda s, e: published.append((s, e))
                    mock_bus_factory.return_value = mock_bus

                    entry1 = _make_entry(
                        tmp_path,
                        entry_id="CL-20260425-120000-001",
                        fingerprint="fp_dedup",
                    )
                    mgr.save_entry(entry1)
                    await asyncio.sleep(0)

                    entry2 = _make_entry(
                        tmp_path,
                        entry_id="CL-20260425-120001-001",
                        fingerprint="fp_dedup",
                    )
                    mgr.save_entry(entry2)
                    await asyncio.sleep(0)

        asyncio.run(run())

        assert len(published) == 2
        _, first_evt = published[0]
        _, second_evt = published[1]

        assert first_evt.is_merge is False
        assert first_evt.seen_count == 1

        assert second_evt.is_merge is True
        assert second_evt.seen_count == 2

    def test_suppressed_entry_does_not_publish(self, tmp_path: pathlib.Path) -> None:
        """Entries rejected via ADR-0040 suppression produce 0 bus events."""
        published: list[Any] = []

        async def run() -> None:
            entry = _make_entry(tmp_path, fingerprint="suppressed_fp")
            mgr = _manager(tmp_path)

            with patch(
                "personal_agent.captains_log.manager.is_fingerprint_suppressed",
                return_value=True,
            ):
                with patch("personal_agent.events.bus.get_event_bus") as mock_bus_factory:
                    mock_bus = AsyncMock()
                    mock_bus.publish.side_effect = lambda s, e: published.append(e)
                    mock_bus_factory.return_value = mock_bus
                    result = mgr.save_entry(entry)
                    await asyncio.sleep(0)

        asyncio.run(run())

        assert len(published) == 0

    def test_durable_failure_does_not_publish(self, tmp_path: pathlib.Path) -> None:
        """OSError on file write propagates; no bus event is published (D4 ordering)."""
        published: list[Any] = []

        async def run() -> None:
            entry = _make_entry(tmp_path)
            mgr = _manager(tmp_path)

            with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
                with patch("personal_agent.events.bus.get_event_bus") as mock_bus_factory:
                    mock_bus = AsyncMock()
                    mock_bus.publish.side_effect = lambda s, e: published.append(e)
                    mock_bus_factory.return_value = mock_bus

                    with pytest.raises(OSError, match="disk full"):
                        mgr.save_entry(entry)
                    await asyncio.sleep(0)

        asyncio.run(run())

        assert len(published) == 0

    def test_bus_failure_does_not_block_save(self, tmp_path: pathlib.Path) -> None:
        """Bus publish failure is swallowed; the durable file is still written (D6)."""

        async def run() -> None:
            entry = _make_entry(tmp_path)
            mgr = _manager(tmp_path)

            with patch("personal_agent.captains_log.manager.schedule_es_index"):
                with patch("personal_agent.events.bus.get_event_bus") as mock_bus_factory:
                    mock_bus = AsyncMock()
                    mock_bus.publish.side_effect = RuntimeError("redis down")
                    mock_bus_factory.return_value = mock_bus
                    path = mgr.save_entry(entry)
                    await asyncio.sleep(0)

            assert path is not None
            assert path.exists()

        asyncio.run(run())

    def test_no_loop_skips_publish_silently(self, tmp_path: pathlib.Path) -> None:
        """Without a running event loop the publish is skipped and no error is raised."""
        entry = _make_entry(tmp_path)
        mgr = _manager(tmp_path)

        with patch("personal_agent.captains_log.manager.schedule_es_index"):
            with patch("personal_agent.events.bus.get_event_bus") as mock_bus_factory:
                mock_bus = MagicMock()
                mock_bus_factory.return_value = mock_bus
                # Call synchronously — no running loop
                path = mgr.save_entry(entry)

        assert path is not None
        assert path.exists()
        # publish was never awaited (no loop)
        mock_bus.publish.assert_not_called()


class TestParseStreamEventDispatch:
    """parse_stream_event() correctly round-trips CaptainLogEntryCreatedEvent."""

    def test_round_trip(self) -> None:
        evt = CaptainLogEntryCreatedEvent(
            entry_id="CL-20260425-120000-001",
            entry_type="reflection",
            title="A proposal",
            fingerprint="fp_abc",
            seen_count=3,
            is_merge=True,
            category="performance",
            scope="orchestrator",
            source_component="captains_log.manager",
            trace_id="trace_xyz",
        )
        payload = evt.model_dump(mode="json")
        parsed = parse_stream_event(payload)

        assert isinstance(parsed, CaptainLogEntryCreatedEvent)
        assert parsed.entry_id == "CL-20260425-120000-001"
        assert parsed.is_merge is True
        assert parsed.seen_count == 3
        assert parsed.source_component == "captains_log.manager"
        assert parsed.schema_version == 1

    def test_unknown_type_still_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown event_type"):
            parse_stream_event({"event_type": "does.not_exist", "source_component": "test"})
