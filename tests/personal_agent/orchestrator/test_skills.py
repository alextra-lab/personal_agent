"""Tests for the skills.py skill-doc loader and get_skill_block gate.

Phase A: frontmatter-driven auto-discovery replaces hardcoded _SKILL_FILES
and _KEYWORD_ROUTES.  Tests validate the new API surface:
  - _load_all_skills() / _get_cache()
  - get_skill_block(message=...)
  - find_skill_for_tool()
  - get_all_skills()
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import personal_agent.orchestrator.skills as skills_module
from personal_agent.config import settings
from personal_agent.orchestrator.skills import (
    _load_all_skills,
    find_skill_for_tool,
    get_all_skills,
    get_skill_block,
)


class TestSkillDiscovery:
    """Test 1: Auto-discovery loads skill docs from docs/skills/."""

    def test_all_skills_nonempty(self) -> None:
        """get_all_skills() returns at least one skill (production docs present)."""
        skills = get_all_skills()
        assert len(skills) > 0, "No skills discovered — frontmatter parse failed?"

    def test_bash_skill_present(self) -> None:
        """The 'bash' skill is always discovered and has a body."""
        skills = get_all_skills()
        assert "bash" in skills, "'bash' skill not found"
        assert skills["bash"].body, "'bash' skill body is empty"

    def test_bash_skill_contains_expected_content(self) -> None:
        """The 'bash' skill body contains the canonical anchor text."""
        skills = get_all_skills()
        assert "bash" in skills["bash"].body.lower()
        assert "bash — Shell Command Executor" in skills["bash"].body

    def test_skills_have_required_fields(self) -> None:
        """Every loaded skill has non-empty name, description, and when_to_use."""
        for name, skill in get_all_skills().items():
            assert skill.name, f"skill '{name}' has empty name"
            assert skill.description, f"skill '{name}' has empty description"
            assert skill.when_to_use, f"skill '{name}' has empty when_to_use"


class TestMissingFileDegradation:
    """Test 2: _load_all_skills() degrades gracefully for missing/bad files."""

    def test_partial_load_returns_nonempty(self, tmp_path: Path) -> None:
        """With one valid skill file, cache is non-empty."""
        skill_file = tmp_path / "bash.md"
        skill_file.write_text(
            "---\nname: bash\ndescription: test\nwhen_to_use: always\n---\n\n# bash content\nMinimal content.",
            encoding="utf-8",
        )
        cache = _load_all_skills(tmp_path)
        assert len(cache.docs) == 1
        assert "bash" in cache.docs
        assert "Minimal content." in cache.docs["bash"].body

    def test_files_without_frontmatter_are_skipped(self, tmp_path: Path) -> None:
        """Markdown files without YAML frontmatter are excluded from the cache."""
        no_fm = tmp_path / "EMPIRICAL_TEST_RESULTS.md"
        no_fm.write_text("# Just a heading\nNo frontmatter here.", encoding="utf-8")
        cache = _load_all_skills(tmp_path)
        assert len(cache.docs) == 0

    def test_all_missing_returns_empty_cache(self, tmp_path: Path) -> None:
        """When the skills directory is empty, the cache has no docs."""
        cache = _load_all_skills(tmp_path)
        assert cache.docs == {}

    def test_missing_name_key_skips_file(self, tmp_path: Path) -> None:
        """Files with frontmatter but no 'name' key are skipped."""
        no_name = tmp_path / "nameless.md"
        no_name.write_text(
            "---\ndescription: something\nwhen_to_use: never\n---\n\nbody",
            encoding="utf-8",
        )
        cache = _load_all_skills(tmp_path)
        assert len(cache.docs) == 0

    def test_unreadable_file_emits_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable skill file logs a warning and does not crash."""
        import unittest.mock

        bad = tmp_path / "bad.md"
        bad.write_text(
            "---\nname: bad\ndescription: x\nwhen_to_use: x\n---\nbody", encoding="utf-8"
        )

        original_read_text = Path.read_text

        def failing_read(self: Path, **kwargs: object) -> str:
            if self.name == "bad.md":
                raise OSError("permission denied")
            return original_read_text(self, **kwargs)

        with unittest.mock.patch.object(skills_module, "log") as mock_log:
            monkeypatch.setattr(Path, "read_text", failing_read)
            _load_all_skills(tmp_path)

        assert mock_log.warning.called


class TestFlagGating:
    """Test 3: get_skill_block() respects settings.prefer_primitives_enabled."""

    def test_returns_empty_when_flag_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_skill_block() returns '' when prefer_primitives_enabled is False."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", False)
        result = get_skill_block()
        assert result == ""

    def test_returns_nonempty_when_flag_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_skill_block() returns non-empty string when prefer_primitives_enabled is True."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        result = get_skill_block()
        assert result != ""
        assert result.startswith("## Skill Library — How to Drive Primitive Tools")

    def test_returns_empty_with_message_when_flag_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_skill_block() returns '' even with a message when flag is False."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", False)
        result = get_skill_block(message="show me logs")
        assert result == ""


class TestKeywordRouting:
    """Test 4: Keyword-based routing injects the correct skill doc."""

    @pytest.mark.parametrize("msg", [
        "show me logs",
        "check your logs",
        "check the logs",
        "app logs",
        "agent logs",
        "any recent errors",
        "show me traces",
        "what happened last hour",
        "show me agent-logs from last 24 hour",
    ])
    def test_es_keywords_inject_es_skill(self, msg: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Natural user phrasing triggers the query-elasticsearch skill."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        result = get_skill_block(message=msg)
        assert "agent-logs-" in result, (
            f"ES skill not injected for message: {msg!r}\n"
            "query-elasticsearch.md keywords are too restrictive."
        )

    def test_no_keyword_match_returns_bash_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A message with no matching keywords returns only the bash skill body."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        result = get_skill_block(message="what is the meaning of life")
        assert "bash — Shell Command Executor" in result
        # ES-specific content should NOT be present
        assert "agent-logs-YYYY.MM.DD" not in result

    def test_none_message_returns_bash_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing message=None returns only the bash skill block."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        result = get_skill_block(message=None)
        assert result.startswith("## Skill Library — How to Drive Primitive Tools")
        assert "bash — Shell Command Executor" in result

    def test_two_calls_return_identical_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated calls with the same message return the same value."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        first = get_skill_block(message="show me logs")
        second = get_skill_block(message="show me logs")
        assert first == second


class TestFindSkillForTool:
    """Test 5: find_skill_for_tool() returns the linked skill by tool name."""

    def test_bash_tool_links_to_some_skill(self) -> None:
        """find_skill_for_tool('bash') returns a SkillDoc listing bash."""
        skill = find_skill_for_tool("bash")
        assert skill is not None
        assert "bash" in skill.tools

    def test_unknown_tool_returns_none(self) -> None:
        """find_skill_for_tool with an unregistered name returns None."""
        result = find_skill_for_tool("nonexistent_tool_xyz")
        assert result is None

    def test_run_python_tool_links_to_run_python_skill(self) -> None:
        """find_skill_for_tool('run_python') returns the run-python skill."""
        skill = find_skill_for_tool("run_python")
        assert skill is not None
        assert skill.name == "run-python"

    def test_read_tool_links_to_read_write_skill(self) -> None:
        """find_skill_for_tool('read') returns the read-write skill."""
        skill = find_skill_for_tool("read")
        assert skill is not None
        assert skill.name == "read-write"


class TestMtimeCache:
    """Test 6: Cache invalidates when skill files change."""

    def test_cache_reloads_after_file_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Modifying a skill file triggers a cache reload on next access."""
        monkeypatch.setattr(skills_module, "_SKILLS_DIR", tmp_path)
        monkeypatch.setattr(skills_module, "_cache", None)

        skill_file = tmp_path / "bash.md"
        skill_file.write_text(
            "---\nname: bash\ndescription: d\nwhen_to_use: w\n---\n\nv1 content",
            encoding="utf-8",
        )
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)

        first = get_skill_block()
        assert "v1 content" in first

        # Ensure mtime changes (some filesystems have 1-second resolution)
        time.sleep(0.05)
        skill_file.write_text(
            "---\nname: bash\ndescription: d\nwhen_to_use: w\n---\n\nv2 content",
            encoding="utf-8",
        )
        # Force mtime to differ (touch with offset to be safe on fast filesystems)
        import os
        stat = skill_file.stat()
        os.utime(skill_file, (stat.st_atime, stat.st_mtime + 1.0))

        second = get_skill_block()
        assert "v2 content" in second
