"""The variance probe set AC-4's false-rejection bar is measured against (FRE-1282).

ADR-0138 D3 fixes the *classes* of surface variance containment must tolerate — case and
Unicode normalization, digit-group separators and decimal precision, common unit
expressions of one quantity, and registered aliases — and D3's "False rejections are a
first-class cost" makes the tolerated rate a measured property rather than a matter of
taste.

**Every probe here is a genuine positive.** Each pairs an atomic claim with a source that
really does support it, differing only in surface form. A probe that fails is therefore a
**false rejection** by construction, which is what makes the rate in
``test_containment.py::test_variance_probe_set_meets_false_rejection_bar`` mean something.
Negative probes — a source that does *not* support the claim — belong with the checks they
exercise (AC-1, AC-3), not here: mixing them would turn a false-rejection rate into an
undifferentiated accuracy number.

The set is deliberately small and hand-built. ADR-0138's "Governance of the set and the
bars" reserves the held-out, versioned corpus for adjudication on the umbrella (FRE-1279);
this is the *in-repo regression* set that keeps the normalization contract from rotting
between adjudications, and it is named in the ticket rather than in the ADR for exactly
that reason.

**One grouping convention is deliberately absent.** Space-grouped digits (``12 500``) are
not folded, and no probe asserts they are. The fold cannot be told apart from two adjacent
numbers by surface form, and getting it wrong in the *source* direction would join two
unrelated figures into one that a claim could then match — a **false acceptance**, which
default-deny does not trade for convenience. Comma grouping and bare digits carry the
class instead.
"""

from __future__ import annotations

from typing import NamedTuple


class VarianceProbe(NamedTuple):
    """One genuinely-supported claim whose source states it in a different surface form.

    Attributes:
        variance_class: Which class of D3 normalization tolerance this exercises. Rates
            are reported per class as well as in aggregate, so a bar met on average while
            one class is wholly broken is still visible.
        claim: The asserted span text, as the model would have written it.
        source: The cited source's retrieved content, which supports ``claim``.
    """

    variance_class: str
    claim: str
    source: str


DIGIT_GROUPING: tuple[VarianceProbe, ...] = (
    VarianceProbe(
        "digit_grouping",
        "The bridge carries 1,000 vehicles daily.",
        "Traffic counts show the bridge carries 1000 vehicles daily across both lanes.",
    ),
    VarianceProbe(
        "digit_grouping",
        "The archive holds 250000 records.",
        "The archive holds 250,000 records catalogued since 1974.",
    ),
    VarianceProbe(
        "digit_grouping",
        "Membership reached 12,500 households.",
        "By the end of the year membership reached 12500 households.",
    ),
)

DECIMAL_PRECISION: tuple[VarianceProbe, ...] = (
    VarianceProbe(
        "decimal_precision",
        "The sample measured 3.0 millimetres.",
        "Each sample measured 3 millimetres under the calibrated gauge.",
    ),
    VarianceProbe(
        "decimal_precision",
        "The reactor ran at 40 percent capacity.",
        "The reactor ran at 40.0 percent capacity throughout the trial.",
    ),
    VarianceProbe(
        "decimal_precision",
        "The coefficient is 0.500 in the published table.",
        "The published table lists the coefficient as 0.5.",
    ),
)

MAGNITUDE_WORDS: tuple[VarianceProbe, ...] = (
    VarianceProbe(
        "magnitude_words",
        "Paris has 2.1 million residents.",
        "Paris counts 2,100,000 residents within the city limits.",
    ),
    VarianceProbe(
        "magnitude_words",
        "The fund disbursed 3 billion euros.",
        "The fund disbursed 3,000,000,000 euros over the programme.",
    ),
)

UNIT_SYNONYMS: tuple[VarianceProbe, ...] = (
    VarianceProbe(
        "unit_synonyms",
        "The tunnel runs 50 km beneath the strait.",
        "The tunnel runs 50 kilometres beneath the strait.",
    ),
    VarianceProbe(
        "unit_synonyms",
        "Storage is capped at 20 GB per account.",
        "Storage is capped at 20 gigabytes per account.",
    ),
    VarianceProbe(
        "unit_synonyms",
        "The alloy melts at 660 °C.",
        "The alloy melts at 660 celsius under standard pressure.",
    ),
    VarianceProbe(
        "unit_synonyms",
        "Turnout was 62 %.",
        "Turnout was 62 percent of the registered electorate.",
    ),
)

REGISTERED_ALIASES: tuple[VarianceProbe, ...] = (
    VarianceProbe(
        "registered_aliases",
        "IBM published the specification.",
        "International Business Machines published the specification in 1987.",
    ),
    VarianceProbe(
        "registered_aliases",
        "The World Health Organization revised the guidance.",
        "The WHO revised the guidance the following spring.",
    ),
    VarianceProbe(
        "registered_aliases",
        "The United Kingdom ratified the treaty.",
        "The UK ratified the treaty ahead of the deadline.",
    ),
)

CASE_VARIANCE: tuple[VarianceProbe, ...] = (
    VarianceProbe(
        "case",
        "The MERCURY content exceeds the advisory level.",
        "Testing found the mercury content exceeds the advisory level.",
    ),
    VarianceProbe(
        "case",
        "iPhone shipments rose that quarter.",
        "IPHONE SHIPMENTS ROSE THAT QUARTER, THE FILING SAYS.",
    ),
)

UNICODE_VARIANCE: tuple[VarianceProbe, ...] = (
    VarianceProbe(
        "unicode",
        "Zürich hosts the assembly.",
        "Zurich hosts the assembly every second year.",
    ),
    VarianceProbe(
        "unicode",
        "The report names Lódz as the site.",
        "The report names Łódź as the site of the works.",
    ),
    VarianceProbe(
        # NFKC folds the ligature and the fullwidth digits; without it these are
        # different codepoints carrying the same word.
        "unicode",
        "The oﬃce recorded ７ incidents.",
        "The office recorded 7 incidents during the audit.",
    ),
)

VARIANCE_PROBES: tuple[VarianceProbe, ...] = (
    *DIGIT_GROUPING,
    *DECIMAL_PRECISION,
    *MAGNITUDE_WORDS,
    *UNIT_SYNONYMS,
    *REGISTERED_ALIASES,
    *CASE_VARIANCE,
    *UNICODE_VARIANCE,
)
"""Every probe, in class order. All are genuine positives — see the module docstring."""

FALSE_REJECTION_BAR = 0.05
"""AC-4's preregistered bound on the false-rejection rate (FRE-1282).

Recorded here, in the probe module, **before any result was seen** — ADR-0138 requires the
bar to be fixed in the implementation ticket rather than in the ADR, and fixed ahead of the
measurement rather than after inspecting it.

**Justified against the failure it prevents.** A containment miss on a legitimate assertion
forces the D4 no-source path and produces a refusal the user did not deserve. At 5%, about
one turn in twenty carrying a cited claim would refuse spuriously — the highest rate at
which the contract still reads as *grounded* rather than as *broken*. Above it, the D5
compliance metric would be measuring the normalizer's defects rather than the model's.

**And demonstrated to bind.** ``test_broken_baseline_fails_the_bar`` runs this same set
through exact-string matching with normalization disabled and asserts it lands above the
bar. A bar that a known-broken implementation would pass is not a bar.
"""
