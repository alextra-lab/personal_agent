"""FRE-1593 (ADR-0156 A1): no live file names the old skills folder.

The runtime skills moved from ``docs/skills/`` to ``skills/``. A test or script that still reads
the old folder finds nothing and passes without checking anything, so this check fails on a
scanned file that names the old path.

It finds the literal ``docs/skills`` and the join forms that name both parts as strings:
``"docs" / "skills"``, ``Path("docs") / "skills"`` and ``join("docs", "skills")``. It cannot find
a path that a variable builds, such as ``DOCS.glob("skills/*")``. A test that scans the skills
folder must assert that its file set is not empty, so that form fails there.

The check scans ``src/``, ``config/``, ``scripts/``, ``tests/``, ``skills/``, ``docs/``,
``.env.example`` and every ``Dockerfile*`` and ``docker-compose*`` file. Three ``docs/``
directories hold history and are skipped. Every other exception is one exact file name.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

#: The literal path, and the join forms that name ``"docs"`` and ``"skills"`` as two strings:
#: ``/`` or ``,`` between them, an optional ``)`` after ``"docs"``, on one line or across lines.
_OLD_PATH = re.compile(r"docs/skills|[\"']docs[\"']\s*\)?\s*[,/]\s*[\"']skills[\"']")

#: Records of what was true at the time (ADR-0156 A1.4). They keep the old path.
_HISTORY_DIRS = (
    "docs/architecture_decisions/",
    "docs/research/",
    "docs/superpowers/plans/",
)
_SCANNED_DIRS = ("src/", "config/", "scripts/", "tests/", "skills/", "docs/")

#: Exact repository paths that may name the old folder. Each one needs a reason.
_EXCEPTIONS = frozenset(
    {
        # The pattern that this check searches for.
        "tests/docs/test_old_skills_path_guard.py",
        # Dated records of what was true when they were written.
        "docs/plans/completed/2026-05-22-completed-archive.md",
        "docs/superpowers/specs/2026-05-14-fre-343-personal-time-window-retrieval-design.md",
    }
)


def _in_scope(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    if rel.startswith(_HISTORY_DIRS):
        return False
    return (
        rel.startswith(_SCANNED_DIRS)
        or rel == ".env.example"
        or name.startswith(("Dockerfile", "docker-compose"))
    )


def find_old_path_references(root: Path, rel_paths: Iterable[str]) -> list[str]:
    """Return the in-scope files that name the old skills folder.

    Args:
        root: The directory that ``rel_paths`` are relative to.
        rel_paths: Repository-relative file paths, with ``/`` separators.

    Returns:
        The sorted paths that name the old folder and are not an exact-name exception.
    """
    hits: list[str] = []
    for rel in rel_paths:
        if not _in_scope(rel) or rel in _EXCEPTIONS:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # a binary file or a path that is gone
        if _OLD_PATH.search(text):
            hits.append(rel)
    return sorted(hits)


def _repo_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"git ls-files failed: {result.stderr}"
    return result.stdout.splitlines()


def _seed(root: Path, rel: str, text: str) -> list[str]:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return [rel]


def test_no_live_file_names_the_old_skills_folder() -> None:
    """No scanned file, tracked or new, names the old folder."""
    files = _repo_files()
    assert any(rel.startswith("skills/") for rel in files), "the skills/ folder holds no file"
    assert find_old_path_references(_REPO, files) == []


def test_the_old_folder_holds_no_tracked_file() -> None:
    """ADR-0156 A1.1: ``docs/skills/`` no longer holds a skill. A file there would never load."""
    assert [rel for rel in _repo_files() if rel.startswith("docs/skills/")] == []


@pytest.mark.parametrize(
    "rel",
    [
        "src/personal_agent/x.py",
        "config/governance/x.yaml",
        "scripts/eval/x.py",
        "tests/test_x.py",
        "skills/a/SKILL.md",
        ".env.example",
        "Dockerfile.gateway",
        "docker-compose.test.yml",
        "scripts/eval/fre1590/docker-compose.fre1590.yml",
        "docs/guides/x.md",
        "docs/plans/x.md",
        "docs/superpowers/specs/x.md",
    ],
)
def test_a_seeded_file_in_each_scanned_place_fails_the_check(tmp_path: Path, rel: str) -> None:
    """A file in each place that AC-2 lists makes the check fail."""
    seeded = _seed(tmp_path, rel, "see docs/skills/a/SKILL.md\n")
    assert find_old_path_references(tmp_path, seeded) == [rel]


@pytest.mark.parametrize(
    "text",
    [
        'P = "docs/skills/a/SKILL.md"\n',
        'P = ROOT / "docs" / "skills"\n',
        "P = ROOT / 'docs' / 'skills'\n",
        'P = (\n    ROOT\n    / "docs"\n    / "skills"\n)\n',
        'P = Path(__file__).resolve().parents[3] / "docs" / "skills"\n',
        'P = Path("docs") / "skills"\n',
        'P = Path("docs", "skills")\n',
        'P = os.path.join("docs", "skills")\n',
        'P = ROOT.joinpath("docs", "skills")\n',
    ],
)
def test_the_literal_and_the_join_form_are_both_found(tmp_path: Path, text: str) -> None:
    """The check finds the literal path and the join form, on one line and over several."""
    seeded = _seed(tmp_path, "scripts/x.py", text)
    assert find_old_path_references(tmp_path, seeded) == ["scripts/x.py"]


def test_the_new_path_is_not_a_hit(tmp_path: Path) -> None:
    """The new path and ``docs/external-skills`` are not hits."""
    text = 'P = ROOT / "skills"\nQ = "skills/a/SKILL.md"\nR = "docs/external-skills/b"\n'
    seeded = _seed(tmp_path, "scripts/x.py", text)
    assert find_old_path_references(tmp_path, seeded) == []


@pytest.mark.parametrize(
    "rel",
    [
        "docs/architecture_decisions/ADR-0001-x.md",
        "docs/research/x.md",
        "docs/superpowers/plans/x.md",
        "telemetry/evaluation/x/report.md",
    ],
)
def test_history_and_places_outside_the_scope_are_not_checked(tmp_path: Path, rel: str) -> None:
    """The three history directories and ``telemetry/`` keep the old path."""
    seeded = _seed(tmp_path, rel, "see docs/skills/a/SKILL.md\n")
    assert find_old_path_references(tmp_path, seeded) == []


def test_an_exception_covers_its_exact_file_name_only(tmp_path: Path) -> None:
    """A file next to an exception, with another name, still fails the check."""
    text = "see docs/skills/a/SKILL.md\n"
    named = _seed(tmp_path, "docs/plans/completed/2026-05-22-completed-archive.md", text)
    sibling = _seed(tmp_path, "docs/plans/completed/2026-05-23-other.md", text)
    assert find_old_path_references(tmp_path, named) == []
    assert find_old_path_references(tmp_path, sibling) == [sibling[0]]


@pytest.mark.parametrize("rel", sorted(_EXCEPTIONS))
def test_each_exception_exists_and_still_needs_its_exception(rel: str) -> None:
    """An exception for a file that is gone, or that is clean, must be removed."""
    path = _REPO / rel
    assert path.is_file(), f"{rel} is gone: remove its exception"
    assert _OLD_PATH.search(path.read_text(encoding="utf-8")), (
        f"{rel} no longer names the old folder: remove its exception"
    )
