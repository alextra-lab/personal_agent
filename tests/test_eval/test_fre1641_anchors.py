"""FRE-1641 / ADR-0158 D5.1: anchor extraction and occurrences."""

from __future__ import annotations

from scripts.eval.fre1641_anchor_retention.anchors import (
    Anchor,
    TextIndex,
    extract_anchors,
    normalize_number,
)


def _values(claim: str, kind: str) -> list[str]:
    return [a.value for a in extract_anchors(claim) if a.kind == kind]


def test_numbers_are_normalized() -> None:
    assert normalize_number("1,119", None) == "1119"
    assert normalize_number("09", None) == "9"
    assert normalize_number("3", "50") == "3.5"
    assert normalize_number("159", "714") == "159.714"
    assert normalize_number("2", "0") == "2"
    assert _values("Revenue grew 45% to $1,119 million, a 3,5 ratio.", "number") == [
        "45",
        "1119",
        "3.5",
    ]


def test_dates_are_one_anchor_each() -> None:
    claim = "It shipped on 2026-09-04, after September 2026 and before 4 October 2026."
    assert _values(claim, "date") == ["2026-09-04", "2026-10-04", "2026-09"]
    # The date's digits are not numbers too.
    assert _values(claim, "number") == []


def test_names_skip_stop_words_and_months() -> None:
    claim = "Nemotron on the M4 Max beat Qwen in May. The GPT-5 run lost. A test."
    assert _values(claim, "name") == ["Nemotron", "M4", "Max", "Qwen", "GPT-5"]


def test_a_claim_with_no_anchor_is_empty() -> None:
    assert extract_anchors("it is faster than the other one") == []


def test_anchors_are_distinct() -> None:
    assert extract_anchors("Qwen and Qwen, 5 and 5") == [
        Anchor("name", "Qwen"),
        Anchor("number", "5"),
    ]


def test_occurrences_match_across_formats() -> None:
    text = "Released September 4, 2026 by NVIDIA; 1,119 runs at 45 percent. In 2026-10 more."
    index = TextIndex(text)
    (d,) = index.occurrences(Anchor("date", "2026-09-04"))
    assert text[d[0] : d[1]] == "September 4, 2026"
    assert index.occurrences(Anchor("date", "2026-09")) == [d]
    assert index.occurrences(Anchor("date", "2026-10")) != []
    (n,) = index.occurrences(Anchor("number", "1119"))
    assert text[n[0] : n[1]] == "1,119"
    assert index.occurrences(Anchor("number", "45")) != []
    # A claim's year matches the year inside a page date.
    assert len(index.occurrences(Anchor("number", "2026"))) == 2
    assert index.occurrences(Anchor("name", "NVIDIA")) != []
    assert index.occurrences(Anchor("name", "Nvidia")) == []
    assert index.occurrences(Anchor("name", "VIDIA")) == []
