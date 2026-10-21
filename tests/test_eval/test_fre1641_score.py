"""FRE-1641 / ADR-0158 D5, D4, D6.1: support, retention and selection."""

from __future__ import annotations

from scripts.eval.fre1641_anchor_retention.anchors import TextIndex, extract_anchors
from scripts.eval.fre1641_anchor_retention.score import (
    MIN_CLASS_SUPPORTED,
    FetchCell,
    Retention,
    SearchCell,
    covered,
    fetch_kept,
    retention,
    search_kept,
    select_fetch,
    select_search,
    snippet_support,
    support_windows,
    url_key,
)

_CLAIM = "Nemotron decodes 159.714 tokens per second on the M4 Max."
_FILLER = "Unrelated filler text about other things. " * 40


def _page() -> str:
    return (
        _FILLER
        + "In our run, Nemotron reached 159.714 tok/s on an M4 Max laptop.\n"
        + _FILLER
        + "Nemotron alone, far away from the number."
    )


def test_url_key_ignores_scheme_www_fragment_and_trailing_slash() -> None:
    assert url_key("https://www.Example.com/a/b/#x") == url_key("http://example.com/a/b")
    assert url_key("https://e.com/a?x=1") != url_key("https://e.com/a?x=2")


def test_support_window_spans_the_co_occurring_anchors() -> None:
    page = _page()
    windows = support_windows(extract_anchors(_CLAIM), TextIndex(page))
    assert windows
    first = windows[0]
    assert page[first.start : first.end].startswith("Nemotron reached 159.714")
    assert first.end - first.start <= 600
    assert len(first.anchors) >= 2


def test_a_lone_anchor_far_from_the_others_does_not_support() -> None:
    page = _FILLER + "Nemotron alone. " + _FILLER + "159.714 elsewhere."
    assert support_windows(extract_anchors(_CLAIM), TextIndex(page)) == []


def test_the_only_anchor_is_enough_for_a_one_anchor_claim() -> None:
    windows = support_windows(extract_anchors("it costs 4,321 dollars"), TextIndex("x 4321 y"))
    assert len(windows) == 1


def test_full_form_keeps_and_seeded_negative_loses() -> None:
    page = _page()
    windows = support_windows(extract_anchors(_CLAIM), TextIndex(page))
    assert fetch_kept(windows, [(0, len(page))], page)  # AC-4: the full form
    assert not fetch_kept(windows, [(0, 200)], page)  # AC-3: the 200-character cut


def test_passages_must_cover_the_window_not_only_its_anchors() -> None:
    page = _page()
    (w, *_) = support_windows(extract_anchors(_CLAIM), TextIndex(page))
    # Two passages hold the two anchors but miss the text between them.
    anchors_only = [(w.start, w.start + 8), (w.end - 7, w.end)]
    assert not fetch_kept([w], anchors_only, page)
    assert fetch_kept([w], [(w.start - 5, w.end + 5)], page)


def test_whitespace_between_adjacent_passages_counts_as_covered() -> None:
    text = "alpha beta\ngamma delta"
    assert covered(0, len(text), [(0, 10), (11, len(text))], text)
    assert not covered(0, len(text), [(0, 5), (11, len(text))], text)


def test_search_support_and_kept() -> None:
    anchors = extract_anchors(_CLAIM)
    full = "Nemotron hits 159.714 tok/s on M4 Max per the benchmark."
    support = snippet_support(anchors, full)
    assert support is not None
    assert search_kept([support], [full])
    assert not search_kept([support], [full[:20]])
    assert snippet_support(anchors, "Nemotron only") is None


def _search(length: int, count: int, pooled: tuple[int, int], chars: float, **cls: tuple[int, int]):
    return SearchCell(
        length=length,
        count=count,
        pooled=Retention(*pooled),
        by_class={k: Retention(*v) for k, v in cls.items()},
        median_chars=chars,
    )


def test_search_selection_takes_the_smallest_passing_cell() -> None:
    cells = [
        _search(500, 5, (90, 100), 2000),  # fails the bar
        _search(800, 5, (96, 100), 4000),
        _search(800, 10, (99, 100), 3500),
        _search(1200, 5, (99, 100), 3500),  # same size, longer highlight
    ]
    sel = select_search(cells, blocked=None)
    assert sel.cell == "800×10"


def test_search_tie_goes_to_shorter_highlight_then_lower_count() -> None:
    cells = [_search(800, 10, (99, 100), 3000), _search(800, 5, (99, 100), 3000)]
    assert select_search(cells, None).cell == "800×5"


def test_a_scored_class_below_the_bar_fails_the_cell() -> None:
    big = MIN_CLASS_SUPPORTED
    ok = _search(500, 5, (96, 100), 1000, standard_thorough=(big, big))
    bad = _search(500, 10, (96, 100), 900, standard_thorough=(big - 5, big))
    # A class under the minimum is not scored on its own.
    small = _search(800, 5, (96, 100), 950, quick=(1, 4))
    assert not bad.passes
    assert small.passes
    assert select_search([ok, bad, small], None).cell == "800×5"


def test_unknown_class_is_pooled_only() -> None:
    big = MIN_CLASS_SUPPORTED
    cell = _search(500, 5, (96, 100), 1000, unknown=(1, big))
    assert cell.scored_classes() == []
    assert cell.passes


def test_no_passing_search_cell_selects_nothing() -> None:
    sel = select_search([_search(500, 5, (10, 100), 100)], None)
    assert sel.cell is None
    assert "Option B" in sel.rule


def test_a_blocked_run_selects_nothing() -> None:
    cells = [_search(500, 5, (100, 100), 100)]
    assert select_search(cells, "sample below minimum").cell is None
    fetch = [FetchCell("R1", 2000, Retention(100, 100), 0.0, 0.001, 0.0, 1900)]
    assert select_fetch(fetch, "seeded negative passed").cell is None


def _fetch(arm: str, budget: int, kept: int, cost: float, latency: float) -> FetchCell:
    return FetchCell(arm, budget, Retention(kept, 100), cost, latency, 0.0, budget * 0.9)


def test_fetch_selection_is_the_cheapest_passing_arm_at_its_smallest_budget() -> None:
    cells = [
        _fetch("R1", 2000, 90, 0.0, 0.002),
        _fetch("R1", 3000, 96, 0.0, 0.002),
        _fetch("R1", 4000, 99, 0.0, 0.002),
        _fetch("R2", 2000, 99, 0.001, 0.5),
        _fetch("R3", 2000, 99, 0.0002, 0.4),
    ]
    sel = select_fetch(cells, None)
    assert sel.cell == "R1×3000"


def test_fetch_tie_order_is_r1_r4_r3_r2() -> None:
    cells = [
        _fetch("R2", 2000, 99, 0.0, 0.1),
        _fetch("R3", 2000, 99, 0.0, 0.1),
        _fetch("R4", 2000, 99, 0.0, 0.1),
    ]
    assert select_fetch(cells, None).cell == "R4×2000"
    cells.append(_fetch("R1", 4000, 99, 0.0, 0.1))
    assert select_fetch(cells, None).cell == "R1×4000"


def test_no_passing_fetch_cell_selects_nothing() -> None:
    sel = select_fetch([_fetch("R1", 2000, 10, 0.0, 0.0)], None)
    assert sel.cell is None
    assert "owner" in sel.rule


def test_retention_share_and_bar() -> None:
    r = retention([True] * 95 + [False] * 5)
    assert (r.kept, r.supported) == (95, 100)
    assert r.passes
    assert not retention([True] * 94 + [False] * 6).passes
    assert not retention([]).passes
