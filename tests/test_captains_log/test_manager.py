"""Tests for Captain's Log Manager."""

import json
import pathlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from personal_agent.captains_log.manager import (
    CaptainLogManager,
    _generate_entry_id,
    _sanitize_filename,
)
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    ProposedChange,
    TelemetryRef,
)


class TestEntryIDGeneration:
    """Test entry ID generation."""

    def test_generate_entry_id_format(self) -> None:
        """Test that entry IDs follow the correct format."""
        entry_id = _generate_entry_id()
        assert entry_id.startswith("CL-")
        # Format: CL-YYYYMMDD-HHMMSS-NNN (4 parts when split by "-")
        parts = entry_id.split("-")
        assert len(parts) == 4  # CL, YYYYMMDD, HHMMSS, NNN
        assert parts[0] == "CL"
        assert len(parts[1]) == 8  # YYYYMMDD
        assert len(parts[2]) == 6  # HHMMSS
        assert len(parts[3]) == 3  # Sequence number

    def test_generate_entry_id_sequence(self, tmp_path: pathlib.Path) -> None:
        """Test that entry IDs increment correctly."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()

        # Create some existing entries for the same timestamp prefix
        (log_dir / "CL-20251228-000000-001-test.json").write_text("test")
        (log_dir / "CL-20251228-000000-002-test.json").write_text("test")

        with patch(
            "personal_agent.captains_log.manager._get_captains_log_dir", return_value=log_dir
        ):
            entry_id = _generate_entry_id(datetime(2025, 12, 28, tzinfo=timezone.utc))
            assert entry_id == "CL-20251228-000000-003"

    def test_generate_entry_id_new_date(self, tmp_path: pathlib.Path) -> None:
        """Test that entry IDs reset for new dates."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()

        # Create entries for previous date prefix
        (log_dir / "CL-20251227-000000-999-test.json").write_text("test")

        with patch(
            "personal_agent.captains_log.manager._get_captains_log_dir", return_value=log_dir
        ):
            entry_id = _generate_entry_id(datetime(2025, 12, 28, tzinfo=timezone.utc))
            assert entry_id == "CL-20251228-000000-001"


class TestFilenameSanitization:
    """Test filename sanitization."""

    def test_sanitize_filename_basic(self) -> None:
        """Test basic filename sanitization."""
        assert _sanitize_filename("Test Title") == "test-title"
        assert _sanitize_filename("Hello World") == "hello-world"

    def test_sanitize_filename_special_chars(self) -> None:
        """Test sanitization removes special characters."""
        assert _sanitize_filename("Test@Title#123") == "testtitle123"
        assert _sanitize_filename("Hello/World") == "helloworld"

    def test_sanitize_filename_length_limit(self) -> None:
        """Test that filenames are truncated."""
        long_title = "a" * 100
        sanitized = _sanitize_filename(long_title)
        assert len(sanitized) <= 50


class TestCaptainLogManager:
    """Test CaptainLogManager."""

    def test_init_creates_directory(self, tmp_path: pathlib.Path) -> None:
        """Test that manager creates directory if it doesn't exist."""
        log_dir = tmp_path / "captains_log"
        CaptainLogManager(log_dir=log_dir)
        assert log_dir.exists()
        assert log_dir.is_dir()

    def test_write_entry_creates_file(self, tmp_path: pathlib.Path) -> None:
        """Test that write_entry creates a YAML file."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        entry = CaptainLogEntry(
            entry_id="CL-2025-12-28-001",
            type=CaptainLogEntryType.REFLECTION,
            title="Test Reflection",
            rationale="This is a test reflection entry.",
        )

        file_path = manager.write_entry(entry)
        assert file_path.exists()
        assert file_path.suffix == ".json"

        # Verify YAML content
        content = json.loads(file_path.read_text(encoding="utf-8"))
        assert content["entry_id"] == "CL-2025-12-28-001"
        assert content["type"] == "reflection"
        assert content["title"] == "Test Reflection"

    def test_write_entry_filename_format(self, tmp_path: pathlib.Path) -> None:
        """Test that filename follows expected format."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        entry = CaptainLogEntry(
            entry_id="CL-2025-12-28-001",
            type=CaptainLogEntryType.REFLECTION,
            title="Test Reflection Entry",
            rationale="Test",
        )

        file_path = manager.write_entry(entry)
        assert file_path.name.startswith("CL-2025-12-28-001-")
        assert file_path.name.endswith(".json")

    def test_create_reflection_entry(self, tmp_path: pathlib.Path) -> None:
        """Test create_reflection_entry convenience method."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        entry = manager.create_reflection_entry(
            title="Test Reflection",
            rationale="This is a test.",
            trace_id="trace-123",
            supporting_metrics=["metric1: value1"],
        )

        assert entry.type == CaptainLogEntryType.REFLECTION
        assert entry.title == "Test Reflection"
        assert entry.entry_id.startswith("CL-")
        assert len(entry.telemetry_refs) == 1
        assert entry.telemetry_refs[0].trace_id == "trace-123"

        # Verify file was created
        files = list(log_dir.glob("*.json"))
        assert len(files) == 1

    def test_commit_to_git_success(self, tmp_path: pathlib.Path) -> None:
        """Test successful git commit."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        entry = CaptainLogEntry(
            entry_id="CL-2025-12-28-001",
            type=CaptainLogEntryType.REFLECTION,
            title="Test Reflection",
            rationale="Test",
        )
        file_path = manager.write_entry(entry)

        # Mock git commands
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = manager.commit_to_git("CL-2025-12-28-001", file_path=file_path)

            assert result is True
            assert mock_run.call_count >= 2  # git add and git commit

    def test_commit_to_git_not_in_repo(self, tmp_path: pathlib.Path) -> None:
        """Test that commit fails gracefully when not in git repo."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        entry = CaptainLogEntry(
            entry_id="CL-2025-12-28-001",
            type=CaptainLogEntryType.REFLECTION,
            title="Test Reflection",
            rationale="Test",
        )
        file_path = manager.write_entry(entry)

        # Mock git rev-parse to fail (not in repo)
        with patch("subprocess.run") as mock_run:
            # First call (git rev-parse) raises FileNotFoundError
            def side_effect(*args, **kwargs):
                if args[0][0] == "git" and args[0][1] == "rev-parse":
                    raise FileNotFoundError("git not found")
                return MagicMock(returncode=0)

            mock_run.side_effect = side_effect
            result = manager.commit_to_git("CL-2025-12-28-001", file_path=file_path)

            assert result is False

    def test_write_entry_with_proposed_change(self, tmp_path: pathlib.Path) -> None:
        """Test writing entry with proposed change."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        entry = CaptainLogEntry(
            entry_id="CL-2025-12-28-001",
            type=CaptainLogEntryType.CONFIG_PROPOSAL,
            title="Increase rate limit",
            rationale="Rate limit is too low",
            proposed_change=ProposedChange(
                what="Increase web search rate limit from 20 to 50 requests/hour",
                why="Current limit is too restrictive for research-heavy workflows",
                how="Update config/governance/safety.yaml rate_limits.NORMAL.web_search from 20 to 50",
            ),
        )

        file_path = manager.write_entry(entry)
        content = json.loads(file_path.read_text(encoding="utf-8"))

        assert content["type"] == "config_proposal"
        assert (
            content["proposed_change"]["what"]
            == "Increase web search rate limit from 20 to 50 requests/hour"
        )
        assert (
            content["proposed_change"]["why"]
            == "Current limit is too restrictive for research-heavy workflows"
        )
        assert (
            content["proposed_change"]["how"]
            == "Update config/governance/safety.yaml rate_limits.NORMAL.web_search from 20 to 50"
        )

    def test_write_entry_with_telemetry_refs(self, tmp_path: pathlib.Path) -> None:
        """Test writing entry with telemetry references."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)

        entry = CaptainLogEntry(
            entry_id="CL-2025-12-28-001",
            type=CaptainLogEntryType.REFLECTION,
            title="Test Reflection",
            rationale="Test",
            telemetry_refs=[
                TelemetryRef(trace_id="trace-123"),
                TelemetryRef(metric_name="cpu_load", value=85.5),
            ],
        )

        file_path = manager.write_entry(entry)
        content = json.loads(file_path.read_text(encoding="utf-8"))

        assert len(content["telemetry_refs"]) == 2
        assert content["telemetry_refs"][0]["trace_id"] == "trace-123"
        assert content["telemetry_refs"][1]["metric_name"] == "cpu_load"

    def test_write_entry_reflection_indexes_to_es_when_enabled(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Writing a reflection entry calls schedule_es_index with correct index and doc (Phase 2.3)."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)
        entry = CaptainLogEntry(
            entry_id="CL-2026-02-22-001",
            type=CaptainLogEntryType.REFLECTION,
            title="Test Reflection",
            rationale="Test rationale",
            timestamp=datetime(2026, 2, 22, 12, 0, 0, tzinfo=timezone.utc),
        )
        with patch("personal_agent.captains_log.manager.schedule_es_index") as mock_schedule:
            manager.write_entry(entry)
            mock_schedule.assert_called_once()
            call_args = mock_schedule.call_args[0]
            assert call_args[0] == "agent-captains-reflections-2026-02-22"
            assert isinstance(call_args[1], dict)
            assert call_args[1]["entry_id"] == "CL-2026-02-22-001"
            assert call_args[1]["type"] == "reflection"
            assert call_args[1]["title"] == "Test Reflection"
            assert mock_schedule.call_args[1].get("doc_id") == "CL-2026-02-22-001"

    def test_write_entry_config_proposal_calls_es_index(self, tmp_path: pathlib.Path) -> None:
        """Writing a config proposal entry also calls schedule_es_index."""
        log_dir = tmp_path / "captains_log"
        manager = CaptainLogManager(log_dir=log_dir)
        entry = CaptainLogEntry(
            entry_id="CL-2026-02-22-001",
            type=CaptainLogEntryType.CONFIG_PROPOSAL,
            title="Config proposal",
            rationale="Test",
            timestamp=datetime(2026, 2, 22, 12, 0, 0, tzinfo=timezone.utc),
        )
        with patch("personal_agent.captains_log.manager.schedule_es_index") as mock_schedule:
            manager.write_entry(entry)
            mock_schedule.assert_called_once()
            call_args = mock_schedule.call_args[0]
            assert call_args[0] == "agent-captains-reflections-2026-02-22"
            assert isinstance(call_args[1], dict)
            assert call_args[1]["type"] == "config_proposal"
            assert mock_schedule.call_args[1].get("doc_id") == "CL-2026-02-22-001"
