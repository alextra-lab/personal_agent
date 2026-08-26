"""D3(c) containment — is the asserted claim actually *in* the cited source (FRE-1282).

ADR-0138 D3 calls containment "not optional and not entailment": normalized token presence,
costing nothing, closing the largest hole in a citation regime — a real, reachable,
entirely unrelated source attached to an invented claim. Reachability alone proves a page
exists, never that it mentions the thing asserted.

**The unit, which two earlier drafts got wrong.** The check is not "some token from the
span appears" — matching ``Paris`` alone in *"Paris has 2.1 million residents"* recreates
exactly the citation theatre it exists to close. Nor is it "every entity and figure", which
is **vacuous** for *"this fish is high in mercury"*: that span has neither, so the condition
held over an empty set and any source whatever passed.

This module takes D3's stated unit — *every entity, every figure, and every predicate
content word* — literally. The required set is every content word **of the claim itself**:
not in the closed function-word list, and not part of an attribution frame. So
``Paris has 2.1 million residents`` requires ``paris``, ``2100000`` and ``residents``,
dropping ``has``.

**The frame is not the claim, and that distinction is load-bearing.** A plan review broke
an earlier "every content word" rule with
*"According to the cited table, Paris has 2.1 million residents"*: a table genuinely
containing ``Paris``, ``2.1 million`` and ``residents`` — a source that supports the claim
completely — would be rejected for not also containing ``according``, ``cited`` and
``table``. That is D3's own warning ("demanding every non-stopword would manufacture
refusals") arriving in the concrete. Two closed lists answer it, and only these two:
:data:`EVIDENTIAL_WORDS`, which qualify the *act of asserting* rather than the world, and
a leading attribution clause, which :func:`strip_attribution_frame` removes. Everything
else stays required, because narrowing this set further is how the vacuity bug returns.

**Containment is necessary everywhere and sufficient almost everywhere.** For a span with
no entity and no figure, D3 rules containment "too weak to be meaningful — a page
mentioning ``mercury`` does not thereby support *'this fish is high in mercury'*" and
escalates that class to inline entailment (D3(d), FRE-1286). This module reports the
escalation as a *result*, :attr:`ContainmentOutcome.ENTAILMENT_REQUIRED`; deciding what to
do with it is :mod:`personal_agent.grounding.verification`'s job. The escalation is reached
only *after* containment passes, so AC-3's mercury case fails here rather than being
deferred.

**Why a miss splits two ways — and why not by counting.** ADR-0138 D3 requires
"unverifiable-by-containment" — the paraphrase, translation and unregistered-alias cases —
to stay distinguishable in telemetry from a true no-source outcome, "so a wave of false
refusals can never read as honest not-knowing".

The split is by **which kind of unit is missing**, never by how many tokens matched. A
plan review broke the counting version with *"Paris has 9 million residents"* cited to a
source stating *"Paris has 2.1 million residents"*: two of three tokens match, so a
cardinality rule files a **contradicted figure** — the purest citation theatre there is —
as a mere normalization limitation. Missing an entity or a figure is never a surface
accident; missing only a predicate word plausibly is, because a synonym for a predicate is
exactly the paraphrase case D3 names.

=========================================  ========================================
Missing from the source                    Outcome
=========================================  ========================================
nothing                                    :attr:`~ContainmentOutcome.CONTAINED`
any **entity** or **figure**               :attr:`~ContainmentOutcome.NOT_CONTAINED`
only **predicate content words**           :attr:`~ContainmentOutcome.UNVERIFIABLE`
=========================================  ========================================

Both misses are failures and both take the D4 path. They differ only in what the turn
record says about them, which is the whole of AC-6.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

# ── The normalization contract (ADR-0138 D3) ────────────────────────────────────────

FUNCTION_WORDS: frozenset[str] = frozenset(
    {
        # Determiners and quantifying determiners
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "each",
        "every",
        "some",
        "any",
        "no",
        "all",
        "both",
        "either",
        "neither",
        "such",
        "another",
        "other",
        # Pronouns
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "his",
        "its",
        "our",
        "their",
        "mine",
        "yours",
        "hers",
        "ours",
        "theirs",
        "who",
        "whom",
        "whose",
        "which",
        "what",
        "there",
        "here",
        # Copulas, auxiliaries, modals
        "be",
        "am",
        "is",
        "are",
        "was",
        "were",
        "been",
        "being",
        "have",
        "has",
        "had",
        "having",
        "do",
        "does",
        "did",
        "doing",
        "will",
        "would",
        "shall",
        "should",
        "can",
        "could",
        "may",
        "might",
        "must",
        "ought",
        # Prepositions
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "from",
        "by",
        "as",
        "into",
        "onto",
        "over",
        "under",
        "about",
        "between",
        "among",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "up",
        "down",
        "out",
        "off",
        "per",
        "via",
        "within",
        "without",
        "across",
        "against",
        "toward",
        "towards",
        "upon",
        "than",
        # Conjunctions and discourse connectives
        "and",
        "or",
        "but",
        "nor",
        "so",
        "yet",
        "if",
        "then",
        "because",
        "while",
        "when",
        "where",
        "whether",
        "although",
        "though",
        "since",
        "unless",
        "also",
        "however",
        "therefore",
        "thus",
        "hence",
        "moreover",
        # Negation and degree particles that carry no checkable predicate of their own
        "not",
        "only",
        "just",
        "very",
        "quite",
        "rather",
        "too",
        "still",
        "even",
    }
)
"""Words D3 calls "connective and function words", which containment ignores.

Closed and hand-built rather than imported: the list is part of the *decided* unit, and a
library's stopword set would silently change what must be contained whenever it was
upgraded. Notably **absent**: ``high``, ``low``, ``more``, ``most``, ``first`` and every
comparative or evaluative word — each is a predicate content word, and dropping them is
how the vacuity bug reappears one level down.
"""

EVIDENTIAL_WORDS: frozenset[str] = frozenset(
    {
        "according",
        "reportedly",
        "apparently",
        "allegedly",
        "cited",
        "citing",
        "reports",
        "reported",
        "states",
        "stated",
        "says",
        "said",
        "notes",
        "noted",
        "shows",
        "indicates",
        "suggests",
        "source",
        "sources",
        "per",
    }
)
"""Words that qualify the *act of asserting* rather than the world.

*"According to the source, X"* and *"X"* make the same claim about the world, and a source
that supports the second supports the first. Requiring the source to contain ``according``
therefore rejects a citation on the strength of the model's framing, not its content.

Closed and small, and it stays that way. Every word here is one the contract stops
checking, so the list is the one place where an over-eager addition silently weakens
containment. ``high``, ``low``, ``more``, ``most`` and every comparative are deliberately
**not** here: those are predicate content words, and dropping them is exactly the vacuity
this module exists to prevent.
"""

_ATTRIBUTION_FRAME = re.compile(
    r"^\s*(?:according\s+to|as\s+(?:reported|stated|noted|shown)\s+(?:by|in)|based\s+on|per)\b[^,]{0,80},",
    re.IGNORECASE,
)
"""A sentence-initial attribution clause, terminated by its comma.

Bounded to 80 characters so a comma far downstream in an ordinary sentence cannot swallow
the claim itself — a frame that long is not a frame.
"""

MAGNITUDE_WORDS: dict[str, Decimal] = {
    "hundred": Decimal(100),
    "thousand": Decimal(1_000),
    "million": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
    "trillion": Decimal(1_000_000_000_000),
}
"""Multipliers that fold a spelled magnitude into its figure — ``2.1 million`` ≡ ``2100000``."""

UNIT_SYNONYMS: dict[str, str] = {
    "km": "kilometre",
    "kilometer": "kilometre",
    "kilometers": "kilometre",
    "kilometres": "kilometre",
    "kilometre": "kilometre",
    "m": "metre",
    "meter": "metre",
    "meters": "metre",
    "metres": "metre",
    "kg": "kilogram",
    "kilograms": "kilogram",
    "kilogramme": "kilogram",
    "g": "gram",
    "grams": "gram",
    "gramme": "gram",
    "grammes": "gram",
    "gb": "gigabyte",
    "gigabytes": "gigabyte",
    "gigabyte": "gigabyte",
    "mb": "megabyte",
    "megabytes": "megabyte",
    "megabyte": "megabyte",
    "tb": "terabyte",
    "terabytes": "terabyte",
    "terabyte": "terabyte",
    "%": "percent",
    "pct": "percent",
    "percentage": "percent",
    "°c": "celsius",
    "c": "celsius",
    "centigrade": "celsius",
    "°f": "fahrenheit",
    "s": "second",
    "sec": "second",
    "secs": "second",
    "seconds": "second",
    "min": "minute",
    "mins": "minute",
    "minutes": "minute",
    "hr": "hour",
    "hrs": "hour",
    "hours": "hour",
}
"""Synonyms **within** one unit, folded to a canonical token.

Deliberately never a cross-unit conversion. ``km`` → ``mi`` would let a source stating one
quantity satisfy a claim stating a different number, which is not a normalization but a
change of fact. D3 asks for "common unit expressions of the same quantity"; this is that,
and no more.
"""

ALIASES: dict[str, tuple[str, ...]] = {
    "ibm": ("international", "business", "machines"),
    "who": ("world", "health", "organization"),
    "un": ("united", "nations"),
    "uk": ("united", "kingdom"),
    "usa": ("united", "states"),
    "us": ("united", "states"),
    "eu": ("european", "union"),
    "nasa": ("national", "aeronautics", "and", "space", "administration"),
    "nato": ("north", "atlantic", "treaty", "organization"),
}
"""The registered alias table D3 admits "where an alias table exists".

Seed-sized on purpose. An *unregistered* alias is precisely the case D3 routes to
``unverifiable-by-containment`` rather than to a rejection, so an incomplete table
degrades into the honest outcome instead of into a false refusal.
"""

_STROKED_LETTERS = str.maketrans(
    {
        "ł": "l",
        "Ł": "L",
        "ø": "o",
        "Ø": "O",
        "đ": "d",
        "Đ": "D",
        "ß": "ss",
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
        "ð": "d",
        "Ð": "D",
        "þ": "th",
        "Þ": "Th",
    }
)
"""Letters whose Latin base survives no Unicode decomposition.

``Zürich``/``Zurich`` falls out of NFKD plus combining-mark removal; ``Łódź``/``Lodz`` does
not, because ``Ł`` is an atomic codepoint with no decomposition. D3 asks for "Unicode
normalization" and this is the part of it that a normalization form alone does not deliver.
"""

_TOKEN_PATTERN = re.compile(r"[^\W_]+(?:[.,][^\W_]+)*|%|°[cf]", re.UNICODE)
"""Word, number-with-separators, or a bare unit symbol.

Numbers keep their internal ``.``/``,`` so ``2,100,000`` and ``3.0`` survive tokenization
intact and can be parsed as one figure; ``_`` is excluded from word characters so
``snake_case`` splits rather than fusing two content words.
"""


def _fold(text: str) -> str:
    """Return text in the comparison form: NFKC, casefolded, diacritics removed.

    Args:
        text: Any string.

    Returns:
        The folded form. NFKC first so compatibility characters (the ``ﬃ`` ligature,
        fullwidth digits) become their ordinary equivalents; then stroked letters, which
        no normalization form decomposes; then NFKD with combining marks dropped.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = folded.translate(_STROKED_LETTERS).casefold()
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _as_figure(token: str) -> Decimal | None:
    """Parse a token as a number, tolerating group separators and decimal precision.

    ``1,000`` ≡ ``1000`` and ``3.0`` ≡ ``3``, which is D3's stated tolerance. A token
    carrying both separators and a decimal point (``1,234.5``) is read the English way;
    a locale that swaps the two is not distinguishable from it by surface form alone and
    is deliberately not guessed at.

    Args:
        token: One normalized token.

    Returns:
        The value with trailing zeros stripped, or None when the token is not a number.
    """
    if not any(ch.isdigit() for ch in token):
        return None
    candidate = token.replace(",", "")
    try:
        value = Decimal(candidate)
    except InvalidOperation:
        return None
    return value


def _figure_token(value: Decimal) -> str:
    """Render a figure in its canonical comparison form.

    ``Decimal.normalize`` alone is not enough: it renders ``9000000`` as ``9E+6``, so two
    equal quantities written differently would compare unequal as strings — the precise
    failure ``3.0`` ≡ ``3`` is meant to prevent, reappearing at the other end of the scale.

    Args:
        value: The parsed quantity.

    Returns:
        A ``#``-prefixed plain-notation form with trailing zeros stripped, so it can never
        collide with an ordinary word.
    """
    normalized = value.normalize()
    _, _, exponent = normalized.as_tuple()
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return f"#{normalized:f}"


def _canonical(token: str) -> str:
    """Return the canonical comparison form of one folded token.

    Figures normalize to their numeric value, units to the canonical member of their
    synonym set, and everything else to itself.

    Args:
        token: One folded token.

    Returns:
        The canonical string this token compares as.
    """
    figure = _as_figure(token)
    if figure is not None:
        return _figure_token(figure)
    return UNIT_SYNONYMS.get(token, token)


def normalize_tokens(text: str) -> tuple[str, ...]:
    """Return ``text`` as the canonical token sequence containment compares over.

    Order is preserved because multi-token entities and aliases must match as *contiguous*
    subsequences, and because magnitude folding needs to see a figure's successor.

    Args:
        text: Source content or span text.

    Returns:
        Canonical tokens, in order. Matching over whole tokens is what gives D3's
        token-boundary rule for free: ``ham`` is simply not a member of a sequence
        containing ``birmingham``.
    """
    raw = _TOKEN_PATTERN.findall(_fold(text))
    tokens: list[str] = []
    index = 0
    while index < len(raw):
        token = raw[index]
        figure = _as_figure(token)
        if figure is not None and index + 1 < len(raw):
            multiplier = MAGNITUDE_WORDS.get(raw[index + 1])
            if multiplier is not None:
                tokens.append(_figure_token(figure * multiplier))
                index += 2
                continue
        tokens.append(_canonical(token))
        index += 1
    return tuple(tokens)


# ── The claim unit (ADR-0138 D3) ────────────────────────────────────────────────────


class ClaimUnit(BaseModel):
    """The tokens one atomic-claim span requires its source to contain.

    Attributes:
        required: The canonical content words, deduplicated, in first-appearance order.
        figures: The subset that are figures — the ``#value`` forms.
        entities: The subset that look like named entities in the span text. Used to
            decide D3(d)'s entity-free escalation and to classify a miss as unsupported
            rather than unverifiable — never to narrow ``required``, which is the vacuity
            bug.
    """

    model_config = ConfigDict(frozen=True)

    required: tuple[str, ...]
    figures: tuple[str, ...]
    entities: tuple[str, ...]

    @property
    def is_entity_free_predicate(self) -> bool:
        """Whether D3(d) must decide this span, because containment cannot.

        True when the span names no entity and states no figure — the
        *"this fish is high in mercury"* class, for which a source merely mentioning the
        predicate word proves nothing.
        """
        return not self.entities and not self.figures


_ENTITY_PATTERN = re.compile(r"\b[A-Z][\w'’-]*|\b[A-Z]{2,}\b")


def strip_attribution_frame(span_text: str) -> str:
    """Remove a leading attribution clause, leaving the claim it frames.

    *"According to the cited table, Paris has 2.1 million residents"* asserts the same
    thing about the world as *"Paris has 2.1 million residents"*, and a source supporting
    the second supports the first. Requiring the frame's own words to appear in the source
    rejects a citation on the strength of the model's phrasing.

    Args:
        span_text: The span's text.

    Returns:
        The text with a sentence-initial attribution clause removed, or unchanged when
        there is none. The frame is never removed when nothing follows it: a span that is
        *only* a frame has no claim to check, and returning an empty string would hand
        :func:`check_containment` the empty required set it must never see.
    """
    match = _ATTRIBUTION_FRAME.match(span_text)
    if match is None:
        return span_text
    remainder = span_text[match.end() :].strip()
    return remainder or span_text


def claim_unit(span_text: str) -> ClaimUnit:
    """Derive the required-token set for one atomic-claim span.

    Args:
        span_text: The span's text, with any citation marker already removed — a marker's
            own characters are protocol, and letting them become content words would make
            every cited span require its own identifier to appear in the source.

    Returns:
        The claim unit: every content word of the claim, after the attribution frame and
        the evidential words are removed. See the module docstring for why it is not
        narrowed further.
    """
    claim_text = strip_attribution_frame(span_text)
    required: list[str] = []
    for token in normalize_tokens(claim_text):
        if token in FUNCTION_WORDS or token in EVIDENTIAL_WORDS or token in MAGNITUDE_WORDS:
            continue
        if token not in required:
            required.append(token)

    entities: list[str] = []
    for match in _ENTITY_PATTERN.finditer(claim_text):
        for token in normalize_tokens(match.group(0)):
            if token.startswith("#"):
                continue
            if token in required and token not in entities:
                entities.append(token)

    return ClaimUnit(
        required=tuple(required),
        figures=tuple(t for t in required if t.startswith("#")),
        entities=tuple(entities),
    )


# ── The check ───────────────────────────────────────────────────────────────────────


class ContainmentOutcome(StrEnum):
    """What D3(c) decided about one span against one source.

    ``UNVERIFIABLE`` and ``NOT_CONTAINED`` are kept apart because ADR-0138 D3 requires it:
    a wave of the former is a normalizer defect, a wave of the latter is the contract
    catching citation theatre, and blurring them would let one read as the other.
    """

    CONTAINED = "contained"
    NOT_CONTAINED = "not_contained"
    UNVERIFIABLE = "unverifiable_by_containment"
    ENTAILMENT_REQUIRED = "entailment_required"


class ContainmentResult(BaseModel):
    """One containment decision, with the tokens that decided it.

    Attributes:
        outcome: The decision.
        required: Every token the span required.
        missing: The required tokens absent from the source, in required order.
        entity_free_predicate: Whether the span fell in D3(d)'s escalated class.
    """

    model_config = ConfigDict(frozen=True)

    outcome: ContainmentOutcome
    required: tuple[str, ...]
    missing: tuple[str, ...]
    entity_free_predicate: bool = False

    @property
    def passed(self) -> bool:
        """Whether the source contains the claim and no further check is owed."""
        return self.outcome is ContainmentOutcome.CONTAINED

    @property
    def contained(self) -> bool:
        """Whether the source contains the claim, whatever is still owed after that.

        Distinct from :attr:`passed` because ``ENTAILMENT_REQUIRED`` means containment
        *succeeded* and D3(d) has yet to speak. AC-4's false-rejection bar is measured on
        this, not on ``passed``: an escalation is attributable to the entailment checker
        being absent (FRE-1286), never to the normalization contract, and scoring it as a
        normalization failure would measure the wrong component.
        """
        return self.outcome in {
            ContainmentOutcome.CONTAINED,
            ContainmentOutcome.ENTAILMENT_REQUIRED,
        }


def _contains_sequence(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """Whether ``needle`` appears in ``haystack`` as a contiguous run.

    Args:
        haystack: The source's canonical tokens.
        needle: The alias expansion's canonical tokens.

    Returns:
        True on a contiguous match.
    """
    if not needle:
        return False
    span = len(needle)
    return any(haystack[i : i + span] == needle for i in range(len(haystack) - span + 1))


_ACRONYM_PATTERN = re.compile(r"\b[A-Z]{2,}\b")


def acronyms_in(text: str) -> frozenset[str]:
    """Return the tokens ``text`` writes as acronyms, casefolded.

    **Case is the only thing separating an acronym from its homograph**, and casefolding
    happens early in the normalization contract, so alias matching cannot be driven off
    normalized tokens. ``WHO`` is the World Health Organization; ``who`` is a pronoun that
    appears on almost every English page. ``US``/``us`` and ``UN``/``un`` are the same
    trap. Reading acronyms from the raw text, before :func:`_fold`, is what keeps an
    ordinary word from standing in for an organisation.

    Args:
        text: Raw claim or source text, unnormalized.

    Returns:
        The casefolded acronym tokens, for lookup against :data:`ALIASES`.
    """
    return frozenset(match.group(0).casefold() for match in _ACRONYM_PATTERN.finditer(text))


def _expansion_satisfied_tokens(
    claim_tokens: tuple[str, ...], source_acronyms: frozenset[str]
) -> frozenset[str]:
    """Return claim tokens covered by an acronym the source uses instead.

    The **claim-level** half of alias matching, and it has to be claim-level. A per-token
    rule — "this token is present if it belongs to some expansion whose acronym is in the
    source" — is a false-acceptance generator, because alias keys are ordinary words:
    ``who`` appears in almost any English page, and under a per-token rule it would satisfy
    a required ``world``, ``health`` *or* ``organization`` in a claim the page never makes.
    That is precisely the citation theatre D3(c) exists to close, produced by the check
    itself.

    Two conditions together close it: the claim must spell the expansion out
    **contiguously**, and the source must write the acronym **as an acronym**
    (:func:`acronyms_in`, read from raw text before casefolding). So
    ``The World Health Organization revised the guidance`` is satisfied by a page saying
    ``the WHO revised the guidance`` and *not* by one asking ``who revised the guidance``.

    Args:
        claim_tokens: The claim's canonical tokens, in order and before deduplication —
            contiguity is the whole guarantee, so the ordered form is required.
        source_acronyms: Acronyms the source writes as such.

    Returns:
        The tokens an acronym in the source accounts for.
    """
    covered: set[str] = set()
    for acronym, expansion in ALIASES.items():
        if acronym in source_acronyms and _contains_sequence(claim_tokens, expansion):
            covered.update(expansion)
    return frozenset(covered)


def _present(
    token: str,
    source_tokens: tuple[str, ...],
    source_set: frozenset[str],
    alias_covered: frozenset[str],
    claim_acronyms: frozenset[str],
) -> bool:
    """Whether one required token is present in the source, aliases included.

    Args:
        token: A canonical required token.
        source_tokens: The source's canonical tokens, in order.
        source_set: The same tokens as a set, for the common case.
        alias_covered: Tokens :func:`_expansion_satisfied_tokens` accounted for.
        claim_acronyms: Acronyms the *claim* writes as such — gating the expansion branch
            on these is the mirror of the source-side rule, and for the same reason:
            without it, ``us`` in ``give us the report`` would be satisfied by any page
            mentioning the United States.

    Returns:
        True when the token appears directly, when the claim used an acronym whose
        registered expansion appears in the source as a contiguous run, or when the source
        used an acronym for an expansion the claim spelled out.
    """
    if token in source_set or token in alias_covered:
        return True
    if token not in claim_acronyms:
        return False
    expansion = ALIASES.get(token)
    return expansion is not None and _contains_sequence(source_tokens, expansion)


def check_containment(span_text: str, source_content: str) -> ContainmentResult:
    """Run D3(c) for one span against one source's retrieved content.

    Args:
        span_text: The asserted span, citation marker already stripped.
        source_content: The cited source's retrieved content.

    Returns:
        The decision. A span with no content words at all — which span extraction should
        never emit as a claim — is reported ``UNVERIFIABLE`` rather than passing: an empty
        required set holding vacuously is the exact bug D3's predicate rule was written to
        kill, and it must not be reachable from a degenerate input either.
    """
    unit = claim_unit(span_text)
    source_tokens = normalize_tokens(source_content)
    source_set = frozenset(source_tokens)

    if not unit.required:
        return ContainmentResult(
            outcome=ContainmentOutcome.UNVERIFIABLE,
            required=(),
            missing=(),
            entity_free_predicate=unit.is_entity_free_predicate,
        )

    claim_text = strip_attribution_frame(span_text)
    alias_covered = _expansion_satisfied_tokens(
        normalize_tokens(claim_text), acronyms_in(source_content)
    )
    claim_acronyms = acronyms_in(claim_text)
    missing = tuple(
        token
        for token in unit.required
        if not _present(token, source_tokens, source_set, alias_covered, claim_acronyms)
    )

    if missing:
        # By WHICH unit is missing, never by how many matched. A missing entity or figure
        # is the "Paris has 9 million residents" shape — a source stating a different
        # number is not a normalization limitation, it is the claim being unsupported.
        # Only a predicate word can plausibly be absent for a surface reason, which is
        # the paraphrase/translation/unregistered-alias case D3 routes to unverifiable.
        #
        # A total miss is also hard, whatever the span was made of: a source sharing not
        # one content word with the claim is unrelated, and filing that as "the normalizer
        # could not tell" would let the plainest citation theatre read as our own defect.
        hard = set(unit.entities) | set(unit.figures)
        unsupported = bool(hard.intersection(missing)) or len(missing) == len(unit.required)
        outcome = (
            ContainmentOutcome.NOT_CONTAINED if unsupported else ContainmentOutcome.UNVERIFIABLE
        )
    elif unit.is_entity_free_predicate:
        # Contained, but D3(d) owns this class: a page mentioning `mercury` does not
        # thereby support "this fish is high in mercury". Escalation is reached only
        # after containment passes, so AC-3's case fails above rather than here.
        outcome = ContainmentOutcome.ENTAILMENT_REQUIRED
    else:
        outcome = ContainmentOutcome.CONTAINED

    return ContainmentResult(
        outcome=outcome,
        required=unit.required,
        missing=missing,
        entity_free_predicate=unit.is_entity_free_predicate,
    )


__all__ = [
    "ALIASES",
    "FUNCTION_WORDS",
    "MAGNITUDE_WORDS",
    "UNIT_SYNONYMS",
    "ClaimUnit",
    "acronyms_in",
    "ContainmentOutcome",
    "ContainmentResult",
    "check_containment",
    "claim_unit",
    "normalize_tokens",
]
