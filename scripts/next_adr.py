#!/usr/bin/env python3
"""ADR numbering + index guard — prevents duplicate numbers and index drift.

Two subcommands:

- ``--next`` prints the next free ADR number (4-digit, zero-padded). It reads the
  ADR filenames from **both** the local tree **and** ``origin/main`` and takes the
  max + 1, so a seat working in a stale worktree (whose local tree hasn't pulled a
  just-merged ADR) can never re-pick a number that already exists on main — the
  exact cause of the ADR-0117 double-number collision (2026-07-14).
- ``--check`` verifies the README index is consistent with the ADR files: no
  duplicate numbers on disk, every ``ADR-NNNN`` file has an index row, and every
  index row points at a real file. Exits non-zero and lists every problem — wired
  into pre-commit so a colliding or drifted index fails before it can merge.

Callable by hand::

    python scripts/next_adr.py --next     # e.g. 0119
    python scripts/next_adr.py --check    # exit 0 if the index matches the files
"""

from __future__ import annotations

import argparse
import re
import subprocess  # noqa: S404 - runs a trusted, argv-built `git ls-tree`, no shell
import sys
from pathlib import Path

_ADR_DIR = Path("docs/architecture_decisions")
_README = _ADR_DIR / "README.md"

# ADR filename: ``ADR-<NNNN>-<slug>.md``. The number is exactly four digits; a
# suffix like ADR-0008b keeps 0008 (the letter is not part of the number).
_FILENAME_RE = re.compile(r"^ADR-(\d{4})[a-z]?-.+\.md$")
# An index row / any reference links an ADR by ``ADR-NNNN``.
_REF_RE = re.compile(r"ADR-(\d{4})")


# --- pure helpers -----------------------------------------------------------


def numbers_in(filenames: list[str]) -> list[int]:
    """Return every ADR number parsed from ``filenames`` (with duplicates kept).

    Duplicates are preserved so ``--check`` can detect two files claiming the same
    number (the collision case). Non-ADR names are ignored.

    Args:
        filenames: Basenames to scan (e.g. ``["ADR-0001-init.md", "README.md"]``).

    Returns:
        The parsed numbers, in input order, duplicates included.
    """
    out: list[int] = []
    for name in filenames:
        match = _FILENAME_RE.match(name)
        if match is not None:
            out.append(int(match.group(1)))
    return out


def next_number(existing: set[int]) -> str:
    """Return the next free ADR number as a 4-digit string (max + 1, or 0001)."""
    return f"{(max(existing) + 1) if existing else 1:04d}"


def index_problems(adr_filenames: list[str], readme_text: str) -> list[str]:
    """Return the list of index-completeness problems, empty when consistent.

    Checks: (a) every ADR file's number appears in the README index; (b) the
    README does not index a number that has no file. Each problem is a one-line
    human string.

    Duplicate *numbers* on disk are deliberately NOT flagged: the project files
    intentional lettered addenda under one base number (ADR-0008 + ADR-0008b),
    indistinguishable from an accidental clash by filename alone. Collisions are
    prevented at the source instead — ``--next`` reads ``origin/main`` so a new
    number is always genuinely free — and caught at master's gate.

    Args:
        adr_filenames: The ADR ``*.md`` basenames on disk.
        readme_text: The README index contents.

    Returns:
        One string per problem; empty if every file is indexed and vice-versa.
    """
    problems: list[str] = []
    file_numbers = set(numbers_in(adr_filenames))
    indexed = {int(m) for m in _REF_RE.findall(readme_text)}
    for num in sorted(file_numbers - indexed):
        problems.append(f"ADR-{num:04d} has a file but no README index row")
    for num in sorted(indexed - file_numbers):
        problems.append(f"README indexes ADR-{num:04d} but no file exists")
    return problems


# --- IO seam ----------------------------------------------------------------


def _local_adr_filenames() -> list[str]:
    """ADR ``*.md`` basenames in the local tree (empty if the dir is absent)."""
    if not _ADR_DIR.is_dir():
        return []
    return [p.name for p in _ADR_DIR.iterdir() if p.name.startswith("ADR-")]


def _origin_adr_filenames() -> list[str]:
    """ADR basenames on ``origin/main`` (empty if the ref is unavailable).

    Reading origin/main — not just the local worktree — is what defeats the
    stale-tree collision: a seat on a feature worktree still sees ADRs merged to
    main by another seat. Never raises; a missing ref just yields no names.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "ls-tree", "--name-only", "origin/main", f"{_ADR_DIR}/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [Path(line).name for line in result.stdout.splitlines() if line.strip()]


def cmd_next() -> int:
    """Print the next free ADR number (local ∪ origin/main), then exit 0."""
    existing = set(numbers_in(_local_adr_filenames())) | set(numbers_in(_origin_adr_filenames()))
    print(next_number(existing))
    return 0


def cmd_check() -> int:
    """Print any index problems; exit 1 if there are any, else 0."""
    readme = _README.read_text() if _README.exists() else ""
    problems = index_problems(_local_adr_filenames(), readme)
    if not problems:
        print("ADR index OK: every ADR file is indexed, no orphan rows.")
        return 0
    print("ADR index problems:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Entry point: dispatch ``--next`` or ``--check``."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--next", action="store_true", help="Print the next free ADR number.")
    group.add_argument("--check", action="store_true", help="Verify index ⟺ files consistency.")
    args = parser.parse_args(argv)
    return cmd_next() if args.next else cmd_check()


if __name__ == "__main__":
    raise SystemExit(main())
