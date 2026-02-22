"""Tests for Captain's Log capture module (Phase 2.2 / 2.3)."""

import pathlib
from datetime import datetime, timezone
from unittest.mock import patch

from personal_agent.captains_log.capture import (
    TaskCapture,
    write_capture,
)


class TestWriteCapture:
    """Test write_capture and optional ES indexing."""

    def test_write_capture_creates_file_and_indexes_to_es(
        self, tmp_path: pathlib.Path
    ) -> None:
        """write_capture writes JSON to disk and calls schedule_es_index (Phase 2.3)."""
        capture = TaskCapture(
            trace_id="trace-123",
            session_id="session-456",
            timestamp=datetime(2026, 2, 22, 14, 0, 0, tzinfo=timezone.utc),
            user_message="Hello",
            assistant_response="Hi",
            outcome="completed",
        )
        with patch(
            "personal_agent.captains_log.capture._get_captures_dir",
            return_value=tmp_path / "captures",
        ), patch(
            "personal_agent.captains_log.capture.schedule_es_index"
        ) as mock_schedule:
            path = write_capture(capture)
            assert path.exists()
            assert path.suffix == ".json"
            mock_schedule.assert_called_once()
            call_args = mock_schedule.call_args[0]
            assert call_args[0] == "agent-captains-captures-2026-02-22"
            assert call_args[1]["trace_id"] == "trace-123"
            assert call_args[1]["outcome"] == "completed"
