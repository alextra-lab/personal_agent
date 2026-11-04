"""Shared path-governance helpers for primitive tools.

Provides ``_expand_path``, ``_matches_any``, and ``_check_path_governance``
used by both :mod:`read` and :mod:`write`. A refusal raises :class:`PathRefusalError`, whose
codes are part of each caller's own code set (ADR-0156 D6.1).

FRE-261. FRE-1575 (one tool contract).
"""

import errno
import os
import stat
from collections.abc import Sequence
from contextlib import suppress
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

import structlog

from personal_agent.config import load_governance_config
from personal_agent.tools.contract import ToolError

log = structlog.get_logger(__name__)

PathCode = Literal[
    "governance_unavailable", "no_path_policy", "forbidden_path", "path_not_allowed", "not_durable"
]
"""Refusals of a path by the path rules or the durability check."""


class PathRefusalError(ToolError[PathCode]):
    """A path refused by the path rules (``read`` and ``write``)."""


_PATH_NEXT = "Do not retry this path. Use a path that the path rules allow."
# O_PATH walks a directory that the caller cannot list. macOS has no O_PATH.
DIRECTORY_FLAGS = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY


def _open_directory_no_symlinks(parts: Sequence[str], *, make_parents: bool) -> int:
    """Open a directory from ``/``, one part at a time, and refuse a symbolic link in any part.

    Args:
        parts: The path parts below ``/``.
        make_parents: Whether to create a missing part. A part that exists as a link still
            refuses, so nothing is created through a link.

    Returns:
        The open directory descriptor.

    Raises:
        OSError: A part is a symbolic link (``ENOTDIR``), or the open fails.
    """
    current = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in parts:
            if make_parents:
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=current)
            child = os.open(part, DIRECTORY_FLAGS | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
    except BaseException:
        os.close(current)
        raise
    return current


def open_no_symlinks(
    path: Path, flags: int, mode: int = 0o666, *, make_parents: bool = False
) -> int:
    """Open an absolute path, and refuse a symbolic link in any part of it (FRE-1732).

    The gateway runs as root, and the ``bash`` child owns directories that root tools act in. A
    path that the rules checked can change to a link before the open, so each part opens from
    its parent with ``O_NOFOLLOW``. The caller passes a resolved path, which holds no link.

    Args:
        path: The resolved absolute path.
        flags: The ``os.open`` flags for the last part.
        mode: The permission bits for a new file.
        make_parents: Whether to create a missing directory part. A part that exists as a link
            still refuses, so nothing is created through a link.

    Returns:
        The open file descriptor.

    Raises:
        OSError: A part of the path is a symbolic link (``ELOOP``, or ``ENOTDIR`` for a directory
            part), or the open fails.
    """
    parent = _open_directory_no_symlinks(path.parts[1:-1], make_parents=make_parents)
    try:
        return os.open(path.name, flags | os.O_NOFOLLOW, mode, dir_fd=parent)
    finally:
        os.close(parent)


def open_file_no_symlinks(
    path: Path, flags: int, mode: int = 0o666, *, make_parents: bool = False
) -> int:
    """Open a regular file with :func:`open_no_symlinks`, for ``os.fdopen``.

    ``O_NONBLOCK`` has no effect on a regular file, and it stops a FIFO that the child planted
    from blocking the event loop.

    Args:
        path: The resolved absolute path.
        flags: The ``os.open`` flags.
        mode: The permission bits for a new file.
        make_parents: Whether to create a missing directory part.

    Returns:
        The open file descriptor.

    Raises:
        OSError: A part of the path is a symbolic link, the path is not a regular file
            (``EINVAL``), or the open fails.
    """
    fd = open_no_symlinks(path, flags | os.O_NONBLOCK, mode, make_parents=make_parents)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file", str(path))
    return fd


def make_directory_no_symlinks(path: Path) -> None:
    """Create an absolute directory path, and refuse a symbolic link in any part of it.

    Args:
        path: The absolute directory path.

    Raises:
        OSError: A part of the path is a symbolic link, or the creation fails.
    """
    os.close(_open_directory_no_symlinks(path.parts[1:], make_parents=True))


def _expand_path(path: str) -> str:
    """Expand the home directory in a model's *path*.

    ``$VAR`` stays literal text: a variable's value must not reach a path, an error or a log
    line (FRE-1729).

    Args:
        path: Raw path string from a model, possibly containing ``~``.

    Returns:
        Expanded string.
    """
    return os.path.expanduser(path)


def _expand_pattern(pattern: str) -> str:
    """Expand environment variables and home directory in an operator's *pattern*.

    Args:
        pattern: Glob pattern from the governance config, possibly containing ``~`` or
            ``$VAR`` tokens.

    Returns:
        Expanded string.
    """
    return os.path.expanduser(os.path.expandvars(pattern))


def _matches_any(path: str, patterns: list[str]) -> bool:
    """Return True if *path* matches at least one glob *pattern*.

    Args:
        path: Resolved filesystem path to test.
        patterns: List of glob patterns (may contain ``~`` / ``$VAR``).

    Returns:
        True if any pattern matches.
    """
    return any(fnmatch(path, _expand_pattern(p)) for p in patterns)


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
) -> None:
    """Reject a write into the ephemeral container layer under ``/app``.

    Args:
        resolved: Fully resolved absolute path.
        tool_name: Tool name, included in the log line for §I3 identity threading.
        trace_id: Originating request trace_id.

    Raises:
        PathRefusalError: ``not_durable`` — *resolved* is under ``/app`` but not backed by
            a mount.
    """
    if _is_durable_mount(resolved):
        return

    path_str = str(resolved)
    log.warning("write_not_durable_rejected", tool=tool_name, path=path_str, trace_id=trace_id)
    raise PathRefusalError(
        "not_durable",
        f"{path_str!r} is under /app but outside a mounted volume, so it lives only in "
        "the container's writable layer and is destroyed on the next rebuild or restart.",
        retryable=False,
        next_action="Write durable work to /app/agent_workspace/ or /app/telemetry/ instead.",
    )


def _check_path_governance(
    resolved: Path,
    tool_name: str,
    *,
    trace_id: str | None = None,
) -> None:
    """Validate *resolved* against allowed_paths / forbidden_paths for *tool_name*.

    A governance load error and a missing policy both reject (ADR-0156 D8.1, FRE-1574): a
    path check that cannot run does not permit.

    Args:
        resolved: Fully resolved absolute path.
        tool_name: Key to look up in ``governance_config.tools``.
        trace_id: Originating request trace_id, threaded onto the
            governance-load failure log for §I3 identity threading.

    Raises:
        PathRefusalError: The path is refused, or the rules cannot be checked.
    """
    try:
        governance = load_governance_config()
    except Exception as exc:  # noqa: BLE001 — any load failure must refuse, never permit
        log.warning("governance_load_error", tool=tool_name, error=str(exc), trace_id=trace_id)
        raise PathRefusalError(
            "governance_unavailable",
            f"The path rules could not be loaded, so {tool_name!r} refuses {str(resolved)!r}: "
            f"{exc}",
            retryable=False,
            next_action="No path can be used until the path rules load. Tell the user.",
        ) from exc

    policy = governance.tools.get(tool_name)
    if policy is None:
        log.warning("path_governance_no_policy", tool=tool_name, trace_id=trace_id)
        raise PathRefusalError(
            "no_path_policy",
            f"No path policy exists for {tool_name!r}, so {str(resolved)!r} is refused.",
            retryable=False,
            next_action="No path can be used until a path policy exists. Tell the user.",
        )

    path_str = str(resolved)

    if policy.forbidden_paths and _matches_any(path_str, policy.forbidden_paths):
        raise PathRefusalError(
            "forbidden_path",
            f"Path {path_str!r} is in the forbidden_paths list for tool '{tool_name}'",
            retryable=False,
            next_action=_PATH_NEXT,
        )

    if policy.allowed_paths and not _matches_any(path_str, policy.allowed_paths):
        raise PathRefusalError(
            "path_not_allowed",
            f"Path {path_str!r} is not in the allowed_paths list for tool '{tool_name}'",
            retryable=False,
            next_action=_PATH_NEXT,
        )
