"""A separate workspace for each sub-agent worker (FRE-1565, FRE-1517 stage 3 F9).

Before this module every ``write`` and ``bash`` call shared ``/app/agent_workspace``. A
worker could build on files another session wrote (F9), and two workers of one fan-out
could overwrite each other's files. Now each worker gets its own empty directory,
``settings.worker_workspace_root / <trace_id> / <task_id>``:

- ``write`` puts a relative path inside it and refuses a path that resolves outside it.
- ``bash`` runs with it as the working directory and ``HOME``.

The workspace is a working directory, not a security boundary. Every worker shell runs
as the same unprivileged account (uid 65534), so a ``bash`` command can still name a
sibling's directory. The control on ``bash`` is the owner's per-call approval, which
shows the exact command (ADR-0150 D2 amendment, FRE-1565).

How the workspace reaches the tool. ``dispatch_tool_call`` and the tool executors take
request primitives, not a worker spec, so the path crosses that boundary through a
``ContextVar`` — the pattern ``sub_agent_approval.py`` uses for the approval broker.
``run_sub_agent`` sets it before its tool loop and resets it in its ``finally``. The
primary never sets it, so a ``None`` here means "not a worker" and every tool keeps its
primary behaviour.
"""

from __future__ import annotations

import contextvars
import os
import re
from pathlib import Path

from personal_agent.config import settings

#: The unprivileged account a worker's shell runs as. It must own the workspace, or the
#: shell cannot write in its own working directory. Equal to ``bash.EVAL_CHILD_UID``.
WORKER_CHILD_UID = 65534
WORKER_CHILD_GID = 65534

_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]")

_worker_workspace: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "worker_workspace", default=None
)


def _component(value: str) -> str:
    """Make one path component safe: no separator, no traversal, never empty.

    Args:
        value: A trace or task identifier.

    Returns:
        The identifier with every character outside ``[A-Za-z0-9_.-]`` replaced by ``_``,
        and a leading dot replaced, so ``..`` cannot climb out of the root.
    """
    cleaned = _UNSAFE_COMPONENT.sub("_", value) or "_"
    return "_" + cleaned[1:] if cleaned.startswith(".") else cleaned


def worker_workspace_path(trace_id: str, task_id: str) -> Path:
    """Return the workspace directory of one worker. It is not created here.

    Args:
        trace_id: The turn's trace identifier.
        task_id: The worker's own task identifier, unique per worker.

    Returns:
        ``settings.worker_workspace_root / trace_id / task_id``.
    """
    return Path(settings.worker_workspace_root) / _component(trace_id) / _component(task_id)


def set_worker_workspace(path: Path | None) -> contextvars.Token[Path | None]:
    """Publish this worker's workspace for the current async context.

    Args:
        path: The worker's workspace directory, or ``None``.

    Returns:
        A token for :func:`reset_worker_workspace`.
    """
    return _worker_workspace.set(path)


def get_worker_workspace() -> Path | None:
    """Return the current worker's workspace, or ``None`` outside a worker.

    Returns:
        The path set by ``run_sub_agent``, or ``None`` for the primary.
    """
    return _worker_workspace.get()


def reset_worker_workspace(token: contextvars.Token[Path | None]) -> None:
    """Restore the workspace carrier to its prior value.

    Args:
        token: The token returned by :func:`set_worker_workspace`.
    """
    _worker_workspace.reset(token)


def _give_to_worker(path: Path) -> None:
    """Give one path to the worker account when this process is root.

    Args:
        path: A directory or file inside a worker workspace.
    """
    if os.geteuid() == 0:
        os.chown(path, WORKER_CHILD_UID, WORKER_CHILD_GID)


def ensure_worker_workspace() -> Path | None:
    """Create the current worker's workspace on first use.

    Only a worker that writes a file or runs a shell command gets a directory. As root,
    the directory goes to the worker account, so the unprivileged shell can write in it.

    Returns:
        The existing workspace directory, or ``None`` outside a worker.

    Raises:
        OSError: When the directory cannot be created.
    """
    workspace = get_worker_workspace()
    if workspace is None:
        return None
    if not workspace.is_dir():
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        _give_to_worker(workspace)
    return workspace


def confine_to_workspace(path: str, workspace: Path) -> Path | None:
    """Resolve a requested path inside a worker workspace, or refuse it.

    A relative path is joined to the workspace. The result is resolved (``..`` and
    symlinks followed) and kept only when it stays inside the workspace.

    Args:
        path: The path the worker asked for.
        workspace: The worker's workspace directory.

    Returns:
        The resolved absolute path, or ``None`` when it resolves outside the workspace.
    """
    requested = Path(path).expanduser()
    candidate = requested if requested.is_absolute() else workspace / requested
    resolved = candidate.resolve()
    root = workspace.resolve()
    if resolved != root and root in resolved.parents:
        return resolved
    return None


def give_written_path_to_worker(written: Path, workspace: Path) -> None:
    """Give a file written by the gateway, and its new parent directories, to the worker.

    ``write`` runs as the gateway (root). Without this a worker's shell could read the
    file but not change it.

    Args:
        written: The resolved path just written.
        workspace: The worker's workspace directory.
    """
    root = workspace.resolve()
    if root not in written.parents:
        return
    for item in (written, *written.parents):
        if item == root:
            return
        _give_to_worker(item)
