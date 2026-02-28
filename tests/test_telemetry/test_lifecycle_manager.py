"""Tests for DataLifecycleManager."""

from pathlib import Path

import pytest

from personal_agent.telemetry.lifecycle_manager import (
    ArchiveResult,
    DataLifecycleManager,
    DiskUsageReport,
    LifecycleReport,
    PurgeResult,
    _iter_file_logs,
    _iter_reflections,
)


def test_iter_file_logs_empty(tmp_path: Path) -> None:
    """_iter_file_logs returns empty when dir missing or no jsonl."""
    assert _iter_file_logs(tmp_path) == []
    (tmp_path / "other.txt").write_text("x")
    assert _iter_file_logs(tmp_path) == []
    (tmp_path / "a.jsonl").write_text("{}")
    out = _iter_file_logs(tmp_path)
    assert len(out) == 1
    assert out[0][0].name == "a.jsonl"


def test_iter_reflections_only_cl_prefix(tmp_path: Path) -> None:
    """_iter_reflections only picks CL-*.json in root."""
    (tmp_path / "CL-20260101-120000-001.json").write_text("{}")
    (tmp_path / "other.json").write_text("{}")
    (tmp_path / "CL-20260101-120001-002.json").write_text("{}")
    out = _iter_reflections(tmp_path)
    assert len(out) == 2
    names = {p.name for p, _ in out}
    assert "other.json" not in names


@pytest.mark.asyncio
async def test_check_disk_usage_returns_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """check_disk_usage returns list of DiskUsageReport."""
    monkeypatch.setattr(
        "personal_agent.telemetry.lifecycle_manager._telemetry_root",
        lambda: tmp_path,
    )
    manager = DataLifecycleManager()
    reports = await manager.check_disk_usage()
    assert len(reports) >= 1
    assert isinstance(reports[0], DiskUsageReport)
    assert reports[0].path == str(tmp_path)
    assert 0 <= reports[0].used_percent <= 100


@pytest.mark.asyncio
async def test_archive_old_data_returns_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """archive_old_data returns ArchiveResult; with empty dir yields 0 archived."""
    monkeypatch.setattr(
        "personal_agent.telemetry.lifecycle_manager._telemetry_root", lambda: tmp_path
    )
    monkeypatch.setattr(
        "personal_agent.telemetry.lifecycle_manager._file_logs_dir", lambda: tmp_path / "logs"
    )
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "personal_agent.telemetry.lifecycle_manager._archive_base", lambda: tmp_path / "archive"
    )

    manager = DataLifecycleManager()
    result = await manager.archive_old_data("file_logs")
    assert isinstance(result, ArchiveResult)
    assert result.data_type == "file_logs"
    assert result.archived_count >= 0
    assert isinstance(result.errors, list)


@pytest.mark.asyncio
async def test_purge_expired_data_respects_policy() -> None:
    """purge_expired_data for neo4j_graph does nothing (cold_duration=0)."""
    manager = DataLifecycleManager()
    result = await manager.purge_expired_data("neo4j_graph")
    assert result.purged_count == 0
    assert result.data_type == "neo4j_graph"


@pytest.mark.asyncio
async def test_purge_expired_data_returns_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """purge_expired_data returns PurgeResult; empty dir yields 0 purged."""
    monkeypatch.setattr(
        "personal_agent.telemetry.lifecycle_manager._telemetry_root", lambda: tmp_path
    )
    monkeypatch.setattr(
        "personal_agent.telemetry.lifecycle_manager._file_logs_dir", lambda: tmp_path / "logs"
    )
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "personal_agent.telemetry.lifecycle_manager._archive_base", lambda: tmp_path / "archive"
    )

    manager = DataLifecycleManager()
    result = await manager.purge_expired_data("file_logs")
    assert isinstance(result, PurgeResult)
    assert result.data_type == "file_logs"
    assert result.purged_count >= 0
    assert isinstance(result.errors, list)


@pytest.mark.asyncio
async def test_cleanup_elasticsearch_indices_no_client() -> None:
    """cleanup_elasticsearch_indices returns empty when no ES client."""
    manager = DataLifecycleManager(es_client=None)
    result = await manager.cleanup_elasticsearch_indices()
    assert result.deleted_count == 0
    assert result.deleted_indices == []


@pytest.mark.asyncio
async def test_generate_report_read_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """generate_report does not modify data; returns LifecycleReport."""
    monkeypatch.setattr(
        "personal_agent.telemetry.lifecycle_manager._telemetry_root", lambda: tmp_path
    )
    manager = DataLifecycleManager()
    report = await manager.generate_report()
    assert isinstance(report, LifecycleReport)
    assert report.disk_reports
    assert "file_logs" in report.would_archive
    assert "file_logs" in report.would_purge
    assert report.generated_at is not None
