"""The digest's **wire** contract — what the model is asked to author (FRE-996).

Separate from :mod:`personal_agent.memory.session_digest`, which is the **storage**
record, and the separation is required rather than stylistic:
:class:`~personal_agent.memory.session_digest.UnresolvedItem` carries ``as_of``, a field
ADR-0124 D3 reserves to the producer ("compute state, generate meaning"). Handing the
storage model's schema to a provider would ask the model to author it. So the contract is
declared here, containing **only model-authored fields**, and :func:`to_storage` applies
everything the producer owns after parsing.

The wire model is also *tighter* than the prose prompt it replaces, because a schema can
carry what English can only request:

* ``field`` is closed to ``assistant_text``. The prose prompt asks for exactly that; the
  hand parser accepts any string and lets the span check fail later.
* A correction's ``span``/``locator``/``evidence_*`` are **required**. Same rule the hand
  parser enforces by raising — moved to the decoder, where it costs nothing.

**What this does and does not enforce.** The payload travels in a tool-call argument
field, so fence wrapping and trailing prose have nowhere to occur — that class is
eliminated structurally. Shape and enum conformance are *guided*, not guaranteed:
Anthropic's strict tool use is not reachable through litellm (it sets no ``strict`` on
its synthetic tool and drops the key on the explicit-tool path), so those remain measured
failure classes rather than assumed-impossible ones.

**Not fixed by upgrading.** Checked against litellm 1.93.0, the current release and 19
ahead of the pinned 1.89.2: the ``output_format`` model allowlist is byte-identical and
still omits ``sonnet-5``, the ``stop_reason`` overwrite is unchanged, and ``strict``
appears nowhere in the Anthropic path. The allowlist is hand-maintained per model
(BerriAI/litellm#20533 is the same gap for Opus 4.5/4.6), so it recurs with each release
rather than converging — "wait for an upgrade" is not a remedy here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_agent.memory.session_digest import (
    MAX_LABEL_CHARS,
    BasisTag,
    Correction,
    CorrectionTier,
    DigestItem,
    Locator,
    SessionDigest,
    UnresolvedItem,
)

#: Named from the model's perspective — it is the action the model is taking, not the
#: internal function it maps to.
DIGEST_TOOL_NAME = "emit_session_digest"

_TOOL_DESCRIPTION = (
    "Emit the session digest: a short distinguishing label plus the epistemic state the "
    "session left behind. Every field you emit must follow the rules in the system prompt."
)

#: Per-slot item ceilings for the bounded variant. A model can obey a count; it cannot
#: perceive a token budget. Used only by the FRE-996 pilot's third arm — the production
#: contract is unbounded, because conformance and brevity are different properties and
#: sizing belongs to FRE-993/FRE-994.
_BOUNDED_MAX_ITEMS: dict[str, int] = {
    "established": 5,
    "decisions": 5,
    "unresolved": 5,
    "corrections": 2,
}


def _require_text(value: str) -> str:
    """Reject blank item text.

    Applied as a validator rather than a ``Field(min_length=...)`` deliberately: the
    schema dialect has no ``minLength``, so expressing it as a constraint would emit a
    keyword the provider cannot honour and would imply an enforcement we do not have.
    """
    if not value.strip():
        raise ValueError("item has no text")
    return value.strip()


#: The locator grammar, closed to the assistant's own text (Amendment B). Closing it *in
#: the contract* rather than in a post-hoc check is one of the drifts this pilot removes:
#: today ``_parse_locator`` accepts any string and the span check fails later, by which
#: point the whole digest is discarded.
LocatorField = Literal["assistant_text"]


class WireLocator(BaseModel):
    """Where a verbatim span was taken from, as the model reports it.

    Attributes:
        capture_id: The capture's ``trace_id`` — one capture is one turn.
        field: Where inside that capture. Closed to ``assistant_text``, so an
            off-vocabulary value is rejected at parse time rather than surviving to
            fail the span check and discard the whole digest.
    """

    model_config = ConfigDict(frozen=True)

    capture_id: str
    field: LocatorField


class WireItem(BaseModel):
    """One item in a digest slot, as the model authors it.

    ``span``/``locator`` are deliberately absent: Amendment B retired ``tool_evidence``,
    the only basis that ever obliged a citation outside ``corrections``, and the prose
    prompt already asks only for ``text`` and ``basis``.

    Attributes:
        text: The item itself — what is established, decided or open.
        basis: Provenance tag.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    basis: BasisTag

    _strip_text = field_validator("text")(_require_text)


class WireCorrection(BaseModel):
    """A self-correction, with the provenance that makes it checkable.

    Unlike :class:`WireItem`, every provenance field here is required — a correction
    without a resolvable citation is exactly the cheap failure mode ADR-0124's located-span
    contract exists to make impossible.

    Attributes:
        text: The self-correction.
        basis: Provenance tag.
        tier: ``self_correction`` — the only kind Amendment B allows.
        span: Verbatim text of the claim, from the assistant's own message.
        locator: Where that claim lives.
        evidence_span: Verbatim supporting evidence, also from the assistant's own message.
        evidence_locator: Where that evidence lives.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    basis: BasisTag
    tier: CorrectionTier
    span: str
    locator: WireLocator
    evidence_span: str
    evidence_locator: WireLocator

    _strip_text = field_validator("text")(_require_text)


class WireDigest(BaseModel):
    """The four slots, as the model authors them.

    Attributes:
        established: Facts and observations that survived the interaction.
        decisions: Conclusions that materially constrain future reasoning.
        unresolved: Unfinished state a future reader could wrongly treat as settled.
        corrections: Self-corrections. Usually empty, and that scarcity is correct.
    """

    model_config = ConfigDict(frozen=True)

    established: list[WireItem] = Field(default_factory=list)
    decisions: list[WireItem] = Field(default_factory=list)
    unresolved: list[WireItem] = Field(default_factory=list)
    corrections: list[WireCorrection] = Field(default_factory=list)


class DigestEnvelope(BaseModel):
    """The full reply: a label and a digest.

    The 90-character label bound is **not** declared here. The schema dialect has no
    ``maxLength``, so it stays a Python check in :func:`to_storage` — see FRE-995's audit
    §8.2. Declaring it would advertise an enforcement that does not exist.

    Attributes:
        label: A short distinguishing noun phrase.
        digest: The structured record.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    digest: WireDigest


def to_storage(envelope: DigestEnvelope, *, ended_at: datetime) -> tuple[str, SessionDigest]:
    """Apply the producer-owned half and return the storage record.

    ``as_of`` is stamped here rather than asked of the model (ADR-0124 D3) — it is
    computable state, so it cannot be hallucinated.

    Args:
        envelope: The model's parsed reply.
        ended_at: The session's last-turn timestamp, stamped onto unresolved items.

    Returns:
        The label and the storage-shaped digest.

    Raises:
        ValueError: If the label is blank, or exceeds :data:`MAX_LABEL_CHARS`.
    """
    label = envelope.label.strip()
    if not label:
        raise ValueError("output has no label")
    if len(label) > MAX_LABEL_CHARS:
        raise ValueError(f"label is {len(label)} chars, limit is {MAX_LABEL_CHARS}")

    def _locator(wire: WireLocator) -> Locator:
        return Locator(capture_id=wire.capture_id, field=wire.field)

    def _item(wire: WireItem) -> DigestItem:
        return DigestItem(text=wire.text, basis=wire.basis)

    digest = SessionDigest(
        established=[_item(i) for i in envelope.digest.established],
        decisions=[_item(i) for i in envelope.digest.decisions],
        unresolved=[
            UnresolvedItem(text=i.text, basis=i.basis, as_of=ended_at)
            for i in envelope.digest.unresolved
        ],
        corrections=[
            Correction(
                text=c.text,
                basis=c.basis,
                span=c.span,
                locator=_locator(c.locator),
                tier=c.tier,
                evidence_span=c.evidence_span,
                evidence_locator=_locator(c.evidence_locator),
            )
            for c in envelope.digest.corrections
        ],
    )
    return label, digest


def _normalise_schema(node: object) -> None:
    """Make Pydantic's JSON Schema safe to hand to a provider, in place.

    Two rewrites, each with a reason:

    * ``const`` → ``enum`` with one member. Pydantic v2 emits ``const`` for a
      single-value ``Literal``; ``enum`` is the form providers uniformly honour, and the
      whole point of closing ``tier`` and ``field`` is that the decoder respects them.
    * ``additionalProperties: false`` on every object, which structured-output schemas
      require and which stops a model appending a field nobody declared.
    """
    if isinstance(node, dict):
        if "const" in node and "enum" not in node:
            node["enum"] = [node.pop("const")]
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for value in node.values():
            _normalise_schema(value)
    elif isinstance(node, list):
        for item in node:
            _normalise_schema(item)


def digest_schema(*, bounded: bool = False) -> dict[str, Any]:
    """Build the JSON Schema the model is held to.

    Args:
        bounded: Additionally cap each slot's item count. Pilot-only (FRE-996 arm C) —
            whether a count bound also brings *length* under control is a separate
            finding that FRE-994 inherits, not a claim this contract makes.

    Returns:
        The schema, normalised for provider consumption.
    """
    schema = DigestEnvelope.model_json_schema()
    _normalise_schema(schema)
    if bounded:
        slots = schema["$defs"]["WireDigest"]["properties"]
        for name, limit in _BOUNDED_MAX_ITEMS.items():
            slots[name]["maxItems"] = limit
    return schema


def digest_tool(*, bounded: bool = False) -> dict[str, Any]:
    """Build the OpenAI-format tool definition carrying the contract.

    A tool rather than ``response_format`` deliberately (FRE-996 §1.2): for the deployed
    ``claude-sonnet-5``, litellm turns ``response_format`` into a *synthetic* forced tool
    and then overwrites the provider's ``stop_reason`` with ``"stop"`` — which would make
    a truncated reply indistinguishable from a clean one. The explicit tool path leaves
    the stop reason intact, and truncation must stay visible.

    Args:
        bounded: See :func:`digest_schema`.

    Returns:
        The tool definition, ready to pass as ``tools=[...]``.
    """
    return {
        "type": "function",
        "function": {
            "name": DIGEST_TOOL_NAME,
            "description": _TOOL_DESCRIPTION,
            "parameters": digest_schema(bounded=bounded),
        },
    }


def digest_tool_choice() -> dict[str, Any]:
    """Force selection of the digest tool, so the model cannot answer in prose."""
    return {"type": "function", "function": {"name": DIGEST_TOOL_NAME}}
