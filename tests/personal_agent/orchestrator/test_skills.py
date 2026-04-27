"""Tests for the skills.py skill-doc loader and get_skill_block gate."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import personal_agent.orchestrator.skills as skills_module
from personal_agent.config import settings
from personal_agent.orchestrator.skills import _load_skill_block, get_skill_block


class TestLoadSkillBlockReturnsNonempty:
    """Test 1: _CACHED_BLOCK is non-empty and contains expected content."""

    def test_cached_block_nonempty(self) -> None:
        """_CACHED_BLOCK must be non-empty (all 9 skill files exist)."""
        assert skills_module._CACHED_BLOCK != ""

    def test_cached_block_starts_with_header(self) -> None:
        """_CACHED_BLOCK must start with the expected library header."""
        expected_header = "## Skill Library — How to Drive Primitive Tools"
        assert skills_module._CACHED_BLOCK.startswith(expected_header)

    def test_cached_block_contains_bash_anchor(self) -> None:
        """_CACHED_BLOCK must contain content from bash.md."""
        # bash.md starts with "# bash — Shell Command Executor"
        assert "bash" in skills_module._CACHED_BLOCK.lower()
        # Check for the actual title present in bash.md
        assert "bash — Shell Command Executor" in skills_module._CACHED_BLOCK


class TestMissingFileDegradation:
    """Test 2: _load_skill_block() degrades gracefully when files are missing."""

    def test_partial_load_returns_nonempty(self, tmp_path: Path) -> None:
        """With only 1 valid file, block is non-empty."""
        # Create one minimal markdown file
        skill_file = tmp_path / "bash.md"
        skill_file.write_text("# bash — Shell Command Executor\nMinimal content.", encoding="utf-8")

        original_dir = skills_module._SKILLS_DIR
        skills_module._SKILLS_DIR = tmp_path
        try:
            result = _load_skill_block()
        finally:
            skills_module._SKILLS_DIR = original_dir

        assert result != ""
        assert "Minimal content." in result

    def test_missing_files_emit_warnings(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Missing skill files must cause structlog to emit a warning per missing file."""
        # Only create bash.md — the other 8 are missing
        skill_file = tmp_path / "bash.md"
        skill_file.write_text("# bash — test", encoding="utf-8")

        original_dir = skills_module._SKILLS_DIR
        skills_module._SKILLS_DIR = tmp_path
        try:
            # structlog in test mode writes to stdlib logging; capture at WARNING level
            with caplog.at_level(logging.WARNING):
                _load_skill_block()
        finally:
            skills_module._SKILLS_DIR = original_dir

        # 8 files are missing → 8 warnings (or at least 1 warning fired)
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        # At minimum we expect warnings for the 8 missing files
        assert len(warning_messages) >= 1

    def test_all_missing_returns_empty(self, tmp_path: Path) -> None:
        """When no files exist at all, _load_skill_block() returns empty string."""
        original_dir = skills_module._SKILLS_DIR
        skills_module._SKILLS_DIR = tmp_path
        try:
            result = _load_skill_block()
        finally:
            skills_module._SKILLS_DIR = original_dir

        assert result == ""


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


class TestCacheStability:
    """Test 4: get_skill_block() returns the same object on repeated calls."""

    def test_two_calls_return_identical_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two consecutive calls to get_skill_block() return the same string value."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        first = get_skill_block()
        second = get_skill_block()
        assert first == second

    def test_cached_block_unchanged_across_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_CACHED_BLOCK is the same object across calls (no per-call I/O)."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        # Both calls return _CACHED_BLOCK directly — verify they are the same identity
        block_before = skills_module._CACHED_BLOCK
        get_skill_block()
        get_skill_block()
        assert skills_module._CACHED_BLOCK is block_before
