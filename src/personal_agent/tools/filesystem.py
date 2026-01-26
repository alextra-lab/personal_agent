"""Filesystem tools for reading and manipulating files.

This module provides tools for filesystem operations like reading files,
listing directories, and writing files (future).
"""

import os
from pathlib import Path
from typing import Any

from personal_agent.tools.types import ToolDefinition, ToolParameter


def read_file_executor(path: str, max_size_mb: int = 10) -> dict[str, Any]:
    """Execute read_file tool.

    Reads the contents of a file at the given path, with size limit enforcement.

    Args:
        path: Absolute or relative file path.
        max_size_mb: Maximum file size in MB (default: 10).

    Returns:
        Dictionary with:
        - success: bool
        - content: str (file contents) or None if error
        - size_bytes: int (file size) or None if error
        - error: str or None
    """
    try:
        # Resolve path (expand both ~ and $HOME environment variables)
        expanded_path = os.path.expandvars(os.path.expanduser(path))
        file_path = Path(expanded_path).resolve()

        # Check if file exists
        if not file_path.exists():
            return {
                "success": False,
                "content": None,
                "size_bytes": None,
                "error": f"File not found: {path}",
            }

        # Check if it's a file (not directory)
        if not file_path.is_file():
            return {
                "success": False,
                "content": None,
                "size_bytes": None,
                "error": f"Path is not a file: {path}",
            }

        # Check file size
        file_size_bytes = file_path.stat().st_size
        max_size_bytes = max_size_mb * 1024 * 1024

        if file_size_bytes > max_size_bytes:
            return {
                "success": False,
                "content": None,
                "size_bytes": file_size_bytes,
                "error": f"File size {file_size_bytes} bytes exceeds limit {max_size_bytes} bytes ({max_size_mb} MB)",
            }

        # Read file
        content = file_path.read_text(encoding="utf-8", errors="replace")

        return {
            "success": True,
            "content": content,
            "size_bytes": file_size_bytes,
            "error": None,
        }

    except PermissionError as e:
        return {
            "success": False,
            "content": None,
            "size_bytes": None,
            "error": f"Permission denied: {e}",
        }
    except Exception as e:
        return {
            "success": False,
            "content": None,
            "size_bytes": None,
            "error": f"Error reading file: {e}",
        }


read_file_tool = ToolDefinition(
    name="read_file",
    description="Read contents of a file at the given path",
    category="read_only",
    parameters=[
        ToolParameter(
            name="path",
            type="string",
            description="Absolute or relative file path",
            required=True,
            default=None,
        ),
        ToolParameter(
            name="max_size_mb",
            type="number",
            description="Maximum file size in MB (default: 10)",
            required=False,
            default=10,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=None,
)


def list_directory_executor(
    path: str,
    *,
    include_hidden: bool = True,
    include_details: bool = True,
    files_only: bool = False,
    directories_only: bool = False,
    max_entries: int | None = None,
) -> dict[str, Any]:
    """Execute list_directory tool.

    Lists the contents of a directory at the given path.

    Args:
        path: Absolute or relative directory path.
        include_hidden: Whether to include hidden entries (names starting with ".").
        include_details: Whether to include expensive/large fields (absolute path, size_bytes).
        files_only: If true, return only file entries.
        directories_only: If true, return only directory entries.
        max_entries: Optional cap on number of entries returned (useful for huge directories).

    Returns:
        Dictionary with:
        - success: bool
        - entries: list[dict] (directory entries) or None if error
        - entry_count: int (number of entries) or None if error
        - error: str or None
    """
    try:
        # Resolve path (expand both ~ and $HOME environment variables)
        expanded_path = os.path.expandvars(os.path.expanduser(path))
        dir_path = Path(expanded_path).resolve()

        # Check if path exists
        if not dir_path.exists():
            return {
                "success": False,
                "entries": None,
                "entry_count": None,
                "error": f"Directory not found: {path}",
            }

        # Check if it's a directory
        if not dir_path.is_dir():
            return {
                "success": False,
                "entries": None,
                "entry_count": None,
                "error": f"Path is not a directory: {path}",
            }

        # List directory contents
        entries: list[dict[str, Any]] = []
        returned = 0
        for item in sorted(dir_path.iterdir()):
            name = item.name
            if not include_hidden and name.startswith("."):
                continue

            is_dir = item.is_dir()
            if files_only and is_dir:
                continue
            if directories_only and not is_dir:
                continue

            entry_info: dict[str, Any] = {
                "name": name,
                "type": "directory" if is_dir else "file",
            }

            if include_details:
                entry_info["path"] = str(item)
                if not is_dir:
                    try:
                        entry_info["size_bytes"] = item.stat().st_size
                    except (OSError, PermissionError):
                        entry_info["size_bytes"] = None

            entries.append(entry_info)
            returned += 1
            if max_entries is not None and returned >= max_entries:
                break

        return {
            "success": True,
            "entries": entries,
            "entry_count": len(entries),
            "error": None,
        }

    except PermissionError as e:
        return {
            "success": False,
            "entries": None,
            "entry_count": None,
            "error": f"Permission denied: {e}",
        }
    except Exception as e:
        return {
            "success": False,
            "entries": None,
            "entry_count": None,
            "error": f"Error listing directory: {e}",
        }


list_directory_tool = ToolDefinition(
    name="list_directory",
    description="List contents of a directory. Returns list of files and subdirectories with their types and sizes.",
    category="read_only",
    parameters=[
        ToolParameter(
            name="path",
            type="string",
            description="Directory path to list (NOT 'directory'). Use 'path' parameter. Example: path=\"/tmp\"",
            required=True,
            default=None,
        ),
        ToolParameter(
            name="include_hidden",
            type="boolean",
            description="Include hidden entries (names starting with '.'). Default: true",
            required=False,
            default=True,
        ),
        ToolParameter(
            name="include_details",
            type="boolean",
            description="Include extra fields like absolute path and size_bytes. Default: true",
            required=False,
            default=True,
        ),
        ToolParameter(
            name="files_only",
            type="boolean",
            description="If true, return only file entries. Default: false",
            required=False,
            default=False,
        ),
        ToolParameter(
            name="directories_only",
            type="boolean",
            description="If true, return only directory entries. Default: false",
            required=False,
            default=False,
        ),
        ToolParameter(
            name="max_entries",
            type="number",
            description="Optional cap on returned entries (useful for huge directories). Default: null (no cap)",
            required=False,
            default=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=None,
)
