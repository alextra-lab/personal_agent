"""Shared path-governance helpers for primitive tools.

Provides ``_expand_path``, ``_matches_any``, and ``_check_path_governance``
used by both :mod:`read` and :mod:`write`.

FRE-261.
"""

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import structlog

from personal_agent.config import load_governance_config

log = structlog.get_logger(__name__)


def _expand_path(path: str) -> str:
    """Expand environment variables and home directory in *path*.

    Args:
        path: Raw path string possibly containing ``~`` or ``$VAR`` tokens.

    Returns:
        Expanded string.
    """
    return os.path.expanduser(os.path.expandvars(path))


def _matches_any(path: str, patterns: list[str]) -> bool:
    """Return True if *path* matches at least one glob *pattern*.

    Args:
        path: Resolved filesystem path to test.
        patterns: List of glob patterns (may contain ``~`` / ``$VAR``).

    Returns:
        True if any pattern matches.
    """
    return any(fnmatch(path, _expand_path(p)) for p in patterns)


def _mount_points(mountinfo_path: str = "/proc/self/mountinfo") -> frozenset[str] | None:
    """Return the set of absolute mount-point paths from the kernel mount table.

    Reads ``/proc/self/mountinfo`` rather than ``os.path.ismount``, which only compares
    parent/child device and inode numbers and can misdetect a same-filesystem bind mount.
    Field index 4 of each mountinfo line is the mount point (man 5 proc).

    Args:
        mountinfo_path: Path to the mountinfo file. Parameterized so parsing has a
            hermetic unit test independent of the real filesystem.

    Returns:
        Frozenset of mount-point path strings, or ``None`` when the file cannot be
        read (non-Linux, permission) — distinct from a successfully parsed empty
        table, so callers can fail open on "no mount data" rather than treat it as
        "nothing is durable".
    """
    try:
        with open(mountinfo_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    points = set()
    for line in lines:
        fields = line.split(" ")
        if len(fields) > 4:
            points.add(fields[4])
    return frozenset(points)


def _is_durable_mount(resolved: Path) -> bool:
    """Check whether *resolved* survives a container restart.

    Only ``/app`` is the ephemeral image layer (FRE-1352) — other allowed roots
    (``$HOME/**``, ``/opt/seshat/**``, ``/tmp/**``) are out of this check's scope and
    always considered durable here. For a path under ``/app``, walk its ancestors and
    check each against the live mount table: a bind-mounted subdirectory (or a
    subdirectory of one) survives; a plain image-layer path does not.

    The walk stops at ``/app`` itself and never tests ``/`` — the root filesystem is
    always a mount entry in real mountinfo, so testing it would make every path read
    as durable.

    Args:
        resolved: Fully resolved absolute path.

    Returns:
        True when the path is durable (or the check does not apply), False when it
        is under ``/app`` but not backed by any mount.
    """
    app_root = Path("/app")
    try:
        resolved.relative_to(app_root)
    except ValueError:
        return True

    mount_points = _mount_points()
    if mount_points is None:
        log.warning("mountinfo_unreadable_durability_check_fails_open", path=str(resolved))
        return True

    for ancestor in (resolved, *resolved.parents):
        if str(ancestor) in mount_points:
            return True
        if ancestor == app_root:
            break
    return False


def _check_durability(
    resolved: Path,
    tool_name: str,
    *,
    trace_id: str | None = None,
) -> dict[str, Any] | None:
    """Reject a write into the ephemeral container layer under ``/app``.

    Args:
        resolved: Fully resolved absolute path.
        tool_name: Tool name, included in the log line for §I3 identity threading.
        trace_id: Originating request trace_id.

    Returns:
        An error dict when *resolved* is under ``/app`` but not backed by a mount,
        else ``None``.
    """
    if _is_durable_mount(resolved):
        return None

    path_str = str(resolved)
    log.warning("write_not_durable_rejected", tool=tool_name, path=path_str, trace_id=trace_id)
    return {
        "success": False,
        "error": "not_durable",
        "path": path_str,
        "detail": (
            f"{path_str!r} is under /app but outside a mounted volume, so it lives only in "
            "the container's writable layer and is destroyed on the next rebuild or restart. "
            "Write durable work to /app/agent_workspace/ or /app/telemetry/ instead."
        ),
    }


def _check_path_governance(
    resolved: Path,
    tool_name: str,
    *,
    trace_id: str | None = None,
) -> dict[str, Any] | None:
    """Validate *resolved* against allowed_paths / forbidden_paths for *tool_name*.

    Args:
        resolved: Fully resolved absolute path.
        tool_name: Key to look up in ``governance_config.tools``.
        trace_id: Originating request trace_id, threaded onto the
            governance-load failure log for §I3 identity threading.

    Returns:
        An error dict (with ``success=False``) when the path is rejected,
        or ``None`` when the path is permitted.
    """
    try:
        governance = load_governance_config()
    except Exception as exc:  # noqa: BLE001 — surface as tool error
        log.warning("governance_load_error", tool=tool_name, error=str(exc), trace_id=trace_id)
        return None  # fail open: let the executor proceed

    policy = governance.tools.get(tool_name)
    if policy is None:
        return None  # no policy → permitted

    path_str = str(resolved)

    if policy.forbidden_paths and _matches_any(path_str, policy.forbidden_paths):
        return {
            "success": False,
            "error": "forbidden_path",
            "path": path_str,
            "detail": f"Path {path_str!r} is in the forbidden_paths list for tool '{tool_name}'",
        }

    if policy.allowed_paths and not _matches_any(path_str, policy.allowed_paths):
        return {
            "success": False,
            "error": "path_not_allowed",
            "path": path_str,
            "detail": f"Path {path_str!r} is not in the allowed_paths list for tool '{tool_name}'",
        }

    return None
