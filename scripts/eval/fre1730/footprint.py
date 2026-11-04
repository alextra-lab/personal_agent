"""FRE-1730 AC-4: list a session's ``~`` before and after a run, and report each file that changed."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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

    def unreadable(error: OSError) -> None:
        listing[f"{Path(str(error.filename)).relative_to(home)}/"] = f"unreadable:{error.errno}"

    for folder, folders, names in os.walk(home, onerror=unreadable):
        for name in folders:
            path = Path(folder, name)
            if path.is_symlink():
                listing[f"{path.relative_to(home)}/"] = f"link:{os.readlink(path)}"
        if not folders and not names and Path(folder) != home:
            listing[f"{Path(folder).relative_to(home)}/"] = "empty directory"
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
        digests: The final digest of each added or changed path.
    """

    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]
    digests: Mapping[str, str] = field(default_factory=dict)

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
    added = tuple(sorted(after.keys() - before.keys()))
    changed = tuple(sorted(p for p in after.keys() & before.keys() if after[p] != before[p]))
    return Footprint(
        added=added,
        changed=changed,
        removed=tuple(sorted(before.keys() - after.keys())),
        digests={path: after[path] for path in (*added, *changed)},
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


def plugin_changed_content(
    plugin: Iterable[Mapping[str, str]], control: Iterable[Mapping[str, str]]
) -> tuple[str, ...]:
    """Name the files that both arms wrote, where the plugin arm wrote other content.

    A plugin can write into a file that Claude Code also creates, such as ``.bashrc``. A file counts as stable only if
    at least two control sessions wrote the same content. So one control session names nothing, and a file whose content
    varies with no plugin is not listed. Pass the sessions of one task, because content can differ per task.

    Args:
        plugin: The digests of each plugin arm session.
        control: The digests of each control arm session.

    Returns:
        The paths.
    """
    seen: dict[str, list[str]] = {}
    for session in control:
        for path, digest in session.items():
            seen.setdefault(_VOLATILE.sub("*", path), []).append(digest)
    found = set()
    for session in plugin:
        for path, digest in session.items():
            digests = seen.get(_VOLATILE.sub("*", path), [])
            if len(digests) >= 2 and len(set(digests)) == 1 and digest != digests[0]:
                found.add(path)
    return tuple(sorted(found))
