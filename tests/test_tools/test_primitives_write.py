"""Tests for the primitive ``write`` tool executor.

FRE-261 Step 3.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from personal_agent.governance.models import ToolPolicy
from personal_agent.tools.primitives.write import write_executor


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

        result = await write_executor(str(target), content=content, mode="overwrite")

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

        await write_executor(str(target), content="first\n", mode="append")
        await write_executor(str(target), content="second\n", mode="append")

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

        result = await write_executor(str(target), content="data", mode="invalid")

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

        result = await write_executor(str(target), content="deep write", mode="overwrite")

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
        "personal_agent.tools.primitives.write.load_governance_config",
        return_value=mock_config,
    ):
        result = await write_executor("/etc/shadow", content="pwned", mode="overwrite")

    assert result["success"] is False
    assert result["error"] == "forbidden_path"
