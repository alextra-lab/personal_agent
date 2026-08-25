"""Deliberately broken extractors, and the oracle (FRE-1281, ADR-0138 AC-7).

ADR-0138's floor principle: "a bar that a known-broken implementation would pass is not a
bar." These are the known-broken implementations. ``test_fre1281_bar_floor.py`` scores
each against the real corpus and asserts it fails every bar naming it, so the claim in
``bars.py`` is executable rather than argued.

**Four of the five are handed gold boundaries.** Only :func:`null_extractor` gets
segmentation wrong; the others replay the gold spans and vary only the *labels*. That is
deliberate, and it makes the demonstration stronger rather than weaker: it shows the bars
reject a broken classifier even when segmentation is perfect, which is the failure mode
that matters — an extractor with flawless boundaries that exempts everything still lets
every claim escape the contract.

:func:`entity_triggered_extractor` deserves its own note. It is not a strawman invented
to fail: it is the draft of D1 that review rejected, reproduced faithfully. It fires on a
named entity or a figure, which is a *reasonable-looking* rule that posts a respectable
overall recall while scoring zero on ``factual_bare_predicate`` — "this fish is high in
mercury" contains neither. That single class is the whole reason D1 was inverted to
default-deny, and it is why per-class recall bars exist at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from scripts.eval.fre1281_span_extraction.bars import BaselineName
from scripts.eval.fre1281_span_extraction.corpus import GoldDocument, GoldSpan
from scripts.eval.fre1281_span_extraction.corpus import SpanLabel as GoldLabel

from personal_agent.grounding.spans import (
    ExemptRegion,
    NonExemptReason,
    Span,
    SpanLabel,
)

Extractor = Callable[[GoldDocument], tuple[Span, ...]]

_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_ENTITY_OR_FIGURE = re.compile(r"\d|(?<!^)(?<![.!?]\s)\b[A-Z]\w*")


def _as_span(gold: GoldSpan, label: SpanLabel) -> Span:
    """Rebuild a gold span with a chosen label and the annotation that label demands."""
    if label is SpanLabel.CLAIM_EXEMPT:
        return Span(
            start=gold.start,
            end=gold.end,
            text=gold.text,
            label=label,
            region=ExemptRegion.CODE,
        )
    if label is SpanLabel.CLAIM_NON_EXEMPT:
        return Span(
            start=gold.start,
            end=gold.end,
            text=gold.text,
            label=label,
            reason=NonExemptReason.CLASSIFIED,
        )
    return Span(start=gold.start, end=gold.end, text=gold.text, label=label)


def _claims(document: GoldDocument) -> tuple[GoldSpan, ...]:
    """Gold spans that assert something — the population the baselines relabel."""
    return document.claim_spans


def null_extractor(document: GoldDocument) -> tuple[Span, ...]:
    """Recognise nothing. Recall 0, boundary F1 0."""
    del document
    return ()


def exempt_all_extractor(document: GoldDocument) -> tuple[Span, ...]:
    """Perfect boundaries, everything exempt. Recall 0 with nothing else wrong."""
    return tuple(_as_span(g, SpanLabel.CLAIM_EXEMPT) for g in _claims(document))


def accept_all_extractor(document: GoldDocument) -> tuple[Span, ...]:
    """Perfect boundaries, everything non-exempt.

    Recall is perfect and precision is capped at ``1 - exempt_fraction`` — the corpus
    holds that below the precision bar by arithmetic (``test_fre1281_corpus.py``).
    """
    return tuple(_as_span(g, SpanLabel.CLAIM_NON_EXEMPT) for g in _claims(document))


def entity_triggered_extractor(document: GoldDocument) -> tuple[Span, ...]:
    """The rejected draft of D1: cite only what carries a named entity or a figure.

    Scores zero on ``factual_bare_predicate`` by construction, because the corpus loader
    refuses to admit a span into that class if it contains either.
    """
    spans = []
    for gold in _claims(document):
        fires = bool(_ENTITY_OR_FIGURE.search(gold.text))
        spans.append(
            _as_span(gold, SpanLabel.CLAIM_NON_EXEMPT if fires else SpanLabel.CLAIM_EXEMPT)
        )
    return tuple(spans)


def fence_trusting_extractor(document: GoldDocument) -> tuple[Span, ...]:
    """Treat a fence as proof of code, which D1 explicitly denies.

    "The exemption attaches to *code*, **not to fencing**." This baseline exempts anything
    inside a fence, so prose in a ``text`` fence and a world-fact claim in a string literal
    both escape — the two classes whose recall bars name it.
    """
    fenced = [(m.start(), m.end()) for m in _FENCE.finditer(document.text)]
    spans = []
    for gold in _claims(document):
        inside = any(start <= gold.start and gold.end <= end for start, end in fenced)
        spans.append(
            _as_span(gold, SpanLabel.CLAIM_EXEMPT if inside else SpanLabel.CLAIM_NON_EXEMPT)
        )
    return tuple(spans)


def oracle_extractor(document: GoldDocument) -> tuple[Span, ...]:
    """Replay gold exactly — the positive control.

    Without it, a bar set could be strict by being unsatisfiable, which measures nothing
    in a different way.
    """
    spans = []
    for gold in document.spans:
        label = {
            GoldLabel.CLAIM_EXEMPT: SpanLabel.CLAIM_EXEMPT,
            GoldLabel.CLAIM_NON_EXEMPT: SpanLabel.CLAIM_NON_EXEMPT,
            GoldLabel.NOT_A_CLAIM: SpanLabel.NOT_A_CLAIM,
        }[gold.label]
        spans.append(_as_span(gold, label))
    return tuple(spans)


BASELINES: dict[BaselineName, Extractor] = {
    BaselineName.NULL: null_extractor,
    BaselineName.EXEMPT_ALL: exempt_all_extractor,
    BaselineName.ACCEPT_ALL: accept_all_extractor,
    BaselineName.ENTITY_TRIGGERED: entity_triggered_extractor,
    BaselineName.FENCE_TRUSTING: fence_trusting_extractor,
    BaselineName.ORACLE: oracle_extractor,
}


def run_baseline(
    name: BaselineName, documents: Sequence[GoldDocument]
) -> list[tuple[GoldDocument, tuple[Span, ...]]]:
    """Apply one baseline across a corpus.

    Args:
        name: Which baseline.
        documents: The corpus.

    Returns:
        Document/prediction pairs, ready for :func:`~metrics.score_document`.
    """
    extractor = BASELINES[name]
    return [(document, extractor(document)) for document in documents]


__all__ = ["BASELINES", "Extractor", "run_baseline"]
