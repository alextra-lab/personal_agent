"""The read-only file grant of a ``general`` worker task (ADR-0156 D9.4, FRE-1580).

The ``general`` worker may ``read`` the files that its task names in the structured
``read_paths`` field, and no other file. It may not write them: it holds no write tool. The
worker holds private reads and no outbound tool, so the FRE-1564 split holds.

The rules, from the ADR:

- Paths in the task text grant nothing. Only ``read_paths`` does.
- A directory grants nothing, and a glob grants nothing. Each entry names one regular file.
- At grant time (the task start) each path is resolved to its real path, and the file's
  device and inode numbers are recorded. A path that does not resolve to a regular file is
  dropped, with a log event.
- At read time two checks must both pass. The real path of the requested path must equal a
  recorded real path (this refuses a hard link at another path, and a ``../`` or symlink path
  to another file). Then the opened file's device and inode must equal the recorded ones (this
  refuses a file replaced after the grant, by a rename or by a symlink).
- The read tool's path rules still apply. The grant never widens them.
- The grant ends with the task.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

#: At most this many ``read_paths`` entries are considered for one task.
MAX_GRANTED_FILES = 10

GrantRefusalCode = Literal["not_granted", "file_replaced", "grant_ended"]


class ReadGrantRefused(Exception):
    """A worker read that the task's grant does not cover.

    Attributes:
        code: Why the read is refused.
        message: The model-facing reason.
    """

    def __init__(self, code: GrantRefusalCode, message: str) -> None:
        """Store the refusal code and message.

        Args:
            code: Why the read is refused.
            message: The model-facing reason.
        """
        super().__init__(message)
        self.code: GrantRefusalCode = code
        self.message = message


@dataclass(frozen=True)
class GrantedFile:
    """One file a task may read, as recorded at grant time.

    Attributes:
        real_path: The fully resolved path.
        device: The file's device number.
        inode: The file's inode number.
    """

    real_path: str
    device: int
    inode: int


class ReadGrant:
    """The files one worker task may read. It lives exactly as long as the task.

    Build it with :meth:`for_task` at the task start, and :meth:`close` it when the task ends.
    """

    def __init__(self, files: tuple[GrantedFile, ...]) -> None:
        """Hold the granted files.

        Args:
            files: The files, as recorded at grant time.
        """
        self._files = files
        self._closed = False

    @staticmethod
    def for_task(paths: Sequence[object], *, trace_id: str) -> ReadGrant:
        """Record the regular files that a task's ``read_paths`` names.

        Args:
            paths: The task's ``read_paths`` entries. Only absolute path strings count.
            trace_id: Request trace identifier, for the drop events.

        Returns:
            The grant. It holds no file when no entry names a regular file.
        """
        files: list[GrantedFile] = []
        for raw in list(paths)[:MAX_GRANTED_FILES]:
            reason = _drop_reason(raw)
            entry = None if reason else _record(str(raw))
            if entry is None:
                log.warning(
                    "worker_read_grant_path_dropped",
                    path=str(raw),
                    reason=reason or "not_a_regular_file",
                    trace_id=trace_id,
                )
                continue
            if entry not in files:
                files.append(entry)
        if len(paths) > MAX_GRANTED_FILES:
            log.warning(
                "worker_read_grant_paths_capped",
                requested=len(paths),
                kept=MAX_GRANTED_FILES,
                trace_id=trace_id,
            )
        log.info(
            "worker_read_grant_made",
            files=[f.real_path for f in files],
            trace_id=trace_id,
        )
        return ReadGrant(tuple(files))

    @property
    def files(self) -> tuple[GrantedFile, ...]:
        """The granted files."""
        return self._files

    @property
    def closed(self) -> bool:
        """Whether the task ended."""
        return self._closed

    def close(self) -> None:
        """End the grant. Every later read is refused."""
        self._closed = True

    def check_path(self, real_path: Path) -> GrantedFile:
        """The first read-time check: the requested real path is a granted real path.

        Args:
            real_path: The fully resolved path that the read asks for.

        Returns:
            The granted entry for that path.

        Raises:
            ReadGrantRefused: The grant ended, or the path is not a granted file.
        """
        if self._closed:
            raise ReadGrantRefused("grant_ended", "The task that named this file has ended.")
        for entry in self._files:
            if entry.real_path == str(real_path):
                return entry
        named = ", ".join(f.real_path for f in self._files) or "none"
        raise ReadGrantRefused(
            "not_granted",
            f"{str(real_path)!r} is not a file that this task names. Files it names: {named}.",
        )

    def check_opened(self, entry: GrantedFile, st: os.stat_result) -> None:
        """The second read-time check: the opened file is the file recorded at grant time.

        Args:
            entry: The entry that :meth:`check_path` returned.
            st: ``os.fstat`` of the opened file.

        Raises:
            ReadGrantRefused: The opened file is not a regular file, or its device or inode
                differs from the recorded one.
        """
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (entry.device, entry.inode):
            raise ReadGrantRefused(
                "file_replaced",
                f"{entry.real_path!r} was replaced after the task named it.",
            )


def _drop_reason(raw: object) -> str | None:
    """Return why an entry cannot name a file, or ``None`` when it is an absolute path string."""
    if not isinstance(raw, str) or not raw:
        return "not_a_path_string"
    if "\x00" in raw:
        return "nul_byte"
    if not os.path.isabs(raw):
        return "not_absolute"
    return None


def _record(path: str) -> GrantedFile | None:
    """Resolve one absolute path and record it, or return ``None`` when it is no regular file."""
    try:
        real = Path(path).resolve(strict=True)
        info = os.lstat(real)
    except (OSError, RuntimeError, ValueError):
        # A planner value must grant nothing, never raise (ValueError: an unusable path).
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return GrantedFile(real_path=str(real), device=info.st_dev, inode=info.st_ino)
