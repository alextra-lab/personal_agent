"""D3(c) containment — the unit, the normalization contract, and its measured bars.

ADR-0138 D3 (containment unit, normalization contract) · FRE-1282 AC-3, AC-4.

The two prior failures this must not recreate are asserted directly, not implied:
"some token appears" (``Paris`` alone satisfying a claim about a population), and the
vacuity of "every entity and figure" on a span that has neither.
"""

from __future__ import annotations

from personal_agent.grounding.containment import (
    ContainmentOutcome,
    check_containment,
    claim_unit,
    normalize_tokens,
    strip_attribution_frame,
)

from .probes.containment_variance import (
    FALSE_REJECTION_BAR,
    VARIANCE_PROBES,
    VarianceProbe,
)

# ── The unit (D3(c)) ────────────────────────────────────────────────────────────────


def test_function_words_are_dropped_content_words_are_not() -> None:
    """``Paris has 2.1 million residents`` requires the entity, the figure, the predicate."""
    unit = claim_unit("Paris has 2.1 million residents")

    assert unit.required == ("paris", "#2100000", "residents")
    assert unit.entities == ("paris",)
    assert unit.figures == ("#2100000",)


def test_some_token_appearing_does_not_satisfy_containment() -> None:
    """The first prior failure: matching ``Paris`` alone is citation theatre."""
    result = check_containment(
        "Paris has 2.1 million residents",
        "Paris is the capital of France and hosted the 1900 exposition.",
    )

    assert result.outcome is ContainmentOutcome.NOT_CONTAINED
    assert "#2100000" in result.missing
    assert "residents" in result.missing


def test_entity_free_predicate_is_not_vacuous() -> None:
    """AC-3 — the second prior failure, and the class D1's inversion exists to catch.

    ``this fish is high in mercury`` has no entity and no figure. Under "every entity and
    figure" the condition held over an empty set and *any* source passed. It must not.
    """
    result = check_containment(
        "this fish is high in mercury",
        "Bonito del norte is line-caught in the Bay of Biscay each summer.",
    )

    assert result.outcome is not ContainmentOutcome.CONTAINED
    assert result.outcome is not ContainmentOutcome.ENTAILMENT_REQUIRED
    assert "mercury" in result.missing


def test_entity_free_predicate_escalates_only_after_containment_passes() -> None:
    """D3(d) owns the class, but containment still runs first and can still reject.

    A source that *does* state the predicate reaches the escalation; one that does not is
    rejected above it. Without this ordering the escalation would be a way to skip the
    check rather than to strengthen it.
    """
    result = check_containment(
        "this fish is high in mercury",
        "Testing found this fish is high in mercury, above the advisory level.",
    )

    assert result.outcome is ContainmentOutcome.ENTAILMENT_REQUIRED
    assert result.entity_free_predicate is True
    assert result.missing == ()


def test_attribution_frame_is_not_part_of_the_claim() -> None:
    """A source supporting the claim supports it however the model framed the sentence."""
    assert (
        strip_attribution_frame("According to the cited table, Paris has 2.1 million residents")
        == "Paris has 2.1 million residents"
    )

    result = check_containment(
        "According to the cited table, Paris has 2.1 million residents",
        "Paris counts 2,100,000 residents within the city limits.",
    )

    assert result.outcome is ContainmentOutcome.CONTAINED


def test_a_frame_with_no_claim_after_it_is_left_alone() -> None:
    """Stripping to nothing would hand the check the empty required set it must never see."""
    text = "According to the report,"

    assert strip_attribution_frame(text) == text
    assert check_containment(text, "anything at all").outcome is not ContainmentOutcome.CONTAINED


def test_evidential_words_are_not_required_of_the_source() -> None:
    """``reportedly`` qualifies the asserting, not the world."""
    assert "reportedly" not in claim_unit("The tunnel reportedly runs 50 km")


# ── Token boundary and normalization (D3) ───────────────────────────────────────────


def test_matching_is_on_token_boundaries_never_substrings() -> None:
    """D3's own example: ``Ham`` must not match inside ``Birmingham``."""
    result = check_containment("Ham signed the charter", "Birmingham signed the charter in 1889.")

    assert result.outcome is ContainmentOutcome.NOT_CONTAINED
    assert "ham" in result.missing


def test_figures_normalize_across_separators_precision_and_magnitude() -> None:
    """``1,000`` ≡ ``1000``, ``3.0`` ≡ ``3``, ``2.1 million`` ≡ ``2100000``."""
    assert normalize_tokens("1,000")[0] == normalize_tokens("1000")[0]
    assert normalize_tokens("3.0")[0] == normalize_tokens("3")[0]
    assert normalize_tokens("2.1 million")[0] == normalize_tokens("2,100,000")[0]


def test_units_fold_within_a_quantity_but_never_across_one() -> None:
    """``km`` ≡ ``kilometres``; ``km`` is emphatically not ``miles``."""
    assert normalize_tokens("50 km") == normalize_tokens("50 kilometres")
    assert normalize_tokens("50 km") != normalize_tokens("50 miles")


def test_a_contradicted_figure_is_unsupported_not_merely_unverifiable() -> None:
    """AC-6's sharpest edge: a wrong number must not read as a normalizer limitation.

    ``Paris`` and ``residents`` both match, so a rule keyed on how *many* tokens matched
    would file the purest citation theatre there is as our own defect.
    """
    result = check_containment(
        "Paris has 9 million residents",
        "Paris counts 2,100,000 residents within the city limits.",
    )

    assert result.outcome is ContainmentOutcome.NOT_CONTAINED
    assert result.missing == ("#9000000",)


def test_a_missing_predicate_word_alone_is_unverifiable() -> None:
    """The paraphrase case D3 routes away from a hard rejection.

    Every entity and figure is present and only a predicate word is phrased differently,
    which is exactly the surface ambiguity the outcome exists to name.
    """
    result = check_containment(
        "Paris has 2.1 million inhabitants",
        "Paris counts 2,100,000 residents within the city limits.",
    )

    assert result.outcome is ContainmentOutcome.UNVERIFIABLE
    assert result.missing == ("inhabitants",)


# ── AC-4 — the measured bars, both arms ─────────────────────────────────────────────


def _false_rejection_rate(probes: tuple[VarianceProbe, ...]) -> float:
    """Share of genuinely-supported probes that containment declines to pass."""
    rejected = sum(
        1 for probe in probes if not check_containment(probe.claim, probe.source).contained
    )
    return rejected / len(probes)


def test_variance_probe_set_meets_false_rejection_bar() -> None:
    """AC-4 — normalization tolerates the enumerated classes without manufacturing refusals.

    The bar is preregistered in the probe module, ahead of this measurement. At this set's
    size it admits **zero** false rejections, which is the strictest honest reading of it.
    """
    failures = [
        (probe.variance_class, probe.claim)
        for probe in VARIANCE_PROBES
        if not check_containment(probe.claim, probe.source).contained
    ]

    assert _false_rejection_rate(VARIANCE_PROBES) <= FALSE_REJECTION_BAR, failures


def test_broken_baseline_fails_the_bar() -> None:
    """A bar a known-broken implementation would pass is not a bar (ADR-0138).

    Exact substring matching with no normalization is the deliberately broken baseline —
    it must land **above** the bar, or the bar is measuring nothing.
    """
    rejected = sum(
        1
        for probe in VARIANCE_PROBES
        if not all(
            token in probe.source.lower() for token in probe.claim.lower().replace(".", "").split()
        )
    )

    assert rejected / len(VARIANCE_PROBES) > FALSE_REJECTION_BAR


# The paired arm. Without it, a containment implementation that returns CONTAINED for
# everything scores a perfect 0% false-rejection rate and passes the bar above — the
# degenerate implementation ADR-0138's testing strategy requires each criterion to catch.
UNSUPPORTED_PROBES: tuple[tuple[str, str], ...] = (
    ("Paris has 2.1 million residents", "Lyon is France's third largest city."),
    ("The treaty was ratified in 1974", "The treaty was ratified in 1982 after two delays."),
    ("This fish is high in mercury", "Bonito is line-caught in the Bay of Biscay."),
    ("IBM published the specification", "Digital Equipment published the specification."),
    ("The tunnel runs 50 km beneath the strait", "The tunnel runs 8 km beneath the strait."),
    ("Storage is capped at 20 GB", "Storage is unmetered on every plan."),
    ("Turnout was 62 percent", "Turnout was 41 percent of the registered electorate."),
)
"""Claims their cited source does **not** support — the false-acceptance arm's probes."""


def test_unsupported_claims_are_never_accepted() -> None:
    """AC-4's paired arm: the false-acceptance bar is zero, and it is zero by construction.

    Default-deny (D1) admits no tolerance here — a single false acceptance is a claim
    shipped with no admissible provenance, which is the whole failure the contract exists
    to prevent. Unlike the false-rejection bar this is not a calibration; it is the
    contract restated as a test.
    """
    accepted = [
        claim for claim, source in UNSUPPORTED_PROBES if check_containment(claim, source).contained
    ]

    assert accepted == []
