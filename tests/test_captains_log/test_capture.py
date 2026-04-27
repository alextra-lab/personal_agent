"""Tests for Captain's Log capture module (Phase 2.2 / 2.3)."""

import pathlib
from datetime import datetime, timezone
from unittest.mock import patch

from personal_agent.captains_log.capture import (
    TaskCapture,
    write_capture,
)


def test_user_id_coercion() -> None:
    """Regression: asyncpg UUID must be coerced to uuid.UUID so orjson can serialize it."""
    import orjson
    from uuid import UUID as StdUUID

    _UUID_STR = "550e8400-e29b-41d4-a716-446655440000"
    _base: dict = dict(
        trace_id="trace-coerce",
        session_id="session-coerce",
        timestamp=datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc),
        user_message="test",
        outcome="completed",
    )

    # string input → uuid.UUID
    c1 = TaskCapture(**_base, user_id=_UUID_STR)
    assert isinstance(c1.user_id, StdUUID)
    orjson.dumps(c1.model_dump())

    # asyncpg-style subclass (isinstance passes but type() is not uuid.UUID) → must coerce
    class _SubUUID(StdUUID):
        pass

    c2_sub = TaskCapture(**_base, user_id=_SubUUID(_UUID_STR))
    assert type(c2_sub.user_id) is StdUUID  # strict type, not subclass
    orjson.dumps(c2_sub.model_dump())  # would fail with asyncpg UUID before this fix

    # duck-typed __str__ only → uuid.UUID
    class _FakeUUID:
        def __str__(self) -> str:
            return _UUID_STR

    c2 = TaskCapture(**_base, user_id=_FakeUUID())
    assert isinstance(c2.user_id, StdUUID)
    orjson.dumps(c2.model_dump())

    # None stays None
    c3 = TaskCapture(**_base, user_id=None)
    assert c3.user_id is None
    orjson.dumps(c3.model_dump())


class TestWriteCapture:
    """Test write_capture and optional ES indexing."""

    def test_write_capture_creates_file_and_indexes_to_es(self, tmp_path: pathlib.Path) -> None:
        """write_capture writes JSON to disk and calls schedule_es_index (Phase 2.3)."""
        capture = TaskCapture(
            trace_id="trace-123",
            session_id="session-456",
            timestamp=datetime(2026, 2, 22, 14, 0, 0, tzinfo=timezone.utc),
            user_message="Hello",
            assistant_response="Hi",
            outcome="completed",
        )
        with (
            patch(
                "personal_agent.captains_log.capture._get_captures_dir",
                return_value=tmp_path / "captures",
            ),
            patch("personal_agent.captains_log.capture.schedule_es_index") as mock_schedule,
        ):
            path = write_capture(capture)
            assert path.exists()
            assert path.suffix == ".json"
            mock_schedule.assert_called_once()
            call_args = mock_schedule.call_args[0]
            assert call_args[0] == "agent-captains-captures-2026-02-22"
            assert call_args[1]["trace_id"] == "trace-123"
            assert call_args[1]["outcome"] == "completed"
            assert mock_schedule.call_args[1].get("doc_id") == "trace-123"
