"""Tests for the primitive ``read`` tool executor.

FRE-261 Step 3. FRE-355: tail_lines parameter.
"""

import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from personal_agent.governance.models import ToolPolicy
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.primitives.read import read_executor

_CTX = TraceContext.new_trace()
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

        result = await read_executor(str(test_file), ctx=_CTX)

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
        result = await read_executor(tmpdir, ctx=_CTX)

        assert result["success"] is False
        assert result["error"] == "not_a_file"
        assert "path" in result


# ---------------------------------------------------------------------------
# Oversized read truncates instead of erroring (too_large removed, FRE-410)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_no_too_large_truncates() -> None:
    """A file over max_bytes truncates with truncated=True (no too_large error)."""
    with TemporaryDirectory() as tmpdir:
        large_file = Path(tmpdir) / "big.bin"
        # single 1100-byte line with no newline; read with byte cap at 1000
        large_file.write_bytes(b"x" * 1100)

        result = await read_executor(str(large_file), max_bytes=1000, ctx=_CTX)

        assert result["success"] is True
        assert "error" not in result
        assert result["truncated"] is True
        # degenerate single-line-larger-than-cap case: clipped to the byte cap
        assert len(result["content"].encode("utf-8")) <= 1000


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
        result = await read_executor("/etc/shadow", ctx=_CTX)

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
            result = await read_executor(str(test_file), ctx=_CTX)

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

        result = await read_executor(str(test_file), tail_lines=3, ctx=_CTX)

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

        # Without tail_lines: truncates (no more too_large error, FRE-410)
        normal = await read_executor(str(large_file), max_bytes=1000, ctx=_CTX)
        assert normal["success"] is True
        assert normal["truncated"] is True

        # With tail_lines: succeeds
        tail = await read_executor(str(large_file), max_bytes=1000, tail_lines=5, ctx=_CTX)
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

        result = await read_executor(str(test_file), tail_lines=100, ctx=_CTX)

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

        result = await read_executor(str(test_file), max_bytes=300, tail_lines=20, ctx=_CTX)

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

        result = await read_executor(str(test_file), tail_lines=2, ctx=_CTX)

        assert result["success"] is True
        lines = result["content"].splitlines()
        assert lines == ["beta", "gamma"]


# ---------------------------------------------------------------------------
# Head-cap default: large file returns a truncated head + marker (FRE-410)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_default_truncates_head() -> None:
    """A file with > 200 lines returns the first 200 with a continuation marker."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "long.txt"
        test_file.write_text("\n".join(f"line{i}" for i in range(1, 501)) + "\n", encoding="utf-8")

        result = await read_executor(str(test_file), ctx=_CTX)

        assert result["success"] is True
        assert result["truncated"] is True
        assert result["lines_returned"] == 200
        assert result["total_lines"] == 500
        assert result["offset"] == 1
        assert result["content"].splitlines()[0] == "line1"
        assert result["content"].splitlines()[-1] == "line200"
        # directive grep-first marker: imperative, names grep + the file path + continuation
        marker = result["marker"]
        assert "offset=201" in marker
        assert "grep -n" in marker
        assert str(test_file) in marker  # path embedded so the grep example is copy-pasteable
        assert "Do NOT" in marker
        assert "300 more" in marker  # remaining lines (500 total - 200 shown)


@pytest.mark.asyncio
async def test_read_small_file_not_truncated() -> None:
    """A file under both caps returns full content, no truncation, no marker."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "small.txt"
        test_file.write_text("a\nb\nc\n", encoding="utf-8")

        result = await read_executor(str(test_file), ctx=_CTX)

        assert result["success"] is True
        assert result["truncated"] is False
        assert result["marker"] is None
        assert result["lines_returned"] == 3
        assert result["total_lines"] == 3


# ---------------------------------------------------------------------------
# offset / limit ranged reads (FRE-410)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_offset_limit_range() -> None:
    """offset=10, limit=5 returns exactly lines 10..14."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "ranged.txt"
        test_file.write_text("\n".join(f"line{i}" for i in range(1, 51)) + "\n", encoding="utf-8")

        result = await read_executor(str(test_file), offset=10, limit=5, ctx=_CTX)

        assert result["success"] is True
        assert result["content"].splitlines() == [f"line{i}" for i in range(10, 15)]
        assert result["offset"] == 10
        assert result["limit"] == 5
        assert result["truncated"] is True  # lines 15..50 remain


@pytest.mark.asyncio
async def test_read_offset_beyond_eof() -> None:
    """Offset past the last line returns empty content without error."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "short.txt"
        test_file.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")

        result = await read_executor(str(test_file), offset=100, ctx=_CTX)

        assert result["success"] is True
        assert result["content"] == ""
        assert result["lines_returned"] == 0
        assert result["total_lines"] == 5
        assert result["truncated"] is False


# ---------------------------------------------------------------------------
# Byte-cap behaviour (FRE-410)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_byte_cap_truncates_head() -> None:
    """Wide lines hit the byte cap before the line cap; output ends on a whole line."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "wide.txt"
        # 50 lines of 100 chars each = ~5KB; cap at 1000 bytes
        test_file.write_text("\n".join("x" * 100 for _ in range(50)) + "\n", encoding="utf-8")

        result = await read_executor(str(test_file), max_bytes=1000, ctx=_CTX)

        assert result["success"] is True
        assert result["truncated"] is True
        assert len(result["content"].encode("utf-8")) <= 1000
        # whole-line boundary: every returned line is a full 100-char line
        for line in result["content"].splitlines():
            assert len(line) == 100


@pytest.mark.asyncio
async def test_read_explicit_max_bytes_allows_large() -> None:
    """An explicit large max_bytes returns full content (within the line limit)."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "big.txt"
        # 50 lines of 1000 chars each = ~50KB, over the 8KB default head cap
        body = "\n".join("y" * 1000 for _ in range(50)) + "\n"
        test_file.write_text(body, encoding="utf-8")

        result = await read_executor(str(test_file), max_bytes=200_000, ctx=_CTX)

        assert result["success"] is True
        assert result["truncated"] is False
        assert result["lines_returned"] == 50


@pytest.mark.asyncio
async def test_read_paging_across_byte_cap_no_loss() -> None:
    """Two-call paging across a byte-cap boundary loses and duplicates no content."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "page.txt"
        original = [f"{'z' * 100}-{i:03d}" for i in range(40)]  # 40 wide lines
        test_file.write_text("\n".join(original) + "\n", encoding="utf-8")

        # Page 1: byte cap clips after a whole number of lines
        page1 = await read_executor(str(test_file), max_bytes=1000, ctx=_CTX)
        assert page1["truncated"] is True
        # The marker contains a literal "offset=<that line>" grep example plus the real numeric
        # continuation offset; grab the numeric one.
        next_offset = max(int(m) for m in re.findall(r"offset=(\d+)", page1["marker"]))

        # Page 2: continue from the marker offset, large cap to grab the rest
        page2 = await read_executor(
            str(test_file), offset=next_offset, limit=100, max_bytes=200_000, ctx=_CTX
        )

        combined = page1["content"].splitlines() + page2["content"].splitlines()
        assert combined == original  # no gap, no duplication


# ---------------------------------------------------------------------------
# Empty file & head-mode without trailing newline (FRE-410)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_empty_file() -> None:
    """An empty file returns empty content with no truncation or marker."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "empty.txt"
        test_file.write_text("", encoding="utf-8")

        result = await read_executor(str(test_file), ctx=_CTX)

        assert result["success"] is True
        assert result["content"] == ""
        assert result["lines_returned"] == 0
        assert result["total_lines"] == 0
        assert result["truncated"] is False
        assert result["marker"] is None


@pytest.mark.asyncio
async def test_read_head_no_trailing_newline() -> None:
    """Head mode preserves a final line that has no trailing newline."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "nonl.txt"
        test_file.write_bytes(b"alpha\nbeta\ngamma")  # no trailing newline

        result = await read_executor(str(test_file), ctx=_CTX)

        assert result["success"] is True
        assert result["truncated"] is False
        assert result["content"].splitlines() == ["alpha", "beta", "gamma"]
        assert result["total_lines"] == 3


# ---------------------------------------------------------------------------
# tail mode default cap unchanged — no log regression (FRE-410 AC#3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_tail_default_cap_unchanged() -> None:
    """tail_lines without max_bytes keeps the 1 MiB cap (does not clip at 8 KB)."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "big.log"
        # 200 lines of 100 chars = ~20KB, over the 8KB head cap but under 1 MiB
        test_file.write_text("\n".join("L" * 100 for _ in range(200)) + "\n", encoding="utf-8")

        result = await read_executor(str(test_file), tail_lines=200, ctx=_CTX)

        assert result["success"] is True
        assert result["truncated"] is False
        assert len(result["content"].splitlines()) == 200


# ---------------------------------------------------------------------------
# Oversized single line: clipped, marker points to larger max_bytes (FRE-410)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_oversized_single_line_marker_recovers() -> None:
    """A single line over the byte cap is clipped; marker explains max_bytes recovery."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "giant.txt"
        giant = "q" * 5000  # one 5000-char line, no newline
        test_file.write_text(giant, encoding="utf-8")

        clipped = await read_executor(str(test_file), max_bytes=1000, ctx=_CTX)
        assert clipped["success"] is True
        assert clipped["truncated"] is True
        assert clipped["lines_returned"] == 1
        assert len(clipped["content"].encode("utf-8")) <= 1000
        # marker must tell the reader the line was clipped and how to recover it
        assert "max_bytes" in clipped["marker"]

        # Recovery path: a larger max_bytes returns the full line
        full = await read_executor(str(test_file), max_bytes=200_000, ctx=_CTX)
        assert full["truncated"] is False
        assert full["content"] == giant


# ---------------------------------------------------------------------------
# Non-positive limit is clamped to >= 1 — never skips content (FRE-410)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_limit_zero_clamped() -> None:
    """limit=0 is clamped so the first windowed line is still returned (no skip)."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "lines.txt"
        test_file.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")

        result = await read_executor(str(test_file), offset=3, limit=0, ctx=_CTX)

        assert result["success"] is True
        assert result["lines_returned"] >= 1
        # the line at the requested offset must be present, not skipped
        assert result["content"].splitlines()[0] == "line3"
