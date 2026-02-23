"""Captain's Log Elasticsearch backfill and replay (FRE-30).

Replays missed captures and reflections from local files to Elasticsearch when
ES becomes available. Uses deterministic document IDs for idempotent indexing
and a checkpoint file for resume after restart.
"""

import json
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from personal_agent.captains_log.capture import CAPTURES_INDEX_PREFIX, TaskCapture
from personal_agent.captains_log.models import CaptainLogEntry, CaptainLogEntryType
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.events import (
    CAPTAINS_LOG_BACKFILL_CHECKPOINT_UPDATED,
    CAPTAINS_LOG_BACKFILL_COMPLETED,
    CAPTAINS_LOG_BACKFILL_FILE_FAILED,
    CAPTAINS_LOG_BACKFILL_STARTED,
)

log = get_logger(__name__)

REFLECTIONS_INDEX_PREFIX = "agent-captains-reflections"
CHECKPOINT_VERSION = 1
CHECKPOINT_FILENAME = "es_backfill_checkpoint.json"


def _project_root() -> pathlib.Path:
    """Return project root (parent of src)."""
    return pathlib.Path(__file__).resolve().parent.parent.parent.parent


def _captains_log_dir() -> pathlib.Path:
    """Return telemetry/captains_log directory."""
    return _project_root() / "telemetry" / "captains_log"


def _captures_dir() -> pathlib.Path:
    """Return telemetry/captains_log/captures directory."""
    return _captains_log_dir() / "captures"


def _checkpoint_path() -> pathlib.Path:
    """Return path to backfill checkpoint file."""
    return _captains_log_dir() / CHECKPOINT_FILENAME


def _path_relative_to_root(p: pathlib.Path) -> str:
    """Return path as string relative to project root, with forward slashes."""
    try:
        return p.resolve().relative_to(_project_root()).as_posix()
    except ValueError:
        return p.as_posix()


@dataclass
class BackfillCheckpoint:
    """Replay checkpoint for resume after restart."""

    version: int = CHECKPOINT_VERSION
    last_scan_started_at: str | None = None
    last_scan_completed_at: str | None = None
    captures: dict[str, Any] = field(default_factory=lambda: {"last_processed_path": None, "last_processed_mtime": None})
    reflections: dict[str, Any] = field(default_factory=lambda: {"last_processed_path": None, "last_processed_mtime": None})

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON."""
        return {
            "version": self.version,
            "last_scan_started_at": self.last_scan_started_at,
            "last_scan_completed_at": self.last_scan_completed_at,
            "captures": self.captures,
            "reflections": self.reflections,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackfillCheckpoint":
        """Load from JSON dict."""
        return cls(
            version=data.get("version", 1),
            last_scan_started_at=data.get("last_scan_started_at"),
            last_scan_completed_at=data.get("last_scan_completed_at"),
            captures=data.get("captures", {"last_processed_path": None, "last_processed_mtime": None}),
            reflections=data.get("reflections", {"last_processed_path": None, "last_processed_mtime": None}),
        )


def _load_checkpoint() -> BackfillCheckpoint:
    """Load checkpoint from disk; return default if missing or invalid."""
    path = _checkpoint_path()
    if not path.exists():
        return BackfillCheckpoint()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BackfillCheckpoint.from_dict(data)
    except Exception as e:
        log.warning(
            "captains_log_backfill_checkpoint_load_failed",
            path=str(path),
            error=str(e),
        )
        return BackfillCheckpoint()


def _save_checkpoint(cp: BackfillCheckpoint) -> None:
    """Persist checkpoint to disk."""
    path = _checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cp.to_dict(), indent=2), encoding="utf-8")


def _list_capture_files_sorted() -> list[tuple[pathlib.Path, float]]:
    """List capture files in stable order: by date dir then filename. Returns (path, mtime)."""
    captures_dir = _captures_dir()
    if not captures_dir.exists():
        return []
    out: list[tuple[pathlib.Path, float]] = []
    for date_dir in sorted(captures_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        for f in sorted(date_dir.glob("*.json")):
            try:
                out.append((f, f.stat().st_mtime))
            except OSError:
                continue
    return out


def _list_reflection_files_sorted() -> list[tuple[pathlib.Path, float]]:
    """List reflection files (CL-*.json) in stable order. Returns (path, mtime)."""
    log_dir = _captains_log_dir()
    if not log_dir.exists():
        return []
    out: list[tuple[pathlib.Path, float]] = []
    for f in sorted(log_dir.glob("CL-*.json")):
        try:
            out.append((f, f.stat().st_mtime))
        except OSError:
            continue
    return out


@dataclass
class BackfillResult:
    """Result of a single backfill run."""

    files_scanned: int = 0
    indexed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    elapsed_ms: float = 0.0


async def run_backfill(
    es_logger: Any,
    *,
    checkpoint: BackfillCheckpoint | None = None,
) -> BackfillResult:
    """Run one backfill pass: replay missed captures and reflections to Elasticsearch.

    Uses deterministic document IDs (trace_id for captures, entry_id for reflections)
    so replay is idempotent. Updates checkpoint after successful indexing.

    Args:
        es_logger: ElasticsearchLogger (index_document(index_name, document, id=None) -> str | None).
        checkpoint: Optional pre-loaded checkpoint; loaded from disk if None.

    Returns:
        BackfillResult with counts and timing. Does not raise; failures are logged.
    """
    from time import perf_counter

    result = BackfillResult()
    start = perf_counter()
    cp = checkpoint or _load_checkpoint()
    cp.last_scan_started_at = datetime.now(timezone.utc).isoformat()
    root = _project_root()

    log.info(
        CAPTAINS_LOG_BACKFILL_STARTED,
        checkpoint_captures=cp.captures.get("last_processed_path"),
        checkpoint_reflections=cp.reflections.get("last_processed_path"),
    )

    # Captures
    capture_list = _list_capture_files_sorted()
    last_capture_path: str | None = cp.captures.get("last_processed_path")
    last_capture_mtime: str | None = cp.captures.get("last_processed_mtime")

    for file_path, mtime in capture_list:
        result.files_scanned += 1
        rel = _path_relative_to_root(file_path)
        mtime_str = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        if last_capture_path is not None and (rel < last_capture_path or (rel == last_capture_path and last_capture_mtime and mtime_str <= last_capture_mtime)):
            result.skipped_count += 1
            continue
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            capture = TaskCapture(**raw)
            date_str = capture.timestamp.strftime("%Y-%m-%d")
            index_name = f"{CAPTURES_INDEX_PREFIX}-{date_str}"
            doc = capture.model_dump(mode="json")
            doc_id = capture.trace_id
            rid = await es_logger.index_document(index_name, doc, id=doc_id)
            if rid is not None:
                result.indexed_count += 1
                cp.captures["last_processed_path"] = rel
                cp.captures["last_processed_mtime"] = mtime_str
                _save_checkpoint(cp)
                log.info(
                    CAPTAINS_LOG_BACKFILL_CHECKPOINT_UPDATED,
                    kind="captures",
                    last_processed_path=rel,
                )
            else:
                result.failed_count += 1
        except Exception as e:
            result.failed_count += 1
            log.warning(
                CAPTAINS_LOG_BACKFILL_FILE_FAILED,
                file_path=rel,
                kind="capture",
                error=str(e),
            )

    # Reflections
    refl_list = _list_reflection_files_sorted()
    last_refl_path = cp.reflections.get("last_processed_path")
    last_refl_mtime = cp.reflections.get("last_processed_mtime")

    for file_path, mtime in refl_list:
        result.files_scanned += 1
        rel = _path_relative_to_root(file_path)
        mtime_str = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        if last_refl_path is not None and (rel < last_refl_path or (rel == last_refl_path and last_refl_mtime and mtime_str <= last_refl_mtime)):
            result.skipped_count += 1
            continue
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            entry = CaptainLogEntry(**raw)
            if entry.type not in {CaptainLogEntryType.REFLECTION, CaptainLogEntryType.CONFIG_PROPOSAL}:
                result.skipped_count += 1
                continue
            date_str = entry.timestamp.strftime("%Y-%m-%d")
            index_name = f"{REFLECTIONS_INDEX_PREFIX}-{date_str}"
            doc = entry.model_dump(mode="json")
            doc_id = entry.entry_id
            rid = await es_logger.index_document(index_name, doc, id=doc_id)
            if rid is not None:
                result.indexed_count += 1
                cp.reflections["last_processed_path"] = rel
                cp.reflections["last_processed_mtime"] = mtime_str
                _save_checkpoint(cp)
                log.info(
                    CAPTAINS_LOG_BACKFILL_CHECKPOINT_UPDATED,
                    kind="reflections",
                    last_processed_path=rel,
                )
            else:
                result.failed_count += 1
        except Exception as e:
            result.failed_count += 1
            log.warning(
                CAPTAINS_LOG_BACKFILL_FILE_FAILED,
                file_path=rel,
                kind="reflection",
                error=str(e),
            )

    cp.last_scan_completed_at = datetime.now(timezone.utc).isoformat()
    _save_checkpoint(cp)
    result.elapsed_ms = (perf_counter() - start) * 1000

    log.info(
        CAPTAINS_LOG_BACKFILL_COMPLETED,
        files_scanned=result.files_scanned,
        indexed_count=result.indexed_count,
        failed_count=result.failed_count,
        skipped_count=result.skipped_count,
        elapsed_ms=round(result.elapsed_ms, 2),
    )
    return result
