"""Tests for the primitive ``read`` tool executor.

FRE-261 Step 3. FRE-355: tail_lines parameter.
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


# ---------------------------------------------------------------------------
# tail_lines — basic: returns last N lines of a small file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_tail_lines_basic() -> None:
    """tail_lines=3 returns the last 3 lines of a file."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "multi.txt"
        lines = [f"line{i}" for i in range(1, 11)]  # line1..line10
        test_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = await read_executor(str(test_file), tail_lines=3)

        assert result["success"] is True
        assert result["tail_lines"] == 3
        content_lines = result["content"].strip().splitlines()
        assert content_lines == ["line8", "line9", "line10"]


# ---------------------------------------------------------------------------
# tail_lines — large file: bypasses max_bytes size gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_tail_bypasses_size_gate() -> None:
    """tail_lines bypasses the too_large error for files over max_bytes."""
    with TemporaryDirectory() as tmpdir:
        large_file = Path(tmpdir) / "big.log"
        # 1100 bytes of content, capped at 1000 — normally 'too_large'
        content = "\n".join([f"event_{i:04d}" for i in range(100)])
        large_file.write_text(content, encoding="utf-8")
        assert large_file.stat().st_size > 1000

        # Without tail_lines: too_large
        normal = await read_executor(str(large_file), max_bytes=1000)
        assert normal["error"] == "too_large"

        # With tail_lines: succeeds
        tail = await read_executor(str(large_file), max_bytes=1000, tail_lines=5)
        assert tail["success"] is True
        assert tail["tail_lines"] == 5
        tail_lines_content = tail["content"].strip().splitlines()
        assert len(tail_lines_content) == 5
        assert tail_lines_content[-1] == "event_0099"


# ---------------------------------------------------------------------------
# tail_lines — more lines requested than exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_tail_more_than_file_has() -> None:
    """tail_lines > line count returns all lines without error."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "short.txt"
        test_file.write_text("only\ntwo lines\n", encoding="utf-8")

        result = await read_executor(str(test_file), tail_lines=100)

        assert result["success"] is True
        lines = result["content"].strip().splitlines()
        assert lines == ["only", "two lines"]


# ---------------------------------------------------------------------------
# tail_lines — output cap still enforced via max_bytes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_tail_output_capped_by_max_bytes() -> None:
    """When tail content exceeds max_bytes, it is truncated and truncated=True."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "wide.log"
        # 20 lines of 100 chars each = 2000+ bytes
        lines = ["x" * 100 for _ in range(20)]
        test_file.write_text("\n".join(lines), encoding="utf-8")

        result = await read_executor(str(test_file), max_bytes=300, tail_lines=20)

        assert result["success"] is True
        assert result["truncated"] is True
        assert len(result["content"].encode()) <= 300


# ---------------------------------------------------------------------------
# tail_lines — file without trailing newline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_tail_no_trailing_newline() -> None:
    """tail_lines works correctly on files that don't end with a newline."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "nonl.txt"
        test_file.write_bytes(b"alpha\nbeta\ngamma")  # no trailing newline

        result = await read_executor(str(test_file), tail_lines=2)

        assert result["success"] is True
        lines = result["content"].splitlines()
        assert lines == ["beta", "gamma"]
