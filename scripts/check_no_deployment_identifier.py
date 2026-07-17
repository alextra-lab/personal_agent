#!/usr/bin/env python3
"""Fail if tracked files contain a real deployment identifier (FRE-895).

Scans ``git ls-files`` text for the real deployment domain and the real
Cloudflare Access team domain — neither ever written in this file as a
contiguous literal (built from concatenated fragments), so the checker
doesn't trip its own rule.

Deliberately does NOT ban the bare org/Linear-team-name word alone (used
throughout docs non-sensitively, e.g. "FrenchForest team") — only the
compound identifiers that are actual live deployment/auth hostnames.

Returns:
    0 if no violations; 1 if any match; 2 on environment errors.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

# Built from fragments so the contiguous word never appears in this file's text.
_FORBIDDEN_WORD = "french" + "foret"
_FORBIDDEN_CF_ACCESS_TEAM_DOMAIN = "french" + "forest" + ".cloudflareaccess.com"
_PATTERN = re.compile(
    "|".join(re.escape(w) for w in (_FORBIDDEN_WORD, _FORBIDDEN_CF_ACCESS_TEAM_DOMAIN)),
    re.IGNORECASE,
)

# Denylist, not allowlist: this repo's text files span too many extensions
# (.pwa, .example, .mdc, .ndjson, .mjs, extensionless Makefile/Dockerfile, …)
# for an allowlist to stay complete — a Path.suffix allowlist silently
# excludes anything it doesn't already know about, which is exactly the
# failure mode this guard exists to prevent. Skip only known-binary types.
_BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".webp",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".pyc",
        ".so",
    }
)


def _git_ls_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        text=False,
        check=False,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        print(f"check_no_deployment_identifier: git ls-files failed: {err}", file=sys.stderr)
        sys.exit(2)
    raw = result.stdout.split(b"\0")
    return [p.decode("utf-8", errors="replace") for p in raw if p]


def _is_probably_text(path: str) -> bool:
    return Path(path).suffix.lower() not in _BINARY_SUFFIXES


def find_violations(paths: Iterable[str], read_text: Callable[[str], str]) -> list[str]:
    """Return one formatted violation per match of the forbidden word.

    Args:
        paths: Relative paths to scan.
        read_text: Maps a relative path to its text content.

    Returns:
        ``"path:line: 'snippet'"`` strings, one per match found.
    """
    violations: list[str] = []
    for rel in paths:
        content = read_text(rel)
        for match in _PATTERN.finditer(content):
            line_no = content.count("\n", 0, match.start()) + 1
            snippet = content[match.start() : match.end()]
            violations.append(f"{rel}:{line_no}: {snippet!r}")
    return violations


def main() -> None:
    """Scan tracked text files and exit with an error code if the domain matches."""
    repo_root = Path(__file__).resolve().parent.parent

    # The checker itself necessarily contains the pattern it scans for.
    self_rel = Path(__file__).resolve().relative_to(repo_root).as_posix()

    candidates = [
        rel for rel in _git_ls_files(repo_root) if rel != self_rel and _is_probably_text(rel)
    ]

    def _read(rel: str) -> str:
        try:
            return (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"check_no_deployment_identifier: cannot read {rel}: {exc}", file=sys.stderr)
            sys.exit(2)

    violations = find_violations(candidates, _read)

    if violations:
        print("Real deployment domain found in tracked files:\n", file=sys.stderr)
        for v in violations:
            print(v, file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
