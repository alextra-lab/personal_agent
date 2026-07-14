# ruff: noqa: D103
"""ADR numbering + index guard tests (scripts/next_adr.py).

Pins the two things this tool exists to prevent: a duplicate ADR number and a
drifted index — the failures that produced the ADR-0117 collision and the
0110-0118 index gap on 2026-07-14.
"""

from __future__ import annotations

from scripts.next_adr import index_problems, next_number, numbers_in


def test_numbers_parsed_and_duplicates_kept() -> None:
    names = ["ADR-0001-init.md", "ADR-0008b-course-correction.md", "README.md", "ADR-0008-x.md"]
    # 0008b keeps 0008; README ignored; the two 0008s are both returned (dup kept).
    assert numbers_in(names) == [1, 8, 8]


def test_next_number_is_max_plus_one_zero_padded() -> None:
    assert next_number({1, 8, 117}) == "0118"
    assert next_number(set()) == "0001"


def test_index_clean_when_every_file_has_a_row() -> None:
    files = ["ADR-0116-a.md", "ADR-0117-b.md"]
    readme = "| [ADR-0116](x.md) | t | Accepted |\n| [ADR-0117](y.md) | t | Accepted |"
    assert index_problems(files, readme) == []


def test_intentional_lettered_sibling_is_not_flagged() -> None:
    # ADR-0008 + ADR-0008b share the base number by design — not a collision.
    files = ["ADR-0008-hybrid.md", "ADR-0008-course-correction.md"]
    readme = "| [ADR-0008](x.md) | t | s |\n| [ADR-0008b](y.md) | t | s |"
    assert index_problems(files, readme) == []


def test_file_without_index_row_is_flagged() -> None:
    files = ["ADR-0118-new.md"]
    readme = "| [ADR-0117](x.md) | t | s |"  # 0118 missing from the index
    problems = index_problems(files, readme)
    assert any("ADR-0118 has a file but no README index row" in p for p in problems)


def test_index_row_without_file_is_flagged() -> None:
    files = ["ADR-0117-x.md"]
    readme = "| [ADR-0117](x.md) | t | s |\n| [ADR-0999](z.md) | t | s |"
    problems = index_problems(files, readme)
    assert any("README indexes ADR-0999 but no file exists" in p for p in problems)
