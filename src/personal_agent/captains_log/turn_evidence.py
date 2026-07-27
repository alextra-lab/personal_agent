"""Turn evidence contract primitives (ADR-0125 D3 items 5 and 6, plus D4).

The capture layer records a boolean and a count for memory recall
(``captains_log/capture.py``) and per-turn entity identities only in
``telemetry/compaction.py``, whose purpose is naming what a compaction *dropped*.
The system durably records which facts it discarded and not which facts it relied
on; this module supplies the missing half.

**The admission point**, defined once here and shared by both records, is the first
primary model call of a turn taken at its *provider-neutral wire form*::

    sanitise_messages([{"role": "system", "content": system_prompt}] + request_messages)[0]

which is the pair both LLM clients construct before dispatch
(``llm_client/client.py`` and ``llm_client/litellm_client.py`` perform the identical
prepend-then-sanitise pre-flight). Provider decoration downstream of that — the
Anthropic ``cache_control`` copy — is additive metadata and never removes content,
so the record does not vary by provider.

Admission is resolved **structurally**: identities are threaded from the renderer, the
inliner reports an explicit :class:`InlineOutcome`, and the wire form is checked for the
turn-context fence the executor itself writes. Nothing in the decision path searches
rendered prompt text for identifiers — ADR-0125 AC-3 rules that out, because rendered
content need not contain identifiers at all and identical text recurs across turns.

This module is deliberately free of ``personal_agent`` imports so the request gateway
can build candidate records without an import cycle.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

TURN_CONTEXT_FENCE = "<turn_context>"
"""Opening fence the executor wraps per-turn volatile content in (ADR-0081 §D2).

Mirrors ``orchestrator/executor._TURN_CONTEXT_OPEN``. Duplicated rather than imported
to keep this module import-free; ``tests/personal_agent/captains_log/test_turn_evidence.py``
pins the two together.
"""

EVIDENCE_RECORD_KEYS: tuple[str, ...] = (
    "user_message",
    "assistant_response",
    "reasoning_trace",
    "tool_calls",
    "recalled_memory",
    "assembled_context",
    "identifiers",
    "model_and_params",
)
"""The eight records ADR-0125 D3 requires every turn to carry, in table order."""


class EvidenceState(StrEnum):
    """Presence of one D3 evidence record on one turn.

    ``EMPTY`` and ``NOT_RECORDED`` are the distinction the contract exists to make:
    *"this turn called no tools"* must never be indistinguishable from *"this turn's
    tool calls were not recorded"*.
    """

    PRESENT = "present"
    EMPTY = "empty"
    NOT_RECORDED = "not_recorded"


class MemoryItemKind(StrEnum):
    """Kind of recalled item, which determines where its identity is read from."""

    ENTITY = "entity"
    EPISODE = "episode"
    SESSION = "session"
    SESSION_FACT = "session_fact"
    UNKNOWN = "unknown"


class CandidateSource(StrEnum):
    """Which producer offered a candidate, which determines how admission resolves.

    ``MEMORY_CONTEXT`` items ride the volatile block inlined into the user message and
    are subject to budget trimming, renderer caps, and the inliner. ``SESSION_FACT_SECTION``
    items ride a system message that budget trimming preserves by construction
    (``request_gateway/budget.py`` phase 1 keeps every system message, and phases 2 and 3
    drop ``memory_context`` and ``tool_definitions`` rather than messages).
    """

    MEMORY_CONTEXT = "memory_context"
    SESSION_FACT_SECTION = "session_fact_section"


class DropReason(StrEnum):
    """Why a recalled candidate did not reach the final serialized model input."""

    BUDGET_TRIMMED = "budget_trimmed"
    NOT_RENDERED = "not_rendered"
    ABSENT_FROM_FINAL_INPUT = "absent_from_final_input"


class InlineOutcome(StrEnum):
    """What the volatile-block inliner did with the rendered block."""

    INLINED = "inlined"
    EMPTY_BLOCK = "empty_block"
    ALREADY_WRAPPED = "already_wrapped"
    NO_TARGET = "no_target"


def _text(value: object) -> str:
    """Return a stripped string for a scalar identity field, or ``""``."""
    if value is None:
        return ""
    return str(value).strip()


def memory_item_identity(item: object) -> tuple[MemoryItemKind, str]:
    """Return the kind and durable identity of one memory-context item.

    This is the single definition of identity for the evidence contract; every producer
    and every consumer resolves through it, so two capture surfaces cannot disagree about
    what an item is called. Entity identity is the *name* because the name is the property
    the knowledge graph merges entity nodes on, and is also what ``entity_id`` is set to
    (see ``memory/service.py``).

    Args:
        item: A memory-context item, normally a mapping. Non-mappings are tolerated.

    Returns:
        Tuple of (kind, identity). An unrecognised shape returns
        ``(MemoryItemKind.UNKNOWN, "")`` — the identity is never guessed, and an item
        whose identity is missing is still recorded rather than silently dropped.
    """
    if not isinstance(item, Mapping):
        return (MemoryItemKind.UNKNOWN, "")

    declared = _text(item.get("type")).lower()
    if declared == "entity":
        return (MemoryItemKind.ENTITY, _text(item.get("name")))
    if declared == "episode":
        return (
            MemoryItemKind.EPISODE,
            _text(item.get("conversation_id")) or _text(item.get("turn_id")),
        )
    if declared == "session":
        return (MemoryItemKind.SESSION, _text(item.get("session_id")))
    if declared == "session_fact":
        return (MemoryItemKind.SESSION_FACT, _text(item.get("source_turn")))

    # Undeclared shapes. The executor's entity-name-match recall path emits
    # ``conversation_id`` with no ``type`` key at all.
    for key, kind in (
        ("conversation_id", MemoryItemKind.EPISODE),
        ("turn_id", MemoryItemKind.EPISODE),
        ("session_id", MemoryItemKind.SESSION),
        ("name", MemoryItemKind.ENTITY),
    ):
        identity = _text(item.get(key))
        if identity:
            return (kind, identity)
    return (MemoryItemKind.UNKNOWN, "")


class RecallCandidateRecord(BaseModel):
    """One item recall offered for this turn, before admission is resolved.

    Attributes:
        kind: What the item is, per :func:`memory_item_identity`.
        identity: Durable identifier, empty when the producer supplied none.
        score: Relevance score where the producing path computes one, else None.
        source: Which producer offered it — decides how admission resolves.
    """

    model_config = ConfigDict(frozen=True)

    kind: MemoryItemKind
    identity: str
    score: float | None = None
    source: CandidateSource = CandidateSource.MEMORY_CONTEXT


class RecalledMemoryRecord(BaseModel):
    """One recalled item with its admission resolved against the final model input.

    Attributes:
        kind: What the item is.
        identity: Durable identifier, joinable to the claim record.
        score: Relevance score where one was computed.
        admitted: Whether the item reached the final serialized model input.
        drop_reason: Why it did not, when ``admitted`` is False; None otherwise.
    """

    model_config = ConfigDict(frozen=True)

    kind: MemoryItemKind
    identity: str
    score: float | None
    admitted: bool
    drop_reason: DropReason | None = None


class RecallAdmissionRecord(BaseModel):
    """D3 item 5 — what the turn actually relied on, and what it dropped.

    Attributes:
        state: PRESENT when candidates existed, EMPTY when recall ran and found none.
        candidate_count: Items recall offered.
        admitted_count: Items that reached the final serialized model input.
        items: Every candidate, admitted or dropped. Never filtered.
    """

    model_config = ConfigDict(frozen=True)

    state: EvidenceState
    candidate_count: int
    admitted_count: int
    items: list[RecalledMemoryRecord] = Field(default_factory=list)


class ContextMessageRecord(BaseModel):
    """One message of the final serialized model input, named rather than counted.

    Attributes:
        index: Position in the wire-form message list.
        role: OpenAI role.
        origin_trace_id: Trace of the turn that produced this message, when the message
            carries one. None for content synthesised inside the current turn — that
            content is recorded by the other evidence records rather than named here.
        timestamp: ISO timestamp the message was persisted with, when present.
        chars: Character length of the message's text content.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    role: str
    origin_trace_id: str | None = None
    timestamp: str | None = None
    chars: int = 0


class AssembledContextRecord(BaseModel):
    """D3 item 6 — what the assembled context contained, at item-identity granularity.

    Deliberately *not* an extension of ``PROMPT_COMPONENT_TAXONOMY``: the finding this
    record answers is that category-level presence and absence is the wrong granularity,
    so a longer checklist of section names would not satisfy it. This names which
    conversation turns were in the window, which skill bodies were loaded, and which
    memory items were admitted.

    Attributes:
        state: PRESENT whenever the record was built.
        message_count: Messages in the final serialized model input.
        system_prompt_chars: Character length of the system prompt sent.
        conversation_slice: The window, message by message.
        skill_bodies: Names of the skill bodies loaded into the prompt.
        memory_identities: Identities of the memory items admitted.
        primary_call_index: Which primary call of the turn this describes — always the
            first, where context assembly's output is serialized.
        primary_call_count: How many primary calls the turn ultimately made. Stamped at
            capture time, since it is not knowable at the admission point. A reader needs
            it to interpret the record: it says plainly that this describes call 0 of N.
    """

    model_config = ConfigDict(frozen=True)

    state: EvidenceState
    message_count: int
    system_prompt_chars: int
    conversation_slice: list[ContextMessageRecord] = Field(default_factory=list)
    skill_bodies: list[str] = Field(default_factory=list)
    memory_identities: list[str] = Field(default_factory=list)
    primary_call_index: int = 0
    primary_call_count: int = 1


class TurnEvidence(BaseModel):
    """Both D3 records for one turn, describing one named model call.

    Both halves are resolved against the same wire form so the record can never describe
    two different model calls.

    Attributes:
        recall: D3 item 5.
        assembled_context: D3 item 6, which also names the call both halves describe.
    """

    model_config = ConfigDict(frozen=True)

    recall: RecallAdmissionRecord
    assembled_context: AssembledContextRecord

    @property
    def primary_call_index(self) -> int:
        """Which primary call of the turn both records describe."""
        return self.assembled_context.primary_call_index

    @property
    def primary_call_count(self) -> int:
        """How many primary calls the turn made, when known."""
        return self.assembled_context.primary_call_count


def _content_chars(content: object) -> int:
    """Return the character length of a message content value.

    Handles the multimodal block list form without importing the LLM client, so this
    module stays dependency-free.

    Args:
        content: A message's ``content`` value — string, block list, or other.

    Returns:
        Character count of the text content, 0 when there is none.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, Sequence):
        total = 0
        for block in content:
            if isinstance(block, Mapping):
                total += len(_text(block.get("text")))
            elif isinstance(block, str):
                total += len(block)
        return total
    return len(str(content))


def build_recall_candidates(
    memory_context: Sequence[object] | None,
    scores_by_identity: Mapping[str, float],
) -> tuple[RecallCandidateRecord, ...]:
    """Build candidate records for the memory items recall offered this turn.

    Called at context assembly, before budget trimming, so the candidates survive the
    drop that ``apply_budget`` performs and the drop becomes recordable.

    Args:
        memory_context: Memory-context items as assembled, or None.
        scores_by_identity: Relevance scores keyed by identity, for producing paths that
            compute one. Missing keys yield a None score rather than a fabricated value.

    Returns:
        One record per item, in assembly order. Empty when there is no memory context.
    """
    if not memory_context:
        return ()
    records: list[RecallCandidateRecord] = []
    for item in memory_context:
        kind, identity = memory_item_identity(item)
        records.append(
            RecallCandidateRecord(
                kind=kind,
                identity=identity,
                score=scores_by_identity.get(identity),
                source=CandidateSource.MEMORY_CONTEXT,
            )
        )
    return tuple(records)


def _wire_carries_volatile_fence(wire_messages: Sequence[object], user_message: str) -> bool:
    """Whether *this turn's* volatile fence survived into the final serialized input.

    Anchored on this turn's own user message, deliberately. The fence marker alone is
    not enough: prior turns' user messages carry fences of their own, persisted in
    session history, so a bare marker scan would let a previous turn's fence stand in
    for this one. A turn whose block was truncated away by the client's sanitiser
    (which can drop back to an earlier user turn when the persisted history holds an
    orphaned tool call) would then be recorded as having admitted memory it never sent.
    Over-claiming admission is the one failure this record must not have.

    Anchoring on the user's own text is a provenance check on a whole message, not the
    identifier-hunt through rendered content that ADR-0125 AC-3 rules out — the
    identities themselves still come from the renderer, never from this text.

    Args:
        wire_messages: The final serialized message list.
        user_message: This turn's user message text.

    Returns:
        True when this turn's user message is present in the wire form and carries the
        fence. False when the user message cannot be located at all.
    """
    for message in reversed(wire_messages):
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if user_message and user_message not in content:
            continue
        return TURN_CONTEXT_FENCE in content
    return False


def _resolve_admission(
    candidate: RecallCandidateRecord,
    *,
    memory_context_present: bool,
    rendered_budget: Counter[str],
    block_reached_input: bool,
    session_facts_injected: bool,
) -> RecalledMemoryRecord:
    """Resolve one candidate's disposition against the final serialized model input.

    ``rendered_budget`` is a **multiset that this function consumes**, not a set. Two
    candidates can share an identity — most importantly the empty identity a producer
    that supplied none yields — and a plain membership test would then admit every one
    of them as soon as any single one rendered. Consuming a count instead admits exactly
    as many as the renderer emitted, and because the renderer takes an order-preserving
    prefix (its rank caps) the ones it consumes are the right ones.

    Args:
        candidate: The candidate to resolve.
        memory_context_present: Whether memory context survived budget trimming.
        rendered_budget: Remaining rendered identities, decremented on each match.
        block_reached_input: Whether the volatile block reached the wire form.
        session_facts_injected: Whether the session-fact section was written.

    Returns:
        The candidate with its admission and drop reason resolved.
    """
    admitted: bool
    reason: DropReason | None

    if candidate.source is CandidateSource.SESSION_FACT_SECTION:
        admitted = session_facts_injected
        reason = None if admitted else DropReason.NOT_RENDERED
    elif not memory_context_present:
        admitted, reason = False, DropReason.BUDGET_TRIMMED
    elif rendered_budget[candidate.identity] <= 0:
        admitted, reason = False, DropReason.NOT_RENDERED
    elif not block_reached_input:
        rendered_budget[candidate.identity] -= 1
        admitted, reason = False, DropReason.ABSENT_FROM_FINAL_INPUT
    else:
        rendered_budget[candidate.identity] -= 1
        admitted, reason = True, None

    return RecalledMemoryRecord(
        kind=candidate.kind,
        identity=candidate.identity,
        score=candidate.score,
        admitted=admitted,
        drop_reason=reason,
    )


def build_turn_evidence(
    *,
    candidates: Sequence[RecallCandidateRecord],
    memory_context_present: bool,
    rendered_identities: Sequence[str],
    inline_outcome: InlineOutcome,
    session_facts_injected: bool,
    wire_messages: Sequence[object],
    system_prompt: str,
    user_message: str = "",
    skill_bodies: Sequence[str],
    call_index: int,
    primary_call_count: int = 1,
) -> TurnEvidence:
    """Build both D3 records for one turn from its final serialized model input.

    Args:
        candidates: Everything recall offered, including items the budget dropped.
        memory_context_present: Whether memory context survived budget trimming.
        rendered_identities: Identities the memory renderer actually emitted.
        inline_outcome: What the volatile-block inliner did.
        session_facts_injected: Whether the session-fact section was written into the
            message list.
        wire_messages: The final serialized message list (system message included).
        system_prompt: The system prompt sent on this call.
        user_message: This turn's user message, used to anchor the volatile block to
            this turn rather than to a fence a previous turn left in the history.
        skill_bodies: Names of the skill bodies loaded.
        call_index: Which primary call of the turn this describes.
        primary_call_count: Primary calls the turn made, when known.

    Returns:
        A :class:`TurnEvidence` whose two halves describe the same model call.
    """
    rendered_budget: Counter[str] = Counter(rendered_identities)
    block_reached_input = inline_outcome is InlineOutcome.INLINED and _wire_carries_volatile_fence(
        wire_messages, user_message
    )

    items = [
        _resolve_admission(
            candidate,
            memory_context_present=memory_context_present,
            rendered_budget=rendered_budget,
            block_reached_input=block_reached_input,
            session_facts_injected=session_facts_injected,
        )
        for candidate in candidates
    ]
    admitted_identities = [item.identity for item in items if item.admitted]

    recall = RecallAdmissionRecord(
        state=EvidenceState.PRESENT if items else EvidenceState.EMPTY,
        candidate_count=len(items),
        admitted_count=len(admitted_identities),
        items=items,
    )

    slice_records = [
        ContextMessageRecord(
            index=index,
            role=_text(message.get("role")) if isinstance(message, Mapping) else "",
            origin_trace_id=(
                _text(message.get("trace_id")) or None if isinstance(message, Mapping) else None
            ),
            timestamp=(
                _text(message.get("timestamp")) or None if isinstance(message, Mapping) else None
            ),
            chars=_content_chars(message.get("content") if isinstance(message, Mapping) else None),
        )
        for index, message in enumerate(wire_messages)
    ]

    assembled = AssembledContextRecord(
        state=EvidenceState.PRESENT,
        message_count=len(slice_records),
        system_prompt_chars=len(system_prompt or ""),
        conversation_slice=slice_records,
        # Skill bodies ride the same volatile block as the memory section, so they are
        # gated on the same fact. Listing them when the block never landed would state
        # that the model was given skills it never saw — e.g. a vision turn, whose user
        # content is a block list rather than a string, which the inliner declines.
        skill_bodies=list(skill_bodies) if block_reached_input else [],
        memory_identities=admitted_identities,
        primary_call_index=call_index,
        primary_call_count=primary_call_count,
    )

    return TurnEvidence(recall=recall, assembled_context=assembled)


def derive_evidence_presence(
    *,
    user_message: str,
    assistant_response: str | None,
    tool_results: Sequence[object],
    llm_call_count: int,
    turn_evidence: TurnEvidence | None,
    trace_id: str,
    session_id: str,
    user_id: object | None,
) -> dict[str, EvidenceState]:
    """State every one of the eight D3 records explicitly for this turn.

    ADR-0125 D3: an implicitly missing field is indistinguishable from a capture gap,
    which is precisely the failure the contract exists to prevent. Every key in
    :data:`EVIDENCE_RECORD_KEYS` is always present in the result.

    Args:
        user_message: The turn's user message.
        assistant_response: The turn's assistant response, if it produced one.
        tool_results: Recorded tool results for the turn.
        llm_call_count: Number of recorded model calls.
        turn_evidence: The built evidence record, or None when it was never built.
        trace_id: Turn trace identifier.
        session_id: Session identifier.
        user_id: Resolved user identity.

    Returns:
        Mapping of record key to its state. ``reasoning_trace`` is always
        ``NOT_RECORDED``: the capture model carries no reasoning field, and recording
        that gap explicitly is what makes it visible rather than assumed absent.
    """
    if turn_evidence is None:
        recalled = EvidenceState.NOT_RECORDED
        assembled = EvidenceState.NOT_RECORDED
    else:
        recalled = turn_evidence.recall.state
        assembled = turn_evidence.assembled_context.state

    identifiers = (
        EvidenceState.PRESENT
        if (_text(trace_id) and _text(session_id) and user_id is not None)
        else EvidenceState.NOT_RECORDED
    )

    return {
        "user_message": EvidenceState.PRESENT if _text(user_message) else EvidenceState.EMPTY,
        "assistant_response": (
            EvidenceState.PRESENT if _text(assistant_response) else EvidenceState.EMPTY
        ),
        "reasoning_trace": EvidenceState.NOT_RECORDED,
        "tool_calls": EvidenceState.PRESENT if tool_results else EvidenceState.EMPTY,
        "recalled_memory": recalled,
        "assembled_context": assembled,
        "identifiers": identifiers,
        "model_and_params": (EvidenceState.PRESENT if llm_call_count > 0 else EvidenceState.EMPTY),
    }
