"""Near-miss citation detection (ADR-0139 D1, AC-4, FRE-1332).

The detector is deliberately narrow: citation-shaped, containing ``@``, failing
:data:`CITATION_MARKER_PATTERN`. It only counts; resolving a near-miss against the
registry is D7 (FRE-1355) and is out of this ticket's scope.
"""

from __future__ import annotations

from personal_agent.grounding.citations import count_near_miss_markers

# FRE-1327's confabulated marker — fails on ordinal, hex and length.
FABRICATED_MARKER = "[S@bash-tempo-trace-dba5b2]"


def test_fires_on_the_fre_1327_fabricated_marker() -> None:
    text = f"Tempo shows a spike {FABRICATED_MARKER}."

    assert count_near_miss_markers(text) == 1


def test_does_not_fire_on_a_well_formed_marker() -> None:
    text = "Paris has 2.1 million residents [S1@0123456789abcdef]."

    assert count_near_miss_markers(text) == 0


def test_does_not_fire_on_an_identifier_free_marker() -> None:
    text = "See the source [S1] for details."

    assert count_near_miss_markers(text) == 0


def test_does_not_fire_on_ordinary_bracketed_prose() -> None:
    text = "See the appendix [see below] for the full table."

    assert count_near_miss_markers(text) == 0


def test_fires_on_a_nested_bracket_marker() -> None:
    """Codex's plan-review finding: excluding both bracket characters from the
    candidate pattern let a nested ``[`` hide a malformed marker entirely.
    """
    text = "Tempo shows a spike [S1@[0123]]."

    assert count_near_miss_markers(text) == 1


def test_counts_each_bracket_pair_once() -> None:
    text = f"{FABRICATED_MARKER} and {FABRICATED_MARKER} again."

    assert count_near_miss_markers(text) == 2


def test_adjacent_well_formed_markers_are_not_conflated_into_one_candidate() -> None:
    text = "[see below] [S1@0123456789abcdef]"

    assert count_near_miss_markers(text) == 0
