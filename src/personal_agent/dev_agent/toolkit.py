"""The pinned toolkit volume of the development agent (ADR-0159 D3.3, D7.3; FRE-1751).

The agent container and the check container mount one Docker volume, read-only, at
``/srv/artifact-lib``. It holds the assets of ``config/artifact_lib_manifest.json`` at their pinned
paths. This module builds that volume and checks it.

The check trusts bytes only. It recomputes the SHA-384 of every file against the SRI pin of
``config/artifact_lib_substitution_map.json``, and it refuses a missing, changed or extra file. The
fill command runs the check on a read-back of the new volume. The launcher runs it before every job,
so a half-copied or changed volume never reaches an agent. No container starts: the module reads and
writes the volume through ``docker create`` and ``docker cp`` on a container that never runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import structlog

from personal_agent.exceptions import DevAgentToolkitError
from personal_agent.observability.artifact_envelope.spec import load_lib_manifest
from personal_agent.storage.artifact_export import (
    ArtifactExportError,
    load_substitution_map,
    verify_sri,
)

if TYPE_CHECKING:
    from personal_agent.dev_agent.launcher import CommandRunner

log = structlog.get_logger(__name__)

#: Where the volume mounts in the agent and check containers (D3.3). Not ``/lib``: on the base image
#: ``/lib`` is a symlink to ``usr/lib``.
TOOLKIT_MOUNT = "/srv/artifact-lib"
#: The file in the volume that records the digest and the pins (D7.5 reads the digest from it).
MANIFEST_FILE = "toolkit-manifest.json"
#: The only SRI algorithm that the pins use.
_SRI_ALGORITHM = "sha384"

#: Fetches the bytes of one asset by its manifest path (for example from R2).
FetchAsset = Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True)
class ToolkitAsset:
    """One pinned file of the toolkit.

    Attributes:
        path: The path under ``/lib/``, for example ``chartjs@4.4.7/chart.umd.js``.
        sri: The pinned SRI, ``sha384-<base64>``.
    """

    path: str
    sri: str


def _is_safe_path(path: str) -> bool:
    """Return whether ``path`` stays inside the volume: relative, no ``..``, not the manifest file."""
    pure = PurePosixPath(path)
    return (
        bool(path)
        and not pure.is_absolute()
        and ".." not in pure.parts
        and path != MANIFEST_FILE
        and "\\" not in path
    )


def load_toolkit_assets(
    manifest_path: Path | None = None, map_path: Path | None = None
) -> tuple[ToolkitAsset, ...]:
    """Return every asset of the manifest with its pinned SRI.

    Args:
        manifest_path: The ``/lib/`` manifest. Defaults to the committed one.
        map_path: The substitution map that holds the SRI pins. Defaults to the committed one.

    Returns:
        The assets, in manifest order. The ``eval_gated`` asset is included.

    Raises:
        DevAgentToolkitError: If a path is unsafe, an asset has no pinned SRI, or a pin does not
            use SHA-384.
    """
    try:
        _, manifest_assets = (
            load_lib_manifest(manifest_path) if manifest_path else load_lib_manifest()
        )
        pins = load_substitution_map(map_path).by_lib_path
    except (ArtifactExportError, ValueError, OSError) as exc:
        raise DevAgentToolkitError(f"cannot load the toolkit config: {exc}") from exc
    assets: list[ToolkitAsset] = []
    for entry in manifest_assets:
        if not _is_safe_path(entry.path):
            raise DevAgentToolkitError(f"unsafe toolkit path: {entry.path!r}")
        pin = pins.get(f"lib/{entry.path}")
        if pin is None:
            raise DevAgentToolkitError(f"no pinned SRI for {entry.path}")
        if not pin.sri.startswith(f"{_SRI_ALGORITHM}-"):
            raise DevAgentToolkitError(f"the pin of {entry.path} is not {_SRI_ALGORITHM}")
        assets.append(ToolkitAsset(entry.path, pin.sri))
    return tuple(assets)


def toolkit_digest(assets: Sequence[ToolkitAsset]) -> str:
    """Return the digest of the pins.

    Each SRI binds the bytes of its file, so the digest binds the content of the toolkit.

    Args:
        assets: The pinned assets. Their order does not matter.

    Returns:
        ``sha256:`` and the hex SHA-256 of the lines ``"<path> <sri>"``, sorted by path.
    """
    lines = "".join(f"{a.path} {a.sri}\n" for a in sorted(assets, key=lambda a: a.path))
    return f"sha256:{hashlib.sha256(lines.encode()).hexdigest()}"


def manifest_file_bytes(assets: Sequence[ToolkitAsset]) -> bytes:
    """Return the content of :data:`MANIFEST_FILE` for ``assets``."""
    body = {
        "digest": toolkit_digest(assets),
        "assets": [{"path": a.path, "sri": a.sri} for a in sorted(assets, key=lambda a: a.path)],
    }
    return (json.dumps(body, indent=2) + "\n").encode()


def check_toolkit_contents(assets: Sequence[ToolkitAsset], contents: Mapping[str, bytes]) -> None:
    """Check the files of a toolkit volume against the pins.

    Args:
        assets: The pinned assets.
        contents: The volume's files, by path relative to the mount.

    Raises:
        DevAgentToolkitError: If an asset is missing or differs from its pin, a file is extra, or
            :data:`MANIFEST_FILE` is missing or holds another digest. The message names every
            problem.
    """
    problems: list[str] = []
    pinned = {a.path for a in assets}
    for asset in assets:
        data = contents.get(asset.path)
        if data is None:
            problems.append(f"missing: {asset.path}")
        elif not verify_sri(data, asset.sri):
            problems.append(f"differs from the pin: {asset.path}")
    problems.extend(
        f"unexpected file: {path}" for path in sorted(set(contents) - pinned - {MANIFEST_FILE})
    )
    if MANIFEST_FILE not in contents:
        problems.append(f"missing: {MANIFEST_FILE}")
    elif contents[MANIFEST_FILE] != manifest_file_bytes(assets):
        problems.append(f"{MANIFEST_FILE} holds another digest than {toolkit_digest(assets)}")
    if problems:
        raise DevAgentToolkitError("the toolkit volume is wrong: " + "; ".join(problems))


async def _docker(runner: CommandRunner, what: str, *args: str) -> str:
    """Run ``docker`` with ``args`` and return stdout, or raise :class:`DevAgentToolkitError`."""
    result = await runner(["docker", *args])
    if result.returncode != 0:
        raise DevAgentToolkitError(
            f"{what} failed ({result.returncode}): {result.stderr.strip()[:500]}"
        )
    return result.stdout


def _read_tree(root: Path) -> dict[str, bytes]:
    """Read every file under ``root``. Anything but a regular file or a directory is refused.

    A symlink would make the host read the file it points to, and a pipe would block the read.
    """
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise DevAgentToolkitError(
                f"the toolkit volume holds a special entry: {path.relative_to(root).as_posix()}"
            )
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _write_tree(root: Path, files: Mapping[str, bytes]) -> None:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o444)


async def read_toolkit_volume(volume: str, image: str, runner: CommandRunner) -> dict[str, bytes]:
    """Copy every file out of a toolkit volume.

    The helper container mounts the volume read-only, has no network, and never starts.

    Args:
        volume: The volume name.
        image: An image to create the helper container from (the ``agent`` profile's).
        runner: Runs the ``docker`` CLI.

    Returns:
        The files by path relative to the mount.

    Raises:
        DevAgentToolkitError: If a Docker call fails.
    """
    out = await _docker(
        runner,
        "toolkit read container create",
        "create",
        "--network",
        "none",
        "-v",
        f"{volume}:{TOOLKIT_MOUNT}:ro",
        image,
    )
    container = out.strip()
    try:
        with tempfile.TemporaryDirectory(prefix="seshat-toolkit-read-") as tmp:
            await _docker(runner, "toolkit read cp", "cp", f"{container}:{TOOLKIT_MOUNT}/.", tmp)
            return await asyncio.to_thread(_read_tree, Path(tmp))
    finally:
        await asyncio.shield(runner(["docker", "rm", "-f", container]))


async def fill_toolkit_volume(
    volume: str,
    assets: Sequence[ToolkitAsset],
    fetch: FetchAsset,
    *,
    image: str,
    runner: CommandRunner,
) -> str:
    """Build the toolkit volume from the pinned assets, and check it.

    All bytes are fetched and checked in memory first, so a bad byte fails before any Docker call
    and an older volume stays as it was. Then the volume is replaced, filled, read back and checked.
    A failure after the volume is created removes the helper container, then the volume.

    Args:
        volume: The volume name.
        assets: The pinned assets.
        fetch: Returns the bytes of an asset by its path.
        image: An image to create the helper container from (the ``agent`` profile's).
        runner: Runs the ``docker`` CLI.

    Returns:
        The digest of the toolkit (:func:`toolkit_digest`).

    Raises:
        DevAgentToolkitError: If a byte differs from its pin, a Docker call fails, the volume is in
            use, or the read-back differs.
    """
    fetched = {asset.path: await fetch(asset.path) for asset in assets}
    files = {**fetched, MANIFEST_FILE: manifest_file_bytes(assets)}
    check_toolkit_contents(assets, files)

    exists = await runner(["docker", "volume", "inspect", volume])
    if exists.returncode == 0:
        # Docker refuses to remove a volume that a container refers to.
        await _docker(runner, "volume rm", "volume", "rm", volume)
    await _docker(runner, "volume create", "volume", "create", volume)
    container: str | None = None
    try:
        out = await _docker(
            runner,
            "toolkit container create",
            "create",
            "--network",
            "none",
            "-v",
            f"{volume}:{TOOLKIT_MOUNT}",
            image,
        )
        container = out.strip()
        with tempfile.TemporaryDirectory(prefix="seshat-toolkit-write-") as tmp:
            await asyncio.to_thread(_write_tree, Path(tmp), files)
            await _docker(runner, "toolkit cp", "cp", f"{tmp}/.", f"{container}:{TOOLKIT_MOUNT}")
        await _docker(runner, "toolkit container rm", "rm", "-f", container)
        container = None
        check_toolkit_contents(assets, await read_toolkit_volume(volume, image, runner))
    except BaseException:
        if container is not None:
            await asyncio.shield(runner(["docker", "rm", "-f", container]))
        await asyncio.shield(runner(["docker", "volume", "rm", "-f", volume]))
        raise
    digest = toolkit_digest(assets)
    log.info("dev_agent_toolkit_filled", volume=volume, digest=digest, trace_id=None)
    return digest
