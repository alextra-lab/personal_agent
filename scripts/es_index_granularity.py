#!/usr/bin/env python3
r"""Classify an Elasticsearch index name's date granularity, robustly (FRE-1035).

A live agent turn once inferred index granularity by shell-stripping a trailing
date off an index name — one substitution for dash-dated names, a second for
dot-dated names that stripped only month/day, leaving the year attached. Run
against a dot-dated name, the second substitution produced ``agent-logs-2026``,
a string that looks like a real yearly index but is a truncation artifact. The
agent reported it as real.

This module replaces that improvisation with an anchored, per-shape matcher:
``classify_index_period`` only ever returns a period it matched **in full**
against one of the known shapes for the requested family, and returns ``None``
for anything else — including a name that belongs to a sibling family sharing a
prefix (e.g. ``agent-captains-captures-subagents-*`` under the
``agent-captains-captures`` prefix). It never truncates or guesses.

CLI usage — classify a family's live indices without hitting document data:

    curl -s 'http://elasticsearch:9200/_cat/indices?h=index' \
      | grep '^agent-monitors-joinability-' \
      | python3 scripts/es_index_granularity.py agent-monitors-joinability
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Granularity = Literal["daily", "monthly"]


@dataclass(frozen=True)
class IndexPeriod:
    """The period one index name encodes, for a known shape.

    Attributes:
        granularity: Whether the index is a daily or monthly bucket.
        year: Four-digit year.
        month: Two-digit month.
        day: Two-digit day, or ``None`` for a monthly bucket.
    """

    granularity: Granularity
    year: str
    month: str
    day: str | None


@dataclass(frozen=True)
class FamilyGranularityReport:
    """A family's index names bucketed by granularity.

    Attributes:
        family_prefix: The family prefix the report was built for.
        daily: Index names classified as daily buckets.
        monthly: Index names classified as monthly buckets.
        unrecognized: Index names that matched no known shape — always
            surfaced, never silently dropped.
    """

    family_prefix: str
    daily: tuple[str, ...]
    monthly: tuple[str, ...]
    unrecognized: tuple[str, ...]

    @property
    def is_mixed(self) -> bool:
        """Whether both daily and monthly indices are live for this family at once."""
        return bool(self.daily) and bool(self.monthly)


# sep is already a regex fragment (e.g. r"\." or "-"), not a literal to escape.
def _daily_pattern(sep: str) -> re.Pattern[str]:
    return re.compile(rf"^(?P<year>\d{{4}}){sep}(?P<month>\d{{2}}){sep}(?P<day>\d{{2}})(?:-v2)?$")


# sep is already a regex fragment (e.g. r"\." or "-"), not a literal to escape.
def _monthly_pattern(sep: str) -> re.Pattern[str]:
    return re.compile(rf"^(?P<year>\d{{4}}){sep}(?P<month>\d{{2}})$")


# Four patterns below, six shapes total counting the two optional -v2 daily variants,
# each anchored end-to-end against the suffix after the family prefix — never a
# partial match, never a truncation.
_SHAPES: tuple[tuple[Granularity, re.Pattern[str]], ...] = (
    ("daily", _daily_pattern("-")),
    ("daily", _daily_pattern(r"\.")),
    ("monthly", _monthly_pattern("-")),
    ("monthly", _monthly_pattern(r"\.")),
)


def classify_index_period(index_name: str, family_prefix: str) -> IndexPeriod | None:
    """Classify one index name's date granularity for a known family prefix.

    Matches the suffix after ``family_prefix + "-"`` against each of the
    observed shapes in full (dash-daily, dot-daily, either with a trailing
    ``-v2``, dash-monthly, dot-monthly) rather than stripping tokens off the
    end. A name that matches none of them — including a sibling family sharing
    the prefix — returns ``None``.

    Args:
        index_name: The full index name as returned by Elasticsearch, verbatim.
        family_prefix: The family's fixed prefix, e.g. ``"agent-logs"``.

    Returns:
        The parsed period, or ``None`` if the name doesn't match a known shape
        for this exact family.
    """
    prefix = f"{family_prefix}-"
    if not index_name.startswith(prefix):
        return None
    rest = index_name[len(prefix) :]
    for granularity, pattern in _SHAPES:
        match = pattern.match(rest)
        if match is not None:
            return IndexPeriod(
                granularity, match["year"], match["month"], match.groupdict().get("day")
            )
    return None


def report_family_granularity(
    index_names: Iterable[str], family_prefix: str
) -> FamilyGranularityReport:
    """Bucket a family's index names into daily / monthly / unrecognized.

    Args:
        index_names: Index names to classify, e.g. from `_cat/indices`.
        family_prefix: The family's fixed prefix, e.g. ``"agent-logs"``.

    Returns:
        The bucketed report. ``unrecognized`` always lists every name that
        matched no known shape — nothing is dropped.
    """
    daily: list[str] = []
    monthly: list[str] = []
    unrecognized: list[str] = []
    for name in index_names:
        period = classify_index_period(name, family_prefix)
        if period is None:
            unrecognized.append(name)
        elif period.granularity == "daily":
            daily.append(name)
        else:
            monthly.append(name)
    return FamilyGranularityReport(family_prefix, tuple(daily), tuple(monthly), tuple(unrecognized))


def main() -> int:
    """Read index names on stdin and report one family's granularity mix.

    Returns:
        0 on success, 2 on usage error.
    """
    args = sys.argv[1:]
    if len(args) != 1:
        print(
            "usage: es_index_granularity.py <family-prefix>  (index names on stdin, one per line)",
            file=sys.stderr,
        )
        return 2
    family_prefix = args[0]
    names = [line.strip() for line in sys.stdin if line.strip()]
    report = report_family_granularity(names, family_prefix)

    print(f"family: {family_prefix}")
    print(f"  daily:   {len(report.daily)}")
    print(f"  monthly: {len(report.monthly)}")
    if report.is_mixed:
        print(
            "  MIXED — both daily and monthly indices exist; a query or index-name "
            "assumption scoped to only one shape will silently miss data"
        )
    if report.unrecognized:
        print(f"  unrecognized ({len(report.unrecognized)}) — not matched, not counted:")
        for name in report.unrecognized:
            print(f"    {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
