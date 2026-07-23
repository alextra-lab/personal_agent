# ruff: noqa: D103
"""ADR numbering + index guard tests (scripts/next_adr.py).

Pins the two things this tool exists to prevent: a duplicate ADR number, a
drifted index, and — since FRE-952 — a status column that no longer matches
the ADR file it points at. These are the failures that produced the ADR-0117
collision (2026-07-14), the 0110-0118 index gap (2026-07-14), and the 18-row
status drift found while drafting ADR-0124 (2026-07-23).
"""

from __future__ import annotations

from scripts.next_adr import (
    IndexRow,
    extract_file_status,
    index_problems,
    next_number,
    numbers_in,
    parse_index_rows,
    status_category,
    status_problems,
)


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


# --- status_category ---------------------------------------------------------


def test_status_category_matches_bare_word() -> None:
    assert status_category("Accepted") == "accepted"
    assert status_category("Proposed") == "proposed"


def test_status_category_ignores_markdown_and_trailing_detail() -> None:
    # Links, bold, emoji, and trailing PR/date detail must not block the match —
    # only a genuine category change should trip the comparison.
    text = "**Superseded** by [ADR-0120](ADR-0120-x.md) (2026-07-16) ✅"
    assert status_category(text) == "superseded"


def test_status_category_prefers_longest_matching_category() -> None:
    # "partially delivered" must not collapse to a bare "partially" or get
    # confused with the unrelated "partially superseded" category.
    assert status_category("Partially Delivered (evolved by Redesign v2)") == "partially delivered"
    assert (
        status_category("Partially superseded by ADR-0084 (D1 plumbing stands)")
        == "partially superseded"
    )


def test_status_category_unparseable_returns_none() -> None:
    assert status_category("Needs Discussion") is None
    assert status_category("") is None


# --- parse_index_rows ---------------------------------------------------------


def test_parse_index_rows_keys_on_filename_not_display_number() -> None:
    # The FRE-952 gotcha: two files share display number ADR-0008 (one plain,
    # one lettered). The row's filename is the join key, never the number.
    readme = (
        "| [ADR-0008](ADR-0008-hybrid.md) | Hybrid Tool Calling | Accepted |\n"
        "| [ADR-0008b](ADR-0008-course-correction.md) | Course Correction | Superseded |"
    )
    rows = parse_index_rows(readme)
    assert rows == [
        IndexRow(filename="ADR-0008-hybrid.md", status_text="Accepted"),
        IndexRow(filename="ADR-0008-course-correction.md", status_text="Superseded"),
    ]


def test_parse_index_rows_handles_extra_trailing_columns() -> None:
    readme = "| [ADR-0043](ADR-0043-x.md) | Title | Accepted | Decision summary text |"
    rows = parse_index_rows(readme)
    assert rows == [IndexRow(filename="ADR-0043-x.md", status_text="Accepted")]


# --- extract_file_status -------------------------------------------------------


def test_extract_file_status_heading_style() -> None:
    text = "# ADR-0001: Init\n\n## Status\n\nAccepted\n\n## Context\n"
    assert extract_file_status(text) == "Accepted"


def test_extract_file_status_bold_colon_after_stars() -> None:
    text = "# ADR-0002\n\n**Status:** Accepted — evolved by Redesign v2\n**Date:** 2025-12-28\n"
    assert extract_file_status(text) == "Accepted — evolved by Redesign v2"


def test_extract_file_status_bold_stars_before_colon() -> None:
    text = "# ADR-0008\n\n**Status**: Accepted\n"
    assert extract_file_status(text) == "Accepted"


def test_extract_file_status_dash_bullet_style() -> None:
    text = "# ADR-0003\n\n- Status: Superseded — by ADR-0008\n- Date: 2025-12-28\n"
    assert extract_file_status(text) == "Superseded — by ADR-0008"


def test_extract_file_status_table_style() -> None:
    text = "# ADR-0083\n\n| Field | Value |\n|---|---|\n| **Status** | Accepted |\n| **Date** | 2026-06-02 |\n"
    assert extract_file_status(text) == "Accepted"


def test_extract_file_status_ignores_later_inline_status_mentions() -> None:
    # A secondary "**Status of this section:**" deeper in the doc must not
    # shadow the canonical field that precedes it.
    text = (
        "# ADR-0081\n\n**Status:** Implemented — 2026-05-29\n\n"
        "## D4\n\n**Status of this section:** Decided 2026-06-01.\n"
    )
    assert extract_file_status(text) == "Implemented — 2026-05-29"


def test_extract_file_status_returns_none_when_absent() -> None:
    assert extract_file_status("# ADR-9999\n\nNo status field here.\n") is None


# --- status_problems (AC1, AC2, AC3) -------------------------------------------


def test_status_problems_empty_when_categories_match() -> None:
    readme = "| [ADR-0065](ADR-0065-x.md) | Title | Superseded by ADR-0120 (2026-07-16) |"
    files = {"ADR-0065-x.md": "# ADR-0065\n\n**Status:** Superseded by [ADR-0120](y.md)\n"}
    assert status_problems(readme, files) == []


def test_status_problems_flags_drifted_status() -> None:
    # AC1: index says Accepted, the file it points at says Superseded.
    readme = "| [ADR-0065](ADR-0065-x.md) | Title | Accepted |"
    files = {"ADR-0065-x.md": "# ADR-0065\n\n**Status:** Superseded by [ADR-0120](y.md)\n"}
    problems = status_problems(readme, files)
    assert len(problems) == 1
    assert "ADR-0065-x.md" in problems[0]


def test_status_problems_keyed_by_filename_catches_swapped_rows() -> None:
    # AC2: two files share display number ADR-0008 with different statuses.
    # Swapped rows (each row pointing at the other file's status) must fail.
    files = {
        "ADR-0008-hybrid.md": "# ADR-0008\n\n**Status:** Accepted\n",
        "ADR-0008-course-correction.md": "# ADR-0008b\n\n**Status:** Superseded\n",
    }
    swapped_readme = (
        "| [ADR-0008](ADR-0008-hybrid.md) | Hybrid | Superseded |\n"
        "| [ADR-0008b](ADR-0008-course-correction.md) | Course Correction | Accepted |"
    )
    problems = status_problems(swapped_readme, files)
    assert len(problems) == 2


def test_status_problems_keyed_by_filename_passes_when_correct() -> None:
    # AC2, positive case: same fixture, rows in the correct order.
    files = {
        "ADR-0008-hybrid.md": "# ADR-0008\n\n**Status:** Accepted\n",
        "ADR-0008-course-correction.md": "# ADR-0008b\n\n**Status:** Superseded\n",
    }
    correct_readme = (
        "| [ADR-0008](ADR-0008-hybrid.md) | Hybrid | Accepted |\n"
        "| [ADR-0008b](ADR-0008-course-correction.md) | Course Correction | Superseded |"
    )
    assert status_problems(correct_readme, files) == []


def test_status_problems_unparseable_file_status_fails_naming_the_file() -> None:
    # AC3: a status line that cannot be parsed must fail loudly, naming the
    # file, rather than being silently skipped.
    readme = "| [ADR-9999](ADR-9999-x.md) | Title | Accepted |"
    files = {"ADR-9999-x.md": "# ADR-9999\n\nNo status field at all.\n"}
    problems = status_problems(readme, files)
    assert len(problems) == 1
    assert "ADR-9999-x.md" in problems[0]


def test_status_problems_unparseable_readme_status_fails_naming_the_file() -> None:
    readme = "| [ADR-9999](ADR-9999-x.md) | Title | Needs Discussion |"
    files = {"ADR-9999-x.md": "# ADR-9999\n\n**Status:** Accepted\n"}
    problems = status_problems(readme, files)
    assert len(problems) == 1
    assert "ADR-9999-x.md" in problems[0]


def test_status_problems_skips_rows_whose_file_is_missing() -> None:
    # A missing file is already reported by index_problems; status_problems
    # must not double-report it.
    readme = "| [ADR-9999](ADR-9999-x.md) | Title | Accepted |"
    assert status_problems(readme, {}) == []
