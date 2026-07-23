"""Session digest schema, rendering and provenance validation (ADR-0124 D3, FRE-947).

The digest encodes the **epistemic state left behind by an episode**. It does not
retell the episode. That single sentence governs the shape here: four optional
slots holding what survived, what was concluded, what is still open and what the
evidence contradicts — never a narrative replay, and never a re-derivation of the
per-turn summaries and entity edges that already exist.

Two artifacts come out of one model call and are stored independently:

* ``session_label`` — a short distinguishing noun phrase, replacing the
  first-60-characters title hack.
* ``SessionDigest`` — the structured record below.

**Provenance is structural and verifiable, not aspirational.** ``basis`` is a
model-assigned tag, and nothing stops a model labelling its own inference as
evidence. So every item tagged ``tool_evidence``, and every ``corrections`` entry,
must carry a verbatim span *plus a locator* — the capture id and the field within
it. :func:`validate_digest_provenance` resolves the locator and requires the span
to occur **at that location**. Bare containment somewhere in the session is not
sufficient: a common word appears everywhere and would pass while supporting
nothing.

**Stated limitation (ADR-0124 AC-11).** This proves the citation *resolves*, not
that the span *supports* the proposition. A fabricated item citing a real but
irrelevant span at a valid locator passes. Mechanical entailment is not available
to us; semantic support is carried by the labelled fixture sets (AC-12) and human
review (AC-16). This module is a necessary condition that makes the cheap failure
mode — invented citations — impossible, and is claimed as nothing more.

**Storage is structured; rendering is derived.** The record is canonical.
Consumers receive :func:`render_digest`'s labelled prose assembled at read time —
there is no stored rendered field, because that would be a second staleness surface.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover — typing only
    from personal_agent.captains_log.capture import TaskCapture

# This module is deliberately a LEAF: `SessionNode` (memory/models.py) holds a
# `SessionDigest`, so anything this imports at module scope would become a
# dependency of the memory models. `TaskCapture` is therefore typing-only —
# locator resolution reads four attributes off it and needs no runtime import —
# and the tokenizer is imported inside the one function that counts.

# The four provenance tags an item may carry (ADR-0124 D3).
BasisTag = Literal["tool_evidence", "user_statement", "assistant_reasoning", "mixed"]

# Only two correction tiers may ever be asserted (ADR-0124 D3):
#   A — direct contradiction: authoritative evidence contradicts the SAME
#       proposition the agent asserted. Carries two located spans.
#   B — explicit evidenced self-correction: the agent corrected the record within
#       the session and the correction is supported by evidence in the capture.
# Everything else (weak/partial conflict, failed or incomplete calls, ambiguous
# readings, legitimately changed state, disagreement with a subjective judgment)
# is Tier C and is NEVER a correction — it belongs in `unresolved`, or is omitted.
CorrectionTier = Literal["A", "B"]

# Locator field grammar. Deliberately closed and machine-resolvable: an open
# free-text locator is unverifiable, which would defeat the point of requiring one.
_TOOL_FIELD_RE = re.compile(r"^tool_result\[(\d+)\]\.(output|error)$")
_USER_FIELD = "user_text"
_ASSISTANT_FIELD = "assistant_text"

# Label bound (ADR-0124 D3). A distinguishing noun phrase, not a compressed digest.
MAX_LABEL_CHARS = 90


class SummaryFailureReason(StrEnum):
    """Why a generation attempt failed.

    Split by whether repeating the attempt could plausibly succeed. The
    distinction is load-bearing: ADR-0124's terminal-failure rule states that a
    budget denial is *never* terminal, since it is transient by nature, while
    oversized input is terminal once it has been retried and failed
    deterministically.
    """

    # Deterministic — the same input fails the same way, so it can go terminal.
    OVERSIZED_INPUT = "oversized_input"
    SCHEMA_INVALID = "schema_invalid"
    SPAN_VALIDATION_FAILED = "span_validation_failed"
    DIGEST_OVER_BUDGET = "digest_over_budget"
    # Transient — always retryable, never terminal.
    BUDGET_DENIED = "budget_denied"
    MODEL_ERROR = "model_error"
    TIMEOUT = "timeout"
    EMPTY_OUTPUT = "empty_output"


TERMINAL_ELIGIBLE_REASONS: frozenset[str] = frozenset(
    {
        SummaryFailureReason.OVERSIZED_INPUT,
        SummaryFailureReason.SCHEMA_INVALID,
        SummaryFailureReason.SPAN_VALIDATION_FAILED,
        SummaryFailureReason.DIGEST_OVER_BUDGET,
    }
)


class SessionSummaryStatus(StrEnum):
    """Terminal state of one generation attempt."""

    GENERATED = "generated"
    SKIPPED_BELOW_FLOOR = "skipped_below_floor"
    FAILED = "failed"


class Locator(BaseModel):
    """Points at the exact place a verbatim span was taken from.

    Attributes:
        capture_id: The capture's ``trace_id`` — one capture is one turn.
        field: Where inside that capture, under the closed grammar
            ``user_text`` | ``assistant_text`` | ``tool_result[<i>].output`` |
            ``tool_result[<i>].error``.
    """

    model_config = ConfigDict(frozen=True)

    capture_id: str
    field: str


class DigestItem(BaseModel):
    """One item in a digest slot.

    Attributes:
        text: The item itself — what is established, decided or open.
        basis: Provenance tag. ``tool_evidence`` obliges ``span`` and ``locator``.
        span: Verbatim text taken from the capture, when the item cites evidence.
        locator: Where that span lives.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    basis: BasisTag
    span: str | None = None
    locator: Locator | None = None


class UnresolvedItem(DigestItem):
    """Unfinished state a future session could wrongly treat as settled.

    Attributes:
        as_of: The session's ``ended_at``. **Stamped by the producer, never by the
            model** — compute state, generate meaning. This is the field that lets
            a consumer phrase a nudge as "as of that session, X was open" rather
            than asserting the present tense, and it is what stops the leading risk
            in ADR-0124: a thread left open in session A, settled in session C,
            being re-asserted as permanently open because nothing revisits A.
            Nothing reads it until Phase 3; it ships now because the Phase 3 fix
            has nothing to stand on otherwise.
    """

    as_of: datetime


class Correction(DigestItem):
    """A high-confidence contradiction between evidence and narration.

    Usually empty. **That scarcity is a feature and a monitoring signal**, not an
    under-performing slot — a rising corrections rate is drift, not diligence.

    Error-flagging is precision-first and deliberately asymmetric: a missed error
    is recoverable from raw evidence, whereas a false error writes self-confirming
    state into the graph and feeds its own supposed correction into future
    reasoning.

    For Tier A, the inherited ``span``/``locator`` cite the **contradicted claim in
    the assistant text** and ``evidence_*`` cite the **contradicting evidence** —
    the two located spans ADR-0124 requires. For Tier B, ``span``/``locator`` cite
    the self-correction and ``evidence_*`` cite what supports it.

    Attributes:
        tier: ``A`` (direct contradiction) or ``B`` (evidenced self-correction).
        evidence_span: Verbatim supporting evidence.
        evidence_locator: Where that evidence lives.
    """

    tier: CorrectionTier
    evidence_span: str
    evidence_locator: Locator


class SessionDigest(BaseModel):
    """The structured record. All slots optional; empty slots omitted on render.

    There is deliberately **no** ``intent → trajectory → outcome`` schema. That
    describes tasks, and this corpus is substantially conversational and
    topic-drifting: a session that ran diet principles → a salad → a ratatouille →
    coaching a couscous has no single intent, and that is normal rather than
    pathological. Shape is determined by content, not by a session-type classifier.
    """

    model_config = ConfigDict(frozen=True)

    established: list[DigestItem] = Field(default_factory=list)
    decisions: list[DigestItem] = Field(default_factory=list)
    unresolved: list[UnresolvedItem] = Field(default_factory=list)
    corrections: list[Correction] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """Whether every slot is empty."""
        return not (self.established or self.decisions or self.unresolved or self.corrections)


class SessionSummaryOutcome(BaseModel):
    """Result of one generation attempt.

    Deliberately not ``str | None``. The old producer collapsed "no digest wanted",
    "generation failed" and "model returned nothing" into a single ``None`` that
    the caller then wrote unconditionally — which is precisely how a transient
    failure erased a good summary. The three states are now distinguishable at the
    call site, and only ``GENERATED`` and ``SKIPPED_BELOW_FLOOR`` advance freshness.

    Attributes:
        status: Which of the three outcomes occurred.
        label: The session label, when generated.
        digest: The structured digest, when generated.
        failure_reason: Why it failed, when it failed.
    """

    model_config = ConfigDict(frozen=True)

    status: SessionSummaryStatus
    label: str | None = None
    digest: SessionDigest | None = None
    failure_reason: SummaryFailureReason | None = None


def _normalise(text: str) -> str:
    """Collapse whitespace runs and strip, for span comparison.

    The stated canonical comparison. Raw byte equality is not well-defined once a
    structured payload has been serialised and escaped into a prompt and quoted
    back by a model, so spans are compared with whitespace normalised and case
    preserved — case-folding would let "ERROR" match "error", which is exactly the
    kind of near-miss a provenance check exists to catch.
    """
    return " ".join(text.split())


def resolve_locator(locator: Locator, captures: Sequence[TaskCapture]) -> str | None:
    """Resolve a locator to the exact text it names.

    Args:
        locator: The capture id and field to resolve.
        captures: The session's captures, in any order.

    Returns:
        The text at that location, or ``None`` if the capture is unknown, the field
        is outside the grammar, or the indexed tool result does not exist.
    """
    capture = next((c for c in captures if c.trace_id == locator.capture_id), None)
    if capture is None:
        return None

    if locator.field == _USER_FIELD:
        return capture.user_message or ""
    if locator.field == _ASSISTANT_FIELD:
        return capture.assistant_response or ""

    match = _TOOL_FIELD_RE.match(locator.field)
    if match is None:
        return None

    index = int(match.group(1))
    if index >= len(capture.tool_results):
        return None
    value = capture.tool_results[index].get(match.group(2))
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _check_located_span(
    span: str | None,
    locator: Locator | None,
    captures: Sequence[TaskCapture],
    *,
    where: str,
) -> list[str]:
    """Check one span/locator pair, returning any violations."""
    if span is None or locator is None:
        return [f"{where}: requires both a span and a locator"]

    resolved = resolve_locator(locator, captures)
    if resolved is None:
        return [
            f"{where}: locator {locator.capture_id}/{locator.field} does not resolve",
        ]
    if _normalise(span) not in _normalise(resolved):
        return [
            f"{where}: span not found at {locator.capture_id}/{locator.field} "
            "(bare containment elsewhere in the session does not count)",
        ]
    return []


def validate_digest_provenance(digest: SessionDigest, captures: Sequence[TaskCapture]) -> list[str]:
    """Enforce the located-span contract (ADR-0124 AC-11).

    Every ``tool_evidence`` item and every ``corrections`` entry must carry a span
    and a locator, and the span must occur **at that location**.

    Args:
        digest: The digest to check.
        captures: The session's captures, which the locators are resolved against.

    Returns:
        Human-readable violations, empty when the digest passes.
    """
    violations: list[str] = []

    slots: tuple[tuple[str, Sequence[DigestItem]], ...] = (
        ("established", digest.established),
        ("decisions", digest.decisions),
        ("unresolved", digest.unresolved),
    )
    for slot_name, items in slots:
        for i, item in enumerate(items):
            if item.basis != "tool_evidence":
                continue
            violations += _check_located_span(
                item.span, item.locator, captures, where=f"{slot_name}[{i}] (tool_evidence)"
            )

    for i, correction in enumerate(digest.corrections):
        where = f"corrections[{i}] (tier {correction.tier})"
        violations += _check_located_span(
            correction.span, correction.locator, captures, where=where
        )
        violations += _check_located_span(
            correction.evidence_span,
            correction.evidence_locator,
            captures,
            where=f"{where} evidence",
        )

    return violations


def render_digest(digest: SessionDigest) -> str:
    """Assemble the read-time projection consumers actually read.

    Dense labelled prose; empty slots are omitted entirely rather than rendered as
    empty headings. Derived, never stored — a stored rendering would be a second
    surface that can go stale independently of the record it renders.

    Args:
        digest: The canonical record.

    Returns:
        The rendered digest, or an empty string when every slot is empty.
    """
    sections: list[str] = []
    for label, items in (
        ("Established", digest.established),
        ("Decisions", digest.decisions),
        ("Unresolved", digest.unresolved),
        ("Corrections", digest.corrections),
    ):
        if not items:
            continue
        lines = []
        for item in items:
            suffix = ""
            if isinstance(item, UnresolvedItem):
                # Phrased as-of deliberately: a consumer must not read a stale open
                # thread as present-tense unfinished business.
                suffix = f" (as of {item.as_of.date().isoformat()})"
            lines.append(f"- {item.text}{suffix}")
        sections.append(f"{label}: \n" + "\n".join(lines))
    return "\n\n".join(sections)


def digest_token_count(digest: SessionDigest) -> int:
    """Token count of the digest as a consumer would read it.

    Measured on the rendered projection rather than the JSON record, because the
    rendering is what occupies a consumer's context; counting the record's braces
    and field names would bound the wrong thing.

    Args:
        digest: The digest to measure.

    Returns:
        Estimated tokens under the same tokenizer the budget path uses.
    """
    from personal_agent.llm_client.token_counter import estimate_tokens  # noqa: PLC0415

    return estimate_tokens(render_digest(digest))
