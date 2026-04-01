"""Rejected-proposal fingerprint suppression (ADR-0040 Guard 2)."""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from personal_agent.config import settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


def _project_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent.parent.parent


def feedback_history_dir() -> pathlib.Path:
    """Directory for feedback history JSON (gitignored)."""
    d = _project_root() / "telemetry" / "feedback_history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def suppression_file_path() -> pathlib.Path:
    """Path to suppressed_fingerprints.json."""
    return feedback_history_dir() / "suppressed_fingerprints.json"


def _load_suppressions() -> dict[str, Any]:
    path = suppression_file_path()
    if not path.is_file():
        return {}
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        log.warning("suppression_file_load_failed", path=str(path), error=str(exc))
        return {}


def _atomic_write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def is_fingerprint_suppressed(fingerprint: str) -> bool:
    """Return True if fingerprint is under active rejection suppression."""
    fp = fingerprint.lower().strip()
    if not fp:
        return False
    data = _load_suppressions()
    entry = data.get(fp)
    if not isinstance(entry, dict):
        return False
    until_raw = entry.get("suppressed_until")
    if not until_raw:
        return False
    try:
        until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    now = datetime.now(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if now > until:
        return False
    return True


def record_rejection_suppression(
    fingerprint: str,
    *,
    issue_identifier: str,
    duration_days: int | None = None,
) -> None:
    """Append or update suppression entry for a rejected proposal fingerprint.

    Args:
        fingerprint: Proposal fingerprint hex.
        issue_identifier: Linear human-readable id (e.g. FF-123).
        duration_days: Override suppression length (default from settings).
    """
    fp = fingerprint.lower().strip()
    if not fp:
        return
    days = duration_days if duration_days is not None else settings.feedback_suppression_days
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=days)
    path = suppression_file_path()
    data = _load_suppressions()
    data[fp] = {
        "suppressed_until": until.isoformat(),
        "reason": "Rejected via Linear feedback",
        "issue_id": issue_identifier,
        "rejected_at": now.isoformat(),
    }
    _atomic_write_json(path, data)
    log.info(
        "feedback_rejection_recorded",
        fingerprint=fp,
        issue_id=issue_identifier,
        suppression_until=until.isoformat(),
    )
