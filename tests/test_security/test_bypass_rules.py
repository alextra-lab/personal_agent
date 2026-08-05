"""AC-b — ast-grep static bypass rule set — ADR-0132 D2 / FRE-1147.

Two assertions: the rule set finds zero enumerated bypass forms in the real
`src/personal_agent/` tree (proving all 11 migrated sites — and nothing
else — are clean), and a seeded violation on a scratch fixture makes it fail
(proving the rule provably fires rather than being silently absent).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_ast_grep(
    target: Path, extra_globs: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = ["ast-grep", "scan"]
    for glob in extra_globs or []:
        cmd += ["--globs", glob]
    cmd.append(str(target))
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)


class TestRealTreeIsClean:
    def test_no_bypass_forms_in_src(self) -> None:
        result = _run_ast_grep(
            REPO_ROOT / "src" / "personal_agent",
            extra_globs=["!src/personal_agent/security.py", "!src/personal_agent/ui/**"],
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestSeededViolationFires:
    def test_httpx_asyncclient_construction_is_flagged(self, tmp_path: Path) -> None:
        fixture = tmp_path / "seeded_violation.py"
        fixture.write_text("import httpx\n\nclient = httpx.AsyncClient(timeout=5)\n")
        result = _run_ast_grep(fixture)
        assert result.returncode != 0
        assert "no-raw-httpx-client" in result.stdout

    def test_module_level_httpx_get_is_flagged(self, tmp_path: Path) -> None:
        fixture = tmp_path / "seeded_violation.py"
        fixture.write_text("import httpx\n\nresp = httpx.get('https://example.com')\n")
        result = _run_ast_grep(fixture)
        assert result.returncode != 0
        assert "no-raw-httpx-module-calls" in result.stdout

    def test_raw_openai_client_without_http_client_is_flagged(self, tmp_path: Path) -> None:
        fixture = tmp_path / "seeded_violation.py"
        fixture.write_text("import openai\n\nclient = openai.AsyncOpenAI(api_key='x')\n")
        result = _run_ast_grep(fixture)
        assert result.returncode != 0
        assert "no-raw-openai-client" in result.stdout

    def test_openai_client_with_http_client_kwarg_is_not_flagged(self, tmp_path: Path) -> None:
        fixture = tmp_path / "compliant.py"
        fixture.write_text(
            "import openai\n\n"
            "client = openai.AsyncOpenAI(api_key='x', http_client=create_guarded_http_client())\n"
        )
        result = _run_ast_grep(fixture)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_raw_anthropic_client_without_http_client_is_flagged(self, tmp_path: Path) -> None:
        fixture = tmp_path / "seeded_violation.py"
        fixture.write_text("import anthropic\n\nclient = anthropic.AsyncAnthropic(api_key='x')\n")
        result = _run_ast_grep(fixture)
        assert result.returncode != 0
        assert "no-raw-anthropic-client" in result.stdout
