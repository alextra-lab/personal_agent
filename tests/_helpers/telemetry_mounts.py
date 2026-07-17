"""Test helpers for reading telemetry-mount coverage from docker-compose.cloud.yml.

FRE-910 mounts a single parent volume at ``/app/telemetry`` on the ``seshat-gateway``
service so every ``Path("telemetry/...")`` writer in src is durable by default. Shared
by ``tests/personal_agent/telemetry/test_mount_coverage.py`` (the FRE-910 AC-3 guard)
and ``tests/test_orchestrator/test_compression_gate_proof.py`` (the FRE-908 finding
this ticket fixed) so both check real compose state through one implementation
instead of two independently-drifting text/YAML parses.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path, PurePosixPath

import yaml  # type: ignore[import-untyped]

_WRITER_RE = re.compile(r'Path\("(telemetry/[^"]+)"\)')


@lru_cache(maxsize=None)
def find_telemetry_writers(root: Path) -> frozenset[str]:
    """Every literal ``Path("telemetry/...")`` writer path declared under src/personal_agent."""
    writers: set[str] = set()
    for path in (root / "src" / "personal_agent").rglob("*.py"):
        writers.update(_WRITER_RE.findall(path.read_text(encoding="utf-8")))
    return frozenset(writers)


@lru_cache(maxsize=None)
def load_compose(root: Path) -> dict[str, object]:
    """Parse docker-compose.cloud.yml."""
    doc = yaml.safe_load((root / "docker-compose.cloud.yml").read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def gateway_mounts(compose_doc: dict[str, object]) -> dict[PurePosixPath, str]:
    """Map each seshat-gateway container mount destination to its named-volume source."""
    services = compose_doc["services"]
    assert isinstance(services, dict)
    gateway = services["seshat-gateway"]
    assert isinstance(gateway, dict)
    volumes = gateway["volumes"]
    assert isinstance(volumes, list)

    mounts: dict[PurePosixPath, str] = {}
    for v in volumes:
        if isinstance(v, str) and ":" in v:
            source, dest = v.split(":", 1)
            mounts[PurePosixPath(dest.split(":")[0])] = source
    return mounts


def is_mount_covered(container_path: PurePosixPath, mounts: dict[PurePosixPath, str]) -> bool:
    """True if *container_path* is durable — equal to or nested under some mounted destination."""
    return any(container_path == m or m in container_path.parents for m in mounts)
