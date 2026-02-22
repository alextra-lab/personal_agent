"""Data lifecycle manager: retention, archival, purge, and disk monitoring (Phase 2.3)."""

import asyncio
import gzip
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from personal_agent.config.settings import get_settings
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.events import (
    LIFECYCLE_ARCHIVE,
    LIFECYCLE_DISK_ALERT,
    LIFECYCLE_DISK_CHECK,
    LIFECYCLE_ES_CLEANUP,
    LIFECYCLE_PURGE,
    LIFECYCLE_REPORT,
)
from personal_agent.telemetry.lifecycle import RETENTION_POLICIES

log = get_logger(__name__)


@dataclass
class DiskUsageReport:
    """Result of disk usage check."""

    path: str
    total_bytes: int
    used_bytes: int
    used_percent: float
    alert: bool


@dataclass
class ArchiveResult:
    """Result of archiving old data."""

    data_type: str
    archived_count: int
    archived_bytes: int
    errors: list[str] = field(default_factory=list)


@dataclass
class PurgeResult:
    """Result of purging expired data."""

    data_type: str
    purged_count: int
    freed_bytes: int
    errors: list[str] = field(default_factory=list)


@dataclass
class ESCleanupResult:
    """Result of Elasticsearch index cleanup."""

    deleted_indices: list[str]
    deleted_count: int
    errors: list[str] = field(default_factory=list)


@dataclass
class LifecycleReport:
    """Data lifecycle status report (read-only; does not perform archive/purge)."""

    disk_reports: list[DiskUsageReport]
    would_archive: dict[str, int]  # Count of files that would be archived per data_type
    would_purge: dict[str, int]  # Count of files that would be purged per data_type
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _telemetry_root() -> Path:
    """Return resolved telemetry root (parent of log_dir)."""
    return get_settings().log_dir.parent


def _file_logs_dir() -> Path:
    return get_settings().log_dir


def _captains_log_dir() -> Path:
    return _telemetry_root() / "captains_log"


def _captures_dir() -> Path:
    return _captains_log_dir() / "captures"


def _archive_base() -> Path:
    return _telemetry_root() / "archive"


def _paths_for_data_type(data_type: str) -> Path | None:
    """Return the directory path for a data type, or None if not file-based."""
    if data_type == "file_logs":
        return _file_logs_dir()
    if data_type == "captains_log_captures":
        return _captures_dir()
    if data_type == "captains_log_reflections":
        return _captains_log_dir()
    return None


def _iter_file_logs(path: Path) -> list[tuple[Path, datetime]]:
    """Yield (file_path, mtime_datetime) for files under path."""
    out: list[tuple[Path, datetime]] = []
    if not path.exists():
        return out
    for f in path.rglob("*"):
        if f.is_file() and f.suffix == ".jsonl":
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            out.append((f, mtime))
    return out


def _iter_captures(path: Path) -> list[tuple[Path, datetime]]:
    """Yield (file_path, mtime) for capture JSON files (including in date subdirs)."""
    out: list[tuple[Path, datetime]] = []
    if not path.exists():
        return out
    for f in path.rglob("*.json"):
        if f.is_file():
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            out.append((f, mtime))
    return out


def _iter_reflections(path: Path) -> list[tuple[Path, datetime]]:
    """Yield (file_path, mtime) for CL-*.json in captains_log root (not in captures/)."""
    out: list[tuple[Path, datetime]] = []
    if not path.exists():
        return out
    for f in path.glob("CL-*.json"):
        if f.is_file():
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            out.append((f, mtime))
    return out


def _iter_files(data_type: str, path: Path) -> list[tuple[Path, datetime]]:
    """Return list of (path, mtime) for files to consider for lifecycle."""
    if data_type == "file_logs":
        return _iter_file_logs(path)
    if data_type == "captains_log_captures":
        return _iter_captures(path)
    if data_type == "captains_log_reflections":
        return _iter_reflections(path)
    return []


def _compress_and_move(src: Path, dest: Path) -> int:
    """Compress src with gzip into dest, then remove src. Returns bytes of src."""
    size = src.stat().st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as f_in:
        with gzip.open(dest, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    src.unlink()
    return size


class DataLifecycleManager:
    """Manages data retention, archival, and cleanup with telemetry for all operations."""

    def __init__(
        self,
        es_client: Any | None = None,
    ) -> None:
        """Initialize the lifecycle manager.

        Args:
            es_client: Optional AsyncElasticsearch client for index cleanup.
        """
        self._es_client = es_client

    async def check_disk_usage(self) -> list[DiskUsageReport]:
        """Check disk usage for telemetry and key data paths.

        Returns:
            List of disk usage reports. Emits LIFECYCLE_DISK_CHECK and LIFECYCLE_DISK_ALERT.
        """
        settings = get_settings()
        reports: list[DiskUsageReport] = []
        telemetry_path = _telemetry_root()

        def _stat(path: Path) -> tuple[int, int]:
            total = 0
            used = 0
            if not path.exists():
                return 0, 0
            try:
                stat = shutil.disk_usage(path)
                total = stat.total
                used = stat.used
            except OSError:
                pass
            return total, used

        total, used = await asyncio.to_thread(_stat, telemetry_path)
        used_pct = (used / total * 100.0) if total else 0.0
        alert = used_pct >= settings.disk_usage_alert_percent
        report = DiskUsageReport(
            path=str(telemetry_path),
            total_bytes=total,
            used_bytes=used,
            used_percent=used_pct,
            alert=alert,
        )
        reports.append(report)

        log.info(
            LIFECYCLE_DISK_CHECK,
            path=str(telemetry_path),
            used_percent=round(used_pct, 2),
            total_bytes=total,
            used_bytes=used,
            alert=alert,
        )
        if alert:
            log.warning(
                LIFECYCLE_DISK_ALERT,
                path=str(telemetry_path),
                used_percent=round(used_pct, 2),
                threshold=settings.disk_usage_alert_percent,
            )
        return reports

    async def archive_old_data(self, data_type: str) -> ArchiveResult:
        """Archive data older than hot_duration (compress and move to archive).

        Args:
            data_type: One of file_logs, captains_log_captures, captains_log_reflections.

        Returns:
            ArchiveResult with counts and any errors. Emits LIFECYCLE_ARCHIVE.
        """
        policy = RETENTION_POLICIES.get(data_type)
        if not policy or not policy.archive_enabled:
            log.info(
                LIFECYCLE_ARCHIVE, data_type=data_type, skipped=True, reason="archive_disabled"
            )
            return ArchiveResult(data_type=data_type, archived_count=0, archived_bytes=0)

        path = _paths_for_data_type(data_type)
        if not path:
            return ArchiveResult(data_type=data_type, archived_count=0, archived_bytes=0)

        now = datetime.now(timezone.utc)
        cutoff = now - policy.hot_duration
        files = _iter_files(data_type, path)
        to_archive = [(p, m) for p, m in files if m < cutoff]
        archived_count = 0
        archived_bytes = 0
        errors: list[str] = []

        archive_dir = _archive_base() / data_type

        for file_path, mtime in to_archive:
            try:
                # Preserve relative structure under archive/data_type/YYYY-MM/
                date_prefix = mtime.strftime("%Y-%m")
                rel = file_path.relative_to(path)
                archive_path = archive_dir / date_prefix / f"{rel}.gz"
                size = await asyncio.to_thread(_compress_and_move, file_path, archive_path)
                archived_count += 1
                archived_bytes += size
            except Exception as e:
                errors.append(f"{file_path}: {e}")

        log.info(
            LIFECYCLE_ARCHIVE,
            data_type=data_type,
            archived_count=archived_count,
            archived_bytes=archived_bytes,
            errors_count=len(errors),
        )
        return ArchiveResult(
            data_type=data_type,
            archived_count=archived_count,
            archived_bytes=archived_bytes,
            errors=errors,
        )

    async def purge_expired_data(self, data_type: str) -> PurgeResult:
        """Delete data older than cold_duration (files or archive).

        Args:
            data_type: One of file_logs, captains_log_captures, captains_log_reflections.

        Returns:
            PurgeResult. Emits LIFECYCLE_PURGE.
        """
        policy = RETENTION_POLICIES.get(data_type)
        if not policy or policy.cold_duration.total_seconds() <= 0:
            return PurgeResult(data_type=data_type, purged_count=0, freed_bytes=0)

        path = _paths_for_data_type(data_type)
        if not path:
            return PurgeResult(data_type=data_type, purged_count=0, freed_bytes=0)

        now = datetime.now(timezone.utc)
        cutoff = now - policy.cold_duration
        files = _iter_files(data_type, path)
        to_purge = [(p, m) for p, m in files if m < cutoff]
        purged_count = 0
        freed_bytes = 0
        errors: list[str] = []

        def _unlink(p: Path) -> int:
            try:
                size = p.stat().st_size
                p.unlink()
                return size
            except OSError as e:
                raise RuntimeError(str(e)) from e

        for file_path, _ in to_purge:
            try:
                size = await asyncio.to_thread(_unlink, file_path)
                purged_count += 1
                freed_bytes += size
            except Exception as e:
                errors.append(f"{file_path}: {e}")

        # Purge old archives for this data type
        archive_dir = _archive_base() / data_type
        if archive_dir.exists():
            for archive_file in archive_dir.rglob("*.gz"):
                try:
                    mtime = datetime.fromtimestamp(archive_file.stat().st_mtime, tz=timezone.utc)
                    if mtime < cutoff:
                        size = await asyncio.to_thread(_unlink, archive_file)
                        purged_count += 1
                        freed_bytes += size
                except Exception as e:
                    errors.append(f"{archive_file}: {e}")

        log.info(
            LIFECYCLE_PURGE,
            data_type=data_type,
            purged_count=purged_count,
            freed_bytes=freed_bytes,
            errors_count=len(errors),
        )
        return PurgeResult(
            data_type=data_type,
            purged_count=purged_count,
            freed_bytes=freed_bytes,
            errors=errors,
        )

    async def cleanup_elasticsearch_indices(self) -> ESCleanupResult:
        """Delete ES indices older than cold_duration for logs and captains indices.

        Uses retention policy elasticsearch_logs for age. Requires ES client.

        Returns:
            ESCleanupResult. Emits LIFECYCLE_ES_CLEANUP.
        """
        policy = RETENTION_POLICIES.get("elasticsearch_logs")
        if not policy or policy.cold_duration.total_seconds() <= 0:
            return ESCleanupResult(deleted_indices=[], deleted_count=0)

        if not self._es_client:
            log.debug("lifecycle_es_cleanup_skipped", reason="no_es_client")
            return ESCleanupResult(deleted_indices=[], deleted_count=0)

        settings = get_settings()
        cutoff = datetime.now(timezone.utc) - policy.cold_duration
        prefixes = [
            settings.elasticsearch_index_prefix,  # agent-logs
            "agent-captains-captures",
            "agent-captains-reflections",
        ]
        deleted: list[str] = []
        errors: list[str] = []

        try:
            cat = await self._es_client.cat.indices(
                index=",".join(f"{p}*" for p in prefixes), format="json"
            )
            for idx in cat:
                index_name = idx.get("index", "")
                if not index_name:
                    continue
                # Parse date from index name: agent-logs-2026.02.22 or agent-captains-captures-2026-02-22
                parts = index_name.split("-")
                if len(parts) < 2:
                    continue
                date_part = parts[-1]
                dt = None
                for fmt in ("%Y.%m.%d", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(date_part, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                if dt is None or dt >= cutoff:
                    continue
                try:
                    await self._es_client.indices.delete(index=index_name)
                    deleted.append(index_name)
                except Exception as e:
                    errors.append(f"{index_name}: {e}")
        except Exception as e:
            errors.append(f"cat_indices: {e}")

        log.info(
            LIFECYCLE_ES_CLEANUP,
            deleted_count=len(deleted),
            deleted_indices=deleted[:20],
            errors_count=len(errors),
        )
        return ESCleanupResult(deleted_indices=deleted, deleted_count=len(deleted), errors=errors)

    def _count_would_archive(self, data_type: str) -> int:
        """Return number of files that would be archived for data_type."""
        policy = RETENTION_POLICIES.get(data_type)
        if not policy or not policy.archive_enabled:
            return 0
        path = _paths_for_data_type(data_type)
        if not path:
            return 0
        now = datetime.now(timezone.utc)
        cutoff = now - policy.hot_duration
        files = _iter_files(data_type, path)
        return sum(1 for _, m in files if m < cutoff)

    def _count_would_purge(self, data_type: str) -> int:
        """Return number of files that would be purged for data_type."""
        policy = RETENTION_POLICIES.get(data_type)
        if not policy or policy.cold_duration.total_seconds() <= 0:
            return 0
        path = _paths_for_data_type(data_type)
        if not path:
            return 0
        now = datetime.now(timezone.utc)
        cutoff = now - policy.cold_duration
        files = _iter_files(data_type, path)
        return sum(1 for _, m in files if m < cutoff)

    async def generate_report(self) -> LifecycleReport:
        """Generate a data lifecycle status report (read-only: disk + would-archive/purge counts).

        Returns:
            LifecycleReport. Emits LIFECYCLE_REPORT.
        """
        disk_reports = await self.check_disk_usage()
        would_archive: dict[str, int] = {}
        would_purge: dict[str, int] = {}
        for data_type in ("file_logs", "captains_log_captures", "captains_log_reflections"):
            would_archive[data_type] = self._count_would_archive(data_type)
            would_purge[data_type] = self._count_would_purge(data_type)

        report = LifecycleReport(
            disk_reports=disk_reports,
            would_archive=would_archive,
            would_purge=would_purge,
        )
        log.info(
            LIFECYCLE_REPORT,
            generated_at=report.generated_at.isoformat(),
            disk_alerts=sum(1 for r in disk_reports if r.alert),
            would_archive_total=sum(would_archive.values()),
            would_purge_total=sum(would_purge.values()),
        )
        return report
