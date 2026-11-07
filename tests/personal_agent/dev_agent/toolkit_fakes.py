"""A fake ``docker`` CLI for the toolkit and launcher unit tests (FRE-1751).

It keeps named volumes and containers in memory. It handles the calls that the toolkit module and
the launcher make: ``volume inspect|create|rm|ls``, ``create``, ``cp``, ``rm``, ``ps`` and the rest
as a success with no output. A volume cannot be removed while a container refers to it, as in Docker.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from personal_agent.dev_agent.launcher import CommandResult
from personal_agent.dev_agent.toolkit import MANIFEST_FILE, ToolkitAsset, manifest_file_bytes
from personal_agent.storage.artifact_export import compute_sri

#: Two small assets, one of them under a font-style subfolder, with their real SHA-384 SRI.
SYNTHETIC_FILES: dict[str, bytes] = {
    "alpha@1.0/alpha.min.js": b"window.alpha = 1;\n",
    "fonts/beta@2.0/beta.woff2": b"\x00\x01wOF2-bytes\xff",
}


def synthetic_assets() -> tuple[ToolkitAsset, ...]:
    """Return the assets of :data:`SYNTHETIC_FILES`, sorted by path."""
    return tuple(
        ToolkitAsset(path, compute_sri(data)) for path, data in sorted(SYNTHETIC_FILES.items())
    )


async def fetch_synthetic(path: str) -> bytes:
    """Return the bytes of a synthetic asset. It is a ``FetchAsset`` for the fill function."""
    return SYNTHETIC_FILES[path]


def complete_volume() -> dict[str, bytes]:
    """Return the file set of a correct volume for :func:`synthetic_assets`."""
    return {**SYNTHETIC_FILES, MANIFEST_FILE: manifest_file_bytes(synthetic_assets())}


class FakeDocker:
    """Records every call and keeps volumes and containers in memory.

    Attributes:
        calls: Every ``(argv, env)`` pair, in order.
        fail: Keys that fail with status 1. A key is the first word after ``docker`` or the first
            two words.
        listing: Output of ``ps -aq`` and ``volume ls``, by key, used once.
        volumes: Volume name to its files.
        containers: Container id to the volumes that it refers to.
        tamper_on_copy_in: If true, ``cp`` into a container changes one byte of the first file.
    """

    def __init__(self) -> None:
        """Create a fake with no volume and no container."""
        self.calls: list[tuple[list[str], Mapping[str, str] | None]] = []
        self.fail: set[str] = set()
        self.listing: dict[str, list[str]] = {}
        self.volumes: dict[str, dict[str, bytes]] = {}
        self.containers: dict[str, list[str]] = {}
        self.tamper_on_copy_in = False
        self._next_id = 0

    def argvs(self) -> list[list[str]]:
        """Return the argument lists of all calls."""
        return [argv for argv, _ in self.calls]

    async def __call__(
        self, args: Sequence[str], *, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        """Run one fake ``docker`` call."""
        argv = list(args)
        self.calls.append((argv, env))
        key = " ".join(argv[1:3])
        if key in self.fail or argv[1] in self.fail:
            return CommandResult(1, "", f"{key} broke")
        if argv[1] == "volume":
            return self._volume(argv)
        if argv[1] == "create":
            return self._create(argv)
        if argv[1] == "cp":
            return self._cp(argv)
        if argv[1] == "rm":
            for name in argv[2:]:
                self.containers.pop(name, None)
            return CommandResult(0, "", "")
        if argv[1] == "ps":
            return CommandResult(0, "\n".join(self.listing.pop(key, [])), "")
        return CommandResult(0, "", "")

    def _volume(self, argv: list[str]) -> CommandResult:
        action, name = argv[2], argv[-1]
        if action == "ls":
            return CommandResult(0, "\n".join(self.listing.pop("volume ls", [])), "")
        if action == "inspect":
            if name in self.volumes:
                return CommandResult(0, "[]", "")
            return CommandResult(1, "", f"Error: No such volume: {name}")
        if action == "create":
            self.volumes.setdefault(name, {})
            return CommandResult(0, name, "")
        if action == "rm":
            if any(name in refs for refs in self.containers.values()):
                return CommandResult(1, "", f"volume is in use: {name}")
            if name not in self.volumes and "-f" not in argv:
                return CommandResult(1, "", f"Error: No such volume: {name}")
            self.volumes.pop(name, None)
            return CommandResult(0, name, "")
        return CommandResult(0, "", "")

    def _create(self, argv: list[str]) -> CommandResult:
        refs = [
            argv[i + 1].split(":")[0]
            for i, word in enumerate(argv)
            if word == "-v" and i + 1 < len(argv)
        ]
        if "--name" in argv:
            ident = argv[argv.index("--name") + 1]
        else:
            self._next_id += 1
            ident = f"helper{self._next_id}"
        self.containers[ident] = refs
        return CommandResult(0, ident + "\n", "")

    def _cp(self, argv: list[str]) -> CommandResult:
        src, dst = argv[2], argv[3]
        if not src.startswith("/") and ":" in src:
            volume = self.containers[src.split(":", 1)[0]][0]
            target = Path(dst)
            for rel, data in self.volumes[volume].items():
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                (target / rel).write_bytes(data)
            return CommandResult(0, "", "")
        volume = self.containers[dst.split(":", 1)[0]][0]
        root = Path(src)
        files = {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }
        if self.tamper_on_copy_in and files:
            first = next(iter(files))
            files[first] = files[first] + b"x"
        self.volumes[volume].update(files)
        return CommandResult(0, "", "")
