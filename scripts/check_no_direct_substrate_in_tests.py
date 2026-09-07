#!/usr/bin/env python3
"""Fail if test/eval scripts contain direct production-substrate access patterns.

Scans Python files under ``tests/``, ``scripts/eval/``, and ``scripts/research/``
for patterns that indicate raw access to the production Neo4j, Elasticsearch, or
PostgreSQL substrates, bare ``MemoryService()`` instantiation without mocks, or
(FRE-1372 AC-3) a ``scripts/eval/`` file driving a turn against the isolated eval
gateway (``EVAL_ARMS``/``EVAL_CHAT_BASE_URL``) without going through the one shared
mechanism that isolates arm-to-arm Neo4j writes within a run —
``eval_isolation.IsolatedArmRunner``. That last check is what makes FRE-1338's control
structural rather than a convention a new script can silently omit: a master review of
FRE-1372 found the original PR shipped ``IsolatedArmRunner`` as an opt-in helper with no
enforcement, exactly the failure AC-3's own text rules out.

Any matching line of the substrate-access patterns may be exempted by appending::

    # fre-375-allow: <reason>

The eval-turn-isolation check is whole-file, not per-line, so it has no inline
exemption — add the path to ``_EVAL_ISOLATION_EXEMPT_PATHS`` instead (a deliberate,
reviewable list edit, not a silent comment).

Returns:
    0 if no violations; 1 if any match; 2 on environment errors.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Iterable
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

# FRE-1372 AC-3: a file referencing either of these is trying to drive a turn against
# the isolated eval gateway — it must also reference IsolatedArmRunner somewhere
# (import or definition), or isolation depends on it remembering to, which is the exact
# failure AC-3 rules out ("isolation depends on each script remembering to call a reset
# helper").
_EVAL_SUBSTRATE_MARKERS: tuple[str, ...] = ("EVAL_ARMS", "EVAL_CHAT_BASE_URL")
_ISOLATED_ARM_RUNNER_MARKER = "IsolatedArmRunner"

# The mechanism itself and the module defining the guard constants it checks for — both
# necessarily reference EVAL_ARMS/EVAL_CHAT_BASE_URL without referencing
# IsolatedArmRunner by name in the former case (it IS IsolatedArmRunner).
_EVAL_ISOLATION_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "scripts/eval/eval_isolation.py",
        "scripts/eval/fre1337_intent_probe/substrate.py",
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


def _find_pattern_violations(rel: str, content: str) -> list[str]:
    """The original per-line substrate-access patterns."""
    violations: list[str] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if _EXEMPTION_RE.search(line):
            continue
        for pattern, label in _PATTERNS:
            if pattern.search(line):
                violations.append(f"{rel}:{lineno}: [{label}]  {line.strip()!r}")
    return violations


def _find_eval_turn_isolation_violations(rel: str, content: str) -> list[str]:
    """FRE-1372 AC-3: an eval-gateway turn must go through IsolatedArmRunner.

    Whole-file, not per-line: the marker reference and the turn-driving code that uses
    it are rarely on the same line once a multi-argument ``.post(...)`` call wraps, so
    this checks for the marker anywhere in the file rather than matching one call shape.
    """
    if not rel.startswith("scripts/eval/") or rel in _EVAL_ISOLATION_EXEMPT_PATHS:
        return []
    if not any(marker in content for marker in _EVAL_SUBSTRATE_MARKERS):
        return []
    if _ISOLATED_ARM_RUNNER_MARKER in content:
        return []
    markers = "/".join(_EVAL_SUBSTRATE_MARKERS)
    return [
        f"{rel}: references the isolated eval gateway ({markers}) without referencing "
        f"{_ISOLATED_ARM_RUNNER_MARKER} (FRE-1372 AC-3) — drive turns via "
        "scripts.eval.eval_isolation.IsolatedArmRunner, not a raw HTTP POST."
    ]


def find_violations(paths: Iterable[str], read_text: Callable[[str], str]) -> list[str]:
    """Scan *paths* (already filtered to target files) and return all violations.

    Args:
        paths: Relative repo paths to scan — callers filter with ``_is_target_file``.
        read_text: Maps a relative path to its text content.

    Returns:
        One formatted violation string per match.
    """
    violations: list[str] = []
    for rel in paths:
        content = read_text(rel)
        violations.extend(_find_pattern_violations(rel, content))
        violations.extend(_find_eval_turn_isolation_violations(rel, content))
    return violations


def main() -> None:
    """Scan target files and exit with an error code if forbidden patterns match."""
    repo_root = Path(__file__).resolve().parent.parent
    candidates = [rel for rel in _git_ls_files(repo_root) if _is_target_file(rel)]

    def _read(rel: str) -> str:
        try:
            return (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(
                f"check_no_direct_substrate_in_tests: cannot read {rel}: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)

    violations = find_violations(candidates, _read)

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
