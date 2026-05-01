"""Tests for the within-session compression telemetry module (ADR-0061)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from personal_agent.events.models import (
    STREAM_CONTEXT_WITHIN_SESSION_COMPRESSED,
    WithinSessionCompressionEvent,
)
from personal_agent.telemetry.within_session_compression import (
    WithinSessionCompressionRecord,
    _append_durable,
    _jsonl_line,
    record_compression,
)


def _record(**overrides: object) -> WithinSessionCompressionRecord:
    data: dict[str, object] = {
        "trace_id": "t1",
        "session_id": "s1",
        "trigger": "hard",
        "head_tokens": 100,
        "middle_tokens_in": 5000,
        "middle_tokens_out": 300,
        "tail_tokens": 400,
        "pre_pass_replacements": 3,
        "summariser_called": True,
        "summariser_duration_ms": 850,
        "compressed_at": datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    }
    data.update(overrides)
    return WithinSessionCompressionRecord(**data)  # type: ignore[arg-type]


class TestRecordIsFrozen:
    def test_record_is_frozen(self) -> None:
        rec = _record()
        with pytest.raises(Exception):  # FrozenInstanceError on dataclass
            rec.tail_tokens = 999  # type: ignore[misc]

    def test_tokens_saved_property(self) -> None:
        rec = _record(middle_tokens_in=5000, middle_tokens_out=200)
        assert rec.tokens_saved == 4800

    def test_tokens_saved_clamped_to_zero(self) -> None:
        rec = _record(middle_tokens_in=100, middle_tokens_out=200)
        assert rec.tokens_saved == 0


class TestJsonlSerialisation:
    def test_jsonl_line_round_trips(self) -> None:
        rec = _record()
        line = _jsonl_line(rec)
        payload = json.loads(line)
        assert payload["trace_id"] == "t1"
        assert payload["trigger"] == "hard"
        assert payload["compressed_at"] == "2026-05-01T12:00:00+00:00"
        assert payload["tokens_saved"] == 4700


class TestDualWrite:
    @pytest.mark.asyncio
    async def test_writes_jsonl_then_publishes_bus(self, tmp_path: Path) -> None:
        bus = AsyncMock()
        rec = _record()

        await record_compression(rec, bus, output_dir=tmp_path)

        # Durable file exists
        files = list(tmp_path.glob("WSC-*.jsonl"))
        assert len(files) == 1
        line = files[0].read_text(encoding="utf-8").strip()
        payload = json.loads(line)
        assert payload["session_id"] == "s1"

        # Bus publish was called with the right stream + event class
        bus.publish.assert_awaited_once()
        args, _ = bus.publish.call_args
        stream_name, event = args
        assert stream_name == STREAM_CONTEXT_WITHIN_SESSION_COMPRESSED
        assert isinstance(event, WithinSessionCompressionEvent)
        assert event.tokens_saved == 4700

    @pytest.mark.asyncio
    async def test_durable_failure_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per ADR-0054 §D4 — durable write failure must not be swallowed."""
        rec = _record()

        def fail(*args: object, **kwargs: object) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr(
            "personal_agent.telemetry.within_session_compression._append_durable",
            fail,
        )
        bus = AsyncMock()
        with pytest.raises(OSError):
            await record_compression(rec, bus, output_dir=tmp_path)
        # Bus must NOT have been called when durable failed
        bus.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_bus_failure_logged_and_swallowed(self, tmp_path: Path) -> None:
        """Per ADR-0054 §D6 — bus publish failure must not raise."""
        bus = AsyncMock()
        bus.publish.side_effect = RuntimeError("redis offline")
        rec = _record()

        # Should not raise
        await record_compression(rec, bus, output_dir=tmp_path)

        # Durable file still exists
        files = list(tmp_path.glob("WSC-*.jsonl"))
        assert len(files) == 1

    @pytest.mark.asyncio
    async def test_none_bus_skips_publish(self, tmp_path: Path) -> None:
        rec = _record()
        await record_compression(rec, None, output_dir=tmp_path)
        files = list(tmp_path.glob("WSC-*.jsonl"))
        assert len(files) == 1


class TestAppendDurable:
    def test_per_day_filename(self, tmp_path: Path) -> None:
        rec = _record(
            compressed_at=datetime(2026, 5, 1, 23, 59, 59, tzinfo=timezone.utc)
        )
        path = _append_durable(rec, tmp_path)
        assert path.name == "WSC-2026-05-01.jsonl"

    def test_appends_one_line_per_call(self, tmp_path: Path) -> None:
        rec = _record()
        _append_durable(rec, tmp_path)
        _append_durable(rec, tmp_path)
        path = tmp_path / "WSC-2026-05-01.jsonl"
        assert path.read_text(encoding="utf-8").count("\n") == 2
