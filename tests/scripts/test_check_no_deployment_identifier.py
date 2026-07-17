# ruff: noqa: D103
"""Unit tests for the deployment-identifier guard (FRE-895).

Blocks the real deployment domain from re-entering tracked files. Drives the
pure `find_violations` function directly (no git, no real repo tree) — same
shape as tests/scripts/test_check_identity_threaded.py.
"""

from __future__ import annotations

from scripts.check_no_deployment_identifier import _is_probably_text, find_violations

# Built the same way the checker itself avoids a contiguous forbidden literal.
_REAL_DOMAIN = "french" + "foret" + ".com"


def test_clean_file_has_no_violations() -> None:
    files = {"docs/example.md": "See https://artifacts.example.com for details."}
    assert find_violations(files.keys(), files.__getitem__) == []


def test_domain_match_is_flagged_with_path_and_line() -> None:
    files = {"docs/leaked.md": f"line one\nsee https://slm.{_REAL_DOMAIN}/v1 here\nline three"}
    violations = find_violations(files.keys(), files.__getitem__)
    assert len(violations) == 1
    assert "docs/leaked.md:2" in violations[0]


def test_bare_word_without_dot_com_is_also_flagged() -> None:
    """The ticket AC is the bare word, not just the ``.com`` suffix (FRE-895)."""
    files = {"docs/note.md": f"the {_REAL_DOMAIN.removesuffix('.com')} cleanup ticket"}
    assert len(find_violations(files.keys(), files.__getitem__)) == 1


def test_case_insensitive_match_is_flagged() -> None:
    files = {"docs/note.md": _REAL_DOMAIN.upper()}
    assert len(find_violations(files.keys(), files.__getitem__)) == 1


def test_unrelated_frenchforest_word_is_not_flagged() -> None:
    """The bare Linear team name 'FrenchForest' (extra 's') is not itself sensitive."""
    files = {"CLAUDE.md": "Development Model: Linear issue tracking (FrenchForest team)"}
    assert find_violations(files.keys(), files.__getitem__) == []


def test_real_cf_access_team_domain_is_flagged() -> None:
    """The real Cloudflare Access team domain is a separate live identifier (FRE-895

    codex/security-review catch) — 'FrenchForest' alone is fine, but the compound
    '<team>.cloudflareaccess.com' auto-generated CF Zero Trust hostname is not.
    """
    real_team_domain = "french" + "forest" + ".cloudflareaccess.com"
    files = {"tests/fixture.py": f"https://{real_team_domain}/cdn-cgi/access/login/"}
    assert len(find_violations(files.keys(), files.__getitem__)) == 1


def test_multiple_matches_in_one_file_all_reported() -> None:
    files = {"docs/note.md": f"{_REAL_DOMAIN} appears twice: {_REAL_DOMAIN}"}
    assert len(find_violations(files.keys(), files.__getitem__)) == 2


def test_multiple_files_all_scanned() -> None:
    files = {
        "docs/a.md": "clean",
        "docs/b.md": _REAL_DOMAIN,
        "docs/c.md": "clean too",
    }
    violations = find_violations(files.keys(), files.__getitem__)
    assert len(violations) == 1
    assert "docs/b.md" in violations[0]


class TestIsProbablyText:
    """A denylist, not an allowlist — this repo's text files span too many

    extensions (Dockerfile.pwa, .env.example, .mdc, .ndjson, .mjs,
    extensionless Makefile/LICENSE) for an allowlist to stay complete
    (codex/security-review finding, FRE-895).
    """

    def test_unusual_but_real_text_extensions_are_scanned(self) -> None:
        for path in ("Dockerfile.pwa", ".env.example", "a.mdc", "b.ndjson", "c.mjs"):
            assert _is_probably_text(path), f"{path} should be scanned as text"

    def test_extensionless_files_are_scanned(self) -> None:
        for path in ("Makefile", "LICENSE", ".gitignore", "uv.lock"):
            assert _is_probably_text(path), f"{path} should be scanned as text"

    def test_known_binary_extensions_are_skipped(self) -> None:
        for path in ("logo.png", "font.woff2", "archive.zip", "module.pyc"):
            assert not _is_probably_text(path), f"{path} should be skipped"
