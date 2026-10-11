"""Tests for the skill loader in ``orchestrator/skills.py`` (ADR-0156 D1, D2; FRE-1577).

The loader reads ``<name>/SKILL.md`` folders, skips an invalid file with a warning, reloads when
a file changes, and renders a catalog that depends on the skill files only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import personal_agent.orchestrator.skills as skills_module
from personal_agent.orchestrator.skills import _load_all_skills, get_all_skills


def _write(root: Path, folder: str, frontmatter: str, body: str = "Body text.") -> Path:
    path = root / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\n{body}\n", encoding="utf-8")
    return path


class TestDiscovery:
    """The repository's own skills load."""

    def test_the_eleven_agent_skills_load(self) -> None:
        assert len(get_all_skills()) == 11

    def test_a_folded_skill_is_gone(self) -> None:
        skills = get_all_skills()
        assert "bash" not in skills
        assert "seshat-knowledge" not in skills  # an external skill is not the agent's

    def test_every_skill_has_a_description_and_a_body(self) -> None:
        for name, skill in get_all_skills().items():
            assert skill.description, name
            assert skill.body, name
            assert skill.folder.name == name


class TestInvalidFilesAreSkipped:
    """A file the CI validator would reject is skipped by the loader, not loaded wrong."""

    def test_a_valid_folder_loads(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "alpha",
            "name: alpha\ndescription: Checks a thing.\nmetadata:\n  requires-tools: bash, read\n",
        )
        cache = _load_all_skills(tmp_path)
        assert list(cache.docs) == ["alpha"]
        assert cache.docs["alpha"].requires_tools == ("bash", "read")
        assert cache.docs["alpha"].body == "Body text."

    def test_a_flat_file_is_not_a_skill(self, tmp_path: Path) -> None:
        (tmp_path / "alpha.md").write_text("---\nname: alpha\ndescription: d\n---\nbody\n")
        assert _load_all_skills(tmp_path).docs == {}

    @pytest.mark.parametrize(
        "frontmatter",
        ["description: no name\n", "name: other\ndescription: wrong folder\n", "name: alpha\n"],
    )
    def test_a_bad_frontmatter_is_skipped(self, tmp_path: Path, frontmatter: str) -> None:
        _write(tmp_path, "alpha", frontmatter)
        assert _load_all_skills(tmp_path).docs == {}

    def test_an_unreadable_file_logs_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = _write(tmp_path, "alpha", "name: alpha\ndescription: d\n")
        original = Path.read_text

        def failing_read(self: Path, *args: object, **kwargs: object) -> str:
            if self == bad:
                raise OSError("permission denied")
            return original(self, *args, **kwargs)  # type: ignore[arg-type]

        warnings: list[str] = []
        monkeypatch.setattr(Path, "read_text", failing_read)
        monkeypatch.setattr(skills_module.log, "warning", lambda event, **_: warnings.append(event))
        assert _load_all_skills(tmp_path).docs == {}
        assert warnings == ["skill_doc_unreadable"]


class TestCacheAndCatalog:
    """The cache follows the files; the catalog follows the cache."""

    def test_the_cache_reloads_after_a_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(skills_module, "SKILLS_DIR", tmp_path)
        monkeypatch.setattr(skills_module, "_cache", None)
        path = _write(tmp_path, "alpha", "name: alpha\ndescription: First text.\n")
        assert "First text." in skills_module.assemble_skill_catalog()

        _write(tmp_path, "alpha", "name: alpha\ndescription: Second text.\n")
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 1.0))
        assert "Second text." in skills_module.assemble_skill_catalog()

        _write(tmp_path, "beta", "name: beta\ndescription: Third text.\n")
        assert skills_module.skill_names() == ("alpha", "beta")

    def test_the_catalog_is_one_line_per_skill_without_bodies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(skills_module, "SKILLS_DIR", tmp_path)
        monkeypatch.setattr(skills_module, "_cache", None)
        _write(tmp_path, "beta", "name: beta\ndescription: >-\n  Checks\n  beta.\n", "BETA BODY")
        _write(tmp_path, "alpha", "name: alpha\ndescription: Checks alpha.\n", "ALPHA BODY")
        catalog = skills_module.assemble_skill_catalog()
        assert catalog.endswith("- alpha: Checks alpha.\n- beta: Checks beta.\n")
        assert "BODY" not in catalog
        assert "read_skill" in catalog

    def test_no_skill_gives_no_catalog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(skills_module, "SKILLS_DIR", tmp_path)
        monkeypatch.setattr(skills_module, "_cache", None)
        assert skills_module.assemble_skill_catalog() == ""
