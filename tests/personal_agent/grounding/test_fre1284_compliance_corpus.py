"""AC-1 — the metric agrees with independent labelling (ADR-0138 D5, FRE-1284).

Scored against ``scripts/eval/fre1284_compliance``, whose spans are hand-labelled from the
raw replies. ADR-0138 AC-1 requires exactly that: spans "identified by the **independent
labelling** of AC-7's corpus, not by the system's own extractor", because scoring the
extractor's own output would make the check circular.

**Tolerance is zero.** ``verify_turn`` is pure and synchronous and the corpus excludes the
entity-free predicate class that would need a live judge, so the derivation is
deterministic end to end. There is no sampling noise for a tolerance to absorb, and any
divergence is a defect.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre1284_compliance.corpus import Partition, load_corpus
from scripts.eval.fre1284_compliance.harness import disagreements, score, score_all

CORPUS = load_corpus()


class TestAC1Agreement:
    """The metric's verdict must match the hand label on every turn."""

    @pytest.mark.parametrize("partition", list(Partition))
    def test_no_disagreement_on_either_partition(self, partition: Partition) -> None:
        scores = score_all(CORPUS.partition(partition))
        assert scores, f"{partition.value} partition is empty"
        assert disagreements(scores) == ()

    def test_every_document_is_scored(self) -> None:
        """A silently skipped document would be an agreement nobody earned."""
        assert len(score_all(CORPUS.documents)) == len(CORPUS.documents)


class TestAC1KillerCase:
    """The failure AC-1 names in so many words.

    "An implementation scoring a turn compliant because *at least one* citation is present
    must fail this." The corpus's own load-time guard requires such a document to exist;
    these assertions pin what it must produce.
    """

    def test_one_cited_one_uncited_is_non_compliant(self) -> None:
        turn = next(doc for doc in CORPUS.documents if doc.doc_id == "c03-one-cited-one-not")
        result = score(turn)

        assert result.measured_in_denominator
        assert result.measured_compliant is False
        outcomes = [span.outcome for span in result.record.spans]
        assert "passed" in outcomes, "one span must genuinely pass, or the case is not mixed"
        assert "uncited" in outcomes, "one span must genuinely fail, or the case is not mixed"

    def test_an_any_citation_metric_would_disagree(self) -> None:
        """The corpus discriminates: the broken rule scores differently from the real one.

        This is the seeded-broken-baseline discipline applied to AC-1 — without it, "zero
        disagreement" could be true of a corpus that no wrong implementation could fail.
        """
        scored = score_all(CORPUS.documents)
        in_denominator = [item for item in scored if item.measured_in_denominator]

        def any_citation_rule(item: object) -> bool:
            record = item.record  # type: ignore[attr-defined]
            return any(span.outcome == "passed" for span in record.spans)

        broken = [any_citation_rule(item) for item in in_denominator]
        actual = [item.measured_compliant for item in in_denominator]
        assert broken != actual, "the corpus cannot tell the real rule from the broken one"


class TestCorpusIntegrity:
    """The corpus itself, since AC-1's claim is only as good as the set it is scored on."""

    def test_load_time_guards_hold(self) -> None:
        """Compliant, non-compliant, out-of-denominator and mixed turns all present."""
        CORPUS.validate_discriminating()

    def test_both_partitions_are_populated(self) -> None:
        assert CORPUS.partition(Partition.DEV)
        assert CORPUS.partition(Partition.HELDOUT)

    def test_entitlement_case_fails_on_entitlement_not_containment(self) -> None:
        """c07's source *contains* the claim exactly — the source is the claim.

        The live 2026-08-26 failure. If this ever fails on containment instead, the
        document has stopped testing D2 and quietly become a duplicate of c05.
        """
        turn = next(doc for doc in CORPUS.documents if doc.doc_id == "c07-agent-derived-memory")
        result = score(turn)
        assert result.measured_compliant is False
        assert [span.outcome for span in result.record.spans] == ["source_not_entitled"]

    def test_user_stated_control_passes(self) -> None:
        """c10 is c07 with the entitlement flipped — the negative control."""
        turn = next(
            doc for doc in CORPUS.documents if doc.doc_id == "c10-user-stated-memory-compliant"
        )
        result = score(turn)
        assert result.measured_compliant is True
