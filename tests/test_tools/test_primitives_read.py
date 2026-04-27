"""Tests for the primitive ``read`` tool executor.

FRE-261 Step 3.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from personal_agent.governance.models import ToolPolicy
from personal_agent.tools.primitives.read import read_executor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_policy(**kwargs) -> ToolPolicy:
    """Build a minimal ToolPolicy for test patching."""
    defaults = {
        "category": "read_only",
        "allowed_in_modes": ["NORMAL"],
        "allowed_paths": [],
        "forbidden_paths": [],
    }
    defaults.update(kwargs)
    return ToolPolicy(**defaults)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_happy_path() -> None:
    """Read a real temp file; assert content and size are returned."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "hello.txt"
        expected = "Hello, primitives!\nLine 2."
        test_file.write_text(expected, encoding="utf-8")

        result = await read_executor(str(test_file))

        assert result["success"] is True
        assert result["content"] == expected
        assert result["size_bytes"] == len(expected.encode("utf-8"))
        assert result["path"] == str(test_file.resolve())


# ---------------------------------------------------------------------------
# Not-a-file (directory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_not_a_file() -> None:
    """Reading a directory path returns error='not_a_file'."""
    with TemporaryDirectory() as tmpdir:
        # tmpdir is a directory, not a file
        result = await read_executor(tmpdir)

        assert result["success"] is False
        assert result["error"] == "not_a_file"
        assert "path" in result


# ---------------------------------------------------------------------------
# File too large
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_too_large() -> None:
    """Writing a file larger than max_bytes returns error='too_large'."""
    with TemporaryDirectory() as tmpdir:
        large_file = Path(tmpdir) / "big.bin"
        # write 1100 bytes, read with cap at 1000
        large_file.write_bytes(b"x" * 1100)

        result = await read_executor(str(large_file), max_bytes=1000)

        assert result["success"] is False
        assert result["error"] == "too_large"
        assert result["size_bytes"] == 1100
        assert result["max_bytes"] == 1000


# ---------------------------------------------------------------------------
# Forbidden-path check fires for /etc/shadow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_path_traversal() -> None:
    """Path '/etc/shadow' is rejected by forbidden_paths governance check."""
    # We patch load_governance_config so the test does not depend on the
    # real tools.yaml being present, while still exercising the governance
    # code path.
    from personal_agent.governance.models import GovernanceConfig

    policy = _make_policy(
        forbidden_paths=["/etc/shadow", "/etc/passwd", "/proc/**"],
        allowed_paths=[],
    )

    mock_config = GovernanceConfig.__new__(GovernanceConfig)
    # Manually set tools dict without validation overhead
    object.__setattr__(mock_config, "tools", {"read": policy})

    with patch(
        "personal_agent.tools.primitives._governance.load_governance_config",
        return_value=mock_config,
    ):
        result = await read_executor("/etc/shadow")

    assert result["success"] is False
    assert result["error"] == "forbidden_path"


# ---------------------------------------------------------------------------
# path_not_allowed check — path outside allowed_paths list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_path_not_in_allowed_paths() -> None:
    """A path outside allowed_paths list returns path_not_allowed error."""
    from personal_agent.governance.models import GovernanceConfig

    # Policy with a narrow allowed_paths that does NOT cover /var/tmp/
    policy = _make_policy(
        allowed_paths=["/nonexistent/**"],
        forbidden_paths=[],
    )

    mock_config = GovernanceConfig.__new__(GovernanceConfig)
    object.__setattr__(mock_config, "tools", {"read": policy})

    with TemporaryDirectory(dir="/var/tmp") as tmpdir:
        test_file = Path(tmpdir) / "probe.txt"
        test_file.write_text("data", encoding="utf-8")

        with patch(
            "personal_agent.tools.primitives._governance.load_governance_config",
            return_value=mock_config,
        ):
            result = await read_executor(str(test_file))

    assert result["success"] is False
    assert result["error"] == "path_not_allowed"
    assert "path" in result
