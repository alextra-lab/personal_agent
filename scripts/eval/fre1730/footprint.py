"""FRE-1730 AC-4: list a session's ``~`` before and after a run, and report each file that changed."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: A file name that holds a session id or a time differs between any two sessions.
_VOLATILE = re.compile(r"[0-9a-f]{8}-[0-9a-f-]{27}|\d{4}-\d{2}-\d{2}T[0-9_:.]+Z|\d{8,}")


def snapshot(home: Path) -> dict[str, str]:
    """List every file under a directory, with a digest. A link counts by its target and is not followed.

    A session owns this directory, so a file that the host cannot read is a listed fact and not an error.

    Args:
        home: The directory.

    Returns:
        The digest by relative path.
    """
    if home.is_symlink():
        return {".": f"link:{os.readlink(home)}"}
    listing: dict[str, str] = {}
    for folder, _, names in os.walk(home):
        for name in names:
            path = Path(folder, name)
            relative = str(path.relative_to(home))
            if path.is_symlink():
                listing[relative] = f"link:{os.readlink(path)}"
            elif path.is_file():
                try:
                    listing[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError as error:
                    listing[relative] = f"unreadable:{error.errno}"
    return listing


@dataclass(frozen=True)
class Footprint:
    """What changed between two snapshots.

    Attributes:
        added: Paths that are new.
        changed: Paths whose content differs.
        removed: Paths that are gone.
    """

    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]

    def lines(self) -> list[str]:
        """Format the listing.

        Returns:
            One line for each file that changed.
        """
        return [
            *(f"added    {path}" for path in self.added),
            *(f"changed  {path}" for path in self.changed),
            *(f"removed  {path}" for path in self.removed),
        ]

    def paths(self) -> tuple[str, ...]:
        """Return each path that changed.

        Returns:
            The paths.
        """
        return (*self.added, *self.changed, *self.removed)


def compare(before: Mapping[str, str], after: Mapping[str, str]) -> Footprint:
    """Compare two snapshots.

    Args:
        before: The first snapshot.
        after: The second snapshot.

    Returns:
        The difference.
    """
    return Footprint(
        added=tuple(sorted(after.keys() - before.keys())),
        changed=tuple(sorted(p for p in after.keys() & before.keys() if after[p] != before[p])),
        removed=tuple(sorted(before.keys() - after.keys())),
    )


def plugin_only(plugin: Iterable[str], control: Iterable[str]) -> tuple[str, ...]:
    """Name the files that only the plugin arm touched. Claude Code itself writes the others.

    Args:
        plugin: Paths that changed in the plugin arm.
        control: Paths that changed in the control arm.

    Returns:
        The paths that are only in the plugin arm.
    """
    seen = {_VOLATILE.sub("*", path) for path in control}
    return tuple(sorted(path for path in plugin if _VOLATILE.sub("*", path) not in seen))
