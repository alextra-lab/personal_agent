"""Tests for the primitive ``edit`` tool (ADR-0156 D7.1, FRE-1576).

AC-1: ``edit`` replaces exactly one match. Each case reads the file's bytes after the call: a
refused or failed edit must leave the file unchanged.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from personal_agent.config import load_governance_config
from personal_agent.governance.models import GovernanceConfig, ToolPolicy
from personal_agent.governance.permissions import decide_permission
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.primitives._governance import PathRefusalError
from personal_agent.tools.primitives.edit import EditError, edit_executor

_CTX = TraceContext.new_trace()
_ORIGINAL = "alpha\nbeta\ngamma\n"


def _file(tmp_path: Path, content: str | bytes = _ORIGINAL) -> Path:
    target = tmp_path / "notes.md"
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_bytes(content.encode("utf-8"))
    return target


async def _edit(target: Path | str, old: str, new: str) -> dict[str, object]:
    return await edit_executor(str(target), old_string=old, new_string=new, ctx=_CTX)


def _governance(policy: ToolPolicy) -> GovernanceConfig:
    config = GovernanceConfig.__new__(GovernanceConfig)
    object.__setattr__(config, "tools", {"edit": policy})
    return config


# ---------------------------------------------------------------------------
# AC-1: exactly one match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_match_changes_the_file_only_at_that_span(tmp_path: Path) -> None:
    """One match: the bytes are the original with only the matched span replaced."""
    target = _file(tmp_path)

    result = await _edit(target, "beta", "BETA two")

    assert target.read_bytes() == b"alpha\nBETA two\ngamma\n"
    assert result == {
        "path": str(target.resolve()),
        "line": 2,
        "bytes_written": len(b"alpha\nBETA two\ngamma\n"),
    }


@pytest.mark.asyncio
async def test_zero_matches_is_an_error_with_count_0_and_the_file_unchanged(
    tmp_path: Path,
) -> None:
    """Zero matches: ``no_match`` with count 0, and the bytes are unchanged."""
    target = _file(tmp_path)

    with pytest.raises(EditError) as exc_info:
        await _edit(target, "delta", "DELTA")

    assert exc_info.value.code == "no_match"
    assert "0 times" in exc_info.value.message
    assert exc_info.value.retryable is True
    assert target.read_bytes() == _ORIGINAL.encode()


@pytest.mark.asyncio
async def test_two_matches_is_an_error_with_count_2_and_the_file_unchanged(
    tmp_path: Path,
) -> None:
    """Two matches: ``multiple_matches`` with count 2, and the bytes are unchanged."""
    target = _file(tmp_path, "x = 1\ny = 2\nx = 1\n")

    with pytest.raises(EditError) as exc_info:
        await _edit(target, "x = 1", "x = 3")

    assert exc_info.value.code == "multiple_matches"
    assert "2 times" in exc_info.value.message
    assert "surrounding" in exc_info.value.next_action
    assert target.read_bytes() == b"x = 1\ny = 2\nx = 1\n"


@pytest.mark.asyncio
async def test_overlapping_matches_count_as_several(tmp_path: Path) -> None:
    """The string "aa" occurs twice in "aaa"; str.count would say once and hide it."""
    target = _file(tmp_path, "aaa\n")

    with pytest.raises(EditError) as exc_info:
        await _edit(target, "aa", "b")

    assert exc_info.value.code == "multiple_matches"
    assert "2 times" in exc_info.value.message
    assert target.read_bytes() == b"aaa\n"


@pytest.mark.asyncio
async def test_a_path_outside_the_write_roots_is_refused(tmp_path: Path) -> None:
    """The deployment's own edit row refuses /etc, as write's does."""
    with pytest.raises(PathRefusalError) as exc_info:
        await _edit("/etc/hosts", "localhost", "pwned")

    assert exc_info.value.code == "forbidden_path"


@pytest.mark.asyncio
async def test_a_path_outside_the_allowed_paths_is_refused_and_unchanged(tmp_path: Path) -> None:
    """A path outside the edit row's allowed paths is refused before the read."""
    target = _file(tmp_path)
    policy = ToolPolicy(
        category="system_write", allowed_in_modes=["NORMAL"], allowed_paths=["/nonexistent/**"]
    )

    with patch(
        "personal_agent.tools.primitives._governance.load_governance_config",
        return_value=_governance(policy),
    ):
        with pytest.raises(PathRefusalError) as exc_info:
            await _edit(target, "beta", "BETA")

    assert exc_info.value.code == "path_not_allowed"
    assert target.read_bytes() == _ORIGINAL.encode()


@pytest.mark.asyncio
async def test_a_non_durable_app_path_is_refused() -> None:
    """A path under /app outside a mounted volume is refused, as for write."""
    policy = ToolPolicy(
        category="system_write", allowed_in_modes=["NORMAL"], allowed_paths=["/app/**"]
    )

    with (
        patch(
            "personal_agent.tools.primitives._governance.load_governance_config",
            return_value=_governance(policy),
        ),
        patch(
            "personal_agent.tools.primitives._governance._mount_points",
            return_value=frozenset({"/app/agent_workspace", "/"}),
        ),
    ):
        with pytest.raises(PathRefusalError) as exc_info:
            await _edit("/app/src/personal_agent/config.py", "a", "b")

    assert exc_info.value.code == "not_durable"


# ---------------------------------------------------------------------------
# The other failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_old_string_is_refused(tmp_path: Path) -> None:
    """An empty string matches everywhere; it must not insert text at offset 0."""
    target = _file(tmp_path, "")

    with pytest.raises(EditError) as exc_info:
        await _edit(target, "", "inserted")

    assert exc_info.value.code == "empty_old_string"
    assert target.read_bytes() == b""


@pytest.mark.asyncio
async def test_a_missing_file_is_file_not_found(tmp_path: Path) -> None:
    """A missing file is ``file_not_found``, and the next action names write."""
    with pytest.raises(EditError) as exc_info:
        await _edit(tmp_path / "absent.md", "a", "b")

    assert exc_info.value.code == "file_not_found"
    assert "write" in exc_info.value.next_action


@pytest.mark.asyncio
async def test_a_directory_is_not_a_file(tmp_path: Path) -> None:
    """A directory is ``not_a_file``."""
    with pytest.raises(EditError) as exc_info:
        await _edit(tmp_path, "a", "b")

    assert exc_info.value.code == "not_a_file"


@pytest.mark.asyncio
async def test_a_binary_file_is_not_text(tmp_path: Path) -> None:
    """A file that is not UTF-8 is ``not_text``, and the bytes are unchanged."""
    target = _file(tmp_path, b"\xff\xfe\x00binary")

    with pytest.raises(EditError) as exc_info:
        await _edit(target, "binary", "text")

    assert exc_info.value.code == "not_text"
    assert target.read_bytes() == b"\xff\xfe\x00binary"


@pytest.mark.asyncio
async def test_crlf_line_endings_are_kept(tmp_path: Path) -> None:
    r"""The file's \r\n line endings survive the edit."""
    target = _file(tmp_path, b"one\r\ntwo\r\nthree\r\n")

    result = await _edit(target, "two", "TWO")

    assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\n"
    assert result["line"] == 2


@pytest.mark.asyncio
async def test_a_match_spanning_crlf_lines_is_found(tmp_path: Path) -> None:
    r"""The model copies text it read; read keeps \r\n, so old_string can hold it."""
    target = _file(tmp_path, b"one\r\ntwo\r\n")

    await _edit(target, "one\r\ntwo", "joined")

    assert target.read_bytes() == b"joined\r\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "code", "retryable"),
    [
        (PermissionError(13, "Permission denied"), "permission_denied", False),
        (OSError(28, "No space left on device"), "io_error", True),
    ],
)
async def test_a_failed_write_maps_to_its_code_and_keeps_the_file(
    tmp_path: Path, raised: OSError, code: str, retryable: bool
) -> None:
    """A write the OS refuses maps to its code, and the original bytes stay."""
    target = _file(tmp_path)

    with patch("personal_agent.tools.primitives.edit._replace_file", side_effect=raised):
        with pytest.raises(EditError) as exc_info:
            await _edit(target, "beta", "BETA")

    assert exc_info.value.code == code
    assert exc_info.value.retryable is retryable
    assert target.read_bytes() == _ORIGINAL.encode()


@pytest.mark.asyncio
async def test_the_replace_keeps_the_permission_bits(tmp_path: Path) -> None:
    """An executable script stays executable after an edit."""
    target = _file(tmp_path, "#!/bin/sh\necho one\n")
    target.chmod(0o755)

    await _edit(target, "echo one", "echo two")

    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.asyncio
async def test_a_successful_edit_leaves_no_temporary_file(tmp_path: Path) -> None:
    """The temporary file becomes the target; nothing else stays in the directory."""
    target = _file(tmp_path)

    await _edit(target, "beta", "BETA")

    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.asyncio
async def test_a_failed_replace_keeps_the_file_and_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    """os.replace failing removes the temporary file and keeps the original bytes."""
    target = _file(tmp_path)

    with patch(
        "personal_agent.tools.primitives.edit.os.replace",
        side_effect=OSError(28, "No space left on device"),
    ):
        with pytest.raises(EditError) as exc_info:
            await _edit(target, "beta", "BETA")

    assert exc_info.value.code == "io_error"
    assert target.read_bytes() == _ORIGINAL.encode()
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.asyncio
async def test_an_unreadable_file_is_permission_denied(tmp_path: Path) -> None:
    """A file the process cannot read is ``permission_denied``."""
    target = _file(tmp_path)

    with patch.object(Path, "read_bytes", side_effect=PermissionError(13, "Permission denied")):
        with pytest.raises(EditError) as exc_info:
            await _edit(target, "beta", "BETA")

    assert exc_info.value.code == "permission_denied"


# ---------------------------------------------------------------------------
# Same governance and rules as write
# ---------------------------------------------------------------------------


def test_the_edit_path_policy_is_write_s() -> None:
    """D7.1: edit has the path governance of write, field for field."""
    tools = load_governance_config().tools
    edit, write = tools["edit"], tools["write"]
    for field in (
        "allowed_in_modes",
        "forbidden_in_modes",
        "unattended_paths",
        "allowed_paths",
        "forbidden_paths",
        "loop_max_per_signature",
        "loop_max_consecutive",
    ):
        assert getattr(edit, field) == getattr(write, field), field


@pytest.mark.parametrize("mode", ["NORMAL", "ALERT", "DEGRADED", "RECOVERY", "LOCKDOWN"])
def test_the_edit_permission_rules_are_write_s(mode: str) -> None:
    """D7.1: edit asks and runs where write asks and runs."""
    args = {"path": "/app/agent_workspace/notes.md", "content": "x"}
    edit = decide_permission("edit", {**args, "old_string": "a", "new_string": "b"}, mode)
    write = decide_permission("write", args, mode)
    assert edit.decision == write.decision
