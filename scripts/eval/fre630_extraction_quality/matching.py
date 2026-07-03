"""FRE-630 — pure tiered entity matcher (codex plan-review P0.2).

LLM extraction is non-deterministic and gold entity names will not string-match
cleanly, so a naive exact-match benchmark punishes correct-but-differently-worded
extractions — and, worse, one entity-name mismatch cascades into false relationship
misses (an edge is scored over its endpoints). This module resolves each extracted
entity name to *at most one* gold entity through three tiers of decreasing certainty:

1. **exact** — after deterministic normalization (case-fold, whitespace-collapse,
   surrounding-punctuation strip, Unicode NFKD accent-fold): ``Neo4j`` == ``neo4j``,
   ``Météo France`` == ``meteo france``.
2. **alias** — the extracted name normalizes to one of a gold entity's hand-authored
   accepted surface forms.
3. **fuzzy** — a conservative ``difflib`` similarity ≥ threshold, used *only* for
   still-unmatched candidates and always recorded as a ``fuzzy`` tier so it is
   auditable, never silent.

Everything here is pure: no I/O, no LLM, deterministic for a given input order.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from scripts.eval.fre630_extraction_quality.gold import GoldEntity

#: Bumped whenever the matching algorithm changes; stamped into the run report so
#: scores are never silently compared across matcher revisions.
MATCHER_VERSION = "1.0"

#: Conservative default; only unmatched candidates reach the fuzzy tier.
DEFAULT_FUZZY_THRESHOLD = 0.86

MatchTier = Literal["exact", "alias", "fuzzy"]


def normalize_name(name: str) -> str:
    """Deterministically normalize an entity name for tier-1/2 matching.

    Case-folds, collapses internal whitespace, strips surrounding punctuation and
    whitespace, and applies Unicode NFKD accent-folding (so diacritics do not defeat
    a match). Purely lexical — no stemming, no synonym expansion.

    Args:
        name: A raw entity surface form.

    Returns:
        The normalized key ("" when the input is empty/whitespace).
    """
    decomposed = unicodedata.normalize("NFKD", name or "")
    stripped_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = stripped_accents.casefold()
    collapsed = " ".join(folded.split())
    return collapsed.strip(" \t\n\r.,;:!?\"'()[]{}")


def _fuzzy_ratio(a: str, b: str) -> float:
    """Order-insensitive token similarity in ``[0, 1]`` (pure, deterministic)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class EntityMatch:
    """One resolved (gold ↔ extracted) entity pairing.

    Attributes:
        gold_name: The gold entity's canonical name.
        extracted_name: The extracted surface form that resolved to it.
        tier: Which tier produced the match (``exact`` / ``alias`` / ``fuzzy``).
    """

    gold_name: str
    extracted_name: str
    tier: MatchTier


@dataclass(frozen=True)
class MatchResult:
    """The outcome of resolving extracted names against gold entities.

    Attributes:
        matches: One :class:`EntityMatch` per matched gold entity.
        unmatched_gold: Canonical names of gold entities with no extracted match
            (the extraction *misses* / false negatives).
        unmatched_extracted: Extracted names that resolved to no gold entity (the
            *spurious* extractions / precision hits — some may be hallucination traps).
    """

    matches: tuple[EntityMatch, ...]
    unmatched_gold: tuple[str, ...]
    unmatched_extracted: tuple[str, ...]

    def extracted_to_gold(self) -> dict[str, str]:
        """Map each matched extracted name to its resolved gold canonical name."""
        return {m.extracted_name: m.gold_name for m in self.matches}

    def gold_to_extracted(self) -> dict[str, str]:
        """Map each matched gold canonical name to the extracted surface form."""
        return {m.gold_name: m.extracted_name for m in self.matches}


def match_entities(
    gold: Sequence[GoldEntity],
    extracted: Sequence[str],
    *,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> MatchResult:
    """Resolve extracted entity names to gold entities via the three tiers.

    Each extracted name is claimed by at most one gold entity and each gold entity
    matches at most one extracted name. Tiers are applied in priority order (exact,
    then alias, then fuzzy) so a certain match is never displaced by a fuzzy one; the
    fuzzy pass assigns the globally best remaining pairs first (ties broken by name
    order) for determinism.

    Args:
        gold: The gold entities to resolve against.
        extracted: The extractor's emitted entity names (surface forms).
        fuzzy_threshold: Minimum similarity for a tier-3 match (``[0, 1]``).

    Returns:
        A :class:`MatchResult` partitioning gold and extracted into matched /
        unmatched, each match tagged with the tier that produced it.
    """
    gold_norm = {g.name: normalize_name(g.name) for g in gold}
    gold_aliases = {
        g.name: {normalize_name(a) for a in g.aliases if normalize_name(a)} for g in gold
    }
    extracted_norm = {name: normalize_name(name) for name in extracted}

    matched_gold: dict[str, EntityMatch] = {}
    claimed_extracted: set[str] = set()

    def _claim(gold_name: str, extracted_name: str, tier: MatchTier) -> None:
        matched_gold[gold_name] = EntityMatch(gold_name, extracted_name, tier)
        claimed_extracted.add(extracted_name)

    # Tier 1 — exact (normalized canonical).
    for g in gold:
        if g.name in matched_gold:
            continue
        target = gold_norm[g.name]
        for name in extracted:
            if name in claimed_extracted or not extracted_norm[name]:
                continue
            if extracted_norm[name] == target:
                _claim(g.name, name, "exact")
                break

    # Tier 2 — alias (normalized accepted surface form).
    for g in gold:
        if g.name in matched_gold or not gold_aliases[g.name]:
            continue
        for name in extracted:
            if name in claimed_extracted or not extracted_norm[name]:
                continue
            if extracted_norm[name] in gold_aliases[g.name]:
                _claim(g.name, name, "alias")
                break

    # Tier 3 — fuzzy (globally best remaining pairs first, deterministic).
    remaining_gold = [g for g in gold if g.name not in matched_gold]
    candidate_pairs: list[tuple[float, str, str]] = []
    for g in remaining_gold:
        for name in extracted:
            if name in claimed_extracted or not extracted_norm[name]:
                continue
            ratio = _fuzzy_ratio(gold_norm[g.name], extracted_norm[name])
            if ratio >= fuzzy_threshold:
                candidate_pairs.append((ratio, g.name, name))
    # Highest ratio first; ties broken by (gold, extracted) name for stability.
    candidate_pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    for _ratio, gold_name, extracted_name in candidate_pairs:
        if gold_name in matched_gold or extracted_name in claimed_extracted:
            continue
        _claim(gold_name, extracted_name, "fuzzy")

    matches = tuple(matched_gold[g.name] for g in gold if g.name in matched_gold)
    unmatched_gold = tuple(g.name for g in gold if g.name not in matched_gold)
    unmatched_extracted = tuple(name for name in extracted if name not in claimed_extracted)
    return MatchResult(
        matches=matches, unmatched_gold=unmatched_gold, unmatched_extracted=unmatched_extracted
    )


def matches_any(name: str, candidates: Sequence[str]) -> bool:
    """Whether ``name`` normalizes to any of ``candidates`` (exact/alias tier only).

    Used for trap checks (hallucination `forbid_entities`, dedup variants) where a
    fuzzy match would be too loose — a trap should fire on a genuine surface hit.

    Args:
        name: The surface form to test.
        candidates: Accepted surface forms.

    Returns:
        ``True`` when the normalized ``name`` equals a normalized candidate.
    """
    target = normalize_name(name)
    if not target:
        return False
    return any(target == normalize_name(c) for c in candidates)
