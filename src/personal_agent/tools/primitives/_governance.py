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


def _check_path_governance(
    resolved: Path,
    tool_name: str,
) -> dict[str, Any] | None:
    """Validate *resolved* against allowed_paths / forbidden_paths for *tool_name*.

    Args:
        resolved: Fully resolved absolute path.
        tool_name: Key to look up in ``governance_config.tools``.

    Returns:
        An error dict (with ``success=False``) when the path is rejected,
        or ``None`` when the path is permitted.
    """
    try:
        governance = load_governance_config()
    except Exception as exc:  # noqa: BLE001 — surface as tool error
        log.warning("governance_load_error", tool=tool_name, error=str(exc))
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
