#!/usr/bin/env python3
"""Fail if test/eval scripts contain direct production-substrate access patterns.

Scans Python files under ``tests/``, ``scripts/eval/``, and ``scripts/research/``
for patterns that indicate raw access to the production Neo4j, Elasticsearch, or
PostgreSQL substrates, or bare ``MemoryService()`` instantiation without mocks.

Any matching line may be exempted by appending::

    # fre-375-allow: <reason>

Returns:
    0 if no violations; 1 if any match; 2 on environment errors.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"MemoryService\(\)"),
        "bare MemoryService() instantiation (use mocks or test stack)",
    ),
    (
        re.compile(r"(?:Async)?GraphDatabase\.driver\("),
        "raw Neo4j driver construction",
    ),
    (
        re.compile(r"""["']bolt://localhost:7687"""),
        "hardcoded prod Neo4j bolt URI",
    ),
    (
        re.compile(r"""["']http://localhost:9200"""),
        "hardcoded prod Elasticsearch URL",
    ),
    (
        re.compile(r"neo4j_dev_password"),
        "hardcoded prod Neo4j password",
    ),
)

_EXEMPTION_RE: re.Pattern[str] = re.compile(r"#\s*fre-375-allow")

# Allowlisted files are skipped entirely — never flagged.
_ALLOWLISTED_PATHS: frozenset[str] = frozenset(
    {
        "scripts/research/memory_integration_probe/_common.py",  # read-only research tool
        "scripts/check_no_direct_substrate_in_tests.py",  # this script itself
    }
)

# Only scan .py files whose path starts with one of these prefixes.
_SCAN_PREFIXES: tuple[str, ...] = ("tests/", "scripts/eval/", "scripts/research/")

# Skip any path containing these path segments (e.g. archived experiments).
_SKIP_SEGMENTS: tuple[str, ...] = ("/archive/",)


def _git_ls_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        text=False,
        check=False,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        print(
            f"check_no_direct_substrate_in_tests: git ls-files failed: {err}",
            file=sys.stderr,
        )
        sys.exit(2)
    raw = result.stdout.split(b"\0")
    return [p.decode("utf-8", errors="replace") for p in raw if p]


def _is_target_file(path: str) -> bool:
    """Return True if *path* should be scanned."""
    if not path.endswith(".py"):
        return False
    if not any(path.startswith(prefix) for prefix in _SCAN_PREFIXES):
        return False
    if any(seg in path for seg in _SKIP_SEGMENTS):
        return False
    if path in _ALLOWLISTED_PATHS:
        return False
    return True


def main() -> None:
    """Scan target files and exit with an error code if forbidden patterns match."""
    repo_root = Path(__file__).resolve().parent.parent
    violations: list[str] = []

    for rel in _git_ls_files(repo_root):
        if not _is_target_file(rel):
            continue
        file_path = repo_root / rel
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(
                f"check_no_direct_substrate_in_tests: cannot read {rel}: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)

        for lineno, line in enumerate(content.splitlines(), start=1):
            if _EXEMPTION_RE.search(line):
                continue
            for pattern, label in _PATTERNS:
                if pattern.search(line):
                    violations.append(f"{rel}:{lineno}: [{label}]  {line.strip()!r}")

    if violations:
        print(
            "Direct production-substrate access found in test/eval scripts:\n",
            file=sys.stderr,
        )
        for v in violations:
            print(v, file=sys.stderr)
        print(
            "\nTo suppress a legitimate use, append  # fre-375-allow: <reason>  to the line.",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
