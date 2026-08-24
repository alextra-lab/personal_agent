"""FRE-1281 — labelled-corpus discipline (pure, no LLM).

The corpus is the instrument every other criterion in ADR-0138 is read through, so its
guards are asserted here rather than trusted to review. **Every guard carries a seeded
negative**: a check that has never been shown to reject anything has not been shown to
work, and a clean corpus would let all of these pass vacuously.

Two of these tests are load-bearing for ``bars.py``'s floor principle rather than for the
corpus itself — :func:`test_bare_predicate_class_rejects_a_figure` and
:func:`test_exempt_fraction_caps_accept_all_below_the_precision_bar` are what make
``entity_triggered`` and ``accept_all`` provably fail, instead of merely expected to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from scripts.eval.fre1281_span_extraction.bars import OVERALL_PRECISION
from scripts.eval.fre1281_span_extraction.corpus import (
    CORPUS_SCHEMA_VERSION,
    DEFAULT_CORPUS_PATH,
    EMAIL_PATTERN,
    EXEMPT_CLASSES,
    MIN_EXEMPT_FRACTION,
    MIN_SPANS_PER_CLASS,
    NON_EXEMPT_CLASSES,
    PII_DENYLIST,
    GoldSpan,
    Partition,
    SpanClass,
    SpanLabel,
    all_authored_strings,
    class_counts,
    exempt_fraction,
    load_corpus,
)

CORPUS = load_corpus()


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    """Write a corpus fragment to disk for a seeded-negative case."""
    path = tmp_path / "corpus.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


# ── shape ────────────────────────────────────────────────────────────────────


def test_corpus_loads_and_anchors_every_span() -> None:
    """Every span resolves to a real slice of its document."""
    assert CORPUS
    for document in CORPUS:
        for span in document.spans:
            assert span.start >= 0
            assert document.text[span.start : span.end] == span.text


def test_every_class_meets_the_span_floor() -> None:
    """All thirteen classes carry at least MIN_SPANS_PER_CLASS gold spans.

    Below the floor a 0.85 recall bar degenerates into a perfection test, so this is a
    precondition for the bars meaning anything (bars.py, R1-8).
    """
    counts = class_counts(CORPUS)
    short = {
        span_class.value: counts.get(span_class, 0)
        for span_class in SpanClass
        if counts.get(span_class, 0) < MIN_SPANS_PER_CLASS
    }
    assert not short, f"classes below the {MIN_SPANS_PER_CLASS}-span floor: {short}"


def test_both_partitions_are_populated() -> None:
    """Held-out discipline needs something in each half."""
    partitions = {document.partition for document in CORPUS}
    assert partitions == {Partition.DEV, Partition.HELDOUT}


def test_exempt_fraction_caps_accept_all_below_the_precision_bar() -> None:
    """The arithmetic that makes ``accept_all`` fail, stated rather than assumed.

    ``accept_all`` marks every claim span non-exempt, so its precision is exactly
    ``non_exempt / (non_exempt + exempt)`` — that is, ``1 - exempt_fraction``. Holding the
    exempt fraction at or above MIN_EXEMPT_FRACTION caps that ceiling below the precision
    bar by arithmetic. A plan review found this claim asserted but unsecured.
    """
    fraction = exempt_fraction(CORPUS)
    assert fraction >= MIN_EXEMPT_FRACTION, (
        f"exempt fraction {fraction:.3f} < {MIN_EXEMPT_FRACTION}"
    )
    accept_all_ceiling = 1.0 - fraction
    assert accept_all_ceiling < OVERALL_PRECISION.value, (
        f"accept_all could reach precision {accept_all_ceiling:.3f}, which is not below "
        f"the {OVERALL_PRECISION.value} bar — the baseline would no longer be rejected"
    )


def test_class_partition_covers_every_class() -> None:
    """Exempt and non-exempt classes together are the whole taxonomy, disjointly."""
    assert NON_EXEMPT_CLASSES | EXEMPT_CLASSES == set(SpanClass)
    assert not NON_EXEMPT_CLASSES & EXEMPT_CLASSES


# ── public-repo hygiene ──────────────────────────────────────────────────────


def test_corpus_carries_no_pii() -> None:
    """No owner identifiers, deployment names or email addresses in a public repo."""
    for authored in all_authored_strings(CORPUS):
        lowered = authored.lower()
        for token in PII_DENYLIST:
            assert token not in lowered, f"denylisted token {token!r} in {authored[:60]!r}"
        assert not EMAIL_PATTERN.search(authored), f"email-like string in {authored[:60]!r}"


def test_pii_guard_rejects_a_seeded_leak() -> None:
    """Seeded negative — the denylist and the email pattern both actually fire."""
    assert any(token in "contact kookier for access".lower() for token in PII_DENYLIST)
    assert EMAIL_PATTERN.search("write to someone@example.com about it")


def test_pii_guard_tolerates_a_decorator() -> None:
    """A bare '@' is not a leak — the corpus contains real code (see PII_DENYLIST)."""
    assert not EMAIL_PATTERN.search("@click.command()")


# ── seeded negatives for each load-time guard ────────────────────────────────


def test_bare_predicate_class_rejects_a_figure() -> None:
    """Seeded negative — the guard that makes ``entity_triggered`` provably fail.

    If a bare-predicate span could carry a figure, an entity-or-figure trigger would find
    it and the baseline would no longer be rejected by that class's recall bar.
    """
    with pytest.raises(ValueError, match="contains a digit"):
        GoldSpan(
            text="it holds 40 percent more water",
            label=SpanLabel.CLAIM_NON_EXEMPT,
            span_class=SpanClass.FACTUAL_BARE_PREDICATE,
        )


def test_bare_predicate_class_rejects_a_named_entity() -> None:
    """Seeded negative — a capitalised non-initial token reads as a named entity."""
    with pytest.raises(ValueError, match="capitalised"):
        GoldSpan(
            text="the fish caught near Iceland is leaner",
            label=SpanLabel.CLAIM_NON_EXEMPT,
            span_class=SpanClass.FACTUAL_BARE_PREDICATE,
        )


def test_bare_predicate_class_accepts_a_sentence_initial_capital() -> None:
    """Positive control — the guard must not reject ordinary sentence casing."""
    span = GoldSpan(
        text="That one is high in mercury",
        label=SpanLabel.CLAIM_NON_EXEMPT,
        span_class=SpanClass.FACTUAL_BARE_PREDICATE,
    )
    assert span.span_class is SpanClass.FACTUAL_BARE_PREDICATE


def test_class_and_label_must_agree() -> None:
    """Seeded negative — an exempt class cannot be labelled non-exempt."""
    with pytest.raises(ValueError, match="implies"):
        GoldSpan(
            text="the total is 425 euros",
            label=SpanLabel.CLAIM_NON_EXEMPT,
            span_class=SpanClass.DERIVED_ARITHMETIC,
        )


def test_not_a_claim_carries_no_class() -> None:
    """Seeded negative — NOT_A_CLAIM is classless by construction."""
    with pytest.raises(ValueError, match="carries class"):
        GoldSpan(
            text="Shall I continue?",
            label=SpanLabel.NOT_A_CLAIM,
            span_class=SpanClass.CODE_BODY,
        )


def test_claim_without_class_is_rejected() -> None:
    """Seeded negative — an unclassed claim cannot be scored per class."""
    with pytest.raises(ValueError, match="carries no class"):
        GoldSpan(text="Paris is the capital", label=SpanLabel.CLAIM_NON_EXEMPT)


def test_overlapping_spans_are_rejected(tmp_path: Path) -> None:
    """Seeded negative — ADR-0138 D1 requires non-overlapping atomic claims."""
    payload = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "documents": [
            {
                "doc_id": "overlap",
                "partition": "dev",
                "text": "Paris is the capital of France and sits on the Seine.",
                "spans": [
                    {
                        "text": "Paris is the capital of France",
                        "label": "claim_non_exempt",
                        "span_class": "factual_entity",
                    },
                    {
                        "text": "of France and sits on the Seine",
                        "label": "claim_non_exempt",
                        "span_class": "factual_entity",
                    },
                ],
            }
        ],
    }
    with pytest.raises(ValueError, match="overlap"):
        load_corpus(_write(tmp_path, payload))


def test_unanchorable_span_is_rejected(tmp_path: Path) -> None:
    """Seeded negative — a quote that is not in the document must not load silently."""
    payload = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "documents": [
            {
                "doc_id": "missing",
                "partition": "dev",
                "text": "Paris is the capital of France.",
                "spans": [
                    {
                        "text": "Lyon is the capital of France",
                        "label": "claim_non_exempt",
                        "span_class": "factual_entity",
                    }
                ],
            }
        ],
    }
    with pytest.raises(ValueError, match="not found in document text"):
        load_corpus(_write(tmp_path, payload))


def test_restatement_without_user_message_is_rejected(tmp_path: Path) -> None:
    """Seeded negative — attribution is undecidable without the user's own words."""
    payload = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "documents": [
            {
                "doc_id": "no-user",
                "partition": "dev",
                "text": "You mentioned the caching layer.",
                "spans": [
                    {
                        "text": "You mentioned the caching layer",
                        "label": "claim_exempt",
                        "span_class": "attributed_restatement",
                    }
                ],
            }
        ],
    }
    with pytest.raises(ValueError, match="no user_message"):
        load_corpus(_write(tmp_path, payload))


def test_schema_version_mismatch_is_rejected(tmp_path: Path) -> None:
    """Seeded negative — an old file must fail rather than be misread."""
    payload = {"schema_version": CORPUS_SCHEMA_VERSION + 1, "documents": []}
    with pytest.raises(ValueError, match="schema_version"):
        load_corpus(_write(tmp_path, payload))


def test_occurrence_selects_the_right_repetition(tmp_path: Path) -> None:
    """Positive control — repeated text is disambiguated, as AC-4's probe requires."""
    payload = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "documents": [
            {
                "doc_id": "repeat",
                "partition": "dev",
                "user_message": "I use demo-pkg.",
                "text": "You mentioned demo-pkg. I'd recommend demo-pkg for this.",
                "spans": [
                    {
                        "text": "You mentioned demo-pkg",
                        "label": "claim_exempt",
                        "span_class": "attributed_restatement",
                    },
                    {
                        "text": "demo-pkg",
                        "occurrence": 2,
                        "label": "claim_non_exempt",
                        "span_class": "unattributed_restatement",
                    },
                ],
            }
        ],
    }
    documents = load_corpus(_write(tmp_path, payload))
    second = documents[0].spans[1]
    assert second.start > documents[0].spans[0].end
    assert documents[0].text[second.start : second.end] == "demo-pkg"


def test_default_corpus_path_is_the_committed_artifact() -> None:
    """The corpus under test is the committed one, not a fixture."""
    assert DEFAULT_CORPUS_PATH.exists()
