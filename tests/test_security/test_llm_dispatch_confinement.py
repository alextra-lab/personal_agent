"""AC-a — ast-grep static LLM dispatch confinement — ADR-0141 AC-6 / FRE-1367.

Two assertions per rule: the rule finds zero hits in the real tree (proving the
census is clean), and a seeded violation on a scratch fixture makes it fail
(proving the rule provably fires rather than being silently absent — a guard
proven only on a clean tree is a vacuous check).

Seeded fixtures run with ``cwd=tmp_path`` and a *relative* target path — the
``--globs`` exemption for ``llm_client/`` is evaluated relative to the scan's
cwd (verified: an absolute fixture path outside ``REPO_ROOT`` does not match
``!**/llm_client/**`` when ``cwd=REPO_ROOT`` even though the path contains an
``llm_client`` segment), so this mirrors exactly how
``scripts/check_llm_dispatch_confinement.py`` itself invokes ast-grep
(``cwd=REPO_ROOT``, relative targets).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO_ROOT / ".ast-grep" / "llm-dispatch-rules"
TOMBSTONE_RULE = RULES_DIR / "no-local-llm-client.yml"
DISPATCH_RULE = RULES_DIR / "no-raw-litellm-dispatch.yml"
TARGETS = ["src/personal_agent", "scripts", "experiments", "tests"]


def _run_ast_grep(
    rule: Path,
    cwd: Path,
    target: str,
    extra_globs: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["ast-grep", "scan", "--rule", str(rule)]
    for glob in extra_globs or []:
        cmd += ["--globs", glob]
    cmd.append(target)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


class TestRealTreeIsClean:
    def test_no_local_llm_client_in_repo(self) -> None:
        cmd = ["ast-grep", "scan", "--rule", str(TOMBSTONE_RULE), *TARGETS]
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_no_raw_litellm_dispatch_outside_llm_client(self) -> None:
        cmd = [
            "ast-grep",
            "scan",
            "--rule",
            str(DISPATCH_RULE),
            "--globs",
            "!**/llm_client/**",
            *TARGETS,
        ]
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr


class TestSeededViolationFires:
    def test_local_llm_client_reference_is_flagged(self, tmp_path: Path) -> None:
        fixture = tmp_path / "seeded_violation.py"
        fixture.write_text("from personal_agent.llm_client.client import LocalLLMClient\n")
        result = _run_ast_grep(TOMBSTONE_RULE, tmp_path, "seeded_violation.py")
        assert result.returncode != 0
        assert "no-local-llm-client" in result.stdout

    def test_local_llm_client_string_mention_is_not_flagged(self, tmp_path: Path) -> None:
        """The pattern matches code identifiers, not comments or string content."""
        fixture = tmp_path / "compliant.py"
        fixture.write_text(
            '# Historical note: this used to be LocalLLMClient.\nx = "LocalLLMClient"\n'
        )
        result = _run_ast_grep(TOMBSTONE_RULE, tmp_path, "compliant.py")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_litellm_acompletion_outside_llm_client_is_flagged(self, tmp_path: Path) -> None:
        fixture_dir = tmp_path / "scripts" / "eval"
        fixture_dir.mkdir(parents=True)
        fixture = fixture_dir / "seeded_violation.py"
        fixture.write_text(
            "import litellm\n\n"
            "async def f():\n"
            "    return await litellm.acompletion(model='x', messages=[])\n"
        )
        result = _run_ast_grep(
            DISPATCH_RULE,
            tmp_path,
            "scripts/eval/seeded_violation.py",
            extra_globs=["!**/llm_client/**"],
        )
        assert result.returncode != 0
        assert "no-raw-litellm-dispatch" in result.stdout

    def test_litellm_completion_outside_llm_client_is_flagged(self, tmp_path: Path) -> None:
        fixture = tmp_path / "seeded_violation.py"
        fixture.write_text("import litellm\n\nlitellm.completion(model='x', messages=[])\n")
        result = _run_ast_grep(
            DISPATCH_RULE, tmp_path, "seeded_violation.py", extra_globs=["!**/llm_client/**"]
        )
        assert result.returncode != 0
        assert "no-raw-litellm-dispatch" in result.stdout

    def test_litellm_acompletion_inside_llm_client_is_not_flagged(self, tmp_path: Path) -> None:
        """The --globs exemption for llm_client/ actually works, not just the raw rule.

        Scans the containing *directory* (``src``), not the file directly:
        verified that ``--globs`` filtering only applies during ast-grep's own
        directory-walk discovery — naming a file as the scan target bypasses
        it entirely, which would make this test pass for the wrong reason
        (the exemption never actually gets exercised).
        """
        fixture_dir = tmp_path / "src" / "personal_agent" / "llm_client"
        fixture_dir.mkdir(parents=True)
        fixture = fixture_dir / "litellm_client.py"
        fixture.write_text(
            "import litellm\n\n"
            "async def f():\n"
            "    return await litellm.acompletion(model='x', messages=[])\n"
        )
        result = _run_ast_grep(
            DISPATCH_RULE,
            tmp_path,
            "src",
            extra_globs=["!**/llm_client/**"],
        )
        assert result.returncode == 0, result.stdout + result.stderr
