"""Tests for the shared path-governance durability helpers.

FRE-1352 — the ``write`` tool's ``allowed_paths`` grants all of ``/app/**``, but only
bind-mounted subdirectories survive a container restart. These tests cover the mount-table
parsing and the ancestor-walk durability predicate in isolation from ``write_executor``.
"""

from pathlib import Path
from unittest.mock import patch

from personal_agent.tools.primitives._governance import _is_durable_mount, _mount_points

# ---------------------------------------------------------------------------
# _mount_points — hermetic parsing against fixture mountinfo text
# ---------------------------------------------------------------------------


def test_mount_points_parses_mountinfo_fixture(tmp_path: Path) -> None:
    """Field index 4 of each mountinfo line is the mount point."""
    fixture = tmp_path / "mountinfo"
    fixture.write_text(
        "23 0 253:0 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
        "45 23 253:1 / /app/agent_workspace rw,relatime shared:2 - ext4 /dev/sdb rw\n"
        "46 23 253:2 / /app/telemetry rw,relatime shared:3 - ext4 /dev/sdc rw\n"
        "47 46 253:3 / /app/telemetry/graph_quality rw,relatime shared:4 - ext4 /dev/sdd rw\n",
        encoding="utf-8",
    )

    result = _mount_points(str(fixture))

    assert result == frozenset(
        {"/", "/app/agent_workspace", "/app/telemetry", "/app/telemetry/graph_quality"}
    )


def test_mount_points_missing_file_returns_none() -> None:
    """A missing or unreadable mountinfo file returns None, distinct from an empty table."""
    assert _mount_points("/nonexistent/mountinfo") is None


# ---------------------------------------------------------------------------
# _is_durable_mount — ancestor walk
# ---------------------------------------------------------------------------

_MOUNT_SET = frozenset(
    {"/", "/app/agent_workspace", "/app/telemetry", "/app/telemetry/graph_quality"}
)


def _patched(mount_set: frozenset[str] | None = _MOUNT_SET):
    return patch(
        "personal_agent.tools.primitives._governance._mount_points",
        return_value=mount_set,
    )


def test_is_durable_mount_rejects_nonmounted_app_path() -> None:
    """A path under /app with no mounted ancestor is not durable — the incident path."""
    with _patched():
        assert _is_durable_mount(Path("/app/nfl-predictor/x.py")) is False


def test_is_durable_mount_accepts_direct_mount() -> None:
    """A path directly under a mounted directory is durable."""
    with _patched():
        assert _is_durable_mount(Path("/app/agent_workspace/nfl-predictor/x.py")) is True


def test_is_durable_mount_accepts_nested_mount() -> None:
    """A path under a nested mount (a subdirectory that is itself mounted) is durable."""
    with _patched():
        assert _is_durable_mount(Path("/app/telemetry/graph_quality/report.jsonl")) is True


def test_is_durable_mount_ignores_root_boundary() -> None:
    """'/' is always a mount entry in real mountinfo; the walk must not test it.

    Without the /app stop condition, every path would read as durable because '/' is
    always in the mount set.
    """
    with _patched():
        assert _is_durable_mount(Path("/app/nfl-predictor/x.py")) is False


def test_is_durable_mount_not_applicable_outside_app() -> None:
    """Durability is only enforced under /app — other allowed roots are out of scope."""
    with _patched():
        assert _is_durable_mount(Path("/tmp/scratch.txt")) is True


def test_is_durable_mount_fails_open_when_mountinfo_unreadable() -> None:
    """Unreadable mount data (None) must not be treated as 'nothing is durable'.

    Otherwise every /app write — including genuinely durable ones like
    /app/agent_workspace/** — would be wrongly rejected in any environment where
    /proc/self/mountinfo can't be read (non-Linux, permission-restricted sandbox).
    """
    with _patched(mount_set=None):
        assert _is_durable_mount(Path("/app/agent_workspace/nfl-predictor/x.py")) is True
        assert _is_durable_mount(Path("/app/nfl-predictor/x.py")) is True
