"""FRE-720 -- pure loader for the labeled proposal pair set (ADR-0105 D10 / AC-8).

The shape here is pairwise (same-idea vs distinct-idea real proposals), unlike
``scripts/eval/fre435_memory_recall/probes.py``'s query-vs-entity recall shape --
so this loader mirrors that file's idiom (explicit ``KeyError`` -> a ``ValueError``
subclass, a degenerate-set guard) rather than reusing its ``ProbeCase`` schema.

Free of any ``personal_agent`` / substrate import so it is fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]


class PairSetError(ValueError):
    """Raised when a corpus or pair set is malformed or degenerate.

    A *degenerate* pair set has no positive pair, no negative pair, or is empty --
    such a set cannot measure separation (there is nothing to separate against).
    """


@dataclass(frozen=True)
class CorpusEntry:
    """One real proposal text referenced by the pair set.

    Attributes:
        entry_id: The source `agent-captains-reflections-*` document id.
        text: The proposal's embeddable text (`what` + `why`, verbatim).
        category: The proposal's `ChangeCategory` label, if present.
        scope: The proposal's `ChangeScope` label, if present.
        timestamp: The source document's ISO-8601 `timestamp`, if present.
    """

    entry_id: str
    text: str
    category: str | None = None
    scope: str | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class Corpus:
    """The loaded corpus, keyed by `entry_id`."""

    entries: dict[str, CorpusEntry]

    def __contains__(self, entry_id: str) -> bool:
        """Whether `entry_id` is present in the corpus."""
        return entry_id in self.entries

    def __getitem__(self, entry_id: str) -> CorpusEntry:
        """The `CorpusEntry` for `entry_id`."""
        return self.entries[entry_id]


@dataclass(frozen=True)
class PairCase:
    """One labeled proposal pair.

    Attributes:
        pair_id: Stable identifier (referenced in the probe artifact).
        a: The first proposal's `entry_id` (must exist in the loaded `Corpus`).
        b: The second proposal's `entry_id` (must exist in the loaded `Corpus`).
        label: `"positive"` (same idea, reworded) or `"negative"` (same
            category/topical family, genuinely distinct proposal -- a hard
            near-miss).
        note: A one-line human justification for the label.
    """

    pair_id: str
    a: str
    b: str
    label: Literal["positive", "negative"]
    note: str = ""


def load_corpus(path: Path) -> Corpus:
    """Load the committed proposal corpus from YAML.

    Args:
        path: Path to a YAML file with a top-level `entries` mapping.

    Returns:
        The parsed corpus.

    Raises:
        PairSetError: If the file has no `entries` mapping.
    """
    raw = yaml.safe_load(path.read_text())
    raw_entries = raw.get("entries") if isinstance(raw, dict) else None
    if not raw_entries:
        raise PairSetError(f"No 'entries' found in {path}")
    entries = {
        str(entry_id): CorpusEntry(
            entry_id=str(entry_id),
            text=str(fields["text"]),
            category=fields.get("category"),
            scope=fields.get("scope"),
            timestamp=fields.get("timestamp"),
        )
        for entry_id, fields in raw_entries.items()
    }
    return Corpus(entries=entries)


def _parse_one(raw: dict[str, Any]) -> PairCase:
    """Build a `PairCase` from a raw YAML mapping.

    Args:
        raw: A single pair mapping.

    Returns:
        The parsed pair case.

    Raises:
        PairSetError: If a required field is missing or `label` is invalid.
    """
    try:
        pair_id = str(raw["pair_id"])
        a = str(raw["a"])
        b = str(raw["b"])
        label = str(raw["label"])
    except KeyError as exc:
        raise PairSetError(f"Pair missing required field {exc}") from exc
    if label not in ("positive", "negative"):
        raise PairSetError(
            f"Pair {pair_id} has invalid label {label!r} (must be positive/negative)"
        )
    return PairCase(pair_id=pair_id, a=a, b=b, label=label, note=str(raw.get("note", "")))  # type: ignore[arg-type]


def load_pair_set(path: Path, corpus: Corpus) -> list[PairCase]:
    """Load and validate the labeled pair set against a loaded corpus.

    Args:
        path: Path to a YAML file with a top-level `pairs` list.
        corpus: The corpus each pair's `a`/`b` must reference.

    Returns:
        The parsed, validated pairs.

    Raises:
        PairSetError: If the file has no `pairs` list, a pair references an
            `entry_id` absent from `corpus`, or the set is degenerate (no
            positive pair or no negative pair present).
    """
    raw = yaml.safe_load(path.read_text())
    raw_pairs = raw.get("pairs") if isinstance(raw, dict) else None
    if not raw_pairs:
        raise PairSetError(f"No 'pairs' found in {path}")
    cases = [_parse_one(raw_pair) for raw_pair in raw_pairs]
    for case in cases:
        if case.a not in corpus:
            raise PairSetError(f"Pair {case.pair_id} references unknown entry_id {case.a!r}")
        if case.b not in corpus:
            raise PairSetError(f"Pair {case.pair_id} references unknown entry_id {case.b!r}")
    labels = {case.label for case in cases}
    if "positive" not in labels or "negative" not in labels:
        raise PairSetError(
            "Pair set is degenerate: needs at least one 'positive' and one 'negative' pair "
            "to measure separation."
        )
    return cases
