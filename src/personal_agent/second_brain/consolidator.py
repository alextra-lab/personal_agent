"""Second Brain Consolidator: Background memory consolidation (Phase 2.2).

This component processes recent task captures, extracts entities and relationships
using Claude 4.5 or local SLMs, and updates the Neo4j memory graph.
"""

import asyncio
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import structlog

from personal_agent.captains_log.capture import TaskCapture, read_captures
from personal_agent.config import resolve_role_model_key
from personal_agent.config.settings import get_settings
from personal_agent.cost_gate import BudgetDenied
from personal_agent.events import (
    STREAM_MEMORY_ACCESSED,
    STREAM_MEMORY_ENTITIES_UPDATED,
    AccessContext,
    MemoryAccessedEvent,
    MemoryEntitiesUpdatedEvent,
    get_event_bus,
)
from personal_agent.memory.models import Claim, Entity, Relationship, SessionNode, Stance, TurnNode
from personal_agent.memory.promote import run_promotion_pipeline
from personal_agent.memory.provenance import (
    SourceRecord,
    associate,
    attribution_for_relationship,
    sources_from_tool_results,
)
from personal_agent.memory.service import MemoryService
from personal_agent.memory.weight import KnowledgeWeight
from personal_agent.second_brain.attempts import (
    previous_attempt_count,
    record_consolidation_attempt,
)
from personal_agent.second_brain.entity_extraction import (
    default_extraction_summary,
    extract_entities_and_relationships,
)
from personal_agent.sysgraph import get_default_sysgraph_repo
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.spans import close_root_span, open_root_span
from personal_agent.telemetry.trace import SystemTraceContext
from personal_agent.tools import get_default_registry

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

    from personal_agent.tools.registry import ToolRegistry

log = get_logger(__name__)


def _matching_sources(attribution: str, sources: Sequence[SourceRecord]) -> list[SourceRecord]:
    """Return the sources whose retrieved content contains this item (ADR-0098 A4).

    Args:
        attribution: The item's attribution string — an entity's name, a claim's content,
            or a relationship's verbalization.
        sources: The external artifacts this turn retrieved.

    Returns:
        The matching records. Several matches are all kept: provenance is append-only, so
        an item contained in two fetched pages legitimately carries both.
    """
    matched = set(associate(attribution, sources))
    return [source for source in sources if source.source_id in matched]


def _new_consolidation_trace_id() -> str:
    """Mint a system-scoped trace id for a consolidation run (ADR-0074 §I3)."""
    return SystemTraceContext.new("consolidation").trace_id


def _parse_provenance_dt(provenance: dict[str, Any], key: str) -> datetime | None:
    """Parse an ISO-8601 provenance timestamp, returning None if absent/unparseable."""
    raw = provenance.get(key)
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _build_stance(data: dict[str, Any]) -> Stance | None:
    """Build a :class:`Stance` from an extractor stance dict (ADR-0098 D2/D5).

    Returns None (skip) when the target or the provenance ``observed_at`` — the
    bitemporal ordering axis — is missing, so a stance is never written without
    the timestamp its supersession depends on.

    Args:
        data: One stance object from the extractor's ``stances`` array.

    Returns:
        A Stance, or None when it cannot be safely constructed.
    """
    target = str(data.get("target", "")).strip()
    provenance = data.get("provenance") or {}
    observed_at = _parse_provenance_dt(provenance, "observed_at")
    if not target or observed_at is None:
        return None
    # FRE-1299: only ever "user"/"agent" on the production path (Python stamps it in
    # _finalize_extraction, mirroring _build_claim); anything else resolves to the
    # untrusted tier rather than silently reaching the entitlement gate as an unknown value.
    asserted_by = "user" if data.get("asserted_by") == "user" else "agent"
    return Stance(
        target=target,
        affect=str(data.get("affect", "") or ""),
        mastery=data.get("mastery"),
        trace_id=provenance.get("trace_id"),
        session_id=provenance.get("session_id"),
        source_type=str(provenance.get("source_type", "conversation")),
        asserted_by=asserted_by,
        observed_at=observed_at,
        extracted_at=_parse_provenance_dt(provenance, "extracted_at"),
    )


def _build_claim(data: dict[str, Any]) -> Claim | None:
    """Build a :class:`Claim` from an extractor claim dict (ADR-0098 D2/D5).

    Confidence is derived from the provenance source *channel* together with the
    Python-derived co-authorship (``asserted_by``, FRE-1020) via
    :meth:`KnowledgeWeight.from_claim_provenance` — the weight the correction path
    adjudicates on. Deriving it from the channel alone left it constant at 0.8 for every
    claim, which made ADR-0098 D2's weaker-claim guard ("not naive last-write-wins")
    unreachable — only the ``observed_at`` staleness check still discriminated.
    Returns None (skip) when content or ``observed_at`` is absent.

    Args:
        data: One claim object from the extractor's ``claims`` array.

    Returns:
        A Claim, or None when it cannot be safely constructed.
    """
    content = str(data.get("content", "")).strip()
    provenance = data.get("provenance") or {}
    observed_at = _parse_provenance_dt(provenance, "observed_at")
    if not content or observed_at is None:
        return None
    source_type = str(provenance.get("source_type", "conversation"))
    # Only ever "user"/"agent" on the production path (Python stamps it in
    # _finalize_extraction); anything else — a direct caller, a legacy payload — resolves to
    # the untrusted tier rather than silently reaching the adjudicator as an unknown value.
    asserted_by = "user" if data.get("asserted_by") == "user" else "agent"
    return Claim(
        content=content,
        knowledge_class=str(data.get("class", "Personal")),
        facet=str(data.get("facet", "") or ""),
        update_kind=str(data.get("update_kind", "new") or "new"),
        confidence=KnowledgeWeight.from_claim_provenance(source_type, asserted_by).confidence,
        trace_id=provenance.get("trace_id"),
        session_id=provenance.get("session_id"),
        source_type=source_type,
        asserted_by=asserted_by,
        observed_at=observed_at,
        extracted_at=_parse_provenance_dt(provenance, "extracted_at"),
    )


class SecondBrainConsolidator:
    """Background consolidator for building and maintaining memory graph.

    This component:
    1. Reads recent task captures
    2. Uses Claude 4.5 to extract entities and relationships
    3. Updates Neo4j memory graph
    4. Creates reflection entries for insights

    Usage:
        consolidator = SecondBrainConsolidator()
        await consolidator.consolidate_recent_captures(days=7)
    """

    def __init__(
        self,
        memory_service: MemoryService | None = None,
        tracer: "Tracer | None" = None,
        tool_registry: "ToolRegistry | None" = None,
    ) -> None:  # noqa: D107
        """Initialize consolidator with optional dependencies.

        Args:
            memory_service: Optional memory service (creates new if None).
            tracer: Tracer to open each consolidation run's root span with
                (ADR-0129 D3, FRE-1069). Defaults to the process-wide tracer;
                tests inject their own tracer bound to an in-memory exporter.
            tool_registry: Registry to read each tool's ``referent_parameter``
                declaration from (ADR-0098 Amendment A2). Defaults to the process
                registry; tests inject one holding a fixture tool, which is what makes
                a new referent-declaring tool resolvable end-to-end with no edit inside
                ``grounding/``.
        """
        self.memory_service = memory_service or MemoryService()
        self._tracer = tracer
        self._tool_registry = tool_registry or get_default_registry()

        # Ensure memory service is connected
        if not self.memory_service.connected:
            # Note: In service mode, memory service should already be connected
            # In CLI mode, this will create a temporary connection
            pass

    async def consolidate_recent_captures(
        self,
        days: int = 7,
        limit: int = 50,
        should_pause: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Consolidate recent task captures into memory graph.

        Args:
            days: Number of days to look back
            limit: Maximum number of captures to process
            should_pause: Optional callback indicating whether consolidation
                should temporarily pause before processing the next capture.

        Returns:
            Summary dict with processing results
        """
        span, token, cv_tokens = open_root_span("consolidation", tracer=self._tracer)
        try:
            run_trace_id = _new_consolidation_trace_id()
            log.info("consolidation_started", days=days, limit=limit, trace_id=run_trace_id)

            # Read recent captures
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=days)
            captures = read_captures(start_date=start_date, end_date=end_date, limit=limit)

            if not captures:
                log.info("no_captures_to_consolidate", days=days, trace_id=run_trace_id)
                return {
                    "captures_processed": 0,
                    "captures_skipped": 0,
                    "turns_created": 0,
                    "sessions_created": 0,
                    "entities_created": 0,
                    "relationships_created": 0,
                }

            entity_extraction_role = resolve_role_model_key("entity_extraction")

            log.info(
                "captures_found",
                count=len(captures),
                extraction_model=entity_extraction_role,
                trace_id=run_trace_id,
            )

            # Ensure memory service is connected
            if not self.memory_service.connected:
                await self.memory_service.connect()

            # Process each capture (skip ones already in the graph to avoid duplicate work)
            turns_created = 0
            entities_created = 0
            relationships_created = 0
            stances_created = 0
            claims_created = 0
            entities_dispatched_ephemeral = 0
            entities_dispatched_finding = 0
            entities_dispatch_finding_failed = 0
            relationships_dispatch_skipped = 0
            captures_skipped = 0
            sessions_with_new_turns: set[str] = set()
            all_entity_ids: list[str] = []
            all_relationship_element_ids: list[str] = []

            for i, capture in enumerate(captures, 1):
                if should_pause and should_pause():
                    log.info(
                        "consolidation_paused_request_active",
                        capture_num=i,
                        remaining=len(captures) - i + 1,
                        trace_id=run_trace_id,
                    )
                    while should_pause():
                        await asyncio.sleep(1.0)
                    log.info("consolidation_resumed", capture_num=i, trace_id=run_trace_id)
                # ADR-0107 D5: this loop processes captures from many users in one
                # background pass, so each capture's own identity must be (re)bound
                # per-iteration — bound_contextvars restores the prior value on exit
                # rather than a blanket clear, so it cannot leak capture N's user_id
                # into capture N+1's log lines.
                #
                # Bound as `capture_trace_id`, not `trace_id` (ADR-0129 D3 / FRE-1069):
                # the whole run now executes under one "consolidation" root span, and
                # `_add_span_context` (telemetry/logger.py) unconditionally overwrites
                # event_dict["trace_id"] from the active span on every log call — it
                # does not defer to an explicit kwarg or an already-bound contextvar.
                # Binding/passing `trace_id=capture.trace_id` here would therefore be
                # silently clobbered by the run-level span's trace_id on every log
                # line, destroying the per-capture correlation this binding exists to
                # provide. `capture_trace_id` is a field `_add_span_context` never
                # touches, so it survives.
                with structlog.contextvars.bound_contextvars(
                    capture_trace_id=capture.trace_id,
                    session_id=capture.session_id,
                    user_id=str(capture.user_id),
                ):
                    try:
                        if await self.memory_service.turn_exists(
                            capture.trace_id, trace_id=capture.trace_id
                        ):
                            captures_skipped += 1
                            log.debug(
                                "consolidation_skipped_already_consolidated",
                                capture_num=i,
                                capture_trace_id=capture.trace_id,
                                trace_id=capture.trace_id,
                            )
                            continue
                        log.debug(
                            "consolidation_processing_capture",
                            capture_num=i,
                            total=len(captures),
                            capture_trace_id=capture.trace_id,
                            trace_id=capture.trace_id,
                        )
                        result = await self._process_capture(
                            capture, extractor_model=entity_extraction_role
                        )
                        if result.get("turns_created"):
                            turns_created += result["turns_created"]
                            if capture.session_id:
                                sessions_with_new_turns.add(capture.session_id)
                        entities_created += result.get("entities_created", 0)
                        relationships_created += result.get("relationships_created", 0)
                        stances_created += result.get("stances_created", 0)
                        claims_created += result.get("claims_created", 0)
                        entities_dispatched_ephemeral += result.get(
                            "entities_dispatched_ephemeral", 0
                        )
                        entities_dispatched_finding += result.get("entities_dispatched_finding", 0)
                        entities_dispatch_finding_failed += result.get(
                            "entities_dispatch_finding_failed", 0
                        )
                        relationships_dispatch_skipped += result.get(
                            "relationships_dispatch_skipped", 0
                        )
                        all_entity_ids.extend(result.get("entity_ids", []))
                        all_relationship_element_ids.extend(
                            result.get("relationship_element_ids", [])
                        )
                        log.debug(
                            "consolidation_capture_done",
                            capture_num=i,
                            entities=result.get("entities_created", 0),
                            relationships=result.get("relationships_created", 0),
                            capture_trace_id=capture.trace_id,
                            trace_id=capture.trace_id,
                        )
                    except Exception as e:
                        log.error(
                            "capture_processing_failed",
                            capture_num=i,
                            capture_trace_id=capture.trace_id,
                            trace_id=capture.trace_id,
                            error=str(e),
                            error_type=type(e).__name__,
                            exc_info=True,
                        )

            # Build Session nodes for every session that received new turns this run
            sessions_created = await self._consolidate_sessions(
                captures, sessions_with_new_turns, trace_id=run_trace_id
            )

            # Promote qualifying entities from episodic → semantic memory
            entities_promoted = 0
            if turns_created > 0:
                candidates = await self.memory_service.get_promotion_candidates(
                    min_mentions=1, exclude_already_promoted=True
                )
                if candidates:
                    promotion_result = await run_promotion_pipeline(
                        service=self.memory_service,
                        candidates=candidates,
                        trace_id=run_trace_id,
                    )
                    entities_promoted = promotion_result.promoted_count

            summary = {
                "captures_processed": len(captures),
                "captures_skipped": captures_skipped,
                "turns_created": turns_created,
                "sessions_created": sessions_created,
                "entities_created": entities_created,
                "relationships_created": relationships_created,
                "stances_created": stances_created,
                "claims_created": claims_created,
                "entities_promoted": entities_promoted,
                "entities_dispatched_ephemeral": entities_dispatched_ephemeral,
                "entities_dispatched_finding": entities_dispatched_finding,
                "entities_dispatch_finding_failed": entities_dispatch_finding_failed,
                "relationships_dispatch_skipped": relationships_dispatch_skipped,
            }

            log.info(
                "consolidation_completed",
                **summary,
                extraction_model=entity_extraction_role,
                trace_id=run_trace_id,
            )

            _settings = get_settings()
            bus = get_event_bus()

            # Publish memory entities updated event (Phase 4)
            if all_entity_ids:
                entities_updated_event = MemoryEntitiesUpdatedEvent(
                    entity_ids=all_entity_ids,
                    consolidation_id=run_trace_id,
                    source_component="second_brain.consolidator",
                )
                try:
                    await bus.publish(STREAM_MEMORY_ENTITIES_UPDATED, entities_updated_event)
                except Exception as e:
                    log.warning(
                        "memory_entities_updated_event_publish_failed",
                        error=str(e),
                        event_id=entities_updated_event.event_id,
                        trace_id=run_trace_id,
                    )

            # Publish memory accessed event for consolidation traversal (Phase 4 / ADR-0042)
            rel_ids_deduped = list(dict.fromkeys(all_relationship_element_ids))
            if _settings.freshness_enabled and (all_entity_ids or rel_ids_deduped):
                accessed_event = MemoryAccessedEvent(
                    entity_ids=all_entity_ids,
                    relationship_ids=rel_ids_deduped,
                    access_context=AccessContext.CONSOLIDATION,
                    query_type="consolidation_traversal",
                    trace_id=run_trace_id,
                    source_component="second_brain.consolidator",
                )
                try:
                    await bus.publish(STREAM_MEMORY_ACCESSED, accessed_event)
                    log.debug(
                        "consolidation_memory_access_event_published",
                        entity_count=len(all_entity_ids),
                        relationship_count=len(rel_ids_deduped),
                        trace_id=run_trace_id,
                    )
                except Exception as e:
                    log.warning(
                        "memory_access_event_publish_failed",
                        error=str(e),
                        event_id=accessed_event.event_id,
                        trace_id=run_trace_id,
                    )

            return summary
        finally:
            close_root_span(span, token, cv_tokens)

    async def _consolidate_sessions(
        self,
        all_captures: list[TaskCapture],
        sessions_with_new_turns: set[str],
        *,
        trace_id: str,
    ) -> int:
        """Create or update Session nodes for sessions that received new turns.

        For each affected session:
        1. Derive metadata (timestamps, turn count, dominant entities) from captures
        2. MERGE the Session node
        3. Wire CONTAINS + NEXT + Session-DISCUSSES-Entity relationships

        Args:
            all_captures: All captures from this consolidation run.
            sessions_with_new_turns: session_ids that had at least one new turn.
            trace_id: Trace identifier of the enclosing consolidation run
                (ADR-0074 §I3). Threaded through to summary generation,
                session/turn MERGE calls, and structured logs.

        Returns:
            Number of sessions created/updated.
        """
        if not sessions_with_new_turns:
            return 0

        # Group captures by session_id
        by_session: dict[str, list[TaskCapture]] = defaultdict(list)
        for capture in all_captures:
            if capture.session_id in sessions_with_new_turns:
                by_session[capture.session_id].append(capture)

        sessions_created = 0
        for session_id, session_captures in by_session.items():
            try:
                ordered = sorted(session_captures, key=lambda c: c.timestamp)
                # dominant_entities is populated by _update_session_dominant_entities()
                # after MERGE via a graph query — captures don't carry key_entities directly.

                # ADR-0124 D1 (FRE-947): the summariser is NO LONGER called here.
                # Generation is a derived read model driven by the idle sweep
                # (brainstem/scheduler.py), not a per-turn side effect of
                # consolidation — ADR-0024's original resolution, which the FRE-347
                # implementation inverted. This pass owns only the turn-derived
                # properties; the digest, label and freshness stamp are the sweep's.
                session_node = SessionNode(
                    session_id=session_id,
                    started_at=ordered[0].timestamp,
                    ended_at=ordered[-1].timestamp,
                    turn_count=len(ordered),
                    dominant_entities=[],  # Populated by link_session_turns via graph query
                )
                # FRE-998 / ADR-0107: carry the session's owning user onto the
                # Session node. Every capture in this group belongs to the same
                # session and TaskCapture.user_id is non-optional, so disagreement
                # is an invariant violation rather than routine ambiguity — fail
                # closed. Passing None preserves whatever is already stored
                # (create_session COALESCEs), because a non-null value always wins
                # and an arbitrary pick would silently overwrite correct identity.
                session_user_ids = {c.user_id for c in ordered}
                if len(session_user_ids) > 1:
                    log.error(
                        "session_captures_mixed_user_id",
                        session_id=session_id,
                        user_id_count=len(session_user_ids),
                        trace_id=trace_id,
                    )
                    session_user_id = None
                else:
                    session_user_id = next(iter(session_user_ids), None)

                created = await self.memory_service.create_session(
                    session_node, trace_id=trace_id, user_id=session_user_id
                )
                if created:
                    linked = await self.memory_service.link_session_turns(
                        session_id, trace_id=trace_id
                    )
                    # Refresh dominant_entities from graph after linking
                    await self._update_session_dominant_entities(session_id, trace_id=trace_id)
                    sessions_created += 1
                    log.debug(
                        "session_consolidated",
                        session_id=session_id,
                        turns_linked=linked,
                        trace_id=trace_id,
                    )
            except Exception as e:
                log.error(
                    "session_consolidation_failed",
                    session_id=session_id,
                    error=str(e),
                    exc_info=True,
                    trace_id=trace_id,
                )

        return sessions_created

    async def _update_session_dominant_entities(self, session_id: str, *, trace_id: str) -> None:
        """Update Session.dominant_entities from the top entities discussed in its turns.

        Args:
            session_id: Session to update.
            trace_id: Trace identifier of the enclosing consolidation run
                (ADR-0074 §I3).
        """
        if not self.memory_service.connected or not self.memory_service.driver:
            return
        try:
            async with self.memory_service.driver.session() as db_session:
                result = await db_session.run(
                    """
                    MATCH (s:Session {session_id: $session_id})-[r:DISCUSSES]->(e:Entity)
                    RETURN e.name AS name, r.turn_count AS cnt
                    ORDER BY r.turn_count DESC
                    LIMIT 10
                    """,
                    session_id=session_id,
                )
                records = await result.values()
                dominant = [row[0] for row in records if row[0]]
                if dominant:
                    await db_session.run(
                        "MATCH (s:Session {session_id: $session_id}) SET s.dominant_entities = $dominant",
                        session_id=session_id,
                        dominant=dominant,
                    )
        except Exception as e:
            log.warning(
                "update_dominant_entities_failed",
                session_id=session_id,
                error=str(e),
                trace_id=trace_id,
            )

    async def _process_capture(
        self, capture: TaskCapture, *, extractor_model: str | None = None
    ) -> dict[str, Any]:
        """Process a single capture: extract entities and update graph.

        Args:
            capture: Task capture to process
            extractor_model: Identifier of the entity-extraction model role used
                for this consolidation pass (ADR-0074 §I5). Threaded onto each
                ``:Entity`` node as ``extractor_model``.

        Returns:
            Processing result summary with entity_ids for events.
        """
        # Strip <think>…</think> blocks from the assistant response before extraction.
        # The full response (including thinking) is preserved in the TurnNode below for
        # debugging, but passing raw thinking to the extraction model inflates the prompt
        # and causes extraction of internal tool names (e.g. mcp_perplexity_ask) that the
        # model was only reasoning about, not actually recommending.
        raw_response = capture.assistant_response or ""
        extraction_response = re.sub(
            r"<think>.*?</think>", "", raw_response, flags=re.DOTALL
        ).strip()

        # FRE-307: per-attempt telemetry. attempt_number is 1-based from the
        # count of prior consolidation_attempts rows for this (trace_id, role).
        attempt_started_at = datetime.now(timezone.utc)
        previous_failures = await previous_attempt_count(
            trace_id=capture.trace_id, role="entity_extraction"
        )
        attempt_number = previous_failures + 1

        # Extract entities and relationships using configured model (local SLM or Claude).
        # BudgetDenied bubbles up as a distinct outcome ('budget_denied') so the
        # auto-tuning monitor (FRE-311) and the Extraction Retry Health panel can
        # distinguish cap pressure from actual extraction errors.
        try:
            extraction_result = await extract_entities_and_relationships(
                capture.user_message,
                extraction_response,
                trace_id=capture.trace_id,
                session_id=capture.session_id,
                attempt_number=attempt_number,
                turn_timestamp=capture.timestamp,
                tracer=self._tracer,
            )
        except BudgetDenied as budget_exc:
            await record_consolidation_attempt(
                trace_id=capture.trace_id,
                role="entity_extraction",
                started_at=attempt_started_at,
                outcome="budget_denied",
                denial_reason=budget_exc.denial_reason,
            )
            log.warning(
                "consolidation_extraction_budget_denied",
                capture_trace_id=capture.trace_id,
                trace_id=capture.trace_id,
                attempt_number=attempt_number,
                previous_failure_count=previous_failures,
                denial_reason=budget_exc.denial_reason,
                role=budget_exc.role,
                cap=str(budget_exc.cap),
                spend=str(budget_exc.current_spend),
            )
            return {
                "turns_created": 0,
                "entities_created": 0,
                "relationships_created": 0,
                "entity_ids": [],
                "relationship_element_ids": [],
            }

        # If extraction fell back (LLM error/crash), the historical behavior was to
        # skip writing to Neo4j entirely — preserving the chance to retry on the next
        # consolidation tick (writing a Conversation node would permanently block
        # retries via conversation_exists()).
        #
        # FRE-380 (Stage 1): the unconditional skip becomes joinability-fatal when
        # extraction is broken for extended periods (cf. the 2026-05-23 trace_ctx
        # regression which bled for 17h). After `settings.consolidator_max_extraction_
        # attempts` failures, we instead write a *stub Turn* — full message content,
        # origination identity (§I5), empty `key_entities`, and a properties marker.
        # The capture becomes joinable; the LLM-derived semantic enrichment is
        # accepted as lost. Stage 2 (FRE-381) will decouple Turn creation from
        # extraction entirely.
        summary = extraction_result.get("summary", "")
        is_fallback = (
            not extraction_result.get("entities")
            and summary.strip() == default_extraction_summary(capture.user_message or "").strip()
        )
        if is_fallback:
            time_since_first = (datetime.now(timezone.utc) - attempt_started_at).total_seconds()
            _settings = get_settings()
            max_attempts = getattr(_settings, "consolidator_max_extraction_attempts", 5)
            if attempt_number >= max_attempts:
                # Cap reached — write a stub Turn and stop retrying.
                await record_consolidation_attempt(
                    trace_id=capture.trace_id,
                    role="entity_extraction",
                    started_at=attempt_started_at,
                    outcome="extraction_capped",
                )
                log.warning(
                    "consolidation_extraction_capped",
                    capture_trace_id=capture.trace_id,
                    trace_id=capture.trace_id,
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                    previous_failure_count=previous_failures,
                    time_since_first_attempt_seconds=time_since_first,
                    reason="exhausted extraction retries; writing stub Turn for joinability",
                )

                stub_summary = default_extraction_summary((capture.user_message or "").strip()) or (
                    "(empty)"
                )
                stub_turn = TurnNode(
                    turn_id=capture.trace_id,
                    trace_id=capture.trace_id,
                    session_id=capture.session_id,
                    timestamp=capture.timestamp,
                    summary=stub_summary,
                    user_message=capture.user_message,
                    assistant_response=capture.assistant_response,
                    key_entities=[],
                    properties={
                        "tools_used": capture.tools_used,
                        "duration_ms": capture.duration_ms,
                        "outcome": capture.outcome,
                        "extraction_outcome": "capped_after_retries",
                        "extraction_attempts": attempt_number,
                        # FRE-523: EVAL provenance so eval-derived KG content is identifiable.
                        "eval_mode": capture.eval_mode,
                    },
                )
                # No `_entity_data` attached — entities list is intentionally empty.
                await self.memory_service.create_conversation(
                    stub_turn, user_id=capture.user_id, visibility="group"
                )
                return {
                    "turns_created": 1,
                    "entities_created": 0,
                    "relationships_created": 0,
                    "entity_ids": [],
                    "relationship_element_ids": [],
                }

            # Below cap — original retry path.
            await record_consolidation_attempt(
                trace_id=capture.trace_id,
                role="entity_extraction",
                started_at=attempt_started_at,
                outcome="extraction_returned_fallback",
            )
            log.warning(
                "consolidation_extraction_fallback_skip",
                capture_trace_id=capture.trace_id,
                trace_id=capture.trace_id,
                attempt_number=attempt_number,
                previous_failure_count=previous_failures,
                time_since_first_attempt_seconds=time_since_first,
                max_attempts=max_attempts,
                denial_reason=None,
                reason="extraction returned fallback result; will retry next run",
            )
            return {
                "turns_created": 0,
                "entities_created": 0,
                "relationships_created": 0,
                "entity_ids": [],
                "relationship_element_ids": [],
            }

        # FRE-343: TaskCapture.user_id is non-optional, so authenticated sessions
        # always produce "group"-visibility nodes (visible to all CF Access users).
        visibility = "group"

        # ADR-0115 D1/D3: partition extracted entities by output_kind *before* anything is
        # written. `create_conversation` MERGEs a bare :Entity node for every name in
        # `key_entities` (memory/service.py), so ephemeral/finding names must never reach
        # it. Since FRE-1115 `key_entities` is built from the canonical names `create_entity`
        # returns, and only `knowledge_entities` are passed to it — so the partition is what
        # keeps them out of Core, on both the entity write and the Turn's inline MERGE.
        all_entities = extraction_result.get("entities", [])
        knowledge_entities = [
            e
            for e in all_entities
            if e.get("output_kind", "knowledge") not in ("ephemeral", "finding")
        ]
        ephemeral_entities = [e for e in all_entities if e.get("output_kind") == "ephemeral"]
        finding_entities = [e for e in all_entities if e.get("output_kind") == "finding"]

        # FRE-1115: entities are written FIRST, so the Turn can be recorded against the
        # canonical names dedup actually resolved. When `create_conversation` ran first it
        # bare-MERGEd an :Entity per raw name (memory/service.py); if `create_entity` then
        # renamed the write, the description landed on the canonical node and the raw-name
        # node was orphaned with no description forever — the single generator of all
        # 1,404 empty-description entities on the live graph, still minting at ~9%.
        #
        # ADR-0098 Amendment A (FRE-1346): the external artifacts this turn retrieved.
        # Derived once per capture from `tool_results`, which has held every fetched URL
        # since FRE-947 — the address was already captured and extraction simply never
        # looked. The tool contract is the single source of referents (A2): a result
        # becomes a :Source only because its own ToolDefinition declares which parameter
        # names the thing retrieved.
        sources = sources_from_tool_results(
            capture.tool_results,
            retrieved_at=capture.timestamp,
            capture_trace_id=capture.trace_id,
            tool_registry=self._tool_registry,
        )
        # The false-negative class A4 records rather than hides: an item that fell to
        # `none` *while a source existed* is a countable miss (lowercase/stylized names
        # like `npm`), whereas an item from a turn that fetched nothing never had a source
        # to be contained in. Counting them together would inflate the rate and make the
        # decision to widen the check — or not — rest on a wrong number.
        entities_provenanced = 0
        entities_none_with_sources = 0
        relationships_provenanced = 0
        relationships_none_with_sources = 0

        # Create entity nodes — knowledge items only (ADR-0115 D3 dispatch).
        entities_created = 0
        entity_ids: list[str] = []
        # Raw extractor name -> the canonical name the write actually landed on. Every
        # later reference to an entity (the Turn's key_entities, the inline type map,
        # relationship endpoints) must go through this map or it will name a node that
        # does not exist.
        canonical_by_raw: dict[str, str] = {}
        unresolved_entity_mentions: list[str] = []
        for entity_data in knowledge_entities:
            raw_name = entity_data.get("name", "")
            # A4: an entity contributes its NAME only. That matches the attribution
            # semantics — a page mentioning SafeCart justifies where we learned of
            # SafeCart, never the entity's stored description or type.
            entity_sources = _matching_sources(raw_name, sources)
            if entity_sources:
                entities_provenanced += 1
            elif sources:
                entities_none_with_sources += 1
            entity = Entity(
                name=raw_name,
                entity_type=entity_data.get("type", "Unknown"),
                description=entity_data.get("description"),
                properties=entity_data.get("properties", {}),
                # ADR-0115 D2: the extractor's per-entity P/W class (fail-open to
                # World, FRE-863) carried through to the Entity write.
                knowledge_class=entity_data.get("class"),
            )
            entity_id = await self.memory_service.create_entity(
                entity,
                visibility=visibility,
                originating_trace_id=capture.trace_id,
                originating_session_id=capture.session_id,
                extractor_model=extractor_model,
                # FRE-711: World-description correction gate — source confidence + the
                # eval flag so an eval turn can never overwrite a real description.
                description_confidence=KnowledgeWeight.from_source("conversation").confidence,
                eval_mode=capture.eval_mode,
                # FRE-725: the extractor's per-entity enrichment/correction signal so a later
                # same-confidence description can supersede a thin one (validated in the service).
                description_update_kind=entity_data.get("description_update_kind", "new"),
                # ADR-0098 A4: derived in Python from the captured turn, never read from
                # the extractor's output (D6/FRE-1020) — a model permitted to declare its
                # own provenance can mint the credential that makes it authoritative.
                source_records=entity_sources,
            )
            if entity_id:
                entities_created += 1
                entity_ids.append(entity_id)
                canonical_by_raw[raw_name] = entity_id
            elif raw_name:
                # FRE-1115: create_entity returns "" ONLY on failure (disconnected driver
                # or a caught exception) — never on a rename, which returns the canonical
                # name. So falling back to the raw name here cannot recreate the
                # dedup-orphan class this ticket removes; it only covers a genuinely
                # failed write, where losing the Turn's DISCUSSES edge would be worse.
                # That edge is the sole path from a turn to its entities, and nothing
                # re-derives it, so the turn would become permanently unreachable by every
                # entity-anchored recall query. The bare node the inline MERGE then
                # creates is repaired by the next successful extraction of the same name.
                # The mention is ALSO recorded as a Turn property so the failure stays
                # diagnosable rather than looking like a normal write.
                canonical_by_raw[raw_name] = raw_name
                unresolved_entity_mentions.append(raw_name)
                log.warning(
                    "consolidation_entity_write_unresolved",
                    capture_trace_id=capture.trace_id,
                    trace_id=capture.trace_id,
                    session_id=capture.session_id,
                    entity_name=raw_name,
                    reason="create_entity returned no id; mention recorded without an :Entity",
                )

        # Canonical names, order-preserving and de-duplicated: several raw spellings can
        # resolve to one canonical node, and the Turn must discuss it once.
        canonical_key_entities = list(dict.fromkeys(canonical_by_raw.values()))
        # The inline type map is keyed by name, so it has to speak canonical too.
        canonical_entity_data = [
            {**e, "name": canonical_by_raw.get(e.get("name", ""), e.get("name", ""))}
            for e in all_entities
        ]

        turn_properties: dict[str, Any] = {
            "tools_used": capture.tools_used,
            "duration_ms": capture.duration_ms,
            "outcome": capture.outcome,
            # FRE-523: EVAL provenance so eval-derived KG content is identifiable.
            "eval_mode": capture.eval_mode,
        }
        if unresolved_entity_mentions:
            turn_properties["unresolved_entity_mentions"] = unresolved_entity_mentions

        # Create Turn node
        turn = TurnNode(
            turn_id=capture.trace_id,
            trace_id=capture.trace_id,
            session_id=capture.session_id,
            timestamp=capture.timestamp,
            summary=summary,
            user_message=capture.user_message,
            assistant_response=capture.assistant_response,
            key_entities=canonical_key_entities,
            properties=turn_properties,
        )
        # Attach full entity data so create_conversation can set entity_type on inline nodes.
        # This is a transient attribute — not part of the Pydantic model — used only during write.
        object.__setattr__(turn, "_entity_data", canonical_entity_data)

        # FRE-1115: the result was previously discarded, so a failed Turn write was still
        # reported as one created turn.
        turn_written = await self.memory_service.create_conversation(
            turn, user_id=capture.user_id, visibility=visibility
        )
        turns_created = 1 if turn_written else 0
        if not turn_written:
            log.warning(
                "consolidation_turn_write_failed",
                capture_trace_id=capture.trace_id,
                trace_id=capture.trace_id,
                session_id=capture.session_id,
                reason="create_conversation reported failure; entities written without a Turn",
            )

        relationship_element_ids: list[str] = list(
            await self.memory_service.fetch_turn_discusses_relationship_element_ids(
                capture.trace_id, trace_id=capture.trace_id
            )
        )

        # ephemeral items: no write anywhere -- already durably observed in Elasticsearch
        # via the unconditional write_capture()/schedule_es_index() call at capture time,
        # independent of consolidation (ADR-0115 D3).
        entities_dispatched_ephemeral = len(ephemeral_entities)

        # finding items: route to sysgraph (ADR-0115 D3), never Core. Best-effort against
        # the process-level singleton -- a sysgraph hiccup must not abort the rest of this
        # capture's knowledge/stance/claim writes -- but a failure is counted separately
        # from a successful dispatch so it is never silently conflated with "landed".
        entities_dispatched_finding = 0
        entities_dispatch_finding_failed = 0
        if finding_entities:
            sysgraph_repo = get_default_sysgraph_repo()
            for entity_data in finding_entities:
                if sysgraph_repo is None:
                    entities_dispatch_finding_failed += 1
                    log.warning(
                        "dispatch_finding_sysgraph_unavailable",
                        capture_trace_id=capture.trace_id,
                        trace_id=capture.trace_id,
                        entity_name=entity_data.get("name", ""),
                    )
                    continue
                try:
                    await sysgraph_repo.record_finding(
                        entity_name=entity_data.get("name", ""),
                        entity_type=entity_data.get("type", "Unknown"),
                        description=entity_data.get("description"),
                        trace_id=capture.trace_id,
                        session_id=capture.session_id,
                    )
                except Exception as e:
                    entities_dispatch_finding_failed += 1
                    log.warning(
                        "dispatch_finding_sysgraph_write_failed",
                        capture_trace_id=capture.trace_id,
                        trace_id=capture.trace_id,
                        entity_name=entity_data.get("name", ""),
                        error=str(e),
                    )
                else:
                    entities_dispatched_finding += 1

        # Create relationships — skip any endpoint dispatched away from Core (ADR-0115
        # D3): create_relationship's MATCH (memory/service.py) would otherwise either
        # silently no-op (endpoint never written) or, worse, splice an edge onto an
        # unrelated pre-existing Core entity that happens to share the dispatched-away
        # entity's name from a prior turn.
        dispatched_away_names = {e.get("name", "") for e in ephemeral_entities if e.get("name")} | {
            e.get("name", "") for e in finding_entities if e.get("name")
        }
        relationships_created = 0
        relationships_dispatch_skipped = 0
        for rel_data in extraction_result.get("relationships", []):
            source_name = rel_data.get("source", "")
            target_name = rel_data.get("target", "")
            # ADR-0115 D3 gating is decided on the RAW name, before translation, so the
            # existing skip semantics are unchanged by the canonicalisation.
            if source_name in dispatched_away_names or target_name in dispatched_away_names:
                relationships_dispatch_skipped += 1
                log.warning(
                    "dispatch_relationship_endpoint_skipped",
                    capture_trace_id=capture.trace_id,
                    trace_id=capture.trace_id,
                    source=source_name,
                    target=target_name,
                )
                continue
            # FRE-1115: follow the rename when this turn wrote the entity, otherwise pass
            # the raw name through. create_relationship MATCHes endpoints by
            # `entity_id OR name`, so an endpoint dedup renamed must be translated or the
            # edge lands on nothing. But the extractor is NOT required to repeat an
            # endpoint in `entities[]` — a relationship may legitimately reference an
            # entity written by an earlier turn — so an untranslated name is resolved
            # against the existing graph exactly as it was before this reorder. Dropping
            # it here instead would silently delete every cross-turn edge.
            canonical_source = canonical_by_raw.get(source_name, source_name)
            canonical_target = canonical_by_raw.get(target_name, target_name)
            relationship_type = rel_data.get("type", "RELATED_TO")
            relationship = Relationship(
                source_id=canonical_source,
                target_id=canonical_target,
                relationship_type=relationship_type,
                weight=rel_data.get("weight", 1.0),
                properties=rel_data.get("properties", {}),
            )
            # A4: a relationship contributes its verbalization, `source predicate target`.
            # Built from the CANONICAL endpoint names — the raw extractor spellings may
            # have been renamed by dedup, and attributing the edge under a name the graph
            # does not use would check containment against the wrong string.
            rel_sources = _matching_sources(
                attribution_for_relationship(canonical_source, relationship_type, canonical_target),
                sources,
            )
            if rel_sources:
                relationships_provenanced += 1
            elif sources:
                relationships_none_with_sources += 1
            rel_eid = await self.memory_service.create_relationship(
                relationship,
                visibility=visibility,
                trace_id=capture.trace_id,
                source_records=rel_sources,
            )
            if rel_eid:
                relationships_created += 1
                relationship_element_ids.append(rel_eid)

        # ADR-0098 D2 (FRE-638): wire the FRE-637 stances[]/claims[] into Core.
        # Stances become native owner→World HAS_STANCE edges, resolving the is_owner
        # sentinel (unchanged, ADR-0107 §3). Personal claims become living Claim nodes
        # (correction / bitemporal supersession), resolving the acting user's own
        # Person node by user_id (ADR-0107 §2) — capture.user_id is already in scope.
        stances_created = 0
        for stance_data in extraction_result.get("stances", []):
            stance = _build_stance(stance_data)
            if stance is None:
                continue
            # FRE-1115: assert_stance resolves the target with MATCH (:Entity {name}), so
            # it has to be the canonical name. Before the reorder the raw-name node was
            # bare-MERGEd and the stance landed on it; that node no longer exists, so a
            # renamed target would silently drop the owner's stated preference.
            # (assert_claim needs no equivalent — it resolves via Person/Claim, not by
            # entity name.)
            canonical_target = canonical_by_raw.get(stance.target)
            if canonical_target is not None and canonical_target != stance.target:
                stance = stance.model_copy(update={"target": canonical_target})
            if await self.memory_service.assert_stance(stance, trace_id=capture.trace_id):
                stances_created += 1

        claims_created = 0
        for claim_data in extraction_result.get("claims", []):
            claim = _build_claim(claim_data)
            if claim is None:
                continue
            # A4: a claim contributes its content.
            if await self.memory_service.assert_claim(
                claim,
                user_id=capture.user_id,
                trace_id=capture.trace_id,
                source_records=_matching_sources(claim.content, sources),
            ):
                claims_created += 1

        # FRE-307: terminal success row.
        await record_consolidation_attempt(
            trace_id=capture.trace_id,
            role="entity_extraction",
            started_at=attempt_started_at,
            outcome="success",
        )

        # A5: the rate is reported, never used to widen the containment check. `*_none_
        # with_sources` counts only items that had a source available and still did not
        # match — the population where a false negative is even possible.
        log.info(
            "consolidation_provenance_summary",
            capture_trace_id=capture.trace_id,
            trace_id=capture.trace_id,
            session_id=capture.session_id,
            sources_available=len(sources),
            entities_provenanced=entities_provenanced,
            entities_none_with_sources=entities_none_with_sources,
            relationships_provenanced=relationships_provenanced,
            relationships_none_with_sources=relationships_none_with_sources,
        )

        return {
            "turns_created": turns_created,
            "entities_created": entities_created,
            "relationships_created": relationships_created,
            "entities_provenanced": entities_provenanced,
            "entities_none_with_sources": entities_none_with_sources,
            "relationships_provenanced": relationships_provenanced,
            "relationships_none_with_sources": relationships_none_with_sources,
            "stances_created": stances_created,
            "claims_created": claims_created,
            "entity_ids": entity_ids,
            "relationship_element_ids": list(dict.fromkeys(relationship_element_ids)),
            "entities_dispatched_ephemeral": entities_dispatched_ephemeral,
            "entities_dispatched_finding": entities_dispatched_finding,
            "entities_dispatch_finding_failed": entities_dispatch_finding_failed,
            "relationships_dispatch_skipped": relationships_dispatch_skipped,
        }
