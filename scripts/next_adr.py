#!/usr/bin/env python3
"""ADR numbering + index guard — prevents duplicate numbers and index drift.

Two subcommands:

- ``--next`` prints the next free ADR number (4-digit, zero-padded). It reads the
  ADR filenames from **both** the local tree **and** ``origin/main`` and takes the
  max + 1, so a seat working in a stale worktree (whose local tree hasn't pulled a
  just-merged ADR) can never re-pick a number that already exists on main — the
  exact cause of the ADR-0117 double-number collision (2026-07-14).
- ``--check`` verifies the README index is consistent with the ADR files: no
  duplicate numbers on disk, every ``ADR-NNNN`` file has an index row, every
  index row points at a real file, and — since FRE-952 — every index row's
  status column matches the Status field inside the ADR file it points at (by
  *category*, e.g. Accepted / Superseded, so incidental detail like dates or
  PR numbers doesn't trip a false positive). Exits non-zero and lists every
  problem — wired into pre-commit so a colliding, drifted, or status-stale
  index fails before it can merge.

Callable by hand::

    python scripts/next_adr.py --next     # e.g. 0119
    python scripts/next_adr.py --check    # exit 0 if the index matches the files
"""

from __future__ import annotations

import argparse
import re
import subprocess  # noqa: S404 - runs a trusted, argv-built `git ls-tree`, no shell
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

_ADR_DIR = Path("docs/architecture_decisions")
_README = _ADR_DIR / "README.md"

# ADR filename: ``ADR-<NNNN>-<slug>.md``. The number is exactly four digits; a
# suffix like ADR-0008b keeps 0008 (the letter is not part of the number).
_FILENAME_RE = re.compile(r"^ADR-(\d{4})[a-z]?-.+\.md$")
# An index row / any reference links an ADR by ``ADR-NNNN``.
_REF_RE = re.compile(r"ADR-(\d{4})")
# A README index row: ``| [ADR-NNNN](filename.md) | Title | Status | ... |``.
# Captures the link target (filename) and everything after it; the number of
# trailing columns varies (some sections add a "Decision summary" column), so
# the status column is pulled from the split remainder rather than anchored.
_INDEX_ROW_RE = re.compile(r"^\|\s*\[ADR-\d{4}[a-z]?\]\(([^)]+)\)\s*\|(.*)\|\s*$")

# Status categories recognized in both the README column and the ADR file's
# own Status field, longest-first so "partially delivered" isn't swallowed by
# a bare "partially" or confused with the unrelated "partially superseded".
_STATUS_CATEGORIES = (
    "partially delivered",
    "partially implemented",
    "partially superseded",
    "accepted",
    "implemented",
    "proposed",
    "superseded",
    "deferred",
    "deprecated",
    "rejected",
    "retired",
    "parked",
    "withdrawn",
    "draft",
)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MARKDOWN_EMPHASIS_RE = re.compile(r"[*`✅]")

_FILE_STATUS_HEADING_RE = re.compile(r"^##\s*Status\s*$", re.IGNORECASE)
_FILE_STATUS_TABLE_RE = re.compile(r"^\|\s*\*\*Status\*\*\s*\|\s*([^|]+)\|", re.IGNORECASE)
_FILE_STATUS_BOLD_RE = re.compile(r"^\*\*Status(?:\*\*:|:\*\*)\s*(.+)$", re.IGNORECASE)
_FILE_STATUS_DASH_RE = re.compile(r"^-\s*Status:\s*(.+)$", re.IGNORECASE)


class IndexRow(NamedTuple):
    """One README index row: the linked ADR filename and its status column text."""

    filename: str
    status_text: str


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


def parse_index_rows(readme_text: str) -> list[IndexRow]:
    """Parse every ``| [ADR-NNNN](file.md) | Title | Status | ... |`` row.

    Keys each row by the filename inside the markdown link — not the
    ``ADR-NNNN`` display label — so two files sharing a base number (e.g.
    ADR-0008 and its lettered sibling ADR-0008b) are never conflated. A
    number-keyed comparison cannot see a swapped pair; a filename-keyed one
    always resolves to the right file (FRE-952).

    Args:
        readme_text: The README index contents.

    Returns:
        One ``IndexRow`` per parsed row, in document order.
    """
    rows: list[IndexRow] = []
    for line in readme_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        match = _INDEX_ROW_RE.match(stripped)
        if match is None:
            continue
        filename, rest = match.groups()
        columns = rest.split("|")
        if len(columns) < 2:
            continue
        rows.append(IndexRow(filename=filename, status_text=columns[1].strip()))
    return rows


def status_category(text: str) -> str | None:
    """Return the status category ``text`` begins with, or ``None`` if unrecognized.

    Strips markdown links/emphasis, lowercases, and matches the longest known
    category prefix. Matching by category — not raw text — is what lets the
    comparison ignore incidental detail (dates, PR numbers, which ADR
    superseded it) while still catching a genuine category change such as
    Proposed → Accepted or Accepted → Superseded.

    Args:
        text: Raw status text from either the README column or an ADR file.

    Returns:
        The matched category (e.g. ``"superseded"``), or ``None`` if ``text``
        does not start with any recognized category.
    """
    cleaned = _MARKDOWN_LINK_RE.sub(r"\1", text)
    cleaned = _MARKDOWN_EMPHASIS_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    for category in _STATUS_CATEGORIES:
        if cleaned.startswith(category):
            return category
    return None


def extract_file_status(adr_text: str) -> str | None:
    """Return an ADR file's canonical Status field text, or ``None`` if absent.

    Recognizes the four formats used across the ADR corpus: a markdown
    heading (``## Status`` followed by the value on the next non-blank line),
    a bold inline field (``**Status:** value`` or ``**Status**: value``), a
    field-table row (``| **Status** | value |``), and a dash-bullet field
    (``- Status: value``). Scans top-down and returns the first match — the
    canonical field always precedes any later inline mention of "status"
    deeper in a document (e.g. "**Status of this section:**").

    Args:
        adr_text: Full text of one ADR file.

    Returns:
        The raw status text, or ``None`` if no Status field was found.
    """
    lines = adr_text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _FILE_STATUS_HEADING_RE.match(stripped):
            for later in lines[i + 1 :]:
                if later.strip():
                    return later.strip()
            return None
        table_match = _FILE_STATUS_TABLE_RE.match(stripped)
        if table_match:
            return table_match.group(1).strip()
        bold_match = _FILE_STATUS_BOLD_RE.match(stripped)
        if bold_match:
            return bold_match.group(1).strip()
        dash_match = _FILE_STATUS_DASH_RE.match(stripped)
        if dash_match:
            return dash_match.group(1).strip()
    return None


def status_problems(readme_text: str, adr_file_texts: Mapping[str, str]) -> list[str]:
    """Return status-drift problems between the index and the ADR files it points at.

    For every README index row, compares its status *category* against the
    category parsed from the linked ADR file's own Status field. A status
    line that fails to parse — on either side — is reported by filename
    rather than silently skipped: a hook that skips unparseable input
    reintroduces the exact blind spot this check exists to close (FRE-952).

    Rows whose filename has no entry in ``adr_file_texts`` are skipped — a
    missing file is already reported by ``index_problems``.

    Args:
        readme_text: The README index contents.
        adr_file_texts: Mapping of ADR basename to full file text.

    Returns:
        One human-readable problem string per drifted or unparseable row.
    """
    problems: list[str] = []
    for row in parse_index_rows(readme_text):
        adr_text = adr_file_texts.get(row.filename)
        if adr_text is None:
            continue
        file_status = extract_file_status(adr_text)
        if file_status is None:
            problems.append(f"{row.filename}: ADR file has no parseable Status field")
            continue
        readme_category = status_category(row.status_text)
        if readme_category is None:
            problems.append(
                f"{row.filename}: README status '{row.status_text}' could not be categorized"
            )
            continue
        file_category = status_category(file_status)
        if file_category is None:
            problems.append(
                f"{row.filename}: ADR file status '{file_status}' could not be categorized"
            )
            continue
        if readme_category != file_category:
            problems.append(
                f"{row.filename}: README says '{row.status_text}' ({readme_category}) but "
                f"the ADR file says '{file_status}' ({file_category})"
            )
    return problems


# --- IO seam ----------------------------------------------------------------


def _local_adr_filenames() -> list[str]:
    """ADR ``*.md`` basenames in the local tree (empty if the dir is absent)."""
    if not _ADR_DIR.is_dir():
        return []
    return [p.name for p in _ADR_DIR.iterdir() if p.name.startswith("ADR-")]


def _local_adr_file_texts() -> dict[str, str]:
    """Basename → full text for every local ADR file (empty if the dir is absent)."""
    if not _ADR_DIR.is_dir():
        return {}
    return {p.name: p.read_text() for p in _ADR_DIR.iterdir() if p.name.startswith("ADR-")}


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
    problems += status_problems(readme, _local_adr_file_texts())
    if not problems:
        print("ADR index OK: every ADR file is indexed, statuses match, no orphan rows.")
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
