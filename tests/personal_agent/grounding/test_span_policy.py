"""FRE-1281 — layer 3: D1's invariants, enforced deterministically (ADR-0138 D1).

Layer 3 only ever moves a label *toward* ``CLAIM_NON_EXEMPT``. That asymmetry is what
makes it safe to run after a model: it can manufacture a false positive, which the
precision bar measures, but it can never rescue a claim from the contract.

The coverage tests here close the seam a codex plan review found: layer 3 originally
governed only the spans layer 2 chose to return, so a claim layer 2 simply omitted fell
through with no record at all — "default deny" undercut by silence rather than by a
decision.
"""

from __future__ import annotations

import pytest
from personal_agent.grounding.span_policy import CHECKABLE_PREDICATES, apply_policy

from personal_agent.grounding.code_regions import Region, RegionKind, partition_output
from personal_agent.grounding.spans import (
    ExemptRegion,
    NonExemptReason,
    Span,
    SpanLabel,
    assert_non_overlapping,
)


def _model_span(
    text: str, output: str, label: SpanLabel, region: ExemptRegion | None = None
) -> Span:
    """Build a layer-2 span by locating its text in the output."""
    start = output.index(text)
    return Span(
        start=start,
        end=start + len(text),
        text=text,
        label=label,
        region=region,
        reason=NonExemptReason.CLASSIFIED if label is SpanLabel.CLAIM_NON_EXEMPT else None,
    )


def _labels(extraction, text: str) -> list[SpanLabel]:
    return [s.label for s in extraction.spans if text in s.text]


# ── layer 1 verdicts are honoured ────────────────────────────────────────────


def test_proven_code_is_exempt_without_asking_the_model() -> None:
    """A parse-verified code region needs no classification."""
    output = "```python\nx = 1\n```\n"
    extraction = apply_policy(output, partition_output(output), ())
    code = [s for s in extraction.spans if "x = 1" in s.text]
    assert code and all(s.label is SpanLabel.CLAIM_EXEMPT for s in code)
    assert all(s.region is ExemptRegion.CODE for s in code)


def test_dependency_declarations_are_pinned_non_exempt() -> None:
    """D1's hole in the code exemption is categorical, not the model's call."""
    output = "```python\nimport httpx\n\nx = 1\n```\n"
    extraction = apply_policy(output, partition_output(output), ())
    dep = [s for s in extraction.spans if "import httpx" in s.text]
    assert dep
    assert all(s.label is SpanLabel.CLAIM_NON_EXEMPT for s in dep)
    assert all(s.reason is NonExemptReason.DEPENDENCY_PIN for s in dep)


def test_a_model_cannot_exempt_a_dependency_declaration() -> None:
    """Even an explicit exempt verdict from layer 2 loses to the pin."""
    output = "```python\nimport httpx\n```\n"
    regions = partition_output(output)
    rogue = _model_span("import httpx", output, SpanLabel.CLAIM_EXEMPT, ExemptRegion.CODE)
    extraction = apply_policy(output, regions, (rogue,))
    dep = [s for s in extraction.spans if "import httpx" in s.text]
    assert all(s.label is SpanLabel.CLAIM_NON_EXEMPT for s in dep)


def test_fence_delimiters_claim_nothing() -> None:
    """Structural markup is not a claim and not exempt code."""
    output = "```python\nx = 1\n```\n"
    extraction = apply_policy(output, partition_output(output), ())
    fences = [s for s in extraction.spans if s.text.strip().startswith("```")]
    assert fences
    assert all(s.label is SpanLabel.NOT_A_CLAIM for s in fences)


# ── coverage conservation ────────────────────────────────────────────────────


def test_uncovered_prose_fails_closed() -> None:
    """The seam codex found: text layer 2 never mentioned must not escape.

    An omitted claim produces no segment to move toward NON_EXEMPT, so the gap itself
    becomes the span.
    """
    output = "Ortiz is a Spanish brand. It is sold in most French supermarkets."
    regions = (Region(kind=RegionKind.CLASSIFY, text=output, start=0, end=len(output)),)
    partial = (_model_span("Ortiz is a Spanish brand.", output, SpanLabel.CLAIM_NON_EXEMPT),)

    extraction = apply_policy(output, regions, partial)
    gaps = [s for s in extraction.spans if s.reason is NonExemptReason.COVERAGE_GAP]
    assert gaps, "uncovered prose produced no span at all"
    assert "sold in most French supermarkets" in " ".join(s.text for s in gaps)
    assert extraction.degraded


def test_whitespace_gaps_are_not_claims() -> None:
    """Fail-closed must not mean turning every inter-segment space into an obligation."""
    output = "Ortiz is Spanish.  It ships widely."
    regions = (Region(kind=RegionKind.CLASSIFY, text=output, start=0, end=len(output)),)
    covered = (
        _model_span("Ortiz is Spanish.", output, SpanLabel.CLAIM_NON_EXEMPT),
        _model_span("It ships widely.", output, SpanLabel.CLAIM_NON_EXEMPT),
    )
    extraction = apply_policy(output, regions, covered)
    assert not [s for s in extraction.spans if s.reason is NonExemptReason.COVERAGE_GAP]
    assert not extraction.degraded


def test_full_tiling_is_not_degraded() -> None:
    """Positive control — a well-formed tiling passes through untouched."""
    output = "Ortiz is Spanish. Shall I go on?"
    regions = (Region(kind=RegionKind.CLASSIFY, text=output, start=0, end=len(output)),)
    covered = (
        _model_span("Ortiz is Spanish.", output, SpanLabel.CLAIM_NON_EXEMPT),
        _model_span("Shall I go on?", output, SpanLabel.NOT_A_CLAIM),
    )
    extraction = apply_policy(output, regions, covered)
    assert not extraction.degraded
    assert _labels(extraction, "Shall I go on?") == [SpanLabel.NOT_A_CLAIM]


# ── ambiguity, precedence, denylist ──────────────────────────────────────────


def test_ambiguous_resolves_to_assertion() -> None:
    """D1: "Ambiguous classification resolves to assertion."."""
    output = "That is better value than the other one."
    regions = (Region(kind=RegionKind.CLASSIFY, text=output, start=0, end=len(output)),)
    hedged = (_model_span(output, output, SpanLabel.CLAIM_EXEMPT, ExemptRegion.AMBIGUOUS),)
    extraction = apply_policy(output, regions, hedged)
    assert extraction.spans[0].label is SpanLabel.CLAIM_NON_EXEMPT
    assert extraction.spans[0].reason is NonExemptReason.AMBIGUITY_PIN


@pytest.mark.parametrize("predicate", sorted(CHECKABLE_PREDICATES))
def test_checkable_predicates_cannot_be_laundered_as_evaluation(predicate: str) -> None:
    """D1's round-3 leak: these read as evaluation but are claims about the world.

    An earlier ADR draft used "are both well regarded" as its exemplar of *exempt*
    evaluation. That was the common-knowledge trap reappearing one level down.
    """
    output = f"The library is {predicate} for this workload."
    regions = (Region(kind=RegionKind.CLASSIFY, text=output, start=0, end=len(output)),)
    laundered = (
        _model_span(output, output, SpanLabel.CLAIM_EXEMPT, ExemptRegion.CONNECTIVE_EVALUATIVE),
    )
    extraction = apply_policy(output, regions, laundered)
    assert extraction.spans[0].label is SpanLabel.CLAIM_NON_EXEMPT
    assert extraction.spans[0].reason is NonExemptReason.CHECKABLE_PREDICATE_PIN


def test_genuine_ordering_over_cited_material_stays_exempt() -> None:
    """Positive control — the denylist must not swallow legitimate connective text."""
    output = "Of the two cited prices, the second is cheaper."
    regions = (Region(kind=RegionKind.CLASSIFY, text=output, start=0, end=len(output)),)
    ordering = (
        _model_span(output, output, SpanLabel.CLAIM_EXEMPT, ExemptRegion.CONNECTIVE_EVALUATIVE),
    )
    extraction = apply_policy(output, regions, ordering)
    assert extraction.spans[0].label is SpanLabel.CLAIM_EXEMPT


def test_repeated_user_package_as_recommendation_is_not_rescued() -> None:
    """AC-4 — non-exempt wins on overlap, and the exemption does not travel with a string.

    The user supplies a package name; the model repeats it with attribution (exempt) and
    then again as its own recommendation (not exempt). If the restatement exemption
    rescued the second mention, D1's one-directional precedence would be broken.
    """
    output = "You mentioned demo-pkg. I'd recommend demo-pkg for this."
    regions = (Region(kind=RegionKind.CLASSIFY, text=output, start=0, end=len(output)),)

    attributed = _model_span(
        "You mentioned demo-pkg.",
        output,
        SpanLabel.CLAIM_EXEMPT,
        ExemptRegion.ATTRIBUTED_RESTATEMENT,
    )
    recommendation_start = output.index("I'd recommend demo-pkg for this.")
    recommendation = Span(
        start=recommendation_start,
        end=len(output),
        text=output[recommendation_start:],
        label=SpanLabel.CLAIM_NON_EXEMPT,
        reason=NonExemptReason.CLASSIFIED,
    )
    # Layer 2 also, wrongly, tries to extend the restatement exemption over the whole line.
    over_reaching = Span(
        start=0,
        end=len(output),
        text=output,
        label=SpanLabel.CLAIM_EXEMPT,
        region=ExemptRegion.ATTRIBUTED_RESTATEMENT,
    )

    extraction = apply_policy(output, regions, (attributed, recommendation, over_reaching))
    assert_non_overlapping(extraction.spans)
    surviving = [s for s in extraction.spans if "I'd recommend demo-pkg" in s.text]
    assert surviving
    assert all(s.label is SpanLabel.CLAIM_NON_EXEMPT for s in surviving)


def test_overlapping_exempt_span_is_dropped_not_trimmed() -> None:
    """Trimming an exempt span around a claim would emit a partial proposition."""
    output = "Ortiz is Spanish and widely sold."
    regions = (Region(kind=RegionKind.CLASSIFY, text=output, start=0, end=len(output)),)
    claim = _model_span("Ortiz is Spanish", output, SpanLabel.CLAIM_NON_EXEMPT)
    swallowing = _model_span(output, output, SpanLabel.CLAIM_EXEMPT, ExemptRegion.SYSTEM_RECORD)
    extraction = apply_policy(output, regions, (claim, swallowing))
    assert_non_overlapping(extraction.spans)
    assert not [
        s
        for s in extraction.spans
        if s.label is SpanLabel.CLAIM_EXEMPT and s.region is ExemptRegion.SYSTEM_RECORD
    ]


# ── invariants over the whole result ─────────────────────────────────────────


def test_result_is_always_non_overlapping_and_ordered() -> None:
    """D1's atomicity requirement, enforced rather than hoped for."""
    output = "Ortiz is Spanish.\n\n```python\nimport httpx\nx = 1\n```\n\nIt is popular.\n"
    regions = partition_output(output)
    model_spans = (
        _model_span("Ortiz is Spanish.", output, SpanLabel.CLAIM_NON_EXEMPT),
        _model_span(
            "It is popular.", output, SpanLabel.CLAIM_EXEMPT, ExemptRegion.CONNECTIVE_EVALUATIVE
        ),
    )
    extraction = apply_policy(output, regions, model_spans)
    assert_non_overlapping(extraction.spans)
    offsets = [s.start for s in extraction.spans]
    assert offsets == sorted(offsets)


def test_spans_never_leave_the_output_bounds() -> None:
    """Every emitted span indexes back into the text it came from."""
    output = "Ortiz is Spanish.\n\n```python\nimport httpx\n```\n"
    extraction = apply_policy(output, partition_output(output), ())
    for span in extraction.spans:
        assert output[span.start : span.end] == span.text


def test_layer_three_does_not_invent_claims_from_not_a_claim() -> None:
    """The boundary: layer 3 is not a second classifier.

    A span layer 2 examined and judged inert stays inert. Overriding that would be layer
    3 deciding claim-hood, which is layer 2's job — and a wrongly-inert span is already a
    recall miss the corpus measures. The one exception is a coverage *gap*, where layer 2
    made no judgement at all.
    """
    output = "Shall I keep it safe for you?"
    regions = (Region(kind=RegionKind.CLASSIFY, text=output, start=0, end=len(output)),)
    inert = (_model_span(output, output, SpanLabel.NOT_A_CLAIM),)
    extraction = apply_policy(output, regions, inert)
    assert extraction.spans[0].label is SpanLabel.NOT_A_CLAIM
