#!/usr/bin/env python3
"""Fail if tracked files contain machine-specific path examples.

Scans ``git ls-files`` text for patterns that leak a developer's local layout
(e.g. macOS user home mount + ``Users`` segment, tilde + ``/Dev/`` layout, Windows profile
paths).

Returns:
    0 if no violations; 1 if any match; 2 on environment errors.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Build macOS pattern without a contiguous forbidden literal in source.
_MAC_USERS_PREFIX = "/" + "Users" + "/"
_HOME_DEV = re.escape("$HOME") + "/" + "Dev"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(re.escape(_MAC_USERS_PREFIX)), f"macOS absolute path under {_MAC_USERS_PREFIX!r}"),
    (re.compile("~" + "/Dev/"), "home-relative Dev/ layout"),
    (re.compile(_HOME_DEV + r"(?:/|$)"), "literal HOME env + /Dev/ segment path layout"),
    (re.compile(r'(?i)(?:C:|\\\\)[/\\\\]Users[/\\\\]'), "Windows profile path under drive C"),
    (
        # Exclude /home/user (test fixture) and XML-ish endings like /home/user<
        re.compile(r"/home/(?!user(?=[/>\"<\s]|$))"),
        "Linux home path /home/<name>/ (excluding /home/user fixture)",
    ),
)

_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".md",
        ".py",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".sh",
        ".txt",
        ".rst",
        ".cfg",
        ".ini",
        ".mako",
        ".j2",
        ".css",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".svg",
        "",
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
        print(f"check_no_personal_paths: git ls-files failed: {err}", file=sys.stderr)
        sys.exit(2)
    raw = result.stdout.split(b"\0")
    return [p.decode("utf-8", errors="replace") for p in raw if p]


def _is_probably_text(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    if suffix not in _TEXT_SUFFIXES:
        return False
    return True


def main() -> None:
    """Scan tracked text files and exit with an error code if patterns match."""
    repo_root = Path(__file__).resolve().parent.parent
    violations: list[str] = []

    for rel in _git_ls_files(repo_root):
        if not _is_probably_text(rel):
            continue
        file_path = repo_root / rel
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"check_no_personal_paths: cannot read {rel}: {exc}", file=sys.stderr)
            sys.exit(2)

        for pattern, label in _PATTERNS:
            for match in pattern.finditer(content):
                line_no = content.count("\n", 0, match.start()) + 1
                snippet = content[match.start() : match.end()]
                violations.append(f"{rel}:{line_no}: [{label}] {snippet!r}")

    if violations:
        print("Personal/local path patterns found in tracked files:\n", file=sys.stderr)
        for v in violations:
            print(v, file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
