"""Tests for Captain's Log ES backfill (FRE-30)."""

import json
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.captains_log.backfill import (
    BackfillCheckpoint,
    BackfillResult,
    _list_capture_files_sorted,
    _list_reflection_files_sorted,
    _load_checkpoint,
    _save_checkpoint,
    run_backfill,
)


class TestBackfillCheckpoint:
    """Test checkpoint model and persistence."""

    def test_checkpoint_to_dict(self) -> None:
        """Checkpoint serializes to spec shape."""
        cp = BackfillCheckpoint(
            last_scan_started_at="2026-02-22T14:00:00Z",
            last_scan_completed_at="2026-02-22T14:00:03Z",
            captures={
                "last_processed_path": "telemetry/captains_log/captures/2026-02-22/trace-x.json",
                "last_processed_mtime": "2026-02-22T13:59:59Z",
            },
            reflections={
                "last_processed_path": "telemetry/captains_log/CL-001.json",
                "last_processed_mtime": "2026-02-22T13:59:58Z",
            },
        )
        d = cp.to_dict()
        assert d["version"] == 1
        assert "captures" in d
        assert (
            d["captures"]["last_processed_path"]
            == "telemetry/captains_log/captures/2026-02-22/trace-x.json"
        )
        assert "reflections" in d

    def test_checkpoint_from_dict(self) -> None:
        """Checkpoint loads from dict."""
        d = {
            "version": 1,
            "captures": {"last_processed_path": "a", "last_processed_mtime": "b"},
            "reflections": {"last_processed_path": "c", "last_processed_mtime": "d"},
        }
        cp = BackfillCheckpoint.from_dict(d)
        assert cp.captures["last_processed_path"] == "a"
        assert cp.reflections["last_processed_path"] == "c"

    def test_save_and_load_checkpoint(self, tmp_path: pytest.TempPathFactory) -> None:
        """Checkpoint persists to disk and loads back."""
        with patch(
            "personal_agent.captains_log.backfill._checkpoint_path",
            return_value=tmp_path / "cp.json",
        ):
            cp = BackfillCheckpoint(
                last_scan_started_at="2026-02-22T14:00:00Z",
                captures={"last_processed_path": "p", "last_processed_mtime": "m"},
            )
            _save_checkpoint(cp)
            loaded = _load_checkpoint()
            assert loaded.last_scan_started_at == cp.last_scan_started_at
            assert loaded.captures["last_processed_path"] == "p"


class TestBackfillFileDiscovery:
    """Test file enumeration in stable order."""

    def test_list_capture_files_sorted_empty_when_no_dir(self) -> None:
        """No captures dir returns empty list."""
        with patch("personal_agent.captains_log.backfill._captures_dir") as m:
            m.return_value = pathlib.Path("/nonexistent/captures")
            assert _list_capture_files_sorted() == []

    def test_list_reflection_files_sorted_empty_when_no_dir(self) -> None:
        """No captains_log dir returns empty list."""
        with patch("personal_agent.captains_log.backfill._captains_log_dir") as m:
            m.return_value = pathlib.Path("/nonexistent/captains_log")
            assert _list_reflection_files_sorted() == []


class TestRunBackfill:
    """Test run_backfill with mocked paths and ES."""

    @pytest.mark.asyncio
    async def test_run_backfill_no_files_returns_zero_counts(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """When no capture/reflection files exist, backfill completes with 0 indexed."""
        with patch("personal_agent.captains_log.backfill._project_root", return_value=tmp_path):
            (tmp_path / "telemetry" / "captains_log").mkdir(parents=True)
            (tmp_path / "telemetry" / "captains_log" / "captures").mkdir(parents=True)
            es_logger = AsyncMock()
            es_logger.index_document = AsyncMock(return_value="id")
            result = await run_backfill(es_logger)
            assert isinstance(result, BackfillResult)
            assert result.files_scanned == 0
            assert result.indexed_count == 0
            assert result.elapsed_ms >= 0

    @pytest.mark.asyncio
    async def test_run_backfill_indexes_capture_with_trace_id_as_doc_id(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """One capture file is indexed with doc_id=trace_id (idempotent)."""
        with patch("personal_agent.captains_log.backfill._project_root", return_value=tmp_path):
            base = tmp_path / "telemetry" / "captains_log"
            base.mkdir(parents=True)
            (base / "captures" / "2026-02-22").mkdir(parents=True)
            capture = {
                "trace_id": "trace-abc-123",
                "session_id": "s1",
                "timestamp": "2026-02-22T14:00:00+00:00",
                "user_message": "Hi",
                "outcome": "completed",
            }
            (base / "captures" / "2026-02-22" / "trace-abc-123.json").write_text(
                json.dumps(capture), encoding="utf-8"
            )
            es_logger = AsyncMock()
            es_logger.index_document = AsyncMock(return_value="trace-abc-123")
            result = await run_backfill(es_logger)
            assert result.indexed_count >= 1
            es_logger.index_document.assert_called()
            call = es_logger.index_document.call_args
            assert call[0][0] == "agent-captains-captures-2026-02-22"
            assert call[0][1].get("trace_id") == "trace-abc-123"
            assert call[1].get("id") == "trace-abc-123"

    @pytest.mark.asyncio
    async def test_run_backfill_indexes_reflection_with_entry_id_as_doc_id(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """One reflection file is indexed with doc_id=entry_id (idempotent)."""
        with patch("personal_agent.captains_log.backfill._project_root", return_value=tmp_path):
            base = tmp_path / "telemetry" / "captains_log"
            base.mkdir(parents=True)
            (base / "captures").mkdir(parents=True)
            entry = {
                "entry_id": "CL-20260222-120000-001",
                "type": "reflection",
                "title": "Test",
                "rationale": "R",
                "timestamp": "2026-02-22T12:00:00+00:00",
            }
            (base / "CL-20260222-120000-001-test.json").write_text(
                json.dumps(entry), encoding="utf-8"
            )
            es_logger = AsyncMock()
            es_logger.index_document = AsyncMock(return_value="CL-20260222-120000-001")
            result = await run_backfill(es_logger)
            assert result.indexed_count >= 1
            call_args = es_logger.index_document.call_args
            assert call_args[0][0] == "agent-captains-reflections-2026-02-22"
            assert call_args[1].get("id") == "CL-20260222-120000-001"

    @pytest.mark.asyncio
    async def test_run_backfill_file_failure_logged_does_not_crash(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """Parse or index failure for one file is logged; backfill continues."""
        with patch("personal_agent.captains_log.backfill._project_root", return_value=tmp_path):
            base = tmp_path / "telemetry" / "captains_log"
            base.mkdir(parents=True)
            (base / "captures" / "2026-02-22").mkdir(parents=True)
            (base / "captures" / "2026-02-22" / "bad.json").write_text(
                "{ invalid json", encoding="utf-8"
            )
            es_logger = AsyncMock()
            es_logger.index_document = AsyncMock(return_value="id")
            result = await run_backfill(es_logger)
            assert result.failed_count >= 1
            assert result.indexed_count == 0

    @pytest.mark.asyncio
    async def test_run_backfill_checkpoint_updated_after_success(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """After indexing, checkpoint is updated and survives restart (resume)."""
        with patch("personal_agent.captains_log.backfill._project_root", return_value=tmp_path):
            base = tmp_path / "telemetry" / "captains_log"
            base.mkdir(parents=True)
            (base / "captures" / "2026-02-22").mkdir(parents=True)
            capture = {
                "trace_id": "trace-checkpoint",
                "session_id": "s1",
                "timestamp": "2026-02-22T14:00:00+00:00",
                "user_message": "Hi",
                "outcome": "completed",
            }
            (base / "captures" / "2026-02-22" / "trace-checkpoint.json").write_text(
                json.dumps(capture), encoding="utf-8"
            )
            es_logger = AsyncMock()
            es_logger.index_document = AsyncMock(return_value="trace-checkpoint")
            await run_backfill(es_logger)
            # Checkpoint file was written by run_backfill (under patched _project_root)
            cp_path = tmp_path / "telemetry" / "captains_log" / "es_backfill_checkpoint.json"
            assert cp_path.exists()
            data = json.loads(cp_path.read_text(encoding="utf-8"))
            assert data.get("last_scan_completed_at") is not None
            assert data.get("captures", {}).get("last_processed_path") is not None
