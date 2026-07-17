"""FRE-910 AC-3 — telemetry mount coverage guard.

Every Path("telemetry/...") writer in src resolves under a mounted container path,
so a new writer cannot silently be ephemeral (the guard-as-test the parent mount
makes near-tautological).
"""

from __future__ import annotations

from pathlib import PurePosixPath

from personal_agent.config.config_guard import repo_root
from tests._helpers.telemetry_mounts import (
    find_telemetry_writers,
    gateway_mounts,
    is_mount_covered,
    load_compose,
)


class TestTelemetryMountCoverage:
    def test_nine_known_writers_found(self) -> None:
        # Pins the ticket's measured inventory — a drop below 9 means the regex
        # stopped matching a writer (rename/idiom change), not that one was removed.
        assert len(find_telemetry_writers(repo_root())) == 9

    def test_parent_mount_is_the_named_seshat_telemetry_volume(self) -> None:
        # Not just "something is mounted at /app/telemetry" — the specific named
        # volume this ticket adds, declared with a durable local driver.
        compose = load_compose(repo_root())
        mounts = gateway_mounts(compose)
        assert mounts.get(PurePosixPath("/app/telemetry")) == "seshat_telemetry_cloud"
        top_level_volumes = compose["volumes"]
        assert isinstance(top_level_volumes, dict)
        seshat_telemetry_cloud = top_level_volumes["seshat_telemetry_cloud"]
        assert isinstance(seshat_telemetry_cloud, dict)
        assert seshat_telemetry_cloud["driver"] == "local"

    def test_every_writer_resolves_under_a_mounted_path(self) -> None:
        mounts = gateway_mounts(load_compose(repo_root()))
        writers = find_telemetry_writers(repo_root())
        assert writers, "writer discovery found nothing — regex or scan path broke"
        uncovered = [w for w in writers if not is_mount_covered(PurePosixPath("/app") / w, mounts)]
        assert not uncovered, f"telemetry writers with no durable mount: {uncovered}"
