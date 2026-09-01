"""Tests for the primitive ``write`` tool executor.

FRE-261 Step 3.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from personal_agent.governance.models import ToolPolicy
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.primitives.write import write_executor

_DURABLE_MOUNTS = frozenset({"/app/agent_workspace", "/app/telemetry", "/"})


_CTX = TraceContext.new_trace()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_policy(**kwargs) -> ToolPolicy:
    """Build a minimal ToolPolicy for test patching."""
    defaults = {
        "category": "system_write",
        "allowed_in_modes": ["NORMAL"],
        "allowed_paths": [],
        "forbidden_paths": [],
        "unattended_paths": [],
    }
    defaults.update(kwargs)
    return ToolPolicy(**defaults)


# ---------------------------------------------------------------------------
# Overwrite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_overwrite() -> None:
    """Write a file in overwrite mode and read it back to verify content."""
    with TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "output.txt"
        content = "Hello from write_executor!"

        result = await write_executor(str(target), content=content, mode="overwrite", ctx=_CTX)

        assert result["success"] is True
        assert result["path"] == str(target.resolve())
        assert result["mode"] == "overwrite"
        assert result["bytes_written"] == len(content.encode("utf-8"))
        assert target.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_append() -> None:
    """Write twice in append mode; both parts must be present."""
    with TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "log.txt"

        await write_executor(str(target), content="first\n", mode="append", ctx=_CTX)
        await write_executor(str(target), content="second\n", mode="append", ctx=_CTX)

        combined = target.read_text(encoding="utf-8")
        assert "first\n" in combined
        assert "second\n" in combined
        assert combined == "first\nsecond\n"


# ---------------------------------------------------------------------------
# Invalid mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_invalid_mode() -> None:
    """Passing an invalid mode returns error='invalid_mode'."""
    with TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "file.txt"

        result = await write_executor(str(target), content="data", mode="invalid", ctx=_CTX)

        assert result["success"] is False
        assert result["error"] == "invalid_mode"


# ---------------------------------------------------------------------------
# Auto-creates parent directories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_creates_parent_dirs() -> None:
    """Writing to a deep path automatically creates missing parent directories."""
    with TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "subdir" / "nested" / "file.txt"
        assert not target.parent.exists()

        result = await write_executor(str(target), content="deep write", mode="overwrite", ctx=_CTX)

        assert result["success"] is True
        assert target.parent.exists()
        assert target.read_text(encoding="utf-8") == "deep write"


# ---------------------------------------------------------------------------
# Forbidden-path check fires for /etc/shadow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_forbidden_path() -> None:
    """Writing to '/etc/shadow' is rejected by forbidden_paths governance check."""
    from personal_agent.governance.models import GovernanceConfig

    policy = _make_policy(
        forbidden_paths=["/etc/**"],
        allowed_paths=[],
        unattended_paths=[],
    )

    mock_config = GovernanceConfig.__new__(GovernanceConfig)
    object.__setattr__(mock_config, "tools", {"write": policy})

    with patch(
        "personal_agent.tools.primitives._governance.load_governance_config",
        return_value=mock_config,
    ):
        result = await write_executor("/etc/shadow", content="pwned", mode="overwrite", ctx=_CTX)

    assert result["success"] is False
    assert result["error"] == "forbidden_path"


# ---------------------------------------------------------------------------
# path_not_allowed check — path outside allowed_paths list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_path_not_in_allowed_paths() -> None:
    """A path outside allowed_paths list returns path_not_allowed error."""
    from personal_agent.governance.models import GovernanceConfig

    # Policy with a narrow allowed_paths that does NOT cover /var/tmp/
    policy = _make_policy(
        allowed_paths=["/nonexistent/**"],
        forbidden_paths=[],
        unattended_paths=[],
    )

    mock_config = GovernanceConfig.__new__(GovernanceConfig)
    object.__setattr__(mock_config, "tools", {"write": policy})

    with TemporaryDirectory(dir="/var/tmp") as tmpdir:
        target = Path(tmpdir) / "probe.txt"

        with patch(
            "personal_agent.tools.primitives._governance.load_governance_config",
            return_value=mock_config,
        ):
            result = await write_executor(str(target), content="data", mode="overwrite", ctx=_CTX)

    assert result["success"] is False
    assert result["error"] == "path_not_allowed"
    assert "path" in result


# ---------------------------------------------------------------------------
# Durability guard (FRE-1352) — /app paths outside a bind mount are refused,
# the mounted subdirectory still works. Both tests run in the same suite,
# satisfying AC-5 (seeded negative alongside the passing case).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_rejects_nonmounted_app_path() -> None:
    """AC-1: a write to a non-mounted /app path is refused, not silently written."""
    from personal_agent.governance.models import GovernanceConfig

    policy = _make_policy(allowed_paths=["/app/**"], forbidden_paths=[], unattended_paths=[])
    mock_config = GovernanceConfig.__new__(GovernanceConfig)
    object.__setattr__(mock_config, "tools", {"write": policy})

    with (
        patch(
            "personal_agent.tools.primitives._governance.load_governance_config",
            return_value=mock_config,
        ),
        patch(
            "personal_agent.tools.primitives._governance._mount_points",
            return_value=_DURABLE_MOUNTS,
        ),
    ):
        result = await write_executor(
            "/app/nfl-predictor/x.py", content="junk", mode="overwrite", ctx=_CTX
        )

    assert result["success"] is False
    assert result["error"] == "not_durable"
    assert "agent_workspace" in result["detail"]


@pytest.mark.asyncio
async def test_write_allows_mounted_app_path() -> None:
    """AC-2: the same run's durable /app subdirectory still writes successfully."""
    from personal_agent.governance.models import GovernanceConfig

    policy = _make_policy(allowed_paths=["/app/**"], forbidden_paths=[], unattended_paths=[])
    mock_config = GovernanceConfig.__new__(GovernanceConfig)
    object.__setattr__(mock_config, "tools", {"write": policy})

    target = "/app/agent_workspace/nfl-predictor/x.py"

    with (
        patch(
            "personal_agent.tools.primitives._governance.load_governance_config",
            return_value=mock_config,
        ),
        patch(
            "personal_agent.tools.primitives._governance._mount_points",
            return_value=_DURABLE_MOUNTS,
        ),
        patch.object(Path, "mkdir", return_value=None),
        patch.object(Path, "write_text", return_value=None),
    ):
        result = await write_executor(target, content="model code", mode="overwrite", ctx=_CTX)

    assert result["success"] is True
    assert result["path"] == target
