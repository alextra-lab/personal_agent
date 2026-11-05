"""The ``bash`` child's account, and the paths that it owns (FRE-1732).

On a deployed gateway the ``bash`` child runs as ``nobody``, so that it cannot read the gateway's
credentials. Project work lives in ``/app/agent_workspace``, and scratch work in ``/tmp``. Root-owned
files there would stop the child from changing them. So on the ``cloud`` profile the child's account
owns the workspace tree from startup, and each file and directory that a root tool creates in either
place. A root tool gives away only what its own call created, never a file that was already there.

The ``eval`` profile keeps its own behaviour (FRE-1505), and ``local`` has no account to change.
"""

import os
import stat
from collections.abc import Sequence
from pathlib import Path

import structlog

from personal_agent.config import settings
from personal_agent.tools.primitives._governance import open_no_symlinks

log = structlog.get_logger(__name__)

# 65534 is ``nobody``/``nogroup`` in the Debian-based gateway image.
CHILD_UID = 65534
CHILD_GID = 65534
CHILD_IDS = (CHILD_UID, CHILD_GID)
WORKSPACE_ROOT = Path("/app/agent_workspace")
# The ``run_python`` scratch root, inside the workspace. Root owns it, so the child does not own the
# parent of the trace directories (FRE-1736). The child still owns the workspace itself, so this
# does not stop it from renaming ``sandbox``. It only removes the child's direct ownership.
SANDBOX_DIRECTORY = "sandbox"
ROOT_IDS = (0, 0)
# A root itself keeps its owner: ``/tmp`` stays root's. ``prepare_workspace`` gives the workspace.
CHILD_ROOTS: tuple[Path, ...] = (WORKSPACE_ROOT, Path("/tmp"))


def _child_owns_workspace() -> bool:
    return settings.deployment_profile == "cloud" and os.geteuid() == 0


def _names_another_file(status: os.stat_result) -> bool:
    """Report whether a regular file has a second name, which can be a hard link from elsewhere."""
    return stat.S_ISREG(status.st_mode) and status.st_nlink > 1


def prepare_workspace(root: Path = WORKSPACE_ROOT) -> None:
    """Give the workspace tree to the child's account, at gateway startup.

    No child runs at that time. A symbolic link is changed itself and is not followed. An entry
    that the child owns already, or a file with a second name, stays as it is. The ``sandbox``
    directory goes to root instead, and the walk does not enter it. A missing ``sandbox``
    directory is created for root (FRE-1736).

    Args:
        root: The workspace directory.
    """
    if not _child_owns_workspace() or not root.is_dir():
        return
    changed = 0
    failed = 0
    second_names = 0

    def give(entry: Path, ids: tuple[int, int] = CHILD_IDS) -> None:
        nonlocal changed, failed, second_names
        try:
            status = os.lstat(entry)
            if _names_another_file(status):
                second_names += 1
                return
            if (status.st_uid, status.st_gid) == ids:
                return
            os.chown(entry, *ids, follow_symlinks=False)
            changed += 1
        except OSError:
            failed += 1  # for example, a file that a run removed in this walk

    sandbox = root / SANDBOX_DIRECTORY
    give(root)
    for directory, subdirs, files in os.walk(root, followlinks=False):
        if Path(directory) == sandbox:
            subdirs.clear()  # the trace directories are not the child's to own
            continue
        for name in (*subdirs, *files):
            entry = Path(directory, name)
            give(entry, ROOT_IDS if entry == sandbox else CHILD_IDS)
    if not os.path.lexists(sandbox):
        # A fresh volume has none. Created later by a child's first write, it would be the child's.
        try:
            sandbox.mkdir(mode=0o755)
        except OSError:
            failed += 1
    log.info("child_workspace_prepared", changed=changed, failed=failed, second_names=second_names)


def path_for_root_open(path: Path) -> Path:
    """Return a configured path in the form that the no-link open needs.

    On ``local``, a system path can hold a link (``/tmp`` on macOS), and no child owns anything,
    so the path resolves. On a deployed gateway it stays as it is: the child can plant a link in
    it, and that link must refuse.

    Args:
        path: The configured absolute path.

    Returns:
        The path to open.
    """
    return path.resolve() if settings.deployment_profile == "local" else path


def missing_parts(path: Path) -> list[Path]:
    """Return the parts of a path that do not exist yet, from the top down.

    A root tool calls this before it creates ``path``, and gives only these to the child.

    Args:
        path: The resolved path.

    Returns:
        The missing parts, ``path`` last. Empty when ``path`` exists.
    """
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current) and current != current.parent:
        missing.insert(0, current)
        current = current.parent
    return missing


def give_to_child(
    paths: Sequence[Path], *, trace_id: str | None, roots: Sequence[Path] = CHILD_ROOTS
) -> None:
    """Give the paths that a root tool just created to the child's account.

    The child controls these trees, so each path opens with no symbolic link, and the change goes
    through that descriptor. A planted link cannot give away another file, and a file with a
    second name may be a hard link to a file elsewhere, so it stays as it is. A failure does not
    undo the tool's work, so it only logs.

    Args:
        paths: The resolved paths, from :func:`missing_parts`. A root, or a path outside the
            roots, is left as it is.
        trace_id: The request's trace id.
        roots: The directories whose contents the child can own.
    """
    if not _child_owns_workspace():
        return
    for path in paths:
        if path in roots or not any(path.is_relative_to(root) for root in roots):
            continue
        try:
            fd = open_no_symlinks(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                if not _names_another_file(os.fstat(fd)):
                    os.fchown(fd, CHILD_UID, CHILD_GID)
            finally:
                os.close(fd)
        except FileNotFoundError:
            continue  # the tool failed before it created this part
        except OSError as exc:
            log.warning(
                "child_workspace_give_failed",
                path=str(path),
                error=type(exc).__name__,
                trace_id=trace_id,
            )
