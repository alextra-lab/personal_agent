"""Primitive write tool — supersedes ``write_file``.

Writes content to a file (overwrite or append mode) with explicit path-governance
checks (allowed_paths / forbidden_paths / unattended_paths from
``config/governance/tools.yaml``).

FRE-261 Step 3.
"""

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import structlog

from personal_agent.config import load_governance_config
from personal_agent.telemetry import TraceContext
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

write_tool = ToolDefinition(
    name="write",
    description=(
        "Write content to a file. "
        "Mode 'overwrite' replaces the file; 'append' adds to the end."
    ),
    category="system_write",
    parameters=[
        ToolParameter(
            name="path",
            type="string",
            description="Absolute or home-relative path to write",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="content",
            type="string",
            description="Text content to write",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="mode",
            type="string",
            description="'overwrite' (default) or 'append'",
            required=False,
            default="overwrite",
            json_schema=None,
        ),
    ],
    risk_level="high",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "RECOVERY"],
    requires_approval=False,  # approval is via governance YAML + executor.py
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=None,
)


# ---------------------------------------------------------------------------
# Path-governance helpers
# ---------------------------------------------------------------------------


def _expand(path: str) -> str:
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
    return any(fnmatch(path, _expand(p)) for p in patterns)


def _check_path_governance(
    resolved: Path,
    tool_name: str = "write",
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
        log.warning("write_governance_load_error", error=str(exc))
        return None  # fail open

    policy = governance.tools.get(tool_name)
    if policy is None:
        return None

    path_str = str(resolved)

    if policy.forbidden_paths and _matches_any(path_str, policy.forbidden_paths):
        return {
            "success": False,
            "error": "forbidden_path",
            "path": path_str,
            "detail": (
                f"Path {path_str!r} is in the forbidden_paths list for tool '{tool_name}'"
            ),
        }

    if policy.allowed_paths and not _matches_any(path_str, policy.allowed_paths):
        return {
            "success": False,
            "error": "path_not_allowed",
            "path": path_str,
            "detail": (
                f"Path {path_str!r} is not in the allowed_paths list for tool '{tool_name}'"
            ),
        }

    return None


def _is_unattended_path(resolved: Path, tool_name: str = "write") -> bool:
    """Check whether *resolved* is under any ``unattended_paths`` for *tool_name*.

    Scratch/unattended paths (e.g. ``/tmp/**``) allow ``write`` to proceed
    without an additional approval flag in NORMAL mode.

    Args:
        resolved: Fully resolved absolute path.
        tool_name: Key to look up in ``governance_config.tools``.

    Returns:
        True when the path is within an unattended scratch area.
    """
    try:
        governance = load_governance_config()
    except Exception as exc:  # noqa: BLE001
        log.warning("write_unattended_check_error", error=str(exc))
        return False

    policy = governance.tools.get(tool_name)
    if policy is None or not policy.unattended_paths:
        return False

    path_str = str(resolved)
    return _matches_any(path_str, policy.unattended_paths)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def write_executor(
    path: str,
    content: str,
    mode: str = "overwrite",
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Execute the ``write`` primitive tool.

    Writes *content* to the file at *path*, after applying path-governance
    checks from ``config/governance/tools.yaml``.  Parent directories are
    created automatically.

    Args:
        path: Absolute or home-relative path to write.
        content: Text content to write.
        mode: ``'overwrite'`` (default) replaces the file; ``'append'`` adds
            to the end.
        ctx: Optional trace context for structured logging correlation.

    Returns:
        On success::

            {
                "success": True,
                "path": str,
                "bytes_written": int,
                "mode": str,
            }

        When the path is outside all ``unattended_paths`` an extra advisory
        key is included::

            {"requires_approval_in_normal_mode": True, ...}

        On failure::

            {"success": False, "error": "<error_code>", "path": str, ...}

        Possible ``error`` values:

        * ``"invalid_mode"`` — *mode* is not ``'overwrite'`` or ``'append'``
        * ``"forbidden_path"`` — path matched a ``forbidden_paths`` entry
        * ``"path_not_allowed"`` — path not in ``allowed_paths``
        * ``"permission_denied"`` — OS permission error
        * ``"io_error"`` — other I/O error
    """
    trace_id = ctx.trace_id if ctx else "n/a"

    # 1. Validate mode
    if mode not in ("overwrite", "append"):
        return {
            "success": False,
            "error": "invalid_mode",
            "path": path,
            "detail": f"mode must be 'overwrite' or 'append', got {mode!r}",
        }

    # 2. Resolve path
    resolved = Path(_expand(path)).expanduser().resolve()
    log.debug("write_executor_called", path=path, resolved=str(resolved), mode=mode, trace_id=trace_id)

    # 3. Path governance
    governance_error = _check_path_governance(resolved, tool_name="write")
    if governance_error is not None:
        log.warning(
            "write_path_rejected",
            reason=governance_error.get("error"),
            path=str(resolved),
            trace_id=trace_id,
        )
        return governance_error

    # 4. Scratch-dir / unattended detection
    unattended = _is_unattended_path(resolved, tool_name="write")

    # 5. Create parent directories
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        return {
            "success": False,
            "error": "permission_denied",
            "path": str(resolved),
            "detail": str(exc),
        }
    except OSError as exc:
        return {
            "success": False,
            "error": "io_error",
            "path": str(resolved),
            "detail": str(exc),
        }

    # 6. Write
    try:
        if mode == "overwrite":
            resolved.write_text(content, encoding="utf-8")
        else:  # append
            with open(resolved, "a", encoding="utf-8") as fh:
                fh.write(content)
    except PermissionError as exc:
        log.warning(
            "write_permission_denied", path=str(resolved), error=str(exc), trace_id=trace_id
        )
        return {
            "success": False,
            "error": "permission_denied",
            "path": str(resolved),
            "detail": str(exc),
        }
    except OSError as exc:
        log.error("write_io_error", path=str(resolved), error=str(exc), trace_id=trace_id)
        return {
            "success": False,
            "error": "io_error",
            "path": str(resolved),
            "detail": str(exc),
        }

    bytes_written = len(content.encode("utf-8"))
    log.info(
        "write_executor_success",
        path=str(resolved),
        bytes_written=bytes_written,
        mode=mode,
        unattended=unattended,
        trace_id=trace_id,
    )

    result: dict[str, Any] = {
        "success": True,
        "path": str(resolved),
        "bytes_written": bytes_written,
        "mode": mode,
    }

    if not unattended:
        result["requires_approval_in_normal_mode"] = True

    return result
