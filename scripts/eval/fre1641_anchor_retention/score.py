"""Support, retention and selection (ADR-0158 D5.3 to D5.8, D4, D6.1).

Pure functions over recorded fixtures. Nothing here reads the network or a store.

**Support window (fetch, D5.3).** For a finding, the harness finds every occurrence of each anchor
in the page fixture. From each occurrence, the support set is the distinct anchors that occur
wholly inside the next ``WINDOW_CHARS`` characters. It qualifies if it holds at least two anchors,
or the finding's only anchor. The window runs from that occurrence to the occurrence that
completes the set, and the harness records its offsets.

**Kept (fetch, D5.6).** A form keeps a finding if its returned text covers the offsets of one
recorded window. Whitespace between two returned passages counts as covered. Other text with the
same anchors does not count.

**Support and kept (search, D5.3, D5.6).** A finding is supported if a full-form result of the
same trace has the finding's URL and its snippet holds a support set (at least two anchors, or
the only anchor). A form keeps it if one of its results with that URL has a snippet that holds
every anchor of one such support set.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from scripts.eval.fre1641_anchor_retention.anchors import Anchor, Span, TextIndex

#: D5.3: one window of 600 characters.
WINDOW_CHARS = 600
#: D5.7: the bar.
BAR = 0.95
#: D5.5: the minimum sample.
MIN_FETCH_SUPPORTED = 200
MIN_SEARCH_SUPPORTED = 150
MIN_CLASS_SUPPORTED = 50
#: D6.3: the helper's thoroughness classes.
SCORED_CLASSES = frozenset({"quick", "standard_thorough"})
#: Recorded windows per finding, a bound on pathological pages.
MAX_WINDOWS = 50
#: D4 tie order after cost and latency.
ARM_TIE_ORDER = ("R1", "R4", "R3", "R2")


def url_key(url: str) -> str:
    """Normalize a URL for matching a finding's source to a search result.

    The scheme and a leading ``www.`` are dropped, the host is lower-cased, and the fragment and
    a trailing ``/`` are removed. The query string stays.

    Args:
        url: The URL.

    Returns:
        The match key.
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    return urlunsplit(("", host, path, parts.query, "")).lstrip("/")


def required_anchors(anchors: Sequence[Anchor]) -> int:
    """Return how many anchors a support set needs: two, or one for a one-anchor finding."""
    return 1 if len(anchors) == 1 else 2


@dataclass(frozen=True)
class Window:
    """One recorded support window.

    Attributes:
        start: Offset of the window's first anchor occurrence.
        end: Offset after the occurrence that completes the support set.
        anchors: The support set.
    """

    start: int
    end: int
    anchors: frozenset[Anchor]


def support_windows(anchors: Sequence[Anchor], index: TextIndex) -> list[Window]:
    """Record the support windows of one finding in one page fixture.

    For each anchor occurrence, in text order, the support set is the distinct anchors whose
    occurrences lie wholly inside the ``WINDOW_CHARS`` characters that start there. The window
    ends at the first occurrence that completes that set, so its span is minimal for its start.

    Args:
        anchors: The finding's anchors.
        index: The fixture's text index.

    Returns:
        The distinct windows, at most ``MAX_WINDOWS``. Empty means the fixture does not support
        the finding.
    """
    need = required_anchors(anchors)
    occ = sorted((s, e, a) for a in anchors for s, e in index.occurrences(a))
    windows: list[Window] = []
    for i, (start, _, _) in enumerate(occ):
        limit = start + WINDOW_CHARS
        inside = [o for o in occ[i:] if o[0] < limit and o[1] <= limit]
        support = frozenset(o[2] for o in inside)
        if len(support) < need:
            continue
        seen: set[Anchor] = set()
        end = start
        for _, e, a in inside:
            seen.add(a)
            end = max(end, e)
            if seen == support:
                break
        window = Window(start=start, end=end, anchors=support)
        if window not in windows:
            windows.append(window)
        if len(windows) >= MAX_WINDOWS:
            break
    return windows


def covered(start: int, end: int, passages: Sequence[Span], text: str) -> bool:
    """Whether the returned passages cover ``text[start:end]``.

    Characters between two passages that are only whitespace count as covered: the split drops
    the line breaks between blocks.

    Args:
        start: Start offset.
        end: End offset.
        passages: The ``(start, end)`` offsets of the returned text.
        text: The page text.

    Returns:
        True if every character of the span lies in a passage or is whitespace between them.
    """
    pos = start
    for ps, pe in sorted(passages):
        if pe <= pos or ps >= end:
            continue
        if ps > pos and text[pos:ps].strip():
            return False
        pos = max(pos, pe)
        if pos >= end:
            return True
    return not text[pos:end].strip()


def fetch_kept(windows: Sequence[Window], passages: Sequence[Span], text: str) -> bool:
    """Whether a fetch form keeps a supported finding (D5.6).

    Args:
        windows: The finding's recorded support windows.
        passages: The ``(start, end)`` offsets of the text the form returns.
        text: The page text.

    Returns:
        True if the returned text covers the offsets of one recorded window.
    """
    return any(covered(w.start, w.end, passages, text) for w in windows)


def snippet_support(anchors: Sequence[Anchor], snippet: str) -> frozenset[Anchor] | None:
    """Return the support set a snippet holds for a finding, or ``None`` if it holds none.

    Args:
        anchors: The finding's anchors.
        snippet: The search result's snippet.

    Returns:
        The anchors that occur in the snippet, if they are enough to support the finding.
    """
    index = TextIndex(snippet)
    present = frozenset(a for a in anchors if index.occurrences(a))
    return present if len(present) >= required_anchors(anchors) else None


def search_kept(support_sets: Iterable[frozenset[Anchor]], snippets: Sequence[str]) -> bool:
    """Whether a search form keeps a supported finding (D5.6).

    Args:
        support_sets: The finding's support sets from the full form.
        snippets: The form's snippets for results with the finding's URL.

    Returns:
        True if one snippet holds every anchor of one support set.
    """
    indexes = [TextIndex(s) for s in snippets]
    return any(all(ix.occurrences(a) for a in sset) for sset in support_sets for ix in indexes)


@dataclass(frozen=True)
class Retention:
    """Retention of one form over its supported findings.

    Attributes:
        kept: Supported findings the form keeps.
        supported: Supported findings.
    """

    kept: int
    supported: int

    @property
    def share(self) -> float:
        """Kept over supported, 0.0 when nothing is supported."""
        return self.kept / self.supported if self.supported else 0.0

    @property
    def passes(self) -> bool:
        """Whether the form meets the 95% bar (D5.7)."""
        return self.supported > 0 and self.share >= BAR


def retention(flags: Iterable[bool]) -> Retention:
    """Count the kept flags of the supported findings.

    Args:
        flags: One kept flag per supported finding.

    Returns:
        The retention.
    """
    values = list(flags)
    return Retention(kept=sum(values), supported=len(values))


def median(values: Sequence[float]) -> float:
    """Median, 0.0 for an empty sequence."""
    return float(statistics.median(values)) if values else 0.0


@dataclass(frozen=True)
class SearchCell:
    """One search cell of D2.1.

    Attributes:
        length: Exa ``content_max_characters``.
        count: Result count.
        pooled: Retention over all supported search findings.
        by_class: Retention per task class.
        median_chars: Median rendered result characters over the replayed searches.
    """

    length: int
    count: int
    pooled: Retention
    by_class: Mapping[str, Retention]
    median_chars: float

    def scored_classes(self) -> list[str]:
        """The D6.3 classes with enough supported findings to be scored on their own (D5.5).

        ``unknown`` (a helper with no ``thoroughness``) is not a D6.3 class. It counts in the
        pooled score only.
        """
        return sorted(
            c
            for c, r in self.by_class.items()
            if c in SCORED_CLASSES and r.supported >= MIN_CLASS_SUPPORTED
        )

    @property
    def passes(self) -> bool:
        """D6.1: the bar on the pooled findings and on each class scored on its own."""
        return self.pooled.passes and all(self.by_class[c].passes for c in self.scored_classes())


@dataclass(frozen=True)
class FetchCell:
    """One fetch cell of D4.

    Attributes:
        arm: The ranker arm.
        budget: The passage budget in characters.
        retention: Retention over the supported fetch findings.
        cost_per_fetch: Mean vendor cost per fetch, USD.
        latency_s: Median added ranking latency per fetch, seconds.
        leaves_host: Share of fetches whose page text went to a vendor.
        median_chars: Median returned passage characters.
    """

    arm: str
    budget: int
    retention: Retention
    cost_per_fetch: float
    latency_s: float
    leaves_host: float
    median_chars: float


@dataclass(frozen=True)
class Selection:
    """The cell a rule picked, or why none was picked.

    Attributes:
        cell: ``"<a>×<b>"`` label of the picked cell, or ``None``.
        rule: The rule and the facts that decided it.
    """

    cell: str | None
    rule: str


def select_search(cells: Sequence[SearchCell], blocked: str | None) -> Selection:
    """Pick the search cell by D6.1: the smallest passing cell.

    Smallest is the fewest median result characters. A tie goes to the shorter highlight, then
    to the lower result count.

    Args:
        cells: The 8 search cells.
        blocked: A reason the run cannot select (sample or instrument), or ``None``.

    Returns:
        The selection.
    """
    if blocked:
        return Selection(None, f"No selection: {blocked}.")
    passing = [c for c in cells if c.passes]
    if not passing:
        return Selection(
            None,
            "No search cell passes D6.1. Option B is the owner's decision (D6.3); no search "
            "change ships.",
        )
    best = min(passing, key=lambda c: (c.median_chars, c.length, c.count))
    return Selection(
        f"{best.length}×{best.count}",
        f"D6.1: {len(passing)} of {len(cells)} cells pass; the smallest by median result "
        f"characters is {best.length}×{best.count} ({best.median_chars:.0f}); ties go to the "
        "shorter highlight, then the lower count.",
    )


def select_fetch(cells: Sequence[FetchCell], blocked: str | None) -> Selection:
    """Pick the fetch cell by D4: the cheapest passing arm, with its smallest passing budget.

    Cheapest orders by cost per fetch, then added latency, then the fixed order R1, R4, R3, R2.

    Args:
        cells: The 12 fetch cells.
        blocked: A reason the run cannot select (sample or instrument), or ``None``.

    Returns:
        The selection.
    """
    if blocked:
        return Selection(None, f"No selection: {blocked}.")
    passing = [c for c in cells if c.retention.passes]
    if not passing:
        return Selection(
            None, "No fetch cell passes D4. The table goes to the owner before T3 ships."
        )
    best = min(
        passing,
        key=lambda c: (c.cost_per_fetch, c.latency_s, ARM_TIE_ORDER.index(c.arm), c.budget),
    )
    arms = sorted({c.arm for c in passing}, key=ARM_TIE_ORDER.index)
    return Selection(
        f"{best.arm}×{best.budget}",
        f"D4: passing arms {', '.join(arms)}; the cheapest is {best.arm} "
        f"(${best.cost_per_fetch:.5f} per fetch, {best.latency_s:.3f} s added); its smallest "
        f"passing budget is {best.budget}.",
    )
