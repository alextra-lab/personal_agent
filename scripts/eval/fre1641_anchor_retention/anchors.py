"""Anchor extraction and anchor occurrences (ADR-0158 D5.1).

The anchors of a claim are its numbers, its dates and its capitalized names. A verbatim form of
a page or a snippet keeps or loses them as tokens, so they test whether a smaller form still
holds the fact.

- **Dates** first: ISO (``2026-09-04``, ``2026-09``), ``September 4, 2026``, ``4 September
  2026`` and ``September 2026``, normalized to ``YYYY-MM-DD`` or ``YYYY-MM``. In page text a full
  date also counts as its month, so a claim that names the month matches it.
- **Names** next: a token of two or more characters that starts with a capital letter, matched
  case-sensitively. A stop word or a month name is not a name. A sentence-initial word counts:
  a claim often starts with the name it is about, and case-sensitive matching keeps a common
  word such as ``Measured`` from matching ordinary page text.
- **Numbers** last: thousands separators and ``%`` are dropped, a decimal comma becomes a dot,
  leading zeros and trailing decimal zeros are dropped.

A claim's dates and names are masked before its numbers are read, so ``2026-09-04`` is one date
anchor, not three numbers. Page text is not masked: a claim's ``2026`` matches the year inside
a page's ``2026-09-04``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from personal_agent.tools.passages import STOP_WORDS

AnchorKind = Literal["number", "date", "name"]
Span = tuple[int, int]

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH = r"(?P<mon>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>\d{4})"

_DATE_PATTERNS = (
    re.compile(r"(?<![\w-])(?P<year>\d{4})-(?P<mnum>\d{1,2})(?:-(?P<day>\d{1,2}))?(?![\w-])"),
    re.compile(rf"(?<!\w){_MONTH}\s+{_DAY},?\s+{_YEAR}(?!\w)", re.IGNORECASE),
    re.compile(rf"(?<!\w){_DAY}\s+{_MONTH},?\s+{_YEAR}(?!\w)", re.IGNORECASE),
    re.compile(rf"(?<!\w){_MONTH},?\s+{_YEAR}(?!\w)", re.IGNORECASE),
)

_NAME = re.compile(r"(?<![\w])[A-Z][\w]*(?:[-.'’][\w]+)*")
_NUMBER = re.compile(r"(?<![\w.])(?P<int>\d{1,3}(?:[,  ]\d{3})+|\d+)(?:[.,](?P<dec>\d+))?(?!\d)")


@dataclass(frozen=True, order=True)
class Anchor:
    """One anchor of a claim.

    Attributes:
        kind: ``number``, ``date`` or ``name``.
        value: The normalized value.
    """

    kind: AnchorKind
    value: str


def _date_value(match: re.Match[str]) -> str | None:
    groups = match.groupdict()
    year = int(groups["year"])
    month = int(groups["mnum"]) if groups.get("mnum") else _MONTHS[groups["mon"].lower()]
    if not 1 <= month <= 12 or not 1000 <= year <= 2999:
        return None
    day = groups.get("day")
    if day is None:
        return f"{year:04d}-{month:02d}"
    if not 1 <= int(day) <= 31:
        return None
    return f"{year:04d}-{month:02d}-{int(day):02d}"


def _dates(text: str) -> list[tuple[str, Span]]:
    """Every date in ``text``, with its span. A span claimed by an earlier pattern is skipped."""
    found: list[tuple[str, Span]] = []
    taken: list[Span] = []
    for pattern in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            span = m.span()
            if any(s < span[1] and span[0] < e for s, e in taken):
                continue
            value = _date_value(m)
            if value is not None:
                found.append((value, span))
                taken.append(span)
    return found


def normalize_number(int_part: str, dec_part: str | None) -> str:
    """Normalize one number token.

    Args:
        int_part: The integer digits, possibly with thousands separators.
        dec_part: The decimal digits, or ``None``.

    Returns:
        The digits without separators or leading zeros, with a ``.`` and the decimal digits
        without trailing zeros when any remain.
    """
    whole = re.sub(r"\D", "", int_part).lstrip("0") or "0"
    dec = (dec_part or "").rstrip("0")
    return f"{whole}.{dec}" if dec else whole


def _numbers(text: str) -> list[tuple[str, Span]]:
    return [
        (normalize_number(m.group("int"), m.group("dec")), m.span()) for m in _NUMBER.finditer(text)
    ]


def _mask(text: str, spans: Sequence[Span]) -> str:
    chars = list(text)
    for s, e in spans:
        chars[s:e] = " " * (e - s)
    return "".join(chars)


def _is_name(token: str) -> bool:
    bare = re.sub(r"['’]s$", "", token)
    return len(bare) >= 2 and bare.lower() not in STOP_WORDS and bare.lower() not in _MONTHS


def extract_anchors(claim: str) -> list[Anchor]:
    """Return the distinct anchors of a claim, in order of first appearance (D5.1).

    Args:
        claim: The finding's claim text.

    Returns:
        The anchors. An empty list means the finding is excluded.
    """
    out: list[Anchor] = []
    dates = _dates(claim)
    out.extend(Anchor("date", v) for v, _ in dates)
    masked = _mask(claim, [s for _, s in dates])
    names = [
        (re.sub(r"['’]s$", "", m.group()), m.span())
        for m in _NAME.finditer(masked)
        if _is_name(m.group())
    ]
    out.extend(Anchor("name", v) for v, _ in names)
    masked = _mask(masked, [m.span() for m in _NAME.finditer(masked)])
    out.extend(Anchor("number", v) for v, _ in _numbers(masked))
    seen: set[Anchor] = set()
    distinct: list[Anchor] = []
    for a in out:
        if a not in seen:
            seen.add(a)
            distinct.append(a)
    return distinct


@dataclass
class TextIndex:
    """The number and date occurrences of one text, built once and searched per anchor.

    Attributes:
        text: The indexed text.
    """

    text: str
    _numbers: dict[str, list[Span]] = field(default_factory=dict, init=False)
    _dates: dict[str, list[Span]] = field(default_factory=dict, init=False)
    _names: dict[str, list[Span]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for value, span in _numbers(self.text):
            self._numbers.setdefault(value, []).append(span)
        for value, span in _dates(self.text):
            self._dates.setdefault(value, []).append(span)
            if len(value) == 10:
                self._dates.setdefault(value[:7], []).append(span)

    def occurrences(self, anchor: Anchor) -> list[Span]:
        """Return the spans where ``anchor`` occurs in the text.

        Args:
            anchor: The anchor.

        Returns:
            The spans, in text order.
        """
        match anchor.kind:
            case "number":
                return self._numbers.get(anchor.value, [])
            case "date":
                return sorted(self._dates.get(anchor.value, []))
            case "name":
                if anchor.value not in self._names:
                    pattern = re.compile(rf"(?<![\w]){re.escape(anchor.value)}(?![\w])")
                    self._names[anchor.value] = [m.span() for m in pattern.finditer(self.text)]
                return self._names[anchor.value]
