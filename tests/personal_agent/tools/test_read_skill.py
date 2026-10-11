"""ADR-0156 D3 (FRE-1577 AC-3): ``read_skill(name, path?)`` returns only files of the skill folder.

No path returns the body and the list of the skill's files, without reading them. A path returns
that one listed file. An unknown name, a path out of the folder, an absolute path, a symlink, a
directory and a file that is not listed are refused with a contract error.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from personal_agent.orchestrator import skills
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.read_skill import (
    ReadSkillError,
    build_read_skill_tool,
    read_skill_executor,
)

_SECRET = "SECRET-OUTSIDE-THE-SKILL"


@pytest.fixture
def skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Two skills in a temporary folder, and a secret file beside the folder."""
    root = tmp_path / "skills"
    for name in ("alpha", "bash"):
        folder = root / name
        (folder / "scripts").mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Checks {name}.\n---\n\n# {name}\n\nBody of {name}.\n",
            encoding="utf-8",
        )
        (folder / "scripts" / "run.py").write_text(f"print('{name}')\n", encoding="utf-8")
    (root / "alpha" / "references").mkdir()
    (root / "alpha" / "references" / "guide.md").write_text("Guide text.\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text(_SECRET, encoding="utf-8")
    monkeypatch.setattr(skills, "SKILLS_DIR", root)
    monkeypatch.setattr(skills, "_cache", None)
    yield root
    skills._cache = None


async def _call(**kwargs: str) -> dict[str, object]:
    return await read_skill_executor(ctx=TraceContext.new_trace(), **kwargs)


async def _refused(**kwargs: str) -> ReadSkillError:
    with pytest.raises(ReadSkillError) as exc:
        await _call(**kwargs)
    assert _SECRET not in exc.value.message
    return exc.value


@pytest.mark.asyncio
async def test_no_path_returns_the_body_and_the_file_list(skills_dir: Path) -> None:
    result = await _call(name="alpha")
    assert result["skill_name"] == "alpha"
    assert "Body of alpha." in str(result["body"])
    assert result["files"] == ["references/guide.md", "scripts/run.py"]
    assert result["base_dir"] == str(skills_dir / "alpha")
    # S2: the files are listed, not read.
    assert "Guide text." not in str(result)


@pytest.mark.asyncio
async def test_a_listed_file_is_returned(skills_dir: Path) -> None:
    result = await _call(name="alpha", path="references/guide.md")
    assert result == {
        "skill_name": "alpha",
        "path": "references/guide.md",
        "content": "Guide text.\n",
    }


@pytest.mark.asyncio
async def test_an_unknown_name_is_refused(skills_dir: Path) -> None:
    error = await _refused(name="nonexistent")
    assert error.code == "unknown_skill"
    assert "alpha" in error.message and "bash" in error.message


@pytest.mark.asyncio
async def test_a_missing_name_is_refused(skills_dir: Path) -> None:
    assert (await _refused(name="")).code == "missing_name"


@pytest.mark.parametrize(
    "path",
    [
        "../bash/SKILL.md",
        "../../secret.txt",
        "scripts/../../bash/scripts/run.py",
        "SKILL.md",
        "scripts",
        "scripts/",
        "./scripts/run.py",
        "Scripts/run.py",
        "references/missing.md",
        "",
    ],
)
@pytest.mark.asyncio
async def test_a_path_that_is_not_listed_is_refused(skills_dir: Path, path: str) -> None:
    if path == "":
        # An empty path is "no path": the body comes back, not a file.
        assert "body" in await _call(name="alpha", path=path)
        return
    assert (await _refused(name="alpha", path=path)).code == "invalid_path"


@pytest.mark.asyncio
async def test_an_absolute_path_is_refused(skills_dir: Path) -> None:
    for path in (str(skills_dir / "alpha" / "scripts" / "run.py"), "/etc/passwd"):
        assert (await _refused(name="alpha", path=path)).code == "invalid_path"


@pytest.mark.asyncio
async def test_a_symlink_out_of_the_folder_is_refused(skills_dir: Path) -> None:
    folder = skills_dir / "alpha"
    (folder / "references" / "leak.md").symlink_to(skills_dir.parent / "secret.txt")
    (folder / "linked").symlink_to(skills_dir.parent, target_is_directory=True)
    assert "references/leak.md" not in (await _call(name="alpha"))["files"]
    for path in ("references/leak.md", "linked/secret.txt"):
        assert (await _refused(name="alpha", path=path)).code == "invalid_path"


@pytest.mark.asyncio
async def test_a_symlink_inside_the_folder_is_refused(skills_dir: Path) -> None:
    (skills_dir / "alpha" / "scripts" / "alias.py").symlink_to("run.py")
    assert (await _refused(name="alpha", path="scripts/alias.py")).code == "invalid_path"


@pytest.mark.asyncio
async def test_a_hard_link_is_refused(skills_dir: Path) -> None:
    os.link(
        skills_dir / "alpha" / "scripts" / "run.py", skills_dir / "alpha" / "scripts" / "hard.py"
    )
    assert (await _refused(name="alpha", path="scripts/hard.py")).code == "invalid_path"


def test_a_file_swapped_after_the_listing_is_refused(
    skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opened file must be the listed inode: a rename over it after the listing is refused."""
    folder = skills_dir / "alpha"
    listing = skills.list_skill_files(folder)
    target = folder / "scripts" / "run.py"
    replacement = folder / "scripts" / "new.py"
    replacement.write_text(_SECRET, encoding="utf-8")
    os.replace(replacement, target)
    monkeypatch.setattr(skills, "list_skill_files", lambda _folder: listing)
    with pytest.raises(skills.SkillFileRefused):
        skills.read_skill_file(folder, "scripts/run.py")


def test_a_parent_swapped_for_a_symlink_is_refused(
    skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listed directory replaced by a symlink after the listing is not followed."""
    folder = skills_dir / "alpha"
    listing = skills.list_skill_files(folder)
    outside = skills_dir.parent / "outside"
    (outside).mkdir()
    (outside / "run.py").write_text(_SECRET, encoding="utf-8")
    (folder / "scripts" / "run.py").unlink()
    (folder / "scripts").rmdir()
    (folder / "scripts").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(skills, "list_skill_files", lambda _folder: listing)
    with pytest.raises(skills.SkillFileRefused):
        skills.read_skill_file(folder, "scripts/run.py")


@pytest.mark.asyncio
async def test_a_file_that_is_not_text_is_refused(skills_dir: Path) -> None:
    (skills_dir / "alpha" / "references" / "logo.bin").write_bytes(b"\xff\xfe\x00")
    assert (await _refused(name="alpha", path="references/logo.bin")).code == "invalid_path"


def test_name_is_an_enum_of_the_skill_names(skills_dir: Path) -> None:
    """D2.2: the ``name`` parameter lists the valid names; ``path`` is optional."""
    tool = build_read_skill_tool()
    params = {p.name: p for p in tool.parameters}
    assert params["name"].json_schema is not None
    assert params["name"].json_schema["enum"] == ["alpha", "bash"]
    assert params["name"].required is True
    assert params["path"].required is False
    assert "invalid_path" in tool.error_codes


def test_no_skill_means_no_enum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty ``enum`` is invalid JSON Schema; with no skill folder the field has none."""
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills, "_cache", None)
    schema = build_read_skill_tool().parameters[0].json_schema
    assert schema is not None and "enum" not in schema
    skills._cache = None
