"""Read access to archived Captain's Log entries (FRE-1609).

The lifecycle job (``telemetry/lifecycle_manager.py``) gzips a ``CL-*.json`` file that
is older than its hot window into ``<telemetry>/archive/captains_log_reflections/YYYY-MM/``.
The archive is a storage tier, not a decision: an archived entry keeps its status and
its fingerprint. Write-time dedup and ``_mark_promoted`` read only the live directory,
so a re-sighting of an archived idea created a fresh promotable entry
(``CL-20261003-084335`` carried FRE-1378's fingerprint after its entry aged out).

These helpers find an archived entry and move it back to the live directory, so the
caller merges into or marks the original entry, as if it had never aged out.
"""

from __future__ import annotations

import gzip
import json
import os
import pathlib
import tempfile
from collections.abc import Callable, Mapping

from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_GZ_SUFFIX = ".gz"


def reflection_archive_dir(log_dir: pathlib.Path) -> pathlib.Path:
    """Return the archive directory that the lifecycle job fills for ``log_dir``.

    ``lifecycle_manager`` writes to ``<telemetry root>/archive/captains_log_reflections``,
    and the live directory is ``<telemetry root>/captains_log``.

    Args:
        log_dir: The live Captain's Log directory.

    Returns:
        The reflections archive directory (it may not exist).
    """
    return log_dir.parent / "archive" / "captains_log_reflections"


def find_archived(
    log_dir: pathlib.Path,
    matches: Callable[[Mapping[str, object]], bool],
    *,
    name_prefix: str = "CL-",
    trace_id: str | None = None,
) -> pathlib.Path | None:
    """Return the newest archived entry whose JSON satisfies ``matches``.

    Args:
        log_dir: The live Captain's Log directory.
        matches: Predicate over the decoded entry JSON.
        name_prefix: Only archive files whose name starts with this prefix are read.
        trace_id: Trace of the sighting or promotion that caused the search.

    Returns:
        Path to the matching ``.json.gz`` file, or ``None``.
    """
    archive_dir = reflection_archive_dir(log_dir)
    if not archive_dir.is_dir():
        return None
    candidates = sorted(
        archive_dir.glob(f"*/{name_prefix}*.json{_GZ_SUFFIX}"),
        key=lambda p: p.name,
        reverse=True,
    )
    for gz_path in candidates:
        try:
            with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, EOFError) as exc:
            # Not silent: an unreadable archive file is an entry dedup cannot see, and
            # a missed match is exactly how a duplicate ticket gets filed.
            log.warning(
                "captains_log_archive_entry_unreadable",
                archive_file=str(gz_path),
                error=str(exc),
                trace_id=trace_id,
            )
            continue
        if isinstance(data, dict) and matches(data):
            return gz_path
    return None


def restore_archived(
    gz_path: pathlib.Path, log_dir: pathlib.Path, *, trace_id: str | None = None
) -> pathlib.Path:
    """Move an archived entry back to the live directory, unchanged.

    The decompressed file is written to a temp file and then hard-linked into place,
    which never replaces an existing file: if a concurrent restore already put the entry
    back (and a merge may already have updated it), that live file wins and the stale
    archived bytes are discarded. The ``.gz`` is removed only after the live file exists,
    so an interrupted restore leaves the entry in at least one place. The new file's
    mtime is now, so the lifecycle job keeps it live for a full hot window.

    Args:
        gz_path: An archived ``CL-*.json.gz`` file.
        log_dir: The live Captain's Log directory.
        trace_id: Trace of the sighting or promotion that caused the restore.

    Returns:
        Path to the restored live ``CL-*.json`` file.

    Raises:
        OSError: If the archive cannot be read or the live file cannot be written.
    """
    live_path = log_dir / gz_path.name.removesuffix(_GZ_SUFFIX)
    with gzip.open(gz_path, "rb") as fh:
        payload = fh.read()
    fd, tmp_name = tempfile.mkstemp(dir=log_dir, prefix=".restore-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
        os.link(tmp_name, live_path)
    except FileExistsError:
        pass
    finally:
        pathlib.Path(tmp_name).unlink(missing_ok=True)
    gz_path.unlink(missing_ok=True)
    log.info(
        "captains_log_entry_restored_from_archive",
        archive_file=str(gz_path),
        file=str(live_path),
        trace_id=trace_id,
    )
    return live_path
