"""Stage 6+7: Context Assembly and Budget.

Assembles the final message list for the LLM from:
- Session history
- Seshat memory (via MemoryProtocol adapter)
- User message

In Slice 1, skill loading and budget trimming are deferred.
The budget stage is a pass-through that counts tokens.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from personal_agent.captains_log.turn_evidence import (
    CandidatePopulation,
    CandidateSource,
    DropReason,
    MemoryItemKind,
    RecallCandidateRecord,
    build_discarded_candidates,
    build_recall_candidates,
    mark_truncated,
    memory_item_identity,
)
from personal_agent.config import settings
from personal_agent.llm_client.message_content import get_text_content
from personal_agent.memory.protocol import BroadRecallResult, MemoryProtocol, MemoryRecallQuery
from personal_agent.request_gateway.state_document import build_state_document
from personal_agent.request_gateway.types import (
    AssembledContext,
    IntentResult,
    RecallResult,
    TaskType,
)

logger = structlog.get_logger(__name__)

ProactiveDiscards = tuple[tuple[dict[str, Any], float | None, DropReason], ...]
"""Candidates a producing path's own gates removed: (payload, score, gate) per item.

Plain triples rather than ``ProactiveMemoryDiscard`` because
``captains_log.turn_evidence`` is deliberately free of ``personal_agent`` imports, so the
adaptation from the producer's model happens here (FRE-1060). The score is None where the
gate fired before one was computed.
"""


@dataclass(frozen=True)
class RecallDiscardReport:
    """What a producing path discarded, and whether that account is complete (FRE-1060).

    The two travel together because they are only meaningful together: naming discards is
    worthless if a reader cannot tell whether the naming was exhaustive, and claiming
    completeness is a lie if the path did not report its drops. An earlier revision
    stamped the completeness claim unconditionally at the assembler, which asserted it for
    the broad-recall and entity-match paths that truncate silently — the exact over-claim
    the claim was introduced to prevent (confirmed code-review finding).

    Attributes:
        discards: Every candidate a gate removed, or empty when the path reported none.
        population: OFFERED only when this path accounts for every candidate it
            discarded. Defaults to POST_SELECTION so a path that says nothing cannot
            accidentally claim completeness.
    """

    discards: ProactiveDiscards = ()
    population: CandidatePopulation = CandidatePopulation.POST_SELECTION


def _session_topic_hint(session_messages: Sequence[dict[str, Any]]) -> str | None:
    """Build a short topic proxy from recent user turns (ADR-0039 MVP)."""
    parts: list[str] = []
    for m in session_messages:
        if m.get("role") == "user" and m.get("content"):
            text = get_text_content(m["content"])
            if text:
                parts.append(text)
    if not parts:
        return None
    return " ".join(parts[-3:])[:800]


def _freshness_score_modifier(last_accessed_at: datetime | None) -> float:
    """Compute a freshness multiplier for relevance scoring (D4, ADR-0047).

    Applies a +10 % boost for recently accessed entities (within 7 days) and
    a -15 % penalty for stale entities (90+ days since last access).  No
    adjustment is applied when ``last_accessed_at`` is not available.

    Args:
        last_accessed_at: UTC datetime of last access, or None if unknown.

    Returns:
        Multiplier in the range [0.85, 1.10].  Returns 1.0 when no data.
    """
    if last_accessed_at is None:
        return 1.0

    now = datetime.now(timezone.utc)
    # Ensure the stored datetime is timezone-aware before computing delta
    if last_accessed_at.tzinfo is None:
        last_accessed_at = last_accessed_at.replace(tzinfo=timezone.utc)

    days_since_access = (now - last_accessed_at).days
    if days_since_access <= 7:
        return 1.10  # +10 % for recently accessed
    if days_since_access >= 90:
        return 0.85  # -15 % for stale
    return 1.0


def _format_broad_recall_context(
    broad: BroadRecallResult,
) -> list[dict[str, Any]]:
    """Format broad recall result as memory context for the LLM.

    D4 (ADR-0047): when ``last_accessed_at`` is present on an entity dict,
    a ``freshness_modifier`` field is included so downstream consumers can
    apply the score adjustment.  The modifier itself does not reorder the
    returned list — that is left to the caller's ranking step.

    Args:
        broad: The broad recall result from Seshat.

    Returns:
        List of formatted memory context items.
    """
    context: list[dict[str, Any]] = []

    for entity_type, entities in broad.entities_by_type.items():
        for entity in entities:
            # D4: derive freshness modifier from ADR-0042 access-tracking field
            raw_ts = entity.get("last_accessed_at")
            last_accessed_at: datetime | None = None
            if isinstance(raw_ts, datetime):
                last_accessed_at = raw_ts
            elif isinstance(raw_ts, str):
                try:
                    last_accessed_at = datetime.fromisoformat(raw_ts)
                except ValueError:
                    last_accessed_at = None

            freshness_mod = _freshness_score_modifier(last_accessed_at)

            context.append(
                {
                    "type": "entity",
                    "entity_type": entity_type,
                    "name": entity.get("name", "unknown"),
                    "description": entity.get("description"),
                    "mention_count": entity.get("mention_count", 0),
                    # D4: freshness modifier for downstream relevance scoring
                    "freshness_modifier": freshness_mod,
                }
            )

    for session in broad.recent_sessions:
        context.append(
            {
                "type": "session",
                "session_id": session.get("session_id"),
                "summary": session.get("session_summary"),
                "dominant_entities": session.get("dominant_entities", []),
            }
        )

    return context


def _session_fact_candidates(
    recall_context: RecallResult | None,
) -> tuple[RecallCandidateRecord, ...]:
    """Build candidate records for the recall controller's session facts (FRE-1004).

    These bypass ``memory_context`` by design — they are injected as a system message
    (see :func:`assemble_context`) — so they would be invisible to the evidence contract
    unless captured separately. Facts the controller found but did not inject are still
    recorded, and resolve to a drop at the admission point rather than being omitted.

    Args:
        recall_context: Recall controller result from Stage 4b, or None.

    Returns:
        One record per candidate fact, identified by its source turn.
    """
    if recall_context is None or not recall_context.candidates:
        return ()
    return tuple(
        RecallCandidateRecord(
            kind=MemoryItemKind.SESSION_FACT,
            identity=f"turn:{c.source_turn}",
            score=c.confidence,
            source=CandidateSource.SESSION_FACT_SECTION,
        )
        for c in recall_context.candidates
    )


async def _query_memory_for_intent(
    intent: IntentResult,
    user_message: str,
    memory_adapter: MemoryProtocol,
    trace_id: str,
    session_id: str,
    session_messages: Sequence[dict[str, Any]],
    user_id: UUID | None = None,
    authenticated: bool = False,
) -> tuple[list[dict[str, Any]] | None, dict[str, float], RecallDiscardReport]:
    """Query memory based on intent type.

    Args:
        intent: Classified intent result.
        user_message: The user's message.
        memory_adapter: Seshat protocol adapter.
        trace_id: Request trace identifier.
        session_id: Current session id for proactive retrieval.
        session_messages: Session history for topic proxy extraction.
        user_id: Authenticated user UUID for visibility scoping (FRE-229).
        authenticated: Whether the request carries a verified identity (FRE-229).

    Returns:
        Tuple of (memory context list or None, relevance scores keyed by item
        identity, discard report). The proactive and entity-match paths both supply
        real scores; the broad-recall path computes none and returns an empty mapping
        rather than a fabricated one (ADR-0125 D3 item 5, FRE-1004).

        The third element is a :class:`RecallDiscardReport` (FRE-1060). Its ``discards``
        are populated whenever the proactive path ran, **including when it emitted
        nothing** — see the fall-through below — so the evidence record names the drops
        rather than reporting an absence. Its ``population`` is OFFERED **only** on the
        proactive-success path: every other path here truncates without reporting what it
        cut (broad recall via ``limit``, entity match via ``entity_names[:5]`` and
        ``limit=5``, and ``recall`` again internally), so claiming completeness for them
        would make the flag assert exactly the survivors-as-population reading it exists
        to prevent.
    """
    # Bound before the try so the exception handler below can still return whatever the
    # proactive path had already discarded (FRE-1060, confirmed code-review finding): a
    # downstream failure must not silently convert twelve named drops into "recall offered
    # nothing", which is the absence-vs-drop confusion this ticket closes and would fire on
    # exactly the turns most likely to be investigated.
    discards: ProactiveDiscards = ()
    try:
        if not await memory_adapter.is_connected():
            logger.warning("memory_unavailable", trace_id=trace_id)
            return None, {}, RecallDiscardReport()

        if intent.task_type == TaskType.MEMORY_RECALL:
            broad = await memory_adapter.recall_broad(
                entity_types=None,
                recency_days=90,
                limit=20,
                trace_id=trace_id,
                user_id=user_id,
                authenticated=authenticated,
                query_text=user_message,
            )
            # POST_SELECTION: recall_broad bounds its own read with `limit` and reports
            # nothing about what that cut.
            return _format_broad_recall_context(broad), {}, RecallDiscardReport()

        # FRE-1041: both consumers below share one graph-anchored resolution. The
        # capitalisation heuristic this replaces could not see a lowercase subject, so
        # the entity path was never entered for most of what the owner actually
        # discusses; and it emitted sentence-initial stopwords ("What", "Only") as
        # entity names. Asking the graph which of its entities the message names fixes
        # both directions at once, and cannot invent a name the graph does not hold.
        entity_names = await memory_adapter.resolve_message_entities(
            user_message,
            trace_id=trace_id,
            user_id=user_id,
            authenticated=authenticated,
        )

        # FRE-1060: populated *before* the emptiness test below, so the discards survive
        # the fall-through. A turn where every proactive candidate was discarded is exactly
        # the turn whose record most needs to name them, and binding this inside the
        # `if suggestions.candidates` arm would lose them precisely there. (The name is
        # bound above the `try` so the exception handler can return it too.)
        if settings.proactive_memory_enabled:
            suggestions = await memory_adapter.suggest_relevant(
                user_message=user_message,
                session_entity_names=list(entity_names),
                session_topic_hint=_session_topic_hint(session_messages),
                current_session_id=session_id,
                trace_id=trace_id,
                user_id=user_id,
                authenticated=authenticated,
                # FRE-1062: the same resolved names, but as *literal mentions* — the
                # session_entity_names copy above is merged with DB session entities
                # into the overlap subscore, while this one drives the admission pin.
                mentioned_entity_names=entity_names,
            )
            discards = tuple(
                (d.payload, d.relevance_score, d.drop_reason) for d in suggestions.discarded
            )
            if suggestions.candidates:
                # FRE-1004: the payload is returned unchanged — the score rides a
                # sibling map rather than the item, so nothing the model sees or the
                # budget counts changes. Before this, relevance_score died here.
                scores = {
                    identity: c.relevance_score
                    for c in suggestions.candidates
                    if (identity := memory_item_identity(c.payload)[1])
                }
                # The only OFFERED claim in this function: the proactive path is the one
                # producer that accounts for every candidate its gates removed.
                return (
                    [c.payload for c in suggestions.candidates],
                    scores,
                    RecallDiscardReport(discards, CandidatePopulation.OFFERED),
                )

        # Entity-name matching for analysis and other task types (Slice 2). Reached
        # whenever proactive is disabled or returned no candidates, where the resolved
        # names are the sole gate on entity recall. The control flow here is unchanged —
        # only the discard report rides along, so a fully-discarded proactive result still
        # falls through to this path exactly as before. POST_SELECTION from here down:
        # `entity_names[:5]` and `limit=5` below both truncate without reporting what they
        # cut, and `recall` truncates again internally, so the record must not claim its
        # population is complete even though the proactive drops it carries are named.
        if not entity_names:
            return None, {}, RecallDiscardReport(discards)

        query = MemoryRecallQuery(
            entity_names=entity_names[:5],
            recency_days=30,
            limit=5,
            query_text=user_message,
            user_id=user_id,
            authenticated=authenticated,
        )
        result = await memory_adapter.recall(query, trace_id=trace_id)
        context: list[dict[str, Any]] = []
        for entity in result.entities:
            # D4: derive freshness modifier from ADR-0042 access-tracking field
            raw_ts = entity.get("last_accessed_at")
            ent_last_accessed: datetime | None = None
            if isinstance(raw_ts, datetime):
                ent_last_accessed = raw_ts
            elif isinstance(raw_ts, str):
                try:
                    ent_last_accessed = datetime.fromisoformat(raw_ts)
                except ValueError:
                    ent_last_accessed = None

            context.append(
                {
                    "type": "entity",
                    "name": entity.get("name", "unknown"),
                    "entity_type": entity.get("entity_type"),
                    "description": entity.get("description"),
                    "mention_count": entity.get("mention_count", 0),
                    # D4: freshness modifier for downstream relevance scoring
                    "freshness_modifier": _freshness_score_modifier(ent_last_accessed),
                }
            )
        for ep in result.episodes:
            context.append(
                {
                    "type": "episode",
                    # FRE-1004: the episode's durable identity. The adapter supplies it
                    # (protocol_adapter builds episodes with ``turn_id``) and this dict
                    # dropped it, so every episode on this path was anonymous — and this
                    # is the default path, since proactive_memory_enabled defaults False.
                    # Without it the evidence record cannot name which episode was used,
                    # and two episodes in one turn are indistinguishable.
                    "conversation_id": ep.get("turn_id"),
                    "user_message": ep.get("user_message"),
                    # ADR-0125 D5: the "worst instance" — a digest-less episode
                    # carries no assistant text on this shape at all (the episode
                    # payload proactive.py builds has no assistant_response field;
                    # restoring that is a separate, deeper gap than this marker fix).
                    # 800 chars clears the re-derived p99 user-message length (400,
                    # measured 2026-07-27 against agent-captains-captures-*, N=1864)
                    # with margin, so this is a safety cap, not the dominant case.
                    "summary": ep.get("summary") or mark_truncated(ep.get("user_message", ""), 800),
                    "key_entities": ep.get("key_entities", []),
                }
            )
        # FRE-1004: relevance_scores is keyed by turn_id (memory/service.py sorts
        # conversations by ``relevance_scores.get(c.turn_id)``), which is exactly the
        # episode identity above, so the scores land on the right items.
        return (
            (context if context else None),
            dict(result.relevance_scores),
            RecallDiscardReport(discards),
        )

    except Exception:
        logger.exception("memory_query_failed", trace_id=trace_id)
        # `discards` is carried, not dropped: the proactive gates may already have removed
        # candidates before whatever failed here, and discarding that account would record
        # "recall offered nothing" for a turn that retrieved and gated a full population.
        return None, {}, RecallDiscardReport(discards)


async def assemble_context(
    user_message: str,
    session_messages: Sequence[dict[str, Any]],
    intent: IntentResult,
    memory_adapter: MemoryProtocol | None,
    trace_id: str,
    session_id: str = "",
    recall_context: RecallResult | None = None,
    user_id: UUID | None = None,
    authenticated: bool = False,
) -> AssembledContext:
    """Assemble the full context for the primary agent.

    Combines session history, memory enrichment, and user message
    into a final message list. In Slice 1, skill loading and
    budget trimming are stubs.

    Args:
        user_message: The current user message.
        session_messages: Prior conversation history (OpenAI format).
        intent: Classified intent from Stage 4.
        memory_adapter: Seshat protocol adapter (None if unavailable).
        trace_id: Request trace identifier.
        session_id: Client session id for proactive memory session scoping.
        recall_context: Recall controller result from Stage 4b (None if not triggered).
        user_id: Authenticated user UUID for visibility scoping (FRE-229).
        authenticated: Whether the request carries a verified identity (FRE-229).

    Returns:
        AssembledContext with messages and metadata.
    """
    messages: list[dict[str, Any]] = []
    memory_context: list[dict[str, Any]] | None = None
    memory_scores: dict[str, float] = {}
    discard_report = RecallDiscardReport()

    # Include session history
    messages.extend(session_messages)

    # Prepend structured state document for multi-turn sessions (Phase 4.5).
    state_doc = build_state_document(session_messages, trace_id=trace_id)
    if state_doc:
        messages.insert(0, {"role": "system", "content": state_doc})

    # Query memory if adapter is available
    if memory_adapter is not None:
        memory_context, memory_scores, discard_report = await _query_memory_for_intent(
            intent=intent,
            user_message=user_message,
            memory_adapter=memory_adapter,
            trace_id=trace_id,
            session_id=session_id,
            session_messages=session_messages,
            user_id=user_id,
            authenticated=authenticated,
        )

    # Inject session fact candidates from recall controller (as system message
    # in the main message list, not memory_context, to avoid schema mismatch
    # and budget-trimming that silently drops memory_context items).
    session_facts_injected = bool(
        recall_context and recall_context.reclassified and recall_context.candidates
    )
    if recall_context and recall_context.reclassified and recall_context.candidates:
        recall_section = "## Session Fact Recall\n"
        recall_section += "The user appears to be referring to something discussed earlier.\n"
        recall_section += "Relevant facts from the conversation:\n"
        for c in recall_context.candidates:
            recall_section += f'- Turn {c.source_turn}: "{c.fact}" (matched: "{c.noun_phrase}")\n'
        recall_section += "\nUse these facts to answer accurately. Do not claim you don't know."
        messages.append({"role": "system", "content": recall_section})

    # Add the current user message
    messages.append({"role": "user", "content": user_message})

    # Slice 1: simple token estimation (word count * 1.3)
    total_text = " ".join(get_text_content(m.get("content", "")) for m in messages)
    estimated_tokens = int(len(total_text.split()) * 1.3)

    logger.debug(
        "context_assembled",
        message_count=len(messages),
        has_memory=memory_context is not None,
        estimated_tokens=estimated_tokens,
        task_type=intent.task_type.value,
        trace_id=trace_id,
    )

    # ADR-0125 D3 item 5 (FRE-1004): record what recall offered *before* Stage 7 can
    # drop it. Session-fact candidates are carried here too — they ride a system
    # message rather than memory_context, so without them the record would omit a
    # live, model-visible recalled-fact producer.
    # FRE-1060: the candidates recall's own gates discarded ride here too. Without them
    # the record named only the survivors and reported them as the population — on the
    # melon turn, five of twelve. Three groups in construction order — offered, discarded,
    # session facts — each internally rank-ordered; every item carries its score, so global
    # rank stays recoverable by sorting. The completeness claim comes from the producing
    # path, never asserted here: only that path knows whether it reported its own drops.
    recall_candidates = (
        *build_recall_candidates(memory_context, memory_scores),
        *build_discarded_candidates(discard_report.discards),
        *_session_fact_candidates(recall_context),
    )

    return AssembledContext(
        messages=messages,
        memory_context=memory_context,
        tool_definitions=None,  # Populated by executor's existing tool logic
        token_count=estimated_tokens,
        trimmed=False,  # Slice 1: no budget trimming
        recall_candidates=recall_candidates,
        session_facts_injected=session_facts_injected,
        candidate_population=discard_report.population,
    )
