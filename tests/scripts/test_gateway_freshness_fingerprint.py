"""FRE-1341 — `compute_build_fingerprint` must catch uncommitted staleness.

The whole point of hashing on-disk content instead of comparing `git rev-parse HEAD` is
that an uncommitted or untracked edit under a build-input path changes the fingerprint.
This is what makes AC-1's seeded-negative sequence (build, edit `src/` without committing,
check, refuse) actually detectable — see the plan's addendum for the codex plan-review
finding that ruled out a HEAD-only comparison.
"""

from __future__ import annotations

from pathlib import Path

from scripts.eval.gateway_freshness import compute_build_fingerprint


def _make_tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("print('a')\n")
    (root / "config").mkdir()
    (root / "config" / "c.yaml").write_text("k: v\n")
    (root / "docs").mkdir()
    (root / "docs" / "skills").mkdir()
    (root / "docs" / "skills" / "s.md").write_text("# skill\n")
    (root / "docker").mkdir()
    (root / "docker" / "mcp").mkdir()
    (root / "docker" / "mcp" / "run.sh").write_text("#!/bin/sh\n")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "uv.lock").write_text("version = 1\n")
    (root / "Dockerfile.gateway").write_text("FROM python:3.12-slim\n")


class TestComputeBuildFingerprint:
    def test_deterministic_for_the_same_tree(self, tmp_path: Path) -> None:
        _make_tree(tmp_path)
        first = compute_build_fingerprint(tmp_path)
        second = compute_build_fingerprint(tmp_path)
        assert first == second

    def test_changes_when_a_src_file_is_edited_uncommitted(self, tmp_path: Path) -> None:
        _make_tree(tmp_path)
        before = compute_build_fingerprint(tmp_path)

        (tmp_path / "src" / "a.py").write_text("print('a-edited')\n")

        after = compute_build_fingerprint(tmp_path)
        assert before != after

    def test_changes_when_a_new_untracked_file_is_added(self, tmp_path: Path) -> None:
        _make_tree(tmp_path)
        before = compute_build_fingerprint(tmp_path)

        (tmp_path / "src" / "new_untracked.py").write_text("print('new')\n")

        after = compute_build_fingerprint(tmp_path)
        assert before != after

    def test_changes_when_dockerfile_itself_is_edited(self, tmp_path: Path) -> None:
        _make_tree(tmp_path)
        before = compute_build_fingerprint(tmp_path)

        (tmp_path / "Dockerfile.gateway").write_text("FROM python:3.12-slim\nRUN echo hi\n")

        after = compute_build_fingerprint(tmp_path)
        assert before != after

    def test_unaffected_by_a_file_outside_build_input_paths(self, tmp_path: Path) -> None:
        _make_tree(tmp_path)
        before = compute_build_fingerprint(tmp_path)

        (tmp_path / "README.md").write_text("not a build input\n")
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "not_copied.py").write_text("print('not copied')\n")

        after = compute_build_fingerprint(tmp_path)
        assert before == after

    def test_missing_build_input_paths_do_not_crash(self, tmp_path: Path) -> None:
        # A minimal tree missing config/docs/skills/docker/mcp entirely should still hash.
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("print('a')\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "Dockerfile.gateway").write_text("FROM python:3.12-slim\n")

        result = compute_build_fingerprint(tmp_path)
        assert isinstance(result, str)
        assert len(result) == 64  # sha256 hex digest
