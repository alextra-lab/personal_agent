"""The ``bash`` child keeps its workspace, and root tools refuse a planted link (FRE-1732).

These tests run with no root. ``fchown`` and ``chown`` are spied on, so they show which file each
call acts on. The kernel outcome as ``nobody`` is in
``scripts/eval/probe_bash_credential_isolation.py``, in the gateway image.
"""

from __future__ import annotations

import errno
import os
import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from personal_agent.telemetry import TraceContext
from personal_agent.tools.primitives import _child_workspace as workspace
from personal_agent.tools.primitives import bash as bash_module
from personal_agent.tools.primitives import edit as edit_module
from personal_agent.tools.primitives import write as write_module
from personal_agent.tools.primitives._governance import (
    make_directory_no_symlinks,
    open_file_no_symlinks,
    open_no_symlinks,
)
from personal_agent.tools.primitives.edit import EditError, edit_executor
from personal_agent.tools.primitives.read import ReadError, read_executor
from personal_agent.tools.primitives.write import WriteError, write_executor

_CTX = TraceContext.new_trace()


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[list[tuple[int, int]]]:
    """Report the inode of each ``fchown``, with the profile set to one where the child owns it."""
    calls: list[tuple[int, int]] = []

    def spy(fd: int, uid: int, gid: int) -> None:
        assert (uid, gid) == (workspace.CHILD_UID, workspace.CHILD_GID)
        calls.append((os.fstat(fd).st_dev, os.fstat(fd).st_ino))

    with (
        patch.object(workspace, "_child_owns_workspace", return_value=True),
        patch.object(workspace.os, "fchown", side_effect=spy),
    ):
        yield calls


def _ids(path: Path) -> tuple[int, int]:
    return (path.stat().st_dev, path.stat().st_ino)


def test_missing_parts_lists_what_a_call_will_create(tmp_path: Path) -> None:
    """Only the parts that do not exist, from the top down. An existing file gives nothing."""
    existing = tmp_path / "file.txt"
    existing.write_text("x")
    target = tmp_path / "new" / "deep" / "file.txt"

    assert workspace.missing_parts(target) == [
        tmp_path / "new",
        tmp_path / "new" / "deep",
        target,
    ]
    assert workspace.missing_parts(existing) == []


def test_give_to_child_acts_on_each_path(tmp_path: Path, owned: list[tuple[int, int]]) -> None:
    """Seeded positive for the refusals below: a plain file and directory change owner."""
    target = tmp_path / "project" / "file.txt"
    target.parent.mkdir()
    target.write_text("x")

    workspace.give_to_child([target.parent, target], trace_id="t", roots=(tmp_path,))

    assert owned == [_ids(target.parent), _ids(target)]


def test_give_to_child_leaves_a_root_and_a_path_outside_the_roots(
    tmp_path: Path, owned: list[tuple[int, int]]
) -> None:
    """``/tmp`` itself and ``/app/telemetry`` keep their owners."""
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other.txt"
    other.write_text("x")

    workspace.give_to_child([root, other], trace_id="t", roots=(root,))

    assert owned == []


@pytest.mark.parametrize("link_part", ["file", "directory"])
def test_give_to_child_does_not_follow_a_planted_link(
    link_part: str, tmp_path: Path, owned: list[tuple[int, int]]
) -> None:
    """A link in the workspace cannot give away the file behind it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("x")
    root = tmp_path / "root"
    root.mkdir()
    if link_part == "file":
        (root / "link").symlink_to(secret)
        path = root / "link"
    else:
        (root / "link").symlink_to(outside)
        path = root / "link" / "secret.txt"

    workspace.give_to_child([path], trace_id="t", roots=(root,))

    assert owned == []


def test_give_to_child_leaves_a_file_with_a_second_name(
    tmp_path: Path, owned: list[tuple[int, int]]
) -> None:
    """A hard link can name a file outside the roots, so it stays as it is."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    (root / "hard").hardlink_to(outside)

    workspace.give_to_child([root / "hard"], trace_id="t", roots=(root,))

    assert owned == []


def test_prepare_workspace_changes_the_tree_and_not_a_link_target(tmp_path: Path) -> None:
    """Startup acts on every entry, and never follows a link out of the tree."""
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    root = tmp_path / "root"
    (root / "project").mkdir(parents=True)
    (root / "project" / "file.txt").write_text("x")
    (root / "link").symlink_to(outside)
    calls: list[tuple[Path, bool]] = []

    def spy(path: Path, uid: int, gid: int, *, follow_symlinks: bool) -> None:
        assert (uid, gid) == (workspace.CHILD_UID, workspace.CHILD_GID)
        calls.append((Path(path), follow_symlinks))

    with (
        patch.object(workspace, "_child_owns_workspace", return_value=True),
        patch.object(workspace.os, "chown", side_effect=spy),
    ):
        workspace.prepare_workspace(root)

    assert {path for path, _ in calls} == {
        root,
        root / "project",
        root / "project" / "file.txt",
        root / "link",
    }
    assert not any(follow for _, follow in calls)


def test_prepare_workspace_leaves_an_owned_entry_and_a_second_name(tmp_path: Path) -> None:
    """An entry that the child owns needs no call, and a hard link can name another file."""
    (tmp_path / "plain.txt").write_text("x")
    (tmp_path / "hard.txt").write_text("x")
    (tmp_path / "hard2.txt").hardlink_to(tmp_path / "hard.txt")
    (tmp_path / "owned.txt").write_text("x")
    owned = (tmp_path / "owned.txt").stat()

    with (
        patch.object(workspace, "_child_owns_workspace", return_value=True),
        patch.object(workspace, "CHILD_IDS", (owned.st_uid, owned.st_gid)),
        patch.object(workspace.os, "chown") as chown,
    ):
        workspace.prepare_workspace(tmp_path)
    assert chown.call_count == 0  # every entry here is the "child's" or has a second name

    with (
        patch.object(workspace, "_child_owns_workspace", return_value=True),
        patch.object(workspace.os, "chown") as chown,
    ):
        workspace.prepare_workspace(tmp_path)
    changed = {Path(call.args[0]).name for call in chown.call_args_list}
    assert "plain.txt" in changed
    assert not changed & {"hard.txt", "hard2.txt"}


def test_prepare_workspace_goes_on_after_one_entry_fails(tmp_path: Path) -> None:
    """A file that vanishes in the walk does not leave the rest of the tree with root."""
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text("x")
    done: list[str] = []

    def chown(path: Path, *_args: object, **_kwargs: object) -> None:
        if Path(path).name == "b":
            raise FileNotFoundError(path)
        done.append(Path(path).name)

    with (
        patch.object(workspace, "_child_owns_workspace", return_value=True),
        patch.object(workspace.os, "chown", side_effect=chown),
    ):
        workspace.prepare_workspace(tmp_path)

    assert {"a", "c"} <= set(done)


@pytest.mark.parametrize("profile", ["local", "eval"])
def test_the_workspace_keeps_its_owner_on_other_profiles(profile: str, tmp_path: Path) -> None:
    """Only ``cloud`` changes an owner. ``eval`` keeps FRE-1505, and ``local`` has no account."""
    with (
        patch.object(workspace, "settings") as settings,
        patch.object(workspace.os, "geteuid", return_value=0),
        patch.object(workspace.os, "chown") as chown,
        patch.object(workspace.os, "fchown") as fchown,
    ):
        settings.deployment_profile = profile
        workspace.prepare_workspace(tmp_path)
        workspace.give_to_child([tmp_path / "x"], trace_id="t", roots=(tmp_path,))

    chown.assert_not_called()
    fchown.assert_not_called()


def test_the_workspace_keeps_its_owner_without_root(tmp_path: Path) -> None:
    """A gateway that is not root changes no owner."""
    with (
        patch.object(workspace, "settings") as settings,
        patch.object(workspace.os, "geteuid", return_value=1000),
        patch.object(workspace.os, "chown") as chown,
    ):
        settings.deployment_profile = "cloud"
        workspace.prepare_workspace(tmp_path)

    chown.assert_not_called()


# ---------------------------------------------------------------------------
# Root tools refuse a path that changed to a link after the checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("swap", ["file", "directory"])
def test_open_no_symlinks_refuses_a_link_in_any_part(swap: str, tmp_path: Path) -> None:
    """The last part and a middle part both refuse."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "file.txt").write_text("x")
    (tmp_path / "file_link").symlink_to(real / "file.txt")
    (tmp_path / "dir_link").symlink_to(real)
    path = tmp_path / "file_link" if swap == "file" else tmp_path / "dir_link" / "file.txt"

    with pytest.raises(OSError):
        open_no_symlinks(path, os.O_RDONLY)


def test_open_no_symlinks_opens_a_plain_path(tmp_path: Path) -> None:
    """Seeded positive: the same call opens the file when no link is in the path."""
    target = tmp_path / "file.txt"
    target.write_text("plain")

    with os.fdopen(open_no_symlinks(target, os.O_RDONLY), "rb") as fh:
        assert fh.read() == b"plain"


def _swap_after_the_check(target: Path, replacement: Path) -> object:
    """Replace ``target`` with a link at the moment that the path check returns."""

    def side_effect(*_args: object, **_kwargs: object) -> None:
        target.unlink()
        target.symlink_to(replacement)

    return side_effect


@pytest.mark.asyncio
async def test_read_refuses_a_file_that_changes_to_a_link_after_the_check(
    tmp_path: Path,
) -> None:
    """The read returns no byte of the file behind the link."""
    secret = tmp_path / "secret.txt"
    secret.write_text("fre1732-secret-text")
    target = tmp_path / "target.txt"
    target.write_text("plain")

    with (
        patch(
            "personal_agent.tools.primitives.read._check_path_governance",
            side_effect=_swap_after_the_check(target, secret),
        ),
        pytest.raises(ReadError) as refused,
    ):
        await read_executor(str(target), ctx=_CTX)

    assert "fre1732-secret-text" not in str(refused.value)


@pytest.mark.asyncio
async def test_seeded_a_plain_open_follows_the_same_swap(tmp_path: Path) -> None:
    """Seeded negative: with the old open, the same swap returns the file behind the link."""
    secret = tmp_path / "secret.txt"
    secret.write_text("fre1732-secret-text")
    target = tmp_path / "target.txt"
    target.write_text("plain")

    def old_open(path: Path, flags: int, mode: int = 0o666, **_kwargs: object) -> int:
        return os.open(path, flags, mode)

    with (
        patch(
            "personal_agent.tools.primitives.read._check_path_governance",
            side_effect=_swap_after_the_check(target, secret),
        ),
        patch("personal_agent.tools.primitives.read.open_file_no_symlinks", old_open),
    ):
        result = await read_executor(str(target), ctx=_CTX)

    assert "fre1732-secret-text" in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["overwrite", "append"])
async def test_write_refuses_a_file_that_changes_to_a_link_after_the_check(
    mode: str, tmp_path: Path
) -> None:
    """The write does not change the file behind the link."""
    other = tmp_path / "other.txt"
    other.write_text("unchanged")
    target = tmp_path / "target.txt"
    target.write_text("plain")

    with (
        patch(
            "personal_agent.tools.primitives.write._check_path_governance",
            side_effect=_swap_after_the_check(target, other),
        ),
        patch("personal_agent.tools.primitives.write._check_durability"),
        pytest.raises(WriteError),
    ):
        await write_executor(str(target), "changed", mode=mode, ctx=_CTX)

    assert other.read_text() == "unchanged"


def test_open_no_symlinks_makes_missing_directories_and_refuses_a_link(tmp_path: Path) -> None:
    """``make_parents`` creates plain directories. It does not create through a link."""
    made = tmp_path / "new" / "deep" / "file.txt"
    os.close(open_no_symlinks(made, os.O_WRONLY | os.O_CREAT, make_parents=True))
    assert made.is_file()

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside)

    with pytest.raises(OSError):
        open_no_symlinks(
            tmp_path / "link" / "sub" / "file.txt", os.O_WRONLY | os.O_CREAT, make_parents=True
        )

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_seeded_a_plain_write_follows_the_same_swap(tmp_path: Path) -> None:
    """Seeded negative: with the old open, the same swap changes the file behind the link."""
    other = tmp_path / "other.txt"
    other.write_text("unchanged")
    target = tmp_path / "target.txt"
    target.write_text("plain")

    def old_open(path: Path, flags: int, mode: int = 0o666, **_kwargs: object) -> int:
        return os.open(path, flags, mode)

    with (
        patch(
            "personal_agent.tools.primitives.write._check_path_governance",
            side_effect=_swap_after_the_check(target, other),
        ),
        patch("personal_agent.tools.primitives.write._check_durability"),
        patch("personal_agent.tools.primitives.write.open_file_no_symlinks", old_open),
    ):
        await write_executor(str(target), "changed", ctx=_CTX)

    assert other.read_text() == "changed"


def _project_with_a_swap(tmp_path: Path) -> tuple[Path, Path, object]:
    """Make ``project/file.txt`` and an ``other/file.txt``, and a hook that swaps ``project``."""
    project = tmp_path / "project"
    project.mkdir()
    target = project / "file.txt"
    target.write_text("one two")
    other = tmp_path / "other"
    other.mkdir()
    protected = other / "file.txt"
    protected.write_text("protected")
    real_match = edit_module._match_offsets

    def swap_after_the_read(text: str, old: str, limit: int = 2) -> list[int]:
        project.rename(tmp_path / "project.real")
        project.symlink_to(other)
        return real_match(text, old, limit)

    return target, protected, swap_after_the_read


@pytest.mark.asyncio
async def test_edit_refuses_a_directory_that_changes_to_a_link_after_the_read(
    tmp_path: Path,
) -> None:
    """Root does not replace the same-named file in the directory behind the link."""
    target, protected, swap = _project_with_a_swap(tmp_path)

    with (
        patch("personal_agent.tools.primitives.edit._check_path_governance"),
        patch("personal_agent.tools.primitives.edit._check_durability"),
        patch("personal_agent.tools.primitives.edit._match_offsets", side_effect=swap),
        pytest.raises(EditError),
    ):
        await edit_executor(str(target), "one", "PAYLOAD", ctx=_CTX)

    assert protected.read_text() == "protected"
    assert list(protected.parent.iterdir()) == [protected]


@pytest.mark.asyncio
async def test_seeded_a_replace_by_path_follows_the_same_swap(tmp_path: Path) -> None:
    """Seeded negative: the old replace by path writes through the link."""
    target, protected, swap = _project_with_a_swap(tmp_path)

    def old_replace(path: Path, data: bytes) -> None:
        temp = path.parent / ".temp"
        temp.write_bytes(data)
        os.replace(temp, path)

    with (
        patch("personal_agent.tools.primitives.edit._check_path_governance"),
        patch("personal_agent.tools.primitives.edit._check_durability"),
        patch("personal_agent.tools.primitives.edit._match_offsets", side_effect=swap),
        patch("personal_agent.tools.primitives.edit._replace_file", old_replace),
    ):
        await edit_executor(str(target), "one", "PAYLOAD", ctx=_CTX)

    assert protected.read_text() == "PAYLOAD two"


@pytest.mark.asyncio
async def test_edit_keeps_permission_bits_and_drops_a_setuid_bit(tmp_path: Path) -> None:
    """The new file has the old mode, with no setuid bit for root to create."""
    target = tmp_path / "file.txt"
    target.write_text("one two")
    target.chmod(0o4750)

    with (
        patch("personal_agent.tools.primitives.edit._check_path_governance"),
        patch("personal_agent.tools.primitives.edit._check_durability"),
    ):
        await edit_executor(str(target), "one", "three", ctx=_CTX)

    assert target.read_text() == "three two"
    assert target.stat().st_mode & 0o7777 == 0o750
    assert [path.name for path in tmp_path.iterdir()] == ["file.txt"]


_BIG_OUTPUT = "head -c 60000 /dev/zero | tr '\\0' a"


@pytest.fixture
def overflow_root(tmp_path: Path) -> Iterator[Path]:
    """Send the overflow file to a test directory, in place of ``/tmp/agent_scratch``."""
    root = tmp_path / "agent_scratch"
    with patch.object(bash_module, "_OVERFLOW_ROOT", root):
        yield root


@pytest.mark.asyncio
async def test_the_overflow_file_is_written_when_no_link_is_there(overflow_root: Path) -> None:
    """Seeded positive: the normal path still works, and the file holds the full output."""
    result = await bash_module.bash_executor(_BIG_OUTPUT, ctx=_CTX)

    assert result["truncated_path"] == str(overflow_root / _CTX.trace_id / "bash_output_0.txt")
    assert Path(str(result["truncated_path"])).stat().st_size == 60000


@pytest.mark.asyncio
async def test_the_overflow_write_does_not_follow_a_planted_file_link(
    overflow_root: Path, tmp_path: Path
) -> None:
    """A link at the file's name leaves the file behind it unchanged."""
    protected = tmp_path / "protected.txt"
    protected.write_text("protected")
    trace_dir = overflow_root / _CTX.trace_id
    trace_dir.mkdir(parents=True)
    (trace_dir / "bash_output_0.txt").symlink_to(protected)

    # The name is one past the highest, so this call does not meet the link.
    result = await bash_module.bash_executor(_BIG_OUTPUT, ctx=_CTX)

    assert protected.read_text() == "protected"
    assert result["truncated_path"] == str(trace_dir / "bash_output_1.txt")

    # A link at that exact name refuses, at the open.
    with patch.object(bash_module, "_OVERFLOW_NAME", re.compile(r"no-match")):
        (trace_dir / "bash_output_1.txt").unlink()
        (trace_dir / "bash_output_0.txt").unlink()
        (trace_dir / "bash_output_0.txt").symlink_to(protected)
        result = await bash_module.bash_executor(_BIG_OUTPUT, ctx=_CTX)

    assert protected.read_text() == "protected"
    assert result["truncated_path"] == "<truncated: scratch write failed>"


@pytest.mark.asyncio
async def test_a_gap_in_the_overflow_numbers_does_not_lose_the_output(overflow_root: Path) -> None:
    """A child that removes ``bash_output_0.txt`` does not stop the next overflow file."""
    trace_dir = overflow_root / _CTX.trace_id
    trace_dir.mkdir(parents=True)
    (trace_dir / "bash_output_1.txt").write_text("kept")

    result = await bash_module.bash_executor(_BIG_OUTPUT, ctx=_CTX)

    assert result["truncated_path"] == str(trace_dir / "bash_output_2.txt")
    assert (trace_dir / "bash_output_1.txt").read_text() == "kept"


@pytest.mark.asyncio
@pytest.mark.parametrize(("profile", "kept"), [("local", True), ("cloud", False)])
async def test_a_link_in_the_overflow_root_resolves_only_on_local(
    profile: str, kept: bool, tmp_path: Path
) -> None:
    """``/tmp`` is a link on macOS. On a deployed gateway, a link refuses."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)

    with (
        patch.object(bash_module, "_OVERFLOW_ROOT", tmp_path / "link" / "agent_scratch"),
        patch.object(bash_module, "settings") as bash_settings,
        patch.object(workspace, "settings") as workspace_settings,
        patch.object(bash_module.os, "geteuid", return_value=0),
        patch.object(bash_module, "CHILD_SETPRIV_ARGV", ()),
    ):
        bash_settings.deployment_profile = profile
        workspace_settings.deployment_profile = profile
        result = await bash_module.bash_executor(_BIG_OUTPUT, ctx=_CTX)

    assert (result["truncated_path"] != "<truncated: scratch write failed>") is kept
    assert (real / "agent_scratch").exists() is kept


@pytest.mark.asyncio
async def test_the_overflow_write_does_not_follow_a_planted_directory_link(
    overflow_root: Path, tmp_path: Path
) -> None:
    """A link at the trace directory writes nothing in the directory behind it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    overflow_root.mkdir()
    (overflow_root / _CTX.trace_id).symlink_to(outside)

    result = await bash_module.bash_executor(_BIG_OUTPUT, ctx=_CTX)

    assert list(outside.iterdir()) == []
    assert result["truncated_path"] == "<truncated: scratch write failed>"


@pytest.mark.asyncio
async def test_edit_holds_one_directory_through_a_swap_during_the_replace(tmp_path: Path) -> None:
    """A swap inside the replace changes nothing: all steps use the directory that was opened."""
    project = tmp_path / "project"
    project.mkdir()
    target = project / "file.txt"
    target.write_text("one two")
    other = tmp_path / "other"
    other.mkdir()
    protected = other / "file.txt"
    protected.write_text("protected")
    real_token = edit_module.secrets.token_hex

    def swap_then_name(size: int) -> str:
        project.rename(tmp_path / "project.real")
        project.symlink_to(other)
        return real_token(size)

    with (
        patch("personal_agent.tools.primitives.edit._check_path_governance"),
        patch("personal_agent.tools.primitives.edit._check_durability"),
        patch.object(edit_module.secrets, "token_hex", side_effect=swap_then_name),
    ):
        await edit_executor(str(target), "one", "three", ctx=_CTX)

    assert protected.read_text() == "protected"
    assert (tmp_path / "project.real" / "file.txt").read_text() == "three two"


@pytest.mark.asyncio
async def test_edit_keeps_the_owner_of_the_file(tmp_path: Path) -> None:
    """The new file gets the old owner. As root, the replace would otherwise take it from the child."""
    target = tmp_path / "file.txt"
    target.write_text("one two")
    status = target.stat()

    with (
        patch("personal_agent.tools.primitives.edit._check_path_governance"),
        patch("personal_agent.tools.primitives.edit._check_durability"),
        patch.object(edit_module.os, "fchown") as fchown,
    ):
        await edit_executor(str(target), "one", "three", ctx=_CTX)

    assert fchown.call_args.args[1:] == (status.st_uid, status.st_gid)


@pytest.mark.asyncio
async def test_write_gives_only_what_this_call_created(tmp_path: Path) -> None:
    """A new file and its new directories go to the child. An existing file does not."""
    target = tmp_path / "new" / "deep" / "file.txt"

    with (
        patch("personal_agent.tools.primitives.write._check_path_governance"),
        patch("personal_agent.tools.primitives.write._check_durability"),
        patch("personal_agent.tools.primitives.write.give_to_child") as gave,
    ):
        await write_executor(str(target), "one", ctx=_CTX)
        first = gave.call_args.args[0]
        await write_executor(str(target), "two", ctx=_CTX)
        second = gave.call_args.args[0]

    root = tmp_path.resolve()
    assert first == [root / "new", root / "new" / "deep", root / "new" / "deep" / "file.txt"]
    assert second == []
    assert target.read_text() == "two"


@pytest.mark.asyncio
async def test_a_failed_write_still_gives_what_it_created(tmp_path: Path) -> None:
    """A retry finds these parts in place, so it cannot give them. AC-6 needs them now."""
    target = tmp_path / "new" / "deep" / "file.txt"

    with (
        patch("personal_agent.tools.primitives.write._check_path_governance"),
        patch("personal_agent.tools.primitives.write._check_durability"),
        patch("personal_agent.tools.primitives.write.give_to_child") as gave,
        pytest.raises(UnicodeEncodeError),
    ):
        await write_executor(str(target), "lone surrogate \udc80", ctx=_CTX)

    root = tmp_path.resolve()
    assert gave.call_args.args[0] == [
        root / "new",
        root / "new" / "deep",
        root / "new" / "deep" / "file.txt",
    ]


def test_give_to_child_skips_a_part_that_was_not_created(
    tmp_path: Path, owned: list[tuple[int, int]]
) -> None:
    """A write that failed early leaves no warning about a path that does not exist."""
    workspace.give_to_child([tmp_path / "missing"], trace_id="t", roots=(tmp_path,))

    assert owned == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "number", [errno.ELOOP, errno.ENOTDIR, errno.EINVAL, errno.ENXIO, errno.EMLINK]
)
async def test_a_changed_path_is_not_retryable_in_write_and_edit(
    number: int, tmp_path: Path
) -> None:
    """A retry races the same change, so the model is told not to retry."""
    target = tmp_path / "file.txt"
    target.write_text("one two")
    changed = OSError(number, "changed")

    with (
        patch("personal_agent.tools.primitives.write._check_path_governance"),
        patch("personal_agent.tools.primitives.write._check_durability"),
        patch("personal_agent.tools.primitives.write.open_file_no_symlinks", side_effect=changed),
        pytest.raises(WriteError) as refused_write,
    ):
        await write_executor(str(target), "new", ctx=_CTX)
    assert refused_write.value.code == "path_changed"
    assert refused_write.value.retryable is False

    with (
        patch("personal_agent.tools.primitives.edit._check_path_governance"),
        patch("personal_agent.tools.primitives.edit._check_durability"),
        patch("personal_agent.tools.primitives.edit.open_file_no_symlinks", side_effect=changed),
        pytest.raises(EditError) as refused_read,
    ):
        await edit_executor(str(target), "one", "three", ctx=_CTX)
    assert refused_read.value.code == "path_changed"
    assert refused_read.value.retryable is False

    with (
        patch("personal_agent.tools.primitives.edit._check_path_governance"),
        patch("personal_agent.tools.primitives.edit._check_durability"),
        patch("personal_agent.tools.primitives.edit._replace_file", side_effect=changed),
        pytest.raises(EditError) as refused_replace,
    ):
        await edit_executor(str(target), "one", "three", ctx=_CTX)
    assert refused_replace.value.code == "path_changed"
    assert refused_replace.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["overwrite", "append"])
async def test_write_opens_an_existing_file_with_no_create_flag(mode: str, tmp_path: Path) -> None:
    """The kernel refuses O_CREAT on another user's file in sticky ``/tmp``, even for root."""
    target = tmp_path / "file.txt"
    target.write_text("old")
    flags_seen: list[int] = []
    real_open = write_module.open_file_no_symlinks

    def spy(path: Path, flags: int, *args: object, **kwargs: object) -> int:
        flags_seen.append(flags)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch("personal_agent.tools.primitives.write._check_path_governance"),
        patch("personal_agent.tools.primitives.write._check_durability"),
        patch("personal_agent.tools.primitives.write.open_file_no_symlinks", spy),
    ):
        await write_executor(str(target), "new", mode=mode, ctx=_CTX)

    assert len(flags_seen) == 1
    assert not flags_seen[0] & os.O_CREAT


@pytest.mark.parametrize("flags", [os.O_RDONLY, os.O_WRONLY])
def test_a_planted_fifo_does_not_block_the_open(flags: int, tmp_path: Path) -> None:
    """Read and write refuse a FIFO at once. A blocking open would stop the event loop."""
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    baseline = len(os.listdir("/proc/self/fd"))

    with pytest.raises(OSError):
        open_file_no_symlinks(fifo, flags)

    assert len(os.listdir("/proc/self/fd")) == baseline


def test_a_directory_is_refused_and_leaks_no_descriptor(tmp_path: Path) -> None:
    """A file that changes to a directory is refused before ``os.fdopen`` can leave it open."""
    baseline = len(os.listdir("/proc/self/fd"))

    with pytest.raises(OSError):
        open_file_no_symlinks(tmp_path, os.O_RDONLY)

    assert len(os.listdir("/proc/self/fd")) == baseline


@pytest.mark.parametrize("flags", [os.O_RDONLY, os.O_WRONLY])
def test_a_file_with_a_second_name_is_refused_and_leaks_no_descriptor(
    flags: int, tmp_path: Path
) -> None:
    """A hard link to a file that root acts on is refused, and a plain file still opens."""
    plain = tmp_path / "plain.txt"
    plain.write_text("plain")
    linked = tmp_path / "linked.txt"
    linked.write_text("linked")
    os.link(linked, tmp_path / "second_name.txt")
    baseline = len(os.listdir("/proc/self/fd"))

    os.close(open_file_no_symlinks(plain, flags))
    with pytest.raises(OSError) as raised:
        open_file_no_symlinks(linked, flags)

    assert raised.value.errno == errno.EMLINK
    assert len(os.listdir("/proc/self/fd")) == baseline


def test_a_refused_file_with_a_second_name_is_not_emptied(tmp_path: Path) -> None:
    """``O_TRUNC`` runs after the check: the refusal leaves the shared content in place."""
    linked = tmp_path / "linked.txt"
    linked.write_text("root content")
    os.link(linked, tmp_path / "second_name.txt")
    plain = tmp_path / "plain.txt"
    plain.write_text("old content")

    with pytest.raises(OSError):
        open_file_no_symlinks(linked, os.O_WRONLY | os.O_TRUNC)
    os.close(open_file_no_symlinks(plain, os.O_WRONLY | os.O_TRUNC))

    assert linked.read_text() == "root content"
    assert plain.read_text() == ""


@pytest.mark.asyncio
async def test_read_names_a_second_name_when_it_refuses(tmp_path: Path) -> None:
    """The refusal text for ``read`` does not claim that the path changed only."""
    target = tmp_path / "file.txt"
    target.write_text("one two")
    os.link(target, tmp_path / "second_name.txt")

    with (
        patch("personal_agent.tools.primitives.read._check_path_governance"),
        pytest.raises(ReadError) as refused,
    ):
        await read_executor(str(target), ctx=_CTX)

    assert refused.value.retryable is False
    assert "second name" in refused.value.message


@pytest.mark.asyncio
async def test_read_write_and_edit_still_work_on_a_plain_file(tmp_path: Path) -> None:
    """The second-name refusal does not stop the three tools on a normal file."""
    target = tmp_path / "file.txt"
    target.write_text("one two")
    with (
        patch("personal_agent.tools.primitives.read._check_path_governance"),
        patch("personal_agent.tools.primitives.write._check_path_governance"),
        patch("personal_agent.tools.primitives.write._check_durability"),
        patch("personal_agent.tools.primitives.edit._check_path_governance"),
        patch("personal_agent.tools.primitives.edit._check_durability"),
    ):
        await write_executor(str(target), "one two three", ctx=_CTX)
        await edit_executor(str(target), "three", "four", ctx=_CTX)
        result = await read_executor(str(target), ctx=_CTX)

    assert target.read_text() == "one two four"
    assert "one two four" in str(result)


def test_make_directory_no_symlinks_refuses_a_link(tmp_path: Path) -> None:
    """The scratch directory is not created through a planted link."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside)

    with pytest.raises(OSError):
        make_directory_no_symlinks(tmp_path / "link" / "trace")

    assert list(outside.iterdir()) == []
    make_directory_no_symlinks(tmp_path / "plain" / "trace")
    assert (tmp_path / "plain" / "trace").is_dir()


@pytest.mark.asyncio
async def test_the_overflow_write_gives_what_it_created(overflow_root: Path) -> None:
    """The child can use the scratch directory and the overflow file that root made."""
    with patch.object(bash_module, "give_to_child") as gave:
        result = await bash_module.bash_executor(_BIG_OUTPUT, ctx=_CTX)

    trace_dir = overflow_root / _CTX.trace_id
    assert gave.call_args.args[0] == [
        overflow_root,
        trace_dir,
        Path(str(result["truncated_path"])),
    ]


@pytest.mark.asyncio
async def test_the_sandbox_scratch_directory_goes_to_the_child(tmp_path: Path) -> None:
    """The child can write in the directory that ``run_in_sandbox`` creates."""
    from personal_agent.tools.primitives import sandbox

    scratch = tmp_path / "sandbox" / "trace"

    with (
        patch.object(sandbox, "give_to_child") as gave,
        patch.object(sandbox.asyncio, "create_subprocess_exec", side_effect=FileNotFoundError),
    ):
        await sandbox.run_in_sandbox(
            image="x", script="1", timeout_seconds=1, network=False, scratch_host_path=scratch
        )

    assert gave.call_args.args[0] == [tmp_path / "sandbox", scratch]


def test_the_sandbox_lists_no_files_behind_a_link(tmp_path: Path) -> None:
    """A planted scratch link shows no file name from another directory."""
    from personal_agent.tools.primitives import sandbox

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("x")
    (outside / "subdir").mkdir()
    (outside / "file_link").symlink_to(outside / "private.txt")
    (tmp_path / "link").symlink_to(outside)

    with patch.object(workspace, "settings") as settings:
        settings.deployment_profile = "cloud"
        assert sandbox._list_scratch_files(tmp_path / "link") == []
        assert sandbox._list_scratch_files(outside) == [str(outside / "private.txt")]
        assert sandbox._list_scratch_files(tmp_path / "missing") == []


def test_one_removed_file_does_not_lose_the_scratch_listing(tmp_path: Path) -> None:
    """The child can remove a file during the listing. The other names stay."""
    from personal_agent.tools.primitives import sandbox

    (tmp_path / "kept.txt").write_text("x")
    real_listdir = os.listdir

    with (
        patch.object(workspace, "settings") as settings,
        patch.object(
            sandbox.os,
            "listdir",
            side_effect=lambda fd: [*real_listdir(fd), "removed.txt"],
        ),
    ):
        settings.deployment_profile = "cloud"
        assert sandbox._list_scratch_files(tmp_path) == [str(tmp_path / "kept.txt")]


@pytest.mark.asyncio
async def test_read_reports_the_size_of_the_file_that_it_opened(tmp_path: Path) -> None:
    """The size comes from the open file, so a swap cannot show the size of another file."""
    target = tmp_path / "target.txt"
    target.write_text("plain\n")

    with patch.object(Path, "stat", side_effect=AssertionError("no stat by path for the size")):
        with (
            patch("personal_agent.tools.primitives.read._check_path_governance"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_file", return_value=True),
        ):
            result = await read_executor(str(target), ctx=_CTX)

    assert result["size_bytes"] == len("plain\n")


@pytest.mark.asyncio
async def test_a_failed_scratch_directory_still_gives_what_it_created(tmp_path: Path) -> None:
    """The next call finds the parents in place, so it cannot give them."""
    from personal_agent.tools.primitives import sandbox

    scratch = tmp_path / "sandbox" / "trace"

    with (
        patch.object(sandbox, "give_to_child") as gave,
        patch.object(sandbox, "make_directory_no_symlinks", side_effect=OSError(28, "no space")),
    ):
        result = await sandbox.run_in_sandbox(
            image="x", script="1", timeout_seconds=1, network=False, scratch_host_path=scratch
        )

    assert result.exit_code == 1
    assert gave.call_args.args[0] == [tmp_path / "sandbox", scratch]
