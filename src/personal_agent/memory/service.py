"""Neo4j memory service for knowledge graph operations."""

import asyncio
import re
import statistics
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import orjson
import structlog

from personal_agent.config._substrate_fingerprint import is_prod_neo4j_uri
from personal_agent.config.env_loader import Environment
from personal_agent.config.settings import get_settings
from personal_agent.events import (
    STREAM_MEMORY_ACCESSED,
    AccessContext,
    MemoryAccessedEvent,
    get_event_bus,
)
from personal_agent.llm_client import InferencePriority, LocalLLMClient, ModelRole
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.memory.embeddings import generate_embedding, generate_embeddings_batch
from personal_agent.memory.fact import PromotionCandidate
from personal_agent.memory.freshness_aggregate import (
    GraphStalenessSummary,
    aggregate_graph_staleness,
)
from personal_agent.memory.fusion import (
    FusedResult,
    MultiPathRecallResult,
    RankedResult,
    reciprocal_rank_fusion,
)
from personal_agent.memory.models import (
    Claim,
    Entity,
    EntityNode,
    MemoryQuery,
    MemoryQueryResult,
    Relationship,
    SessionNode,
    Stance,
    TurnNode,
)
from personal_agent.memory.supersession import (
    ClaimRecord,
    SupersessionAction,
    adjudicate,
    matching_candidates,
    strongest_blocker,
)
from personal_agent.telemetry.trace import TraceContext

Neo4jAsyncGraphDatabase: Any = None
try:
    from neo4j import AsyncGraphDatabase as _Neo4jAsyncGraphDatabase

    Neo4jAsyncGraphDatabase = _Neo4jAsyncGraphDatabase
except ModuleNotFoundError:  # pragma: no cover - optional dependency in test environments
    pass

# Backward-compatibility alias
ConversationNode = TurnNode

log = structlog.get_logger()
settings = get_settings()

# FRE-711: legacy :Entity rows predate description confidence; treat a null stored
# confidence as this baseline so legacy descriptions are not mass-reset on the first
# post-deploy consolidation (a same-baseline write does not clear strict '>').
_DEFAULT_DESCRIPTION_CONFIDENCE = 0.8

# FRE-725: the extractor's per-entity description signal. FRE-711's strict-'>' gate never
# fires for same-source re-extraction (every conversation write is 0.8), so a thin non-empty
# description can never be enriched at equal confidence. An explicit "enrichment"/"correction"
# kind unlocks equal-confidence supersession (still archived, still eval-gated). Unlike the
# FRE-712 claim update_kind (a label applied AFTER safety checks), this signal is
# write-authorizing, so create_entity validates it server-side before it reaches Cypher.
_DEFAULT_DESCRIPTION_UPDATE_KIND = "new"
_EXPLICIT_DESCRIPTION_UPDATE_KINDS = ("enrichment", "correction")
_VALID_DESCRIPTION_UPDATE_KINDS = frozenset(
    {_DEFAULT_DESCRIPTION_UPDATE_KIND, *_EXPLICIT_DESCRIPTION_UPDATE_KINDS}
)


def _parse_iso(value: Any, *, fallback: datetime) -> datetime:
    """Parse a stored ISO-8601 string back to a datetime, tolerating bad data.

    Claim ``observed_at`` is written as an ISO string, so it reads back as a string;
    Neo4j native temporals expose ``.to_native()``. Anything unparseable falls back
    so a single corrupt row cannot break supersession ordering.

    Args:
        value: The raw property value (str, native temporal, or None).
        fallback: The datetime to use when ``value`` cannot be parsed.

    Returns:
        A timezone-aware datetime.
    """
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return fallback
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        native = to_native()
        if isinstance(native, datetime):
            return native
    if isinstance(value, datetime):
        return value
    return fallback


def _build_visibility_filter(
    alias: str,
    user_id: UUID | None,
    authenticated: bool,
) -> tuple[str, dict[str, Any]]:
    """Build a Cypher WHERE fragment for memory visibility scoping (FRE-229).

    This is the single chokepoint for all memory read access. Every MATCH on
    :Turn and :Entity in this module must merge the returned params and append
    the fragment to its WHERE clause.

    Args:
        alias: The Cypher node alias to qualify (e.g. "c", "e", "t").
        user_id: Authenticated user UUID; None for unauthenticated paths.
        authenticated: True when the request carries a verified CF Access identity.

    Returns:
        Tuple of (cypher_fragment, params_dict). Params use unique keys
        (``vis_authenticated``, ``vis_user_id``) to avoid collisions.
    """
    user_id_str = str(user_id) if user_id else ""
    fragment = (
        f"({alias}.visibility IS NULL "
        f"OR {alias}.visibility = 'public' "
        f"OR ({alias}.visibility = 'group' AND $vis_authenticated = true) "
        f"OR {alias}.visibility = 'private:' + $vis_user_id)"
    )
    params: dict[str, Any] = {"vis_authenticated": authenticated, "vis_user_id": user_id_str}
    return fragment, params


_LUCENE_SPECIAL_CHARS = re.compile(r'([+\-!(){}\[\]^"~*?:\\/&|])')


def _escape_lucene_query(text: str) -> str:
    r"""Escape Lucene special characters so free text is a literal query.

    Neo4j's full-text index is Lucene-backed; unescaped punctuation (``: ( ) "
    / \\`` etc.) throws a parse error on ordinary sentences, not just
    adversarial input — this is a real system boundary (user text → Lucene
    query DSL), not speculative hardening.

    Args:
        text: Raw query text.

    Returns:
        Text with Lucene special characters backslash-escaped.
    """
    return _LUCENE_SPECIAL_CHARS.sub(r"\\\1", text)


_PARAPHRASE_SYSTEM_PROMPT = (
    "Rewrite the user's query as alternative phrasings using different vocabulary "
    "for the same meaning. Reply with exactly one paraphrase per line, no numbering, "
    "no commentary."
)


async def generate_query_paraphrases(
    query_text: str,
    count: int,
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> list[str]:
    """Generate up to `count` paraphrases of a query via the local SUB_AGENT model.

    Fails open (ADR-0104 / FRE-723): any error — timeout, slot exhaustion,
    malformed response, connection failure — returns []. Callers degrade to the
    dense arm alone on the original query; this function never raises and must
    never hard-fail recall.

    Args:
        query_text: The original query to paraphrase.
        count: Max paraphrases to request (not including the original).
        trace_id: Optional trace id for correlation.
        session_id: Optional session id for correlation.

    Returns:
        Up to `count` paraphrase strings, one per line of the model's reply.
        Empty on any failure or when count < 1 or query_text is blank.
    """
    if count < 1 or not query_text.strip():
        return []
    try:
        client = LocalLLMClient()
        trace_ctx = TraceContext(trace_id=trace_id or str(uuid4()), session_id=session_id)
        response = await client.respond(
            role=ModelRole.SUB_AGENT,
            messages=[{"role": "user", "content": query_text}],
            system_prompt=_PARAPHRASE_SYSTEM_PROMPT,
            max_tokens=200,
            max_retries=0,
            timeout_s=10.0,
            priority=InferencePriority.BACKGROUND,
            priority_timeout=10.0,
            trace_ctx=trace_ctx,
        )
        lines = [line.strip() for line in response["content"].splitlines() if line.strip()]
        return lines[:count]
    except Exception as exc:
        log.warning(
            "multiquery_paraphrase_generation_failed",
            error=str(exc),
            trace_id=trace_id,
            session_id=session_id,
        )
        return []


def _build_structural_arm_query(
    *,
    entity_types: Sequence[str] | None,
    type_predicate_enabled: bool,
    recency_days: int | None,
    anchor_names: Sequence[str] | None,
    top_k: int,
    vis_fragment_e: str,
    vis_fragment_t: str,
    vis_fragment_a: str,
) -> tuple[str, dict[str, Any]]:
    """Build the closed-axis structural arm's Cypher and params (ADR-0104 AC-4).

    Pure function (no substrate) so the safe type predicate and the open-axis
    exclusion are unit-testable. Composes three optional closed-axis predicates —
    entity type (safe), recency-as-predicate, relationship hops — over entities.
    Never filters on the open axis (name/description): AC-4c.

    Args:
        entity_types: Requested entity types for the type predicate, or None.
        type_predicate_enabled: Whether the type sub-predicate is active (AC-4a).
        recency_days: Recency window in days for last_seen, or None for no window.
        anchor_names: Entity names to seed 1-hop co-occurrence traversal, or None.
        top_k: Maximum entities to return.
        vis_fragment_e: Visibility WHERE fragment for the entity alias ``e`` (FRE-229).
        vis_fragment_t: Visibility fragment for the intermediate Turn alias ``t``.
        vis_fragment_a: Visibility fragment for the anchor entity alias ``a``.

    Returns:
        Tuple of (cypher, params). Params never include a name/description filter.
    """
    params: dict[str, Any] = {"top_k": top_k}
    e_where: list[str] = [vis_fragment_e]

    if type_predicate_enabled and entity_types:
        # SAFE type predicate (AC-4b): narrow to the requested types but keep
        # unenforced-type rows so none is silently dropped until FRE-637's
        # contract has back-filled the graph. The explicit IS NULL branch makes
        # the disjunction true for null-typed rows (Cypher null semantics).
        e_where.append(
            "(e.entity_type IN $entity_types "
            "OR e.entity_type IS NULL "
            "OR e.entity_type = '' "
            "OR e.entity_type = 'Unknown')"
        )
        params["entity_types"] = list(entity_types)

    if recency_days is not None:
        # last_seen is heterogeneous (ISO string on the mention path, Neo4j
        # datetime() on the create/access path); normalise both sides with
        # toString for a valid lexicographic, day-granular comparison. Rows with
        # a null last_seen are excluded by the window (NULL >= x is null).
        params["recency_cutoff"] = (
            datetime.now(timezone.utc) - timedelta(days=recency_days)
        ).isoformat()
        e_where.append("toString(e.last_seen) >= $recency_cutoff")

    e_where_clause = " AND ".join(e_where)

    if anchor_names:
        params["anchor_names"] = list(anchor_names)
        # 1-hop co-occurrence over the bipartite Turn-Entity graph. Scope a, t
        # AND e (FRE-229): never surface an entity reached through an anchor or
        # intermediate Turn the caller cannot see.
        cypher = f"""
        MATCH (a:Entity)<-[:DISCUSSES]-(t:Turn)-[:DISCUSSES]->(e:Entity)
        WHERE a.name IN $anchor_names AND e.name <> a.name
          AND {vis_fragment_a} AND {vis_fragment_t} AND {e_where_clause}
        WITH e, count(DISTINCT a) AS cooccur
        RETURN e AS e
        ORDER BY cooccur DESC, toString(e.last_seen) DESC, e.name
        LIMIT $top_k
        """
    else:
        cypher = f"""
        MATCH (e:Entity)
        WHERE {e_where_clause}
        RETURN e AS e
        ORDER BY toString(e.last_seen) DESC, e.name
        LIMIT $top_k
        """
    return cypher, params


def _entity_node_from_record(node: Any) -> EntityNode:
    """Build an EntityNode from a Neo4j entity node (shared parse).

    Handles the heterogeneous storage of the temporal fields (Neo4j
    ``DateTime`` objects vs ISO strings vs missing) and the JSON-string
    ``properties`` field. Extracted so the broad recall paths and the
    structural arm (FRE-707) parse entities identically.

    Args:
        node: A Neo4j node (mapping of entity properties) from a query record.

    Returns:
        The parsed EntityNode. ``entity_type`` defaults to ``"Unknown"`` and
        timestamps default to now when absent.
    """
    first_seen = node.get("first_seen")
    if hasattr(first_seen, "to_native"):
        first_seen = first_seen.to_native()
    elif isinstance(first_seen, str):
        first_seen = datetime.fromisoformat(first_seen)
    elif first_seen is None:
        first_seen = datetime.utcnow()

    last_seen = node.get("last_seen")
    if hasattr(last_seen, "to_native"):
        last_seen = last_seen.to_native()
    elif isinstance(last_seen, str):
        last_seen = datetime.fromisoformat(last_seen)
    elif last_seen is None:
        last_seen = datetime.utcnow()

    properties = node.get("properties", "{}")
    if isinstance(properties, str):
        properties = orjson.loads(properties)
    elif properties is None:
        properties = {}

    return EntityNode(
        entity_id=node.get("name", ""),
        name=node.get("name", ""),
        entity_type=node.get("entity_type", "Unknown"),
        description=node.get("description"),
        interest_weight=min(node.get("mention_count", 0) / 100.0, 1.0),
        first_seen=first_seen,
        last_seen=last_seen,
        mention_count=node.get("mention_count", 0),
        properties=properties,
    )


def _rank_conversations_by_relevance(
    conversations: Sequence[TurnNode],
    relevance_scores: dict[str, float],
) -> list[TurnNode]:
    """Sort conversations by combined relevance score, descending (ADR-0100 defect 3).

    The legacy recall path returns turns in Cypher (timestamp) order even though
    relevance scores are computed; this applies those scores to the ordering.
    The sort is stable, so equal-score turns keep their input order, and a turn
    with no score is treated as 0.0.

    Args:
        conversations: Candidate turns to order.
        relevance_scores: Map of turn_id to combined relevance score (0.0-1.0).

    Returns:
        A new list ordered by descending relevance score.
    """
    return sorted(
        conversations,
        key=lambda c: relevance_scores.get(c.turn_id, 0.0),
        reverse=True,
    )


def _select_rerank_candidates(
    conversations: Sequence[TurnNode],
    vector_scores: dict[str, float],
    input_cap: int,
) -> list[int]:
    """Select indices of the top-N candidates by vector score for reranking (FRE-672).

    The cross-attention reranker cross-attends over every document it is sent, so its
    latency scales with the candidate count (FRE-656: ~4.4 s over the 500-turn set).
    Most candidates are low-vector-score distractors the reranker will not promote.
    This bounds the reranker input to the ``input_cap`` candidates with the highest
    vector score — where cross-attention adjudication adds value — and lets the rest
    pass through on their existing vector+recency score (they receive no reranker
    score, so ``_calculate_relevance_scores`` scores them on the non-reranker path).

    A conversation's vector score is the max cosine similarity across its
    ``key_entities`` (0.0 if none matched the vector query), mirroring
    ``_calculate_relevance_scores``. The sort is stable, so equal-score turns keep
    their input (candidate-query) order.

    Args:
        conversations: Candidate turns, in candidate-query order.
        vector_scores: Map of entity name to cosine similarity from the vector query.
        input_cap: Max number of candidate indices to return.

    Returns:
        Indices into ``conversations`` of the top-``input_cap`` candidates by vector
        score, in descending score order.
    """

    def _conv_vector_score(conv: TurnNode) -> float:
        hits = [vector_scores[e] for e in conv.key_entities if e in vector_scores]
        return max(hits) if hits else 0.0

    ranked = sorted(
        range(len(conversations)),
        key=lambda i: _conv_vector_score(conversations[i]),
        reverse=True,
    )
    return ranked[:input_cap]


def _filter_entities_by_floor(
    vector_results: Sequence[dict[str, Any]],
    floor: float,
) -> tuple[list[str], dict[str, float]]:
    """Keep only vector-matched entities at or above the similarity floor (ADR-0100).

    The floor is the safety gate that replaces the recency filter on the
    vector-expanded branch: entities whose cosine similarity is below it are
    dropped before turn expansion, so a relevance-keyed candidate set does not
    admit junk (AC-4). Malformed rows (missing name or score) are skipped.

    Args:
        vector_results: Rows from the entity_embedding vector query, each with
            ``name`` and ``score`` keys.
        floor: Minimum cosine similarity (0.0 = no floor).

    Returns:
        Tuple of (entity names at/above the floor, name->score map).
    """
    names: list[str] = []
    scores: dict[str, float] = {}
    for row in vector_results:
        name = row.get("name")
        score = row.get("score")
        if name is None or score is None:
            continue
        if float(score) >= floor:
            names.append(name)
            scores[name] = float(score)
    return names, scores


def _hard_recency_cutoff_iso(query: MemoryQuery) -> str | None:
    """ISO cutoff for an explicit hard recency window, or None (FRE-658).

    Used by the de-gated relevance-bounded path in ``query_memory`` to re-apply a
    hard ``c.timestamp >= cutoff`` bound when the caller supplied an explicit time
    window (``hard_recency_days``). The format matches the **naive**
    ``datetime.utcnow().isoformat()`` the legacy cutoff uses, so the string
    comparison against stored turn timestamps is byte-consistent. Returns ``None``
    when no explicit window is set (the automatic path), preserving ADR-0100 AC-1a
    invariance to ``recency_days``.

    Args:
        query: The memory query; only ``hard_recency_days`` is consulted.

    Returns:
        A naive ISO-8601 cutoff string, or ``None`` when no hard window applies.
    """
    if not query.hard_recency_days:
        return None
    return (datetime.utcnow() - timedelta(days=query.hard_recency_days)).isoformat()


def _filter_turns_by_hard_recency(
    turns: Sequence[TurnNode], hard_recency_days: int | None
) -> list[TurnNode]:
    """Drop turns older than an explicit hard recency window (FRE-658).

    The de-gated multi-path recall path (ADR-0104 / FRE-724) generates candidates
    by relevance and takes no recency predicate, so an explicit caller-supplied
    time window is enforced here as a hard **post-recall** filter (the fused arms
    would otherwise return all-time matches for a time-scoped query). A falsy
    ``hard_recency_days`` (the automatic path) is a no-op, so ADR-0100 AC-1a
    invariance is preserved. Naive turn timestamps are treated as UTC to avoid an
    aware/naive comparison error.

    Args:
        turns: Resolved turns from the fused recall set.
        hard_recency_days: Explicit hard window in days, or None/0 for no window.

    Returns:
        The turns within the window, in input order (all turns when no window).
    """
    if not hard_recency_days:
        return list(turns)
    cutoff = datetime.now(timezone.utc) - timedelta(days=hard_recency_days)
    kept: list[TurnNode] = []
    for turn in turns:
        ts = turn.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept.append(turn)
    return kept


def _build_memory_recall_event(
    returned: Sequence[TurnNode],
    candidate_set_size: int,
    vector_scores: dict[str, float],
    vector_entity_count: int,
    recall_latency_ms: float,
    similarity_floor: float,
    relevance_bounded_enabled: bool,
) -> dict[str, Any]:
    """Assemble the ``memory_recall`` telemetry event payload (ADR-0100).

    Mirrors the FRE-435 harness metrics into the live recall path so recall
    regressions — chiefly the "no prior discussions" false negative — are
    visible in production. The ``empty_result`` flag is derived from the actual
    returned payload (AC-6), not asserted independently.

    Args:
        returned: The turns actually returned after ranking and limiting.
        candidate_set_size: Number of candidate turns before ranking/limit.
        vector_scores: Map of entity name to cosine score from the vector query.
        vector_entity_count: Number of entities that passed the similarity floor.
        recall_latency_ms: Wall-clock latency of the recall, in milliseconds.
        similarity_floor: The similarity floor in effect for this recall.
        relevance_bounded_enabled: Whether the relevance-bounded flag was on.

    Returns:
        A dict of event fields for ``log.info("memory_recall", ...)``.
    """
    scores = list(vector_scores.values())
    top_vector_score = max(scores) if scores else 0.0
    median_vector_score = statistics.median(scores) if scores else 0.0

    recency_span_seconds = 0.0
    if len(returned) > 1:
        timestamps = [
            c.timestamp.astimezone(timezone.utc).replace(tzinfo=None)
            if c.timestamp.tzinfo is not None
            else c.timestamp
            for c in returned
        ]
        recency_span_seconds = (max(timestamps) - min(timestamps)).total_seconds()

    recalled_token_count = sum(estimate_tokens(c.summary or c.user_message or "") for c in returned)

    return {
        "candidate_set_size": candidate_set_size,
        "result_count": len(returned),
        "empty_result": len(returned) == 0,
        "top_vector_score": round(top_vector_score, 4),
        "median_vector_score": round(median_vector_score, 4),
        "vector_entity_count": vector_entity_count,
        "recency_span_seconds": round(recency_span_seconds, 3),
        "recall_latency_ms": round(recall_latency_ms, 3),
        "recalled_token_count": recalled_token_count,
        "similarity_floor": round(similarity_floor, 4),
        "relevance_bounded_enabled": relevance_bounded_enabled,
    }


class MemoryService:
    """Neo4j-based memory service for persistent knowledge graph.

    Usage:
        service = MemoryService()
        await service.connect()
        await service.create_conversation(conversation_node, user_id=user_id)
        results = await service.query_memory(MemoryQuery(entity_names=["France"]))
        await service.disconnect()
    """

    def __init__(self) -> None:  # noqa: D107
        """Initialize memory service with Neo4j connection settings."""
        self.driver: Any | None = None
        self.connected = False
        self._query_feedback_by_key: dict[str, dict[str, Any]] = {}

    async def connect(self) -> bool:
        """Connect to Neo4j database.

        Returns:
            True if connected successfully, False otherwise
        """
        if Neo4jAsyncGraphDatabase is None:
            log.error("neo4j_dependency_missing")
            self.connected = False
            return False

        try:
            uri = settings.neo4j_uri
            user = settings.neo4j_user
            password = settings.neo4j_password

            if (
                settings.environment == Environment.TEST
                and is_prod_neo4j_uri(uri)
                and not settings.allow_test_writes_to_prod_substrate
            ):
                _parsed = urlparse(uri)
                log.error(
                    "memory_service_refused_prod_uri_in_test_env",
                    uri_host=_parsed.hostname,
                    uri_port=_parsed.port,
                    hint=(
                        "Set AGENT_NEO4J_URI=bolt://localhost:7688 (test stack) "
                        "or AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1 to bypass."
                    ),
                )
                self.connected = False
                return False

            self.driver = Neo4jAsyncGraphDatabase.driver(uri, auth=(user, password))
            await self.driver.verify_connectivity()
            self.connected = True
            log.info("neo4j_connected", uri=uri)
            return True
        except Exception as e:
            log.error("neo4j_connection_failed", error=str(e), exc_info=True)
            self.connected = False
            return False

    async def disconnect(self) -> None:
        """Close Neo4j connection."""
        if self.driver:
            await self.driver.close()
            self.driver = None
            self.connected = False
            log.info("neo4j_disconnected")

    async def turn_exists(self, turn_id: str, trace_id: str | None = None) -> bool:
        """Check if a Turn node already exists (i.e. already consolidated).

        Args:
            turn_id: Turn ID (equals trace_id for the originating request).
            trace_id: Optional request trace identifier for log correlation
                (ADR-0074 §I3). Defaults to ``turn_id`` when omitted because
                Turn IDs are themselves trace IDs for consolidation flows.

        Returns:
            True if a Turn node with this id exists, False otherwise.
        """
        effective_trace_id = trace_id if trace_id is not None else turn_id
        if not self.connected or not self.driver:
            return False
        try:
            async with self.driver.session() as session:
                result = await session.run(
                    "MATCH (t:Turn {turn_id: $turn_id}) RETURN t LIMIT 1",
                    turn_id=turn_id,
                )
                record = await result.single()
                return record is not None
        except Exception as e:
            log.warning("turn_exists_check_failed", error=str(e), trace_id=effective_trace_id)
            return False

    async def conversation_exists(self, conversation_id: str) -> bool:
        """Backward-compatible alias for turn_exists.

        Args:
            conversation_id: Conversation/turn ID (trace_id).

        Returns:
            True if the Turn node exists.
        """
        return await self.turn_exists(conversation_id)

    async def fetch_session_discussed_entity_names(
        self,
        session_id: str,
        user_id: UUID | None = None,
        authenticated: bool = False,
        trace_id: str | None = None,
    ) -> list[str]:
        """Return distinct Entity names linked to turns in the given session.

        Args:
            session_id: Session identifier (matches ``Turn.session_id``).
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity (FRE-229).
            trace_id: Optional request trace identifier for log correlation
                (ADR-0074 §I3).

        Returns:
            Sorted entity names; empty if unavailable or on error.
        """
        if not session_id or not self.connected or not self.driver:
            return []
        vis_frag, vis_params = _build_visibility_filter("t", user_id, authenticated)
        try:
            async with self.driver.session() as session:
                result = await session.run(
                    f"""
                    MATCH (t:Turn {{session_id: $session_id}})-[:DISCUSSES]->(e:Entity)
                    WHERE {vis_frag}
                    RETURN collect(DISTINCT e.name) AS names
                    """,
                    session_id=session_id,
                    **vis_params,
                )
                rec = await result.single()
                if not rec:
                    return []
                raw = rec.get("names") or []
                return sorted({str(n) for n in raw if n})
        except Exception as e:
            log.warning(
                "fetch_session_discussed_entity_names_failed",
                session_id=session_id,
                error=str(e),
                trace_id=trace_id,
            )
            return []

    async def suggest_proactive_raw(
        self,
        query_embedding: list[float],
        current_session_id: str,
        trace_id: str,
        user_id: UUID | None = None,
        authenticated: bool = False,
        query_text: str | None = None,
    ) -> list[dict[str, Any]]:
        """Vector entity retrieval plus best cross-session turn per entity (ADR-0039).

        Args:
            query_embedding: Query embedding (zero vector yields no rows).
            current_session_id: Exclude turns from this session.
            trace_id: For error logging.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity (FRE-229).
            query_text: Original query text (FRE-724). When provided and
                ``multipath_recall_enabled`` is on, the proactive candidate set is
                broadened with the lexical arm's entity hits (multi-path candidacy),
                entering at the noise-guard baseline score. Proactive keeps its own
                cosine scoring and min-score/budget gates — it is deliberately NOT
                run through the rerank/operating point (proactive is not the AC-5
                "no prior discussions" surface). None / flag off = unchanged.

        Returns:
            Row dicts for :func:`personal_agent.memory.proactive.build_proactive_suggestions`.
        """
        cfg = get_settings()
        top_k = cfg.proactive_memory_vector_top_k
        if not self.connected or not self.driver:
            return []
        if not query_embedding or not any(x != 0.0 for x in query_embedding):
            return []
        sid = current_session_id or ""
        node_vis_frag, vis_params = _build_visibility_filter("node", user_id, authenticated)
        turn_vis_frag, _ = _build_visibility_filter("t", user_id, authenticated)
        try:
            async with self.driver.session() as session:
                result = await session.run(
                    f"""
                    CALL db.index.vector.queryNodes('entity_embedding', $top_k, $embedding)
                    YIELD node, score
                    WITH node, score
                    WHERE {node_vis_frag}
                    ORDER BY score DESC
                    LIMIT $top_k
                    OPTIONAL MATCH (node)<-[:DISCUSSES]-(t:Turn)
                    WHERE (t IS NULL OR coalesce(t.session_id, '') <> $current_session)
                      AND (t IS NULL OR {turn_vis_frag})
                    WITH node, score, t
                    ORDER BY node, t.timestamp DESC
                    WITH node, score, collect(t)[0] AS t
                    RETURN node.name AS name,
                           node.entity_type AS entity_type,
                           node.description AS description,
                           score AS vector_score,
                           t.turn_id AS turn_id,
                           t.session_id AS session_id,
                           t.timestamp AS timestamp,
                           t.user_message AS user_message,
                           t.summary AS summary,
                           t.key_entities AS key_entities
                    """,
                    top_k=top_k,
                    embedding=query_embedding,
                    current_session=sid,
                    **vis_params,
                )
                rows = await result.data()
        except Exception as e:
            log.warning(
                "suggest_proactive_raw_failed",
                trace_id=trace_id,
                error=str(e),
            )
            return []

        out: list[dict[str, Any]] = []
        for row in rows:
            ts = row.get("timestamp")
            ts_iso: str | None
            if ts is None:
                ts_iso = None
            elif isinstance(ts, datetime):
                ts_iso = ts.isoformat()
            else:
                ts_iso = str(ts)

            out.append(
                {
                    "name": row.get("name"),
                    "entity_type": row.get("entity_type"),
                    "description": row.get("description"),
                    "vector_score": float(row.get("vector_score") or 0.0),
                    "turn_id": row.get("turn_id"),
                    "session_id": row.get("session_id"),
                    "timestamp_iso": ts_iso,
                    "user_message": row.get("user_message"),
                    "summary": row.get("summary"),
                    "key_entities": list(row.get("key_entities") or []),
                    "mention_count": 0,
                }
            )

        if query_text and cfg.multipath_recall_enabled:
            out = await self._augment_proactive_with_lexical(
                out,
                query_text,
                current_session_id=sid,
                trace_id=trace_id,
                user_id=user_id,
                authenticated=authenticated,
            )
        return out

    async def _augment_proactive_with_lexical(
        self,
        rows: list[dict[str, Any]],
        query_text: str,
        *,
        current_session_id: str,
        trace_id: str,
        user_id: UUID | None,
        authenticated: bool,
    ) -> list[dict[str, Any]]:
        """Broaden proactive candidacy with the lexical arm's entity hits (FRE-724).

        Multi-path candidacy for the proactive path: lexical-arm entity hits not
        already surfaced by the dense vector query are appended, resolved to the
        proactive row shape (best cross-session turn), entering at the noise-guard
        baseline score (``recall_similarity_floor``) so they remain subject to
        proactive's own min-score/budget gates. Turn-kind lexical hits are ignored
        here (proactive is entity-centric). Fails open to the input rows.

        Args:
            rows: The dense-arm proactive rows already assembled.
            query_text: The recall query for the lexical arm.
            current_session_id: Session to exclude from cross-session turns.
            trace_id: Request trace id for event correlation.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity.

        Returns:
            The rows augmented with lexical-only entity candidates.
        """
        try:
            lexical = await self.lexical_recall_arm(
                query_text,
                trace_id=trace_id,
                user_id=user_id,
                authenticated=authenticated,
            )
        except Exception as exc:
            log.warning("proactive_lexical_augment_failed", error=str(exc), trace_id=trace_id)
            return rows
        entity_ids = [r.item_id for r in lexical if r.kind == "entity"]
        if not entity_ids or not self.driver:
            return rows

        existing_names = {r.get("name") for r in rows}
        baseline = get_settings().recall_similarity_floor
        node_vis, vis_params = _build_visibility_filter("e", user_id, authenticated)
        turn_vis, _ = _build_visibility_filter("t", user_id, authenticated)
        try:
            async with self.driver.session() as session:
                result = await session.run(
                    f"""
                    UNWIND $ids AS eid
                    MATCH (e:Entity) WHERE elementId(e) = eid AND {node_vis}
                    OPTIONAL MATCH (e)<-[:DISCUSSES]-(t:Turn)
                    WHERE (t IS NULL OR coalesce(t.session_id, '') <> $current_session)
                      AND (t IS NULL OR {turn_vis})
                    WITH e, t ORDER BY t.timestamp DESC
                    WITH e, collect(t)[0] AS t
                    RETURN e.name AS name, e.entity_type AS entity_type,
                           e.description AS description,
                           t.turn_id AS turn_id, t.session_id AS session_id,
                           t.timestamp AS timestamp, t.user_message AS user_message,
                           t.summary AS summary, t.key_entities AS key_entities
                    """,
                    ids=entity_ids,
                    current_session=current_session_id,
                    **vis_params,
                )
                lex_rows = await result.data()
        except Exception as exc:
            log.warning("proactive_lexical_resolve_failed", error=str(exc), trace_id=trace_id)
            return rows

        for row in lex_rows:
            name = row.get("name")
            if not name or name in existing_names:
                continue
            existing_names.add(name)
            ts = row.get("timestamp")
            ts_iso = ts.isoformat() if isinstance(ts, datetime) else (str(ts) if ts else None)
            rows.append(
                {
                    "name": name,
                    "entity_type": row.get("entity_type"),
                    "description": row.get("description"),
                    "vector_score": float(baseline),
                    "turn_id": row.get("turn_id"),
                    "session_id": row.get("session_id"),
                    "timestamp_iso": ts_iso,
                    "user_message": row.get("user_message"),
                    "summary": row.get("summary"),
                    "key_entities": list(row.get("key_entities") or []),
                    "mention_count": 0,
                }
            )
        return rows

    async def create_conversation(
        self,
        conversation: TurnNode,
        user_id: UUID | None = None,
        visibility: str = "public",
    ) -> bool:
        """Create a Turn node in the graph and (optionally) link the participating user.

        Args:
            conversation: Turn node to create (accepts TurnNode or legacy ConversationNode).
            user_id: UUID of the connected user. When provided, MERGEs a
                (:Person {user_id})-[:PARTICIPATED_IN]->(:Turn) edge per FRE-343.
                MATCH (not MERGE) on :Person — chosen to avoid silently creating
                a name-less :Person on every Turn. If the :Person is missing,
                the MERGE writes no edge (Turn itself is still created); that is
                a logic bug worth investigating. The production consolidator
                path always provides user_id (TaskCapture invariant); other
                callers (store_episode) pass it from their own context.
            visibility: Visibility scope for the Turn node (FRE-229). Defaults
                to "public" for backward compatibility; callers should pass
                "group" for authenticated sessions.

        Returns:
            True if successful, False otherwise.
        """
        # Support both TurnNode (turn_id) and legacy ConversationNode (conversation_id)
        turn_id = getattr(conversation, "turn_id", None) or getattr(
            conversation, "conversation_id", None
        )
        if not turn_id:
            log.warning(
                "create_conversation_missing_id",
                trace_id=getattr(conversation, "trace_id", None),
            )
            return False

        if not self.connected or not self.driver:
            log.warning(
                "neo4j_not_connected",
                trace_id=getattr(conversation, "trace_id", None),
            )
            return False

        try:
            async with self.driver.session() as session:
                await session.run(
                    """
                    MERGE (t:Turn {turn_id: $turn_id})
                    SET t.trace_id = $trace_id,
                        t.session_id = $session_id,
                        t.sequence_number = $sequence_number,
                        t.timestamp = $timestamp,
                        t.summary = $summary,
                        t.user_message = $user_message,
                        t.assistant_response = $assistant_response,
                        t.key_entities = $key_entities,
                        t.properties = $properties,
                        t.visibility = $visibility,
                        t.originating_trace_id = $originating_trace_id,
                        t.originating_session_id = $originating_session_id
                    """,
                    turn_id=turn_id,
                    trace_id=conversation.trace_id,
                    session_id=conversation.session_id,
                    sequence_number=getattr(conversation, "sequence_number", 0),
                    timestamp=conversation.timestamp.isoformat(),
                    summary=conversation.summary,
                    user_message=conversation.user_message,
                    assistant_response=conversation.assistant_response,
                    key_entities=conversation.key_entities,
                    properties=orjson.dumps(conversation.properties).decode(),
                    visibility=visibility,
                    originating_trace_id=conversation.trace_id,
                    originating_session_id=conversation.session_id,
                )

                # FRE-343: provenance edge linking the user to this Turn.
                # MATCH (not MERGE) on :Person — the node must exist
                # (get_or_provision_user_person bootstraps it on first auth request).
                if user_id is not None:
                    await session.run(
                        """
                        MATCH (p:Person {user_id: $user_id})
                        MATCH (t:Turn {turn_id: $turn_id})
                        MERGE (p)-[r:PARTICIPATED_IN]->(t)
                        ON CREATE SET r.created_at = $timestamp
                        """,
                        user_id=str(user_id),
                        turn_id=turn_id,
                        timestamp=conversation.timestamp.isoformat(),
                    )
                    log.info(
                        "participated_in_edge_written",
                        turn_id=turn_id,
                        user_id=str(user_id),
                        trace_id=conversation.trace_id,
                        was_backfilled=False,
                    )

                # Create Turn→Entity DISCUSSES edges.
                # entity_types_map lets us set entity_type on the node when we know it;
                # falls back to preserving any existing type if unknown.
                entity_types_map: dict[str, str] = {}
                for entity_data in getattr(conversation, "_entity_data", []):
                    if isinstance(entity_data, dict) and entity_data.get("name"):
                        entity_types_map[entity_data["name"]] = entity_data.get("type", "")

                for entity_name in conversation.key_entities:
                    entity_type = entity_types_map.get(entity_name, "")
                    await session.run(
                        """
                        MERGE (e:Entity {name: $name})
                        ON CREATE SET e.visibility = $visibility,
                                      e.originating_trace_id = $originating_trace_id,
                                      e.originating_session_id = $originating_session_id
                        SET e.last_seen = $timestamp,
                            e.mention_count = COALESCE(e.mention_count, 0) + 1,
                            e.first_seen = COALESCE(e.first_seen, $timestamp),
                            e.entity_type = CASE WHEN $entity_type <> '' THEN $entity_type
                                                 ELSE COALESCE(e.entity_type, '') END
                        WITH e
                        MATCH (t:Turn {turn_id: $turn_id})
                        MERGE (t)-[:DISCUSSES]->(e)
                        """,
                        name=entity_name,
                        entity_type=entity_type,
                        timestamp=conversation.timestamp.isoformat(),
                        turn_id=turn_id,
                        visibility=visibility,
                        originating_trace_id=conversation.trace_id,
                        originating_session_id=conversation.session_id,
                    )

                log.info(
                    "turn_created",
                    turn_id=turn_id,
                    session_id=conversation.session_id,
                    entity_count=len(conversation.key_entities),
                    trace_id=conversation.trace_id,
                )
                return True
        except Exception as e:
            log.error(
                "turn_creation_failed",
                error=str(e),
                exc_info=True,
                trace_id=getattr(conversation, "trace_id", None),
            )
            return False

    async def create_session(self, session_node: SessionNode, trace_id: str | None = None) -> bool:
        """Create or update a Session node in the graph.

        Args:
            session_node: Session to create or update.
            trace_id: Optional request trace identifier for log correlation
                (ADR-0074 §I3). Callers should pass the consolidation/request
                trace_id when available; ``None`` is acceptable for batch flows.

        Returns:
            True if successful, False otherwise.
        """
        if not self.connected or not self.driver:
            log.warning(
                "neo4j_not_connected",
                trace_id=trace_id,
                session_id=session_node.session_id,
            )
            return False

        try:
            async with self.driver.session() as db_session:
                await db_session.run(
                    """
                    MERGE (s:Session {session_id: $session_id})
                    SET s.started_at = $started_at,
                        s.ended_at = $ended_at,
                        s.turn_count = $turn_count,
                        s.dominant_entities = $dominant_entities,
                        s.session_summary = $session_summary
                    """,
                    session_id=session_node.session_id,
                    started_at=session_node.started_at.isoformat(),
                    ended_at=session_node.ended_at.isoformat(),
                    turn_count=session_node.turn_count,
                    dominant_entities=session_node.dominant_entities,
                    session_summary=session_node.session_summary,
                )
                log.info(
                    "session_created",
                    session_id=session_node.session_id,
                    turn_count=session_node.turn_count,
                    trace_id=trace_id,
                )
                return True
        except Exception as e:
            log.error(
                "session_creation_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
                session_id=session_node.session_id,
            )
            return False

    async def link_session_turns(self, session_id: str, trace_id: str | None = None) -> int:
        """Wire all Turn nodes for a session into an ordered sequence.

        Creates:
        - (Session)-[:CONTAINS {sequence}]->(Turn) for every turn
        - (Turn)-[:NEXT]->(Turn) chain ordered by timestamp
        - (Session)-[:DISCUSSES]->(Entity) aggregated from all turns

        Args:
            session_id: Session ID to link.
            trace_id: Optional request trace identifier for log correlation
                (ADR-0074 §I3).

        Returns:
            Number of turns linked.
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected", trace_id=trace_id, session_id=session_id)
            return 0

        try:
            async with self.driver.session() as db_session:
                # CONTAINS + sequence_number update (ordered by timestamp)
                await db_session.run(
                    """
                    MATCH (s:Session {session_id: $session_id})
                    MATCH (t:Turn {session_id: $session_id})
                    WITH s, t ORDER BY t.timestamp ASC
                    WITH s, collect(t) AS turns
                    UNWIND range(0, size(turns)-1) AS idx
                    WITH s, turns[idx] AS t, idx+1 AS seq
                    SET t.sequence_number = seq
                    MERGE (s)-[:CONTAINS {sequence: seq}]->(t)
                    """,
                    session_id=session_id,
                )

                # NEXT chain between consecutive turns
                await db_session.run(
                    """
                    MATCH (t:Turn {session_id: $session_id})
                    WITH t ORDER BY t.timestamp ASC
                    WITH collect(t) AS turns
                    UNWIND range(0, size(turns)-2) AS idx
                    WITH turns[idx] AS t1, turns[idx+1] AS t2
                    MERGE (t1)-[:NEXT]->(t2)
                    """,
                    session_id=session_id,
                )

                # Session DISCUSSES entities — aggregate from all turns
                await db_session.run(
                    """
                    MATCH (s:Session {session_id: $session_id})
                    MATCH (t:Turn {session_id: $session_id})-[:DISCUSSES]->(e:Entity)
                    WITH s, e, count(t) AS turn_count
                    MERGE (s)-[r:DISCUSSES]->(e)
                    SET r.turn_count = turn_count
                    """,
                    session_id=session_id,
                )

                # Count linked turns
                result = await db_session.run(
                    "MATCH (:Session {session_id: $session_id})-[:CONTAINS]->(t:Turn) RETURN count(t) AS cnt",
                    session_id=session_id,
                )
                record = await result.single()
                count: int = record["cnt"] if record else 0
                log.info(
                    "session_turns_linked",
                    session_id=session_id,
                    turn_count=count,
                    trace_id=trace_id,
                )
                return count
        except Exception as e:
            log.error(
                "link_session_turns_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
                session_id=session_id,
            )
            return 0

    async def create_entity(
        self,
        entity: Entity,
        visibility: str = "public",
        originating_trace_id: str | None = None,
        originating_session_id: str | None = None,
        extractor_model: str | None = None,
        description_confidence: float = _DEFAULT_DESCRIPTION_CONFIDENCE,
        eval_mode: bool = False,
        description_update_kind: str = _DEFAULT_DESCRIPTION_UPDATE_KIND,
    ) -> str:
        """Create or update an entity node with dedup and optional embedding.

        The World-fact ``description`` is a **living** value (ADR-0098 D2 / FRE-711):
        first-write-wins is retired for it. A later extraction with a *strictly higher*
        confidence corrects it (the prior value is archived to a ``HAD_DESCRIPTION`` ->
        ``EntityDescriptionVersion`` node, never deleted); an ``eval_mode`` write never
        overwrites a non-eval description (this replaces the FRE-375 freeze as the
        anti-test-overwrite guard). ``entity_type``/``properties`` stay first-write-wins.

        Args:
            entity: Entity to create.
            visibility: Visibility scope for the Entity node (FRE-229). Uses ON CREATE SET
                semantics — an existing entity's visibility is never overwritten on merge,
                preserving first-write semantics.
            originating_trace_id: Trace identifier of the request that produced this
                entity (ADR-0074 §I5). Written as ``e.originating_trace_id`` on first
                creation — preserved on subsequent merges to keep first-write semantics.
            originating_session_id: Session identifier of the originating request
                (ADR-0074 §I5). Written as ``e.originating_session_id`` on first creation.
            extractor_model: Identifier of the LLM that produced this entity's
                description / extraction (ADR-0074 §I5). ``None`` for user-provided
                facts (gateway ``store_fact`` path); set for entity-extraction outputs.
            description_confidence: Confidence of this write's description; a correction
                lands only when it is *strictly greater* than the stored one (FRE-711).
            eval_mode: Whether this write originates from eval/test traffic; an eval write
                never overwrites a non-eval description (FRE-711, preserving FRE-375).
            description_update_kind: The extractor's per-entity description signal (FRE-725) —
                ``"enrichment"``/``"correction"`` unlock a correction at *equal* confidence
                (which FRE-711's strict ``>`` gate would otherwise block), still archiving the
                prior value and still eval-gated; ``"enrichment"`` may only land if it does not
                shrink the description. ``"new"`` (the default) keeps the strict ``>`` behaviour.
                Off-vocabulary/``None`` values are coerced to ``"new"`` here (the signal is
                write-authorizing, so validation is server-side, not caller-trusted).

        Returns:
            Entity ID (name-based, may be canonical name if deduplicated).
        """
        if not self.connected or not self.driver:
            log.warning(
                "neo4j_not_connected",
                trace_id=originating_trace_id,
                session_id=originating_session_id,
            )
            return ""

        try:
            # Generate embedding if not provided
            embedding = entity.embedding
            if embedding is None and entity.description:
                embed_text = f"{entity.name}: {entity.description}"
                embedding = await generate_embedding(embed_text)

            # Single session for dedup check + MERGE write (atomicity)
            effective_name = entity.name
            async with self.driver.session() as session:
                # Dedup check
                if embedding and any(x != 0.0 for x in embedding):
                    from personal_agent.memory.dedup import (  # noqa: PLC0415
                        DedupDecision,
                        check_entity_duplicate,
                    )

                    dedup_result = await check_entity_duplicate(
                        name=entity.name,
                        entity_type=entity.entity_type,
                        embedding=embedding,
                        neo4j_session=session,
                    )
                    if (
                        dedup_result.decision == DedupDecision.MERGE_EXISTING
                        and dedup_result.canonical_name
                    ):
                        effective_name = dedup_result.canonical_name
                        log.info(
                            "entity_deduplicated",
                            original_name=entity.name,
                            canonical_name=effective_name,
                            similarity=dedup_result.similarity_score,
                            trace_id=originating_trace_id,
                        )

                # MERGE using effective_name
                set_clauses = [
                    "e.entity_id = COALESCE(e.entity_id, $entity_id)",
                    # entity_type: first-write-wins (FRE-375 — prevent test overwrites)
                    "e.entity_type = CASE WHEN e.entity_type IS NULL OR e.entity_type = '' THEN $entity_type ELSE e.entity_type END",
                    # properties: first-write-wins (FRE-375 — prevent test overwrites)
                    "e.properties = CASE WHEN e.properties IS NULL OR e.properties = '{}' THEN $properties ELSE e.properties END",
                    # description: living value (FRE-711) — apply the gated correction
                    # computed in the WITH block above; ON CREATE sets the first value.
                    "e.description = CASE WHEN _do_correct OR _do_fill THEN $description ELSE e.description END",
                    "e.description_confidence = CASE WHEN _do_correct OR _do_fill THEN $description_confidence ELSE e.description_confidence END",
                    "e.description_eval_mode = CASE WHEN _do_correct OR _do_fill THEN $eval_mode ELSE e.description_eval_mode END",
                    "e.description_set_at = CASE WHEN _do_correct OR _do_fill THEN datetime() ELSE e.description_set_at END",
                    "e.last_seen = datetime()",
                    "e.mention_count = COALESCE(e.mention_count, 0) + 1",
                    "e.first_seen = COALESCE(e.first_seen, datetime())",
                    # Access tracking (FRE-161: KG Freshness)
                    "e.first_accessed_at = COALESCE(e.first_accessed_at, datetime())",
                    "e.last_accessed_at = datetime()",
                    "e.access_count = COALESCE(e.access_count, 0)",
                    "e.last_access_context = COALESCE(e.last_access_context, 'created')",
                ]
                params: dict[str, Any] = {
                    "name": effective_name,
                    "entity_id": effective_name,
                    "entity_type": entity.entity_type,
                    "description": entity.description,
                    "properties": orjson.dumps(entity.properties).decode(),
                    # FRE-711 living-description gate inputs.
                    "description_confidence": description_confidence,
                    "eval_mode": eval_mode,
                    "default_description_confidence": _DEFAULT_DESCRIPTION_CONFIDENCE,
                    "proposed_name": entity.name,
                    "description_source_trace_id": originating_trace_id,
                    # FRE-725 equal-confidence enrichment/correction signal. Validated here
                    # (write-authorizing) so an off-vocabulary/None kind from any caller is
                    # coerced to "new" before it can reach the correction gate.
                    "description_update_kind": (
                        description_update_kind
                        if description_update_kind in _VALID_DESCRIPTION_UPDATE_KINDS
                        else _DEFAULT_DESCRIPTION_UPDATE_KIND
                    ),
                    "explicit_description_update_kinds": list(_EXPLICIT_DESCRIPTION_UPDATE_KINDS),
                }

                # FRE-659: never persist a zero-vector embedding. When the embedder is
                # unreachable ``generate_embedding`` degrades to a zero vector; writing it
                # bakes an unrecallable node into the ``entity_embedding`` index. Persist
                # only a real vector — a missing embedding is repaired by the periodic
                # backfill (``backfill_missing_embeddings``) once the embedder returns.
                if embedding is not None and any(x != 0.0 for x in embedding):
                    set_clauses.append("e.embedding = $embedding")
                    params["embedding"] = embedding

                if entity.coordinates is not None:
                    set_clauses.append(
                        "e.location = point({latitude: $latitude, longitude: $longitude})"
                    )
                    params["latitude"] = entity.coordinates[0]
                    params["longitude"] = entity.coordinates[1]

                if entity.geocoded:
                    set_clauses.append("e.geocoded = $geocoded")
                    params["geocoded"] = entity.geocoded

                params["visibility"] = visibility
                # ADR-0074 §I5: origination written ON CREATE SET so first-write
                # semantics preserve the originating request even across merges.
                # FRE-711: the first description write also stamps its confidence/eval
                # provenance here (ON CREATE); later corrections flow through the SET gate.
                on_create_clauses = [
                    "e.visibility = $visibility",
                    "e.description = $description",
                    "e.description_confidence = $description_confidence",
                    "e.description_eval_mode = $eval_mode",
                    "e.description_set_at = datetime()",
                ]
                if originating_trace_id is not None:
                    on_create_clauses.append("e.originating_trace_id = $originating_trace_id")
                    params["originating_trace_id"] = originating_trace_id
                if originating_session_id is not None:
                    on_create_clauses.append("e.originating_session_id = $originating_session_id")
                    params["originating_session_id"] = originating_session_id
                if extractor_model is not None:
                    on_create_clauses.append("e.extractor_model = $extractor_model")
                    params["extractor_model"] = extractor_model
                # FRE-711: the description-correction gate is evaluated inside the MERGE
                # against the freshly-matched node (no app-side stale read → race-safe:
                # two concurrent consolidations cannot double-archive), then the old value
                # is archived to a HAD_DESCRIPTION history node BEFORE the SET overwrites it.
                query = (
                    "MERGE (e:Entity {name: $name})\n"
                    "ON CREATE SET " + ",\n    ".join(on_create_clauses) + "\n"
                    "WITH e, e.description AS _old_desc, e.description_confidence AS _old_conf,\n"
                    "     e.description_eval_mode AS _old_eval, e.description_set_at AS _old_set_at\n"
                    "WITH e, _old_desc, _old_conf, _old_eval, _old_set_at,\n"
                    "     ($description <> '' AND (_old_desc IS NULL OR _old_desc = '')) AS _do_fill,\n"
                    "     ($description <> '' AND _old_desc IS NOT NULL AND _old_desc <> ''\n"
                    "      AND $description <> _old_desc\n"
                    "      AND NOT ($eval_mode AND coalesce(_old_eval, false) = false)\n"
                    # FRE-711 strict-'>' arm OR FRE-725 equal-confidence signal arm. The
                    # signal arm needs an explicit enrichment/correction kind at >= confidence
                    # (never a downgrade); an 'enrichment' must additionally not shrink the
                    # description, so it can only add information — 'correction' is length-free.
                    "      AND ($description_confidence > coalesce(_old_conf, $default_description_confidence)\n"
                    "           OR ($description_update_kind IN $explicit_description_update_kinds\n"
                    "               AND $description_confidence >= coalesce(_old_conf, $default_description_confidence)\n"
                    "               AND ($description_update_kind = 'correction' OR size($description) >= size(_old_desc))))\n"
                    "     ) AS _do_correct\n"
                    "FOREACH (_ IN CASE WHEN _do_correct THEN [1] ELSE [] END |\n"
                    "    CREATE (e)-[:HAD_DESCRIPTION]->(:EntityDescriptionVersion {\n"
                    "        text: _old_desc, confidence: _old_conf,\n"
                    "        eval_mode: coalesce(_old_eval, false),\n"
                    "        valid_from: _old_set_at, valid_to: datetime(),\n"
                    "        source_trace_id: $description_source_trace_id, proposed_name: $proposed_name\n"
                    "    })\n"
                    ")\n"
                    "SET " + ",\n    ".join(set_clauses) + "\n"
                    "RETURN e.name as entity_id"
                )

                result = await session.run(query, **params)
                record = await result.single()
                entity_id: str = record["entity_id"] if record else effective_name
                log.info(
                    "entity_created",
                    entity_id=entity_id,
                    entity_type=entity.entity_type,
                    trace_id=originating_trace_id,
                )
                return entity_id
        except Exception as e:
            log.error(
                "entity_creation_failed",
                error=str(e),
                exc_info=True,
                trace_id=originating_trace_id,
            )
            return ""

    async def backfill_missing_embeddings(
        self,
        *,
        batch_size: int = 100,
        trace_id: str | None = None,
    ) -> int:
        """Re-embed entities whose embedding is missing or zero-vectored (FRE-659).

        Idempotent, outage-safe remediation for the zero-vector corruption. An entity
        created while the embedder was unreachable is either missing ``e.embedding``
        (post-guard write path) or carries a baked-in zero vector (pre-guard corruption).
        This pass finds such entities (bounded to ``batch_size``, deterministic
        ``ORDER BY e.name`` so successive runs converge rather than re-selecting the same
        page), batch-regenerates their embeddings in one call, and persists ONLY the
        non-zero results — so a run during a continuing embedder outage is a safe no-op
        rather than re-poisoning the index.

        The write is guarded (``WHERE e.embedding IS NULL OR none(...)``) so it never
        clobbers a fresher non-zero embedding a concurrent :meth:`create_entity` may have
        written between the read and the write.

        Args:
            batch_size: Max entities repaired per run (bounds one scheduler tick's cost).
            trace_id: System-scoped scheduler trace id for log correlation (ADR-0074 §I3).

        Returns:
            Number of entities whose embedding was populated this run.
        """
        if not self.connected or not self.driver:
            return 0

        try:
            async with self.driver.session() as session:
                read = await session.run(
                    "MATCH (e:Entity)\n"
                    "WHERE e.description IS NOT NULL AND e.description <> ''\n"
                    "  AND (e.embedding IS NULL OR none(x IN e.embedding WHERE x <> 0.0))\n"
                    "RETURN e.name AS name, e.description AS description\n"
                    "ORDER BY e.name\n"
                    "LIMIT $batch_size",
                    batch_size=batch_size,
                )
                candidates = await read.data()
                if not candidates:
                    return 0

                texts = [f"{c['name']}: {c['description']}" for c in candidates]
                embeddings = await generate_embeddings_batch(texts)
                updates = [
                    {"name": c["name"], "embedding": emb}
                    for c, emb in zip(candidates, embeddings, strict=True)
                    if any(x != 0.0 for x in emb)
                ]
                if not updates:
                    log.info(
                        "embedding_backfill_skipped_embedder_down",
                        candidates=len(candidates),
                        trace_id=trace_id,
                    )
                    return 0

                write = await session.run(
                    "UNWIND $updates AS u\n"
                    "MATCH (e:Entity {name: u.name})\n"
                    "WHERE e.embedding IS NULL OR none(x IN e.embedding WHERE x <> 0.0)\n"
                    "SET e.embedding = u.embedding\n"
                    "RETURN count(*) AS filled",
                    updates=updates,
                )
                record = await write.single()
                filled = int(record["filled"]) if record else 0
                log.info(
                    "embedding_backfill_completed",
                    candidates=len(candidates),
                    filled=filled,
                    trace_id=trace_id,
                )
                return filled
        except Exception as exc:
            log.warning(
                "embedding_backfill_error",
                error=str(exc),
                exc_info=True,
                trace_id=trace_id,
            )
            return 0

    async def assert_stance(
        self,
        stance: Stance,
        *,
        trace_id: str | None = None,
    ) -> bool:
        """Assert a Stance as a native ``HAS_STANCE`` edge from the owner (ADR-0098 D2/D3).

        Resolves the FRE-637 ``"owner"`` sentinel to the ``is_owner=true`` Person
        node (ADR-0052) and writes a native edge to the target World ``:Entity`` —
        the crown-jewel traversal stays inside Core (AC-5). A stance is the most
        temporal class (ADR-0098 D4): a new stance to the same concept unconditionally
        supersedes the prior current edge (its ``valid_to``/``invalid_at`` are set and
        it is retained), so exactly one stance to a concept is ever current.

        The write is a single atomic statement. It is skipped (logged) when the owner
        node or the target ``:Entity`` is absent — a stance is never written dangling.

        Args:
            stance: The stance to assert (target, affect, mastery, provenance).
            trace_id: Request/consolidation trace id for log correlation (ADR-0074 §I3).

        Returns:
            True if a HAS_STANCE edge was created, False if skipped or on error.
        """
        if not self.connected or not self.driver:
            log.warning("assert_stance_skipped_not_connected", trace_id=trace_id)
            return False

        now = datetime.now(timezone.utc)
        # ADR-0074 identity threading: trace_id/session_id ride on the edge.
        # ADR-0107 §3: deliberately still the is_owner sentinel, not user_id — a Stance
        # is the harness owner's worldview toward World knowledge, not a per-User fact
        # (unlike assert_claim, which ADR-0107 §2 moved to per-User resolution).
        query = (
            "MATCH (o:Person {is_owner: true})\n"
            "MATCH (c:Entity {name: $target})\n"
            "WITH o, c\n"
            "CALL {\n"
            "    WITH o, c\n"
            "    MATCH (o)-[cur:HAS_STANCE]->(c)\n"
            "    WHERE cur.valid_to IS NULL AND cur.invalid_at IS NULL\n"
            "    SET cur.valid_to = $valid_from, cur.invalid_at = $now\n"
            "    RETURN count(*) AS superseded\n"
            "}\n"
            "CREATE (o)-[s:HAS_STANCE {\n"
            "    affect: $affect, mastery: $mastery, review_due: $review_due,\n"
            "    class: 'Stance', valid_from: $valid_from, valid_to: null, invalid_at: null,\n"
            "    trace_id: $trace_id, session_id: $session_id, source_type: $source_type,\n"
            "    observed_at: $observed_at, extracted_at: $extracted_at\n"
            "}]->(c)\n"
            "RETURN superseded"
        )
        params: dict[str, Any] = {
            "target": stance.target,
            "affect": stance.affect,
            "mastery": stance.mastery,
            "review_due": stance.review_due.isoformat() if stance.review_due else None,
            "valid_from": stance.observed_at.isoformat(),
            "now": now.isoformat(),
            "trace_id": stance.trace_id or trace_id,
            "session_id": stance.session_id,
            "source_type": stance.source_type,
            "observed_at": stance.observed_at.isoformat(),
            "extracted_at": stance.extracted_at.isoformat() if stance.extracted_at else None,
        }
        try:
            async with self.driver.session() as session:
                result = await session.run(query, **params)
                record = await result.single()
                if record is None:
                    log.warning(
                        "assert_stance_skipped_no_owner_or_target",
                        target=stance.target,
                        trace_id=trace_id,
                    )
                    return False
                log.info(
                    "stance_asserted",
                    target=stance.target,
                    superseded=record["superseded"],
                    trace_id=trace_id,
                )
                return True
        except Exception as e:
            log.error(
                "assert_stance_failed",
                target=stance.target,
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
            )
            return False

    async def assert_claim(
        self,
        claim: Claim,
        *,
        user_id: UUID,
        trace_id: str | None = None,
    ) -> str:
        """Assert a durable Claim under the acting User, killing first-write-wins.

        Durable facts are living: a new Claim about the same fact-slot (matched by
        content-embedding similarity, :data:`CLAIM_MATCH_THRESHOLD`) either supersedes
        the current one (correction/evolution — AC-1/AC-2) or is rejected as a weaker/
        stale re-assertion (retained as a non-current audit record — *not* naive
        last-write-wins). Superseded Claims are retained with ``valid_to``/``invalid_at``/
        ``superseded_by`` set; the current Claim is the one with both temporal bounds null.

        Resolves the subject Person by ``user_id`` (ADR-0107 §2), mirroring the
        existing ``PARTICIPATED_IN`` precedent (ADR-0052) — never by name, and never
        the ``is_owner`` sentinel, so every authenticated user's Personal claims
        attach to their own Person node instead of silently collapsing onto the
        owner. Matching/supersession is likewise scoped to that user's own current
        Claims: a claim never matches or supersedes a different user's claim.

        User-resolution and the full write are one atomic statement. Skipped
        (logged) when the target Person node is absent — a Claim is never written
        orphaned, and this method never provisions a Person (that is
        ``get_or_provision_user_person``'s job, run earlier in the request).

        Args:
            claim: The Claim to assert (content, class, confidence, provenance).
            user_id: The acting authenticated user's UUID (ADR-0107 §2).
            trace_id: Request/consolidation trace id for log correlation (ADR-0074 §I3).

        Returns:
            The new Claim's id, or "" if skipped or on error.
        """
        if not self.connected or not self.driver:
            log.warning("assert_claim_skipped_not_connected", trace_id=trace_id)
            return ""

        embedding = await generate_embedding(claim.content)
        now = datetime.now(timezone.utc)
        claim_id = str(uuid4())
        user_id_str = str(user_id)

        try:
            async with self.driver.session() as session:
                # Fetch this user's current Claims (bounded set) for similarity matching.
                current = await session.run(
                    "MATCH (o:Person {user_id: $user_id})-[:HAS_FACT]->(cl:Claim)\n"
                    "WHERE cl.valid_to IS NULL AND cl.invalid_at IS NULL\n"
                    "RETURN cl.claim_id AS claim_id, cl.content AS content,\n"
                    "       cl.confidence AS confidence, cl.observed_at AS observed_at,\n"
                    "       cl.embedding AS embedding, cl.facet AS facet",
                    user_id=user_id_str,
                )
                candidates: list[ClaimRecord] = []
                async for row in current:
                    if row["embedding"] is None or row["claim_id"] is None:
                        continue
                    candidates.append(
                        ClaimRecord(
                            claim_id=row["claim_id"],
                            content=row["content"] or "",
                            confidence=float(row["confidence"] or 0.0),
                            observed_at=_parse_iso(row["observed_at"], fallback=claim.observed_at),
                            embedding=list(row["embedding"]),
                            # Legacy rows predate facet → property reads back None; "" is
                            # neutral in the facet-weighted matcher (FRE-712, Codex #5).
                            facet=row["facet"] or "",
                        )
                    )

                # Facet-aware matching (FRE-712): adjudicate against the strongest blocker
                # so a weaker new claim never supersedes past a higher-confidence one, and
                # invalidate ALL matches on supersede so ≤1-current-per-slot self-heals.
                matches = matching_candidates(claim.facet, embedding, candidates)
                decision = adjudicate(
                    new_confidence=claim.confidence,
                    new_observed_at=claim.observed_at,
                    candidate=strongest_blocker(matches),
                    new_update_kind=claim.update_kind,
                )
                supersede_ids: list[str] = []
                new_valid_to: str | None = None
                new_invalid_at: str | None = None
                if decision.action is SupersessionAction.SUPERSEDE:
                    supersede_ids = [c.claim_id for c in matches]
                elif decision.action is SupersessionAction.REJECT:
                    # New claim arrives already non-current (retained for audit).
                    new_valid_to = claim.observed_at.isoformat()
                    new_invalid_at = now.isoformat()

                write = await session.run(
                    "MATCH (o:Person {user_id: $user_id})\n"
                    "WITH o\n"
                    "CALL {\n"
                    "    WITH o\n"
                    "    UNWIND $supersede_ids AS sid\n"
                    "    MATCH (o)-[:HAS_FACT]->(old:Claim {claim_id: sid})\n"
                    "    SET old.valid_to = $valid_from, old.invalid_at = $now,\n"
                    "        old.superseded_by = $claim_id, old.supersession_reason = $reason\n"
                    "    RETURN count(*) AS invalidated\n"
                    "}\n"
                    "CREATE (o)-[:HAS_FACT]->(cl:Claim {\n"
                    "    claim_id: $claim_id, content: $content, class: $knowledge_class,\n"
                    "    facet: $facet, update_kind: $update_kind,\n"
                    "    confidence: $confidence, embedding: $embedding,\n"
                    "    valid_from: $valid_from, valid_to: $new_valid_to, invalid_at: $new_invalid_at,\n"
                    "    superseded_by: null, supersession_reason: null,\n"
                    "    trace_id: $trace_id, session_id: $session_id, source_type: $source_type,\n"
                    "    observed_at: $observed_at, extracted_at: $extracted_at\n"
                    "})\n"
                    "RETURN cl.claim_id AS claim_id, invalidated",
                    user_id=user_id_str,
                    supersede_ids=supersede_ids,
                    claim_id=claim_id,
                    content=claim.content,
                    knowledge_class=claim.knowledge_class,
                    facet=claim.facet,
                    update_kind=claim.update_kind,
                    confidence=claim.confidence,
                    embedding=embedding,
                    valid_from=claim.observed_at.isoformat(),
                    new_valid_to=new_valid_to,
                    new_invalid_at=new_invalid_at,
                    now=now.isoformat(),
                    reason=decision.reason,
                    trace_id=claim.trace_id or trace_id,
                    session_id=claim.session_id,
                    source_type=claim.source_type,
                    observed_at=claim.observed_at.isoformat(),
                    extracted_at=claim.extracted_at.isoformat() if claim.extracted_at else None,
                )
                record = await write.single()
                if record is None:
                    log.warning(
                        "assert_claim_skipped_no_user_person",
                        trace_id=trace_id,
                        user_id=user_id_str,
                    )
                    return ""
                log.info(
                    "claim_asserted",
                    claim_id=claim_id,
                    action=decision.action.value,
                    reason=decision.reason,
                    superseded=record["invalidated"],
                    trace_id=trace_id,
                )
                return claim_id
        except Exception as e:
            log.error(
                "assert_claim_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
            )
            return ""

    async def ensure_vector_index(self) -> bool:
        """Create Neo4j vector index on Entity.embedding, recreating if dimensions changed.

        Drops and recreates the index when the configured embedding dimensions differ
        from what is already indexed (e.g. after switching from 768-dim to 1024-dim
        embeddings). Requires Neo4j 5.11+.

        Returns:
            True if index exists or was created successfully.
        """
        if not self.connected or not self.driver:
            return False

        try:
            current_settings = get_settings()
            target_dims = current_settings.embedding_dimensions

            async with self.driver.session() as session:
                # Check existing index dimensions
                result = await session.run(
                    """
                    SHOW VECTOR INDEXES
                    YIELD name, options
                    WHERE name = 'entity_embedding'
                    RETURN options
                    """,
                )
                rows = await result.data()
                if rows:
                    existing_dims = (
                        rows[0].get("options", {}).get("indexConfig", {}).get("vector.dimensions")
                    )
                    if existing_dims is not None and int(existing_dims) != target_dims:
                        log.warning(
                            "vector_index_dimension_mismatch",
                            existing_dims=existing_dims,
                            target_dims=target_dims,
                            action="drop_and_recreate",
                        )
                        await session.run("DROP INDEX entity_embedding IF EXISTS")

                await session.run(
                    """
                    CREATE VECTOR INDEX entity_embedding IF NOT EXISTS
                    FOR (e:Entity)
                    ON (e.embedding)
                    OPTIONS {
                        indexConfig: {
                            `vector.dimensions`: $dimensions,
                            `vector.similarity_function`: 'cosine'
                        }
                    }
                    """,
                    dimensions=target_dims,
                )
                log.info(
                    "vector_index_ensured",
                    index_name="entity_embedding",
                    dimensions=target_dims,
                )
                return True
        except Exception as e:
            log.error("vector_index_creation_failed", error=str(e), exc_info=True)
            return False

    async def ensure_fulltext_index(self) -> bool:
        """Create the Turn/Entity full-text index (ADR-0104 / FRE-723 lexical arm).

        Idempotent (IF NOT EXISTS). Mirrors ensure_vector_index()'s pattern.

        Returns:
            True if the index exists or was created successfully.
        """
        if not self.connected or not self.driver:
            return False
        try:
            async with self.driver.session() as session:
                await session.run(
                    """
                    CREATE FULLTEXT INDEX turn_entity_fulltext IF NOT EXISTS
                    FOR (n:Turn|Entity)
                    ON EACH [n.user_message, n.name]
                    """
                )
            log.info("fulltext_index_ensured", index_name="turn_entity_fulltext")
            return True
        except Exception as e:
            log.error("fulltext_index_creation_failed", error=str(e), exc_info=True)
            return False

    async def bootstrap_owner_identity(
        self,
        agent_id: str,
        user_id: UUID,
        email: str,
        name: str,
    ) -> bool:
        """Create or update the owner :Person and :Agent nodes in Neo4j.

        Idempotent — safe to call on every startup. Anchors identity by
        ``user_id`` only (never by name) to prevent collision with
        same-named third-party entities extracted from conversations.

        Args:
            agent_id: Stable deployment identifier (e.g. "seshat-local").
            user_id: Owner's UUID from the Postgres users table.
            email: Owner's email (sourced from AGENT_OWNER_EMAIL).
            name: Owner's display name (sourced from AGENT_OWNER_NAME).

        Returns:
            True on success, False on failure.
        """
        if not self.connected or not self.driver:
            log.warning("owner_bootstrap_skipped", reason="neo4j_not_connected")
            return False
        if not name:
            log.info("owner_bootstrap_skipped", reason="owner_name_empty")
            return False

        user_id_str = str(user_id)
        try:
            async with self.driver.session() as session:
                # Ensure uniqueness constraint exists (idempotent).
                await session.run(
                    "CREATE CONSTRAINT person_user_id_unique IF NOT EXISTS "
                    "FOR (p:Person) REQUIRE p.user_id IS UNIQUE"
                )
                # Bootstrap :Agent and owner :Person, anchored on user_id.
                await session.run(
                    """
                    MERGE (agent:Agent {id: $agent_id})
                    MERGE (person:Person {user_id: $user_id})
                      ON CREATE SET person.is_owner   = true,
                                    person.name       = $name,
                                    person.email      = $email,
                                    person.created_at = datetime(),
                                    person.source     = "config_bootstrap"
                      ON MATCH  SET person.is_owner   = true,
                                    person.email      = coalesce(person.email, $email),
                                    person.name       = coalesce(person.name,  $name)
                    MERGE (agent)-[:OPERATED_BY]->(person)
                    """,
                    agent_id=agent_id,
                    user_id=user_id_str,
                    name=name,
                    email=email,
                )
            log.info("owner_bootstrap_ran", agent_id=agent_id)
            return True
        except Exception as e:
            log.error("owner_bootstrap_failed", error=str(e), exc_info=True)
            return False

    async def get_or_provision_user_person(
        self,
        user_id: UUID,
        email: str,
        display_name: str | None,
        trace_id: str | None = None,
    ) -> dict[str, str]:
        """Ensure a :Person node exists for an authenticated user and return its facts.

        Creates the node on first call (lazy provisioning); subsequent calls
        return existing properties without overwriting them — enrichment from
        entity extraction survives intact. Anchored by ``user_id`` (never by name).

        Args:
            user_id: Authenticated user's UUID from the Postgres users table.
            email: User's CF Access email.
            display_name: User's display_name from the users table (nullable);
                falls back to the local-part of their email.
            trace_id: Optional request trace identifier for log correlation
                (ADR-0074 §I3).

        Returns:
            Dict of known facts (name, location, pronouns, role, languages)
            for the whitelist fields. Missing fields are absent from the dict.
        """
        if not self.connected or not self.driver:
            return {}

        user_id_str = str(user_id)
        email_localpart = email.split("@")[0] if "@" in email else email
        resolved_name = display_name or email_localpart

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    """
                    MERGE (p:Person {user_id: $user_id})
                      ON CREATE SET p.email      = $email,
                                    p.name       = $name,
                                    p.created_at = datetime(),
                                    p.source     = "auto_provision"
                      ON MATCH  SET p.email      = coalesce(p.email, $email)
                    RETURN p { .name, .location, .pronouns, .role, .languages } AS facts
                    """,
                    user_id=user_id_str,
                    email=email,
                    name=resolved_name,
                )
                record = await result.single()
                if record is None:
                    return {}
                raw: dict[str, Any] = record["facts"] or {}
                return {k: v for k, v in raw.items() if v is not None}
        except Exception as e:
            log.warning(
                "user_person_provision_failed",
                error=str(e),
                trace_id=trace_id,
                user_id=user_id_str,
            )
            return {}

    async def update_person_name_if_default(
        self,
        user_id: UUID,
        current_default: str,
        new_name: str,
        trace_id: str | None = None,
    ) -> bool:
        """Overwrite :Person.name only when it still equals the default email local-part.

        Coalesce rule mirrors the Postgres upsert: if entity extraction has already
        enriched the name (i.e. it differs from the local-part), leave it untouched.

        Args:
            user_id: User's UUID (anchors the :Person node).
            current_default: The email local-part that signals "never enriched".
            new_name: The configured display name to write.
            trace_id: Optional request trace identifier for log correlation
                (ADR-0074 §I3).

        Returns:
            True when the name was updated, False otherwise (node missing, already
            enriched, or Neo4j unavailable).
        """
        if not self.connected or not self.driver:
            return False

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    """
                    MATCH (p:Person {user_id: $user_id})
                    WHERE p.name = $current_default OR p.name IS NULL
                    SET p.name = $new_name
                    RETURN count(p) AS updated
                    """,
                    user_id=str(user_id),
                    current_default=current_default,
                    new_name=new_name,
                )
                record = await result.single()
                return bool(record and record["updated"] > 0)
        except Exception as e:
            log.warning(
                "person_name_update_failed",
                user_id=str(user_id),
                error=str(e),
                trace_id=trace_id,
            )
            return False

    async def get_person_location_consent(self, user_id: str, trace_id: str) -> bool:
        """Return whether a person has opted into location features.

        Args:
            user_id: User UUID string anchored on the :Person node.
            trace_id: Request trace identifier for log correlation.

        Returns:
            True when location consent is enabled; False when disabled, absent,
            or Neo4j is unavailable.
        """
        if not self.connected or not self.driver:
            log.warning(
                "person_location_consent_unavailable",
                trace_id=trace_id,
                user_id=user_id,
                reason="neo4j_not_connected",
            )
            return False

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    """
                    MATCH (p:Person {user_id: $user_id})
                    RETURN coalesce(p.location_consent_enabled, false) AS enabled
                    """,
                    user_id=user_id,
                )
                record = await result.single()
                enabled = bool(record and record["enabled"])
                log.info(
                    "person_location_consent_read",
                    trace_id=trace_id,
                    user_id=user_id,
                    enabled=enabled,
                )
                return enabled
        except Exception as e:
            log.warning(
                "person_location_consent_read_failed",
                trace_id=trace_id,
                user_id=user_id,
                error=str(e),
            )
            return False

    async def set_person_location_consent(
        self,
        user_id: str,
        enabled: bool,
        trace_id: str,
    ) -> None:
        """Set a person's location consent preference.

        Args:
            user_id: User UUID string anchored on the :Person node.
            enabled: Whether the user consents to location features.
            trace_id: Request trace identifier for log correlation.
        """
        if not self.connected or not self.driver:
            log.warning(
                "person_location_consent_set_unavailable",
                trace_id=trace_id,
                user_id=user_id,
                reason="neo4j_not_connected",
            )
            return

        try:
            async with self.driver.session() as session:
                # MERGE (not MATCH) on user_id: a user may toggle consent before
                # their first chat turn has bootstrapped the :Person. This mirrors
                # ensure_person_for_user's own `MERGE (p:Person {user_id})` anchor,
                # so any skeletal node created here is reconciled (name, is_owner)
                # on the next authenticated turn rather than left orphaned.
                await session.run(
                    """
                    MERGE (p:Person {user_id: $user_id})
                    SET p.location_consent_enabled = $enabled
                    """,
                    user_id=user_id,
                    enabled=enabled,
                )
            log.info(
                "person_location_consent_set",
                trace_id=trace_id,
                user_id=user_id,
                enabled=enabled,
            )
        except Exception as e:
            log.warning(
                "person_location_consent_set_failed",
                trace_id=trace_id,
                user_id=user_id,
                error=str(e),
            )

    async def update_person_location(
        self,
        user_id: str,
        latitude: float,
        longitude: float,
        timezone: str | None,
        source: str,
        trace_id: str,
    ) -> None:
        """Update a person's current client-provided location.

        Args:
            user_id: User UUID string anchored on the :Person node.
            latitude: Latitude to store on the :Location node.
            longitude: Longitude to store on the :Location node.
            timezone: Browser-provided IANA timezone string.
            source: Location source label.
            trace_id: Request trace identifier for log correlation.
        """
        if not self.connected or not self.driver:
            log.warning(
                "person_location_update_unavailable",
                trace_id=trace_id,
                user_id=user_id,
                source=source,
                reason="neo4j_not_connected",
            )
            return

        try:
            async with self.driver.session() as session:
                await session.run(
                    """
                    MATCH (p:Person {user_id: $user_id})
                    MERGE (l:Location {latitude: $latitude, longitude: $longitude})
                      ON CREATE SET l.point = point({latitude: $latitude, longitude: $longitude}),
                                    l.timezone = $timezone,
                                    l.source = $source,
                                    l.resolved_at = datetime()
                      ON MATCH SET l.timezone = $timezone,
                                   l.resolved_at = datetime()
                    WITH p, l
                    OPTIONAL MATCH (p)-[old:CURRENTLY_AT]->(:Location)
                    DELETE old
                    MERGE (p)-[c:CURRENTLY_AT]->(l)
                    SET c.since = datetime(), c.trace_id = $trace_id
                    MERGE (p)-[v:VISITED]->(l)
                      ON CREATE SET v.at = datetime(), v.trace_id = $trace_id
                    """,
                    user_id=user_id,
                    latitude=latitude,
                    longitude=longitude,
                    timezone=timezone,
                    source=source,
                    trace_id=trace_id,
                )
            log.info(
                "person_location_updated",
                trace_id=trace_id,
                user_id=user_id,
                source=source,
                timezone_set=timezone is not None,
            )
        except Exception as e:
            log.warning(
                "person_location_update_failed",
                trace_id=trace_id,
                user_id=user_id,
                source=source,
                error=str(e),
            )

    async def get_person_location(self, user_id: str, trace_id: str) -> dict[str, object] | None:
        """Return a person's current client-provided location.

        Args:
            user_id: User UUID string anchored on the :Person node.
            trace_id: Request trace identifier for log correlation.

        Returns:
            Location properties from the CURRENTLY_AT edge, or None.
        """
        if not self.connected or not self.driver:
            log.warning(
                "person_location_read_unavailable",
                trace_id=trace_id,
                user_id=user_id,
                reason="neo4j_not_connected",
            )
            return None

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    """
                    MATCH (:Person {user_id: $user_id})-[:CURRENTLY_AT]->(l:Location)
                    RETURN l.latitude AS latitude,
                           l.longitude AS longitude,
                           l.timezone AS timezone,
                           l.source AS source
                    """,
                    user_id=user_id,
                )
                record = await result.single()
                if record is None:
                    log.info("person_location_not_found", trace_id=trace_id, user_id=user_id)
                    return None
                source = record["source"]
                timezone_value = record["timezone"]
                log.info(
                    "person_location_read",
                    trace_id=trace_id,
                    user_id=user_id,
                    source=source,
                    timezone_set=timezone_value is not None,
                )
                return {
                    "latitude": record["latitude"],
                    "longitude": record["longitude"],
                    "timezone": timezone_value,
                    "source": source,
                }
        except Exception as e:
            log.warning(
                "person_location_read_failed",
                trace_id=trace_id,
                user_id=user_id,
                error=str(e),
            )
            return None

    async def create_relationship(
        self,
        relationship: Relationship,
        visibility: str = "public",
        trace_id: str | None = None,
    ) -> str | None:
        """Create a relationship between nodes.

        Args:
            relationship: Relationship to create
            visibility: Visibility scope for the relationship (FRE-229). Stored as a
                property on the relationship edge.
            trace_id: Optional request trace identifier for log correlation
                (ADR-0074 §I3).

        Returns:
            Neo4j ``elementId(rel)`` on success, or ``None`` on failure.
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected", trace_id=trace_id)
            return None

        try:
            async with self.driver.session() as session:
                # Use APOC to create a relationship with a dynamic type label.
                # Standard Cypher cannot parameterize relationship type labels;
                # apoc.merge.relationship handles this cleanly.
                # Access tracking properties (FRE-161: KG Freshness) are initialized on creation.
                result = await session.run(
                    """
                    MATCH (source)
                    WHERE source.entity_id = $source_id OR source.name = $source_id
                       OR (source:Turn AND source.turn_id = $source_id)
                    MATCH (target)
                    WHERE target.entity_id = $target_id OR target.name = $target_id
                    CALL apoc.merge.relationship(
                        source, $relationship_type,
                        {},
                        {
                            weight: $weight,
                            visibility: $visibility,
                            created_at: datetime(),
                            first_accessed_at: datetime(),
                            last_accessed_at: datetime(),
                            access_count: 0,
                            last_access_context: 'created'
                        },
                        target
                    ) YIELD rel
                    RETURN elementId(rel) AS element_id
                    """,
                    source_id=relationship.source_id,
                    target_id=relationship.target_id,
                    relationship_type=relationship.relationship_type,
                    weight=relationship.weight,
                    visibility=visibility,
                )
                rec = await result.single()
                element_id = rec.get("element_id") if rec else None
                eid_str = str(element_id) if element_id is not None else None
                log.info(
                    "relationship_created",
                    source=relationship.source_id,
                    target=relationship.target_id,
                    type=relationship.relationship_type,
                    element_id=eid_str,
                    trace_id=trace_id,
                )
                return eid_str
        except Exception as e:
            log.error(
                "relationship_creation_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
            )
            return None

    async def fetch_turn_discusses_relationship_element_ids(
        self, turn_id: str, trace_id: str | None = None
    ) -> list[str]:
        """Return ``elementId`` values for ``DISCUSSES`` edges from a turn.

        Args:
            turn_id: Turn node ``turn_id`` (typically trace id).
            trace_id: Optional request trace identifier for log correlation
                (ADR-0074 §I3); defaults to ``turn_id`` when omitted because
                Turn IDs are themselves trace IDs in the consolidation path.

        Returns:
            Distinct relationship element ids, or empty list if unavailable.
        """
        if not self.connected or not self.driver or not turn_id:
            return []
        try:
            async with self.driver.session() as session:
                result = await session.run(
                    """
                    MATCH (t:Turn {turn_id: $turn_id})-[rel:DISCUSSES]->(:Entity)
                    RETURN collect(DISTINCT elementId(rel)) AS ids
                    """,
                    turn_id=turn_id,
                )
                rec = await result.single()
                raw = rec.get("ids") if rec else None
                if not raw:
                    return []
                return [str(x) for x in raw if x is not None]
        except Exception as e:
            log.warning(
                "fetch_turn_discusses_rel_ids_failed",
                turn_id=turn_id,
                error=str(e),
                trace_id=trace_id if trace_id is not None else turn_id,
            )
            return []

    async def _collect_discusses_relationship_element_ids_for_memory_query(
        self,
        session: Any,
        conversations: list[TurnNode],
        entity_names: list[str],
    ) -> list[str]:
        """Collect ``DISCUSSES`` relationship element ids touched by a memory query."""
        out: list[str] = []
        turn_ids = [c.turn_id for c in conversations if getattr(c, "turn_id", None)]
        if turn_ids:
            result = await session.run(
                """
                MATCH (c:Turn)-[rel:DISCUSSES]->(:Entity)
                WHERE c.turn_id IN $turn_ids
                RETURN collect(DISTINCT elementId(rel)) AS ids
                """,
                turn_ids=turn_ids,
            )
            rec = await result.single()
            raw = rec.get("ids") if rec else None
            if raw:
                out.extend(str(x) for x in raw if x is not None)
        if not out and entity_names:
            result = await session.run(
                """
                MATCH (c:Turn)-[rel:DISCUSSES]->(e:Entity)
                WHERE e.name IN $entity_names
                RETURN collect(DISTINCT elementId(rel)) AS ids
                """,
                entity_names=entity_names,
            )
            rec = await result.single()
            raw = rec.get("ids") if rec else None
            if raw:
                out.extend(str(x) for x in raw if x is not None)
        return list(dict.fromkeys(out))

    async def query_memory(
        self,
        query: MemoryQuery,
        feedback_key: str | None = None,
        query_text: str | None = None,
        access_context: AccessContext = AccessContext.SEARCH,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
    ) -> MemoryQueryResult:
        """Query memory graph for relevant conversations and entities.

        Args:
            query: Query parameters
            feedback_key: Optional session/user key for implicit feedback tracking.
            query_text: Optional original user query text. When provided,
                generates a vector embedding for hybrid similarity search
                and enables implicit rephrase detection for feedback tracking.
            access_context: Typed context where the query originated.
                Used for access tracking events (ADR-0042).
            trace_id: Optional request trace identifier for event correlation.
            session_id: Optional session identifier for event correlation.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity (FRE-229).

        Returns:
            MemoryQueryResult with conversations, entities, and relationships
        """
        # Prefer caller-supplied args; fall back to query fields for adapter callers.
        effective_user_id = user_id if user_id is not None else query.user_id
        effective_authenticated = authenticated or query.authenticated

        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected", trace_id=trace_id, session_id=session_id)
            return MemoryQueryResult()

        # Multi-path recall (ADR-0104 / FRE-724): behind multipath_recall_enabled,
        # the entity-name path converges onto the shared fused+reranked core rather
        # than its single dense+entity-name candidate set. Flag off falls through to
        # the legacy path below, unchanged.
        if query_text and get_settings().multipath_recall_enabled:
            return await self._multipath_query_memory(
                query,
                query_text,
                access_context=access_context,
                trace_id=trace_id,
                session_id=session_id,
                user_id=effective_user_id,
                authenticated=effective_authenticated,
            )

        vis_frag, vis_params = _build_visibility_filter(
            "c", effective_user_id, effective_authenticated
        )

        try:
            async with self.driver.session() as session:
                current_settings = get_settings()
                relevance_bounded = current_settings.relevance_bounded_recall_enabled
                recall_start = time.perf_counter()

                # --- Hybrid: vector similarity search over entity_embedding ---
                # Runs before candidate generation so relevance-bounded recall can
                # union vector-matched entities into the candidate set (ADR-0100),
                # not merely re-score recency-selected survivors.
                vector_results: list[Any] = []
                if query_text:
                    try:
                        query_embedding = await generate_embedding(query_text, mode="query")
                        if any(x != 0.0 for x in query_embedding):
                            vec_top_k = (
                                current_settings.proactive_memory_vector_top_k
                                if relevance_bounded
                                else min(query.limit, 20)
                            )
                            vector_result = await session.run(
                                """
                                CALL db.index.vector.queryNodes(
                                    'entity_embedding', $top_k, $embedding
                                )
                                YIELD node, score
                                RETURN node.name AS name,
                                       node.entity_type AS entity_type,
                                       node.description AS description,
                                       score
                                ORDER BY score DESC
                                """,
                                top_k=vec_top_k,
                                embedding=query_embedding,
                            )
                            vector_results = await vector_result.data()
                    except Exception as vec_exc:
                        log.warning(
                            "vector_search_failed",
                            error=str(vec_exc),
                            query_text_length=len(query_text),
                            trace_id=trace_id,
                        )

                # Build vector_scores for relevance calculation; floor-filter to
                # the candidate entities for relevance-bounded candidate generation.
                vector_scores: dict[str, float] = {}
                for vr in vector_results:
                    if "name" in vr and "score" in vr:
                        vector_scores[vr["name"]] = float(vr["score"])
                relevant_entity_names, floored_vector_scores = _filter_entities_by_floor(
                    vector_results, current_settings.recall_similarity_floor
                )
                # Apply the similarity floor consistently to the scores used for
                # ranking (not just candidacy): under the flag, a below-floor entity
                # must not boost relevance ranking either, or the floor would not keep
                # junk out (AC-4). Flag off keeps legacy full-score behaviour.
                vector_scores_for_ranking = (
                    floored_vector_scores if relevance_bounded else vector_scores
                )

                # Build Cypher query dynamically based on query parameters
                cypher_parts = []
                entity_recall = bool(query.entity_names or query.entity_types)
                cutoff_date = ""

                if relevance_bounded and entity_recall:
                    # ADR-0100: relevance-keyed candidate generation. Union the
                    # entity-name/type match with vector-matched entities and bound
                    # turn expansion *per entity* (most-recent) so recent distractors
                    # under other entities cannot crowd out the relevant old turn.
                    #
                    # Contract (ADR-0100 §2/§3, AC-1a): recency_days is DEMOTED to a
                    # ranking weight on this path — it no longer gates candidacy, so
                    # the default cutoff is intentionally absent. recency_days is still
                    # honoured as a recency component inside _calculate_relevance_scores.
                    #
                    # FRE-658 resolution: an EXPLICIT caller-supplied hard window
                    # (query.hard_recency_days, set only by the memory_search tool) is
                    # re-applied as a hard candidacy bound below via {time_frag}. The
                    # automatic callers never set it, so they stay invariant (AC-1a).
                    #
                    # The candidate set is ordered by entity relevance (explicit name
                    # match = 1.0, else the floored vector score) then recency BEFORE
                    # the candidate_cap, so the cap keeps the most relevant turns
                    # rather than an arbitrary slice. Final per-turn relevance ranking
                    # and the result LIMIT are applied in Python after scoring.
                    if query.entity_names:
                        entity_predicate = "e.name IN $entity_names"
                        cypher_parts.append("entity_names: $entity_names")
                    else:
                        entity_predicate = "e.entity_type IN $entity_types"
                        cypher_parts.append("entity_types: $entity_types")
                    hard_cutoff = _hard_recency_cutoff_iso(query)
                    time_frag = "AND c.timestamp >= $cutoff_date" if hard_cutoff else ""
                    base_query = f"""
                    MATCH (c:Turn)-[:DISCUSSES]->(e:Entity)
                    WHERE {vis_frag}
                    AND ({entity_predicate} OR e.name IN $relevant_entity_names)
                    {time_frag}
                    WITH e, c,
                         CASE WHEN {entity_predicate} THEN 1.0
                              ELSE coalesce($entity_scores[e.name], 0.0) END AS escore
                    ORDER BY c.timestamp DESC
                    WITH e, escore, collect(DISTINCT c)[0..$per_entity_cap] AS turns
                    UNWIND turns AS c
                    WITH c, max(escore) AS turn_rel
                    ORDER BY turn_rel DESC, c.timestamp DESC
                    RETURN c
                    LIMIT $candidate_cap
                    """
                else:
                    # Legacy recency-keyed candidate generation (flag off, or no
                    # relevance signal: direct id lookups and the bare fallback).
                    if query.entity_names or query.entity_types:
                        base_query = f"""
                        MATCH (c:Turn)-[:DISCUSSES]->(e:Entity)
                        WHERE {vis_frag}
                        AND """
                        if query.entity_names:
                            base_query += "e.name IN $entity_names"
                            cypher_parts.append("entity_names: $entity_names")
                        elif query.entity_types:
                            base_query += "e.entity_type IN $entity_types"
                            cypher_parts.append("entity_types: $entity_types")
                    elif query.conversation_ids or query.trace_ids:
                        # Direct turn/trace lookup (conversation_ids maps to turn_id)
                        base_query = f"MATCH (c:Turn) WHERE {vis_frag} AND "
                        if query.conversation_ids:
                            base_query += "c.turn_id IN $conversation_ids"
                            cypher_parts.append("conversation_ids: $conversation_ids")
                        elif query.trace_ids:
                            base_query += "c.trace_id IN $trace_ids"
                            cypher_parts.append("trace_ids: $trace_ids")
                    else:
                        base_query = f"MATCH (c:Turn) WHERE {vis_frag}"

                    # Add WHERE clauses for recency
                    if query.recency_days:
                        cutoff_date = (
                            datetime.utcnow() - timedelta(days=query.recency_days)
                        ).isoformat()
                        base_query += " AND c.timestamp >= $cutoff_date"

                    # Add ordering and limiting
                    base_query += """
                    RETURN DISTINCT c
                    ORDER BY c.timestamp DESC
                    LIMIT $limit
                    """

                # Execute query
                params: dict[str, Any] = {
                    "limit": query.limit,
                    "max_depth": query.max_depth,
                    **vis_params,
                }

                if query.entity_names:
                    params["entity_names"] = query.entity_names
                if query.entity_types:
                    params["entity_types"] = query.entity_types
                if query.conversation_ids:
                    params["conversation_ids"] = query.conversation_ids
                if query.trace_ids:
                    params["trace_ids"] = query.trace_ids
                if relevance_bounded and entity_recall:
                    params["relevant_entity_names"] = relevant_entity_names
                    params["entity_scores"] = floored_vector_scores
                    params["per_entity_cap"] = current_settings.recall_per_entity_turn_cap
                    params["candidate_cap"] = current_settings.recall_candidate_cap
                    # FRE-658: bind the explicit hard-window cutoff when one applies.
                    if hard_cutoff:
                        params["cutoff_date"] = hard_cutoff
                elif query.recency_days:
                    params["cutoff_date"] = cutoff_date

                result = await session.run(base_query, parameters=params)
                records = await result.values()

                # Parse results
                conversations = []
                for record in records:
                    if record and record[0]:
                        node = record[0]
                        # Support both Turn nodes (turn_id) and legacy Conversation nodes
                        turn_id = node.get("turn_id") or node.get("conversation_id", "")
                        conversations.append(
                            TurnNode(
                                turn_id=turn_id,
                                trace_id=node.get("trace_id"),
                                session_id=node.get("session_id"),
                                sequence_number=node.get("sequence_number", 0),
                                timestamp=datetime.fromisoformat(
                                    node.get("timestamp", datetime.utcnow().isoformat())
                                ),
                                summary=node.get("summary"),
                                user_message=node.get("user_message", ""),
                                assistant_response=node.get("assistant_response"),
                                key_entities=node.get("key_entities", []),
                                properties=orjson.loads(node.get("properties", "{}"))
                                if isinstance(node.get("properties"), str)
                                else node.get("properties", {}),
                            )
                        )

                # --- Reranker: re-score top-N candidates via cross-attention (FRE-672) ---
                # The reranker cross-attends over every document it receives, so its
                # cost scales with candidate count, not with recall_candidate_cap. Bound
                # the input to the top-N by vector score; the rest pass through on their
                # vector+recency score (no reranker_scores entry → non-reranker path).
                reranker_scores: dict[str, float] = {}
                if current_settings.reranker_enabled and query_text and len(conversations) > 1:
                    try:
                        from personal_agent.memory.reranker import rerank  # noqa: PLC0415

                        rerank_indices = _select_rerank_candidates(
                            conversations,
                            vector_scores_for_ranking,
                            current_settings.reranker_input_cap,
                        )
                        docs = [
                            conversations[i].summary or conversations[i].user_message or ""
                            for i in rerank_indices
                        ]
                        rerank_results = await rerank(
                            query=query_text,
                            documents=docs,
                            top_k=current_settings.reranker_top_k,
                            trace_id=trace_id,
                            session_id=session_id,
                        )
                        # rr.index is into the bounded docs list; map back to conversations.
                        if rerank_results:
                            max_score = max(r.score for r in rerank_results)
                            for rr in rerank_results:
                                if rr.index < len(rerank_indices):
                                    conv_idx = rerank_indices[rr.index]
                                    norm = rr.score / max_score if max_score > 0 else 0.0
                                    reranker_scores[conversations[conv_idx].turn_id] = norm
                    except Exception as rerank_exc:
                        log.warning(
                            "reranker_integration_failed",
                            error=str(rerank_exc),
                            trace_id=trace_id,
                        )

                # Calculate plausibility/relevance scores
                relevance_scores = await self._calculate_relevance_scores(
                    conversations,
                    query,
                    vector_scores=vector_scores_for_ranking,
                    reranker_scores=reranker_scores,
                    trace_id=trace_id,
                )

                candidate_set_size = len(conversations)
                if relevance_bounded and entity_recall:
                    # ADR-0100 defect 3: order returned turns by combined relevance
                    # and apply the result limit *after* ranking, not after the
                    # timestamp ordering of the candidate query. Gated on entity_recall
                    # so flag-on id-lookups / bare-fallback (legacy Cypher) are untouched.
                    conversations = _rank_conversations_by_relevance(
                        conversations, relevance_scores
                    )[: query.limit]
                    # Restrict the score map to the returned set so downstream
                    # consumers (quality metrics, MemoryQueryResult, the recall event)
                    # report the post-slice count, not the full candidate set.
                    relevance_scores = {
                        c.turn_id: relevance_scores.get(c.turn_id, 0.0) for c in conversations
                    }

                # Emit the live recall telemetry event in both flag states (pure
                # observability — never alters the recall result; wrapped so a
                # telemetry failure cannot break recall). Mirrors the FRE-435
                # harness metrics into prod so the "no prior discussions" false
                # negative becomes measurable (ADR-0100).
                try:
                    log.info(
                        "memory_recall",
                        **_build_memory_recall_event(
                            returned=conversations,
                            candidate_set_size=candidate_set_size,
                            vector_scores=vector_scores_for_ranking,
                            vector_entity_count=len(relevant_entity_names),
                            recall_latency_ms=(time.perf_counter() - recall_start) * 1000.0,
                            similarity_floor=current_settings.recall_similarity_floor,
                            relevance_bounded_enabled=relevance_bounded,
                        ),
                        trace_id=trace_id,
                        session_id=session_id,
                    )
                except Exception as recall_evt_exc:
                    log.warning(
                        "memory_recall_event_failed",
                        error=str(recall_evt_exc),
                        trace_id=trace_id,
                    )

                accessed_entity_ids = list(query.entity_names or [])
                for conversation in conversations:
                    accessed_entity_ids.extend(conversation.key_entities or [])
                accessed_entity_ids = list(dict.fromkeys(accessed_entity_ids))

                relationship_element_ids = (
                    await self._collect_discusses_relationship_element_ids_for_memory_query(
                        session, conversations, accessed_entity_ids
                    )
                )

                log.info(
                    "memory_query_completed",
                    query_params=cypher_parts,
                    result_count=len(conversations),
                    trace_id=trace_id,
                    session_id=session_id,
                )

                self._log_query_quality_metrics(
                    query=query,
                    relevance_scores=relevance_scores,
                    feedback_key=feedback_key,
                    query_text=query_text,
                    trace_id=trace_id,
                )

                result = MemoryQueryResult(
                    conversations=conversations,
                    relevance_scores=relevance_scores,
                )

                # Publish memory access event (Phase 4)
                if settings.freshness_enabled and accessed_entity_ids and trace_id:
                    event = MemoryAccessedEvent(
                        entity_ids=accessed_entity_ids,
                        relationship_ids=relationship_element_ids,
                        access_context=access_context,
                        query_type="query_memory",
                        trace_id=trace_id,
                        session_id=session_id,
                        source_component="memory.service",
                    )
                    bus = get_event_bus()
                    try:
                        await bus.publish(STREAM_MEMORY_ACCESSED, event)
                        log.debug(
                            "memory_access_event_published",
                            trace_id=trace_id,
                            entity_count=len(accessed_entity_ids),
                            relationship_count=len(relationship_element_ids),
                            access_context=access_context.value,
                        )
                    except Exception as e:
                        log.warning(
                            "memory_access_event_publish_failed",
                            error=str(e),
                            event_id=event.event_id,
                            trace_id=trace_id,
                        )

                return result

        except Exception as e:
            log.error(
                "memory_query_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
                session_id=session_id,
            )
            return MemoryQueryResult()

    async def _query_entity_vector_candidates(
        self,
        session: Any,
        query_embedding: list[float],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Run the entity_embedding vector index query, returning {name, score} rows.

        Shared seam for relevance-keyed candidate generation on the recall paths
        (ADR-0100). Extracted so the broad-path flag-on branch is unit-testable
        without a live embedder.

        Args:
            session: An open Neo4j async session.
            query_embedding: The query embedding vector.
            top_k: Number of nearest entities to return.

        Returns:
            List of {"name": str, "score": float} rows ordered by score desc.
        """
        result = await session.run(
            """
            CALL db.index.vector.queryNodes('entity_embedding', $top_k, $embedding)
            YIELD node, score
            RETURN node.name AS name, score
            ORDER BY score DESC
            """,
            top_k=top_k,
            embedding=query_embedding,
        )
        rows: list[dict[str, Any]] = await result.data()
        return rows

    async def structural_recall_arm(
        self,
        *,
        entity_types: Sequence[str] | None = None,
        recency_days: int | None = None,
        anchor_names: Sequence[str] | None = None,
        limit: int | None = None,
        access_context: AccessContext = AccessContext.SEARCH,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
    ) -> list[EntityNode]:
        """Closed-axis structural recall arm (ADR-0104 AC-4 / FRE-707).

        Returns a ranked list of entities narrowed by the closed axes — entity
        type (safe predicate, gated by ``structural_type_predicate_enabled``),
        recency, and 1-hop relationship co-occurrence. Feature-gated OFF
        (``structural_arm_enabled``); flag-dark until the multi-path fusion core
        (FRE-722/724) wires it in. Never filters on the open axis
        (name/description) — that stays semantic (AC-4c).

        Args:
            entity_types: Closed-axis type filter; applied only when the type
                sub-predicate is enabled. Unenforced-type rows are never dropped.
            recency_days: Recency window for last_seen; None = no window.
            anchor_names: Seeds for 1-hop co-occurrence traversal; None = plain scan.
            limit: Max entities; defaults to ``structural_arm_top_k``.
            access_context: Typed access context (ADR-0042).
            trace_id: Request trace id for event correlation.
            session_id: Session id for event correlation.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity (FRE-229).

        Returns:
            Ranked list of EntityNode (best-first). Empty when the arm is gated
            off, the service is disconnected, or nothing matches.
        """
        current_settings = get_settings()
        if not current_settings.structural_arm_enabled:
            return []
        if not self.connected or not self.driver:
            return []

        top_k = limit if limit is not None else current_settings.structural_arm_top_k
        vis_e, vis_params = _build_visibility_filter("e", user_id, authenticated)
        vis_t, _ = _build_visibility_filter("t", user_id, authenticated)
        vis_a, _ = _build_visibility_filter("a", user_id, authenticated)
        cypher, params = _build_structural_arm_query(
            entity_types=entity_types,
            type_predicate_enabled=current_settings.structural_type_predicate_enabled,
            recency_days=recency_days,
            anchor_names=anchor_names,
            top_k=top_k,
            vis_fragment_e=vis_e,
            vis_fragment_t=vis_t,
            vis_fragment_a=vis_a,
        )
        params.update(vis_params)

        try:
            async with self.driver.session() as db_session:
                result = await db_session.run(cypher, parameters=params)
                records = await result.data()
        except Exception as e:
            log.error(
                "structural_recall_arm_failed",
                error=str(e),
                trace_id=trace_id,
                session_id=session_id,
            )
            return []

        entities = [_entity_node_from_record(r["e"]) for r in records]
        log.info(
            "structural_recall_arm_completed",
            arm="structural",
            entity_count=len(entities),
            type_predicate_enabled=current_settings.structural_type_predicate_enabled,
            has_recency=recency_days is not None,
            has_anchors=bool(anchor_names),
            trace_id=trace_id,
            session_id=session_id,
        )
        return entities

    async def lexical_recall_arm(
        self,
        query_text: str,
        *,
        limit: int | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
    ) -> list[RankedResult]:
        """Lexical full-text recall arm (ADR-0104 / FRE-723).

        Ranked hits over Turn.user_message and Entity.name via the
        ``turn_entity_fulltext`` index. Feature-gated OFF
        (``lexical_arm_enabled``); flag-dark until the multi-path fusion core
        (FRE-724) wires it in. Recovers rare tokens/IDs/names the dense
        embedder blurs (ADR-0104 §2).

        Args:
            query_text: Free-text query. Lucene special characters are escaped.
            limit: Max hits; defaults to ``multipath_arm_top_k``.
            trace_id: Request trace id for event correlation.
            session_id: Session id for event correlation.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity.

        Returns:
            Ranked list of RankedResult (best-first, 1-based rank). item_id is
            Turn.turn_id for turns, Entity elementId for entities. Empty when
            the arm is gated off, the service is disconnected, the query is
            empty, or nothing matches.
        """
        current_settings = get_settings()
        if not current_settings.lexical_arm_enabled:
            return []
        if not self.connected or not self.driver or not query_text.strip():
            return []

        top_k = limit if limit is not None else current_settings.multipath_arm_top_k
        vis_frag, vis_params = _build_visibility_filter("node", user_id, authenticated)
        params: dict[str, Any] = {
            "query_text": _escape_lucene_query(query_text),
            "top_k": top_k,
            **vis_params,
        }

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    f"""
                    CALL db.index.fulltext.queryNodes('turn_entity_fulltext', $query_text)
                    YIELD node, score
                    WITH node, score
                    WHERE {vis_frag}
                    RETURN
                        CASE WHEN node:Turn THEN node.turn_id ELSE elementId(node) END AS item_id,
                        CASE WHEN node:Turn THEN 'turn' ELSE 'entity' END AS kind,
                        score
                    ORDER BY score DESC
                    LIMIT $top_k
                    """,
                    parameters=params,
                )
                rows = await result.data()
        except Exception as e:
            log.error(
                "lexical_recall_arm_failed",
                error=str(e),
                trace_id=trace_id,
                session_id=session_id,
            )
            return []

        ranked = [
            RankedResult(item_id=r["item_id"], rank=i + 1, kind=r["kind"])
            for i, r in enumerate(rows)
        ]
        log.info(
            "lexical_recall_arm_completed",
            arm="lexical",
            hit_count=len(ranked),
            trace_id=trace_id,
            session_id=session_id,
        )
        return ranked

    async def _dense_vector_search_ranked(
        self,
        session: Any,
        embedding: list[float],
        top_k: int,
        vis_frag: str,
        vis_params: dict[str, Any],
    ) -> list[RankedResult]:
        """Run the entity_embedding ANN search, ranked by descending similarity.

        The "dense arm" the multi-query wrapper fans paraphrases through.

        Args:
            session: Active Neo4j async session.
            embedding: Query embedding vector; a zero vector short-circuits to [].
            top_k: Max candidates to retrieve.
            vis_frag: Visibility WHERE fragment (see _build_visibility_filter).
            vis_params: Params for vis_frag.

        Returns:
            RankedResult list, 1-based rank, item_id = Entity elementId. Rows below
            the ``recall_similarity_floor`` noise guard are dropped before ranking
            (ADR-0103 §4 per-arm guard / FRE-724); with the floor at its 0.0 default
            this is a no-op.
        """
        if not any(x != 0.0 for x in embedding):
            return []
        result = await session.run(
            f"""
            CALL db.index.vector.queryNodes('entity_embedding', $top_k, $embedding)
            YIELD node, score
            WITH node, score
            WHERE {vis_frag}
            RETURN elementId(node) AS item_id, score
            ORDER BY score DESC
            """,
            parameters={"top_k": top_k, "embedding": embedding, **vis_params},
        )
        rows = await result.data()
        floor = get_settings().recall_similarity_floor
        kept = [r for r in rows if r.get("score") is not None and float(r["score"]) >= floor]
        return [RankedResult(item_id=r["item_id"], rank=i + 1) for i, r in enumerate(kept)]

    async def dense_recall_arm(
        self,
        query_text: str,
        *,
        limit: int | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
    ) -> list[RankedResult]:
        """Dense vector recall arm (ADR-0104 / FRE-724).

        The baseline arm: embed ``query_text`` and rank ``entity_embedding`` ANN
        hits, applying the ``recall_similarity_floor`` noise guard. Used by the
        multi-path core as the dense signal when the multi-query arm is off (the
        multi-query arm subsumes the original-query dense pass). Fails open to an
        empty list — never hard-fails recall.

        Args:
            query_text: Free-text query.
            limit: Max hits; defaults to ``multipath_arm_top_k``.
            trace_id: Request trace id for event correlation.
            session_id: Session id for event correlation.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity.

        Returns:
            Ranked list of RankedResult (best-first, 1-based rank, entity kind).
            Empty when disconnected, the query is empty, embedding fails, or
            nothing clears the floor.
        """
        if not self.connected or not self.driver or not query_text.strip():
            return []
        current_settings = get_settings()
        top_k = limit if limit is not None else current_settings.multipath_arm_top_k
        try:
            embedding = await generate_embedding(query_text, mode="query")
        except Exception as exc:
            log.warning(
                "dense_recall_arm_embed_failed",
                error=str(exc),
                trace_id=trace_id,
                session_id=session_id,
            )
            return []
        vis_frag, vis_params = _build_visibility_filter("node", user_id, authenticated)
        try:
            async with self.driver.session() as session:
                ranked = await self._dense_vector_search_ranked(
                    session, embedding, top_k, vis_frag, vis_params
                )
        except Exception as exc:
            log.error(
                "dense_recall_arm_failed",
                error=str(exc),
                trace_id=trace_id,
                session_id=session_id,
            )
            return []
        log.info(
            "dense_recall_arm_completed",
            arm="dense",
            hit_count=len(ranked),
            trace_id=trace_id,
            session_id=session_id,
        )
        return ranked

    async def _multipath_fused_recall(
        self,
        query_text: str,
        *,
        path: str,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
    ) -> MultiPathRecallResult:
        """Shared multi-path recall core (ADR-0104 seam owner / FRE-724).

        Runs the v1 arms in parallel, fuses them by Reciprocal Rank Fusion, caps
        the fused set to the reranker input cap, reranks that capped set, and
        applies the soft operating point. Every recall path routes through this
        one core (behind ``multipath_recall_enabled``); each adapts the returned
        ``MultiPathRecallResult`` to its own shape.

        The v1 arm set is dense + lexical + multi-query (design spec §2). The
        multi-query arm subsumes the original-query dense pass, so the standalone
        dense arm runs only when multi-query is off — never double-counting the
        original query's agreement. The noise-guard floor lives inside the dense
        arm (ADR-0103 §4); this core never applies a score threshold to the fused
        or reranked set — the reranker orders, it does not gate (AC-5).

        Args:
            query_text: Free-text recall query.
            path: Which recall path the core serves ("broad"/"entity"/"proactive")
                — telemetry only.
            trace_id: Request trace id for event correlation.
            session_id: Session id for event correlation.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity.

        Returns:
            The fused + reranked candidates (ordered, deduped, ≤ the input cap)
            with the AC-1/AC-6 telemetry (arms executed/failed, per-arm counts,
            fused-set size, path).
        """
        current_settings = get_settings()

        arm_kwargs: dict[str, Any] = {
            "trace_id": trace_id,
            "session_id": session_id,
            "user_id": user_id,
            "authenticated": authenticated,
        }
        arm_names: list[str] = []
        arm_coros: list[Any] = []
        if current_settings.multiquery_arm_enabled:
            arm_names.append("multi_query")
            arm_coros.append(self.multi_query_recall_arm(query_text, **arm_kwargs))
        else:
            arm_names.append("dense")
            arm_coros.append(self.dense_recall_arm(query_text, **arm_kwargs))
        if current_settings.lexical_arm_enabled:
            arm_names.append("lexical")
            arm_coros.append(self.lexical_recall_arm(query_text, **arm_kwargs))

        arm_results = await asyncio.gather(*arm_coros, return_exceptions=True)

        arm_rankings: list[list[RankedResult]] = []
        arms_failed: list[str] = []
        per_arm_counts: dict[str, int] = {}
        for name, res in zip(arm_names, arm_results, strict=True):
            if isinstance(res, BaseException):
                arms_failed.append(name)
                per_arm_counts[name] = 0
                log.warning(
                    "multipath_arm_failed",
                    arm=name,
                    error=str(res),
                    trace_id=trace_id,
                    session_id=session_id,
                )
                continue
            per_arm_counts[name] = len(res)
            if res:
                arm_rankings.append(res)

        fused = reciprocal_rank_fusion(arm_rankings, k=current_settings.multipath_rrf_k)
        capped = fused[: current_settings.reranker_input_cap]
        items = await self._rerank_fused_items(
            query_text, capped, trace_id=trace_id, session_id=session_id
        )

        log.info(
            "multipath_recall",
            path=path,
            arms_executed=arm_names,
            arms_failed=arms_failed,
            per_arm_counts=per_arm_counts,
            fused_set_size=len(capped),
            reranked=bool(items) and current_settings.reranker_enabled,
            trace_id=trace_id,
            session_id=session_id,
        )

        return MultiPathRecallResult(
            items=items,
            arms_executed=arm_names,
            arms_failed=arms_failed,
            per_arm_counts=per_arm_counts,
            fused_set_size=len(capped),
            path=path,
        )

    async def _resolve_item_texts(
        self,
        items: Sequence[FusedResult],
        *,
        trace_id: str | None = None,
    ) -> dict[str, str]:
        """Fetch rerank doc text for a heterogeneous fused set, keyed by item_id.

        Entities (elementId) resolve to ``name + description``; turns (turn_id)
        resolve to ``summary`` (falling back to ``user_message``). Missing items
        simply have no entry — the caller substitutes an empty document so the
        reranker still scores (and never drops) them.

        Args:
            items: The capped fused set to resolve.
            trace_id: Request trace id for event correlation.

        Returns:
            Map of item_id → document text (may omit ids with no matching node).
        """
        texts: dict[str, str] = {}
        if not self.driver or not items:
            return texts
        entity_ids = [it.item_id for it in items if it.kind == "entity"]
        turn_ids = [it.item_id for it in items if it.kind == "turn"]
        try:
            async with self.driver.session() as session:
                if entity_ids:
                    r = await session.run(
                        """
                        UNWIND $ids AS eid
                        MATCH (e:Entity) WHERE elementId(e) = eid
                        RETURN eid AS id,
                               coalesce(e.name, '') + ' ' + coalesce(e.description, '') AS text
                        """,
                        ids=entity_ids,
                    )
                    for row in await r.data():
                        texts[row["id"]] = row["text"]
                if turn_ids:
                    r = await session.run(
                        """
                        UNWIND $ids AS tid
                        MATCH (t:Turn {turn_id: tid})
                        RETURN tid AS id, coalesce(t.summary, t.user_message, '') AS text
                        """,
                        ids=turn_ids,
                    )
                    for row in await r.data():
                        texts[row["id"]] = row["text"]
        except Exception as exc:
            log.warning(
                "multipath_resolve_texts_failed",
                error=str(exc),
                trace_id=trace_id,
            )
        return texts

    async def _rerank_fused_items(
        self,
        query_text: str,
        fused_items: Sequence[FusedResult],
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
    ) -> list[FusedResult]:
        """Rerank the capped fused set as a soft ordering signal (AC-5).

        Every item is scored (``top_k = len(docs)``) and the set is reordered by
        rerank score; items the reranker omits keep fused order after the scored
        ones. The reranker never drops an item — a disabled/failed/empty reranker
        returns the fused order unchanged. It orders; it does not gate.

        Args:
            query_text: The recall query the reranker scores against.
            fused_items: The capped fused set (already RRF-ordered).
            trace_id: Request trace id for event correlation.
            session_id: Session id for event correlation.

        Returns:
            The fused items, reordered by rerank score; a permutation of the input
            (never a subset).
        """
        current_settings = get_settings()
        items = list(fused_items)
        if not current_settings.reranker_enabled or not query_text or len(items) <= 1:
            return items

        texts = await self._resolve_item_texts(items, trace_id=trace_id)
        docs = [texts.get(it.item_id, "") for it in items]
        try:
            from personal_agent.memory.reranker import rerank  # noqa: PLC0415

            rerank_results = await rerank(
                query=query_text,
                documents=docs,
                top_k=len(docs),
                trace_id=trace_id,
                session_id=session_id,
            )
        except Exception as exc:
            log.warning(
                "multipath_rerank_failed",
                error=str(exc),
                trace_id=trace_id,
                session_id=session_id,
            )
            return items
        if not rerank_results:
            return items

        scored: dict[int, float] = {
            rr.index: rr.score for rr in rerank_results if 0 <= rr.index < len(items)
        }
        scored_order = sorted(scored, key=lambda i: (-scored[i], i))
        unscored_order = [i for i in range(len(items)) if i not in scored]
        return [items[i] for i in [*scored_order, *unscored_order]]

    async def _multipath_broad_entities(
        self,
        query_text: str,
        *,
        limit: int,
        entity_types: list[str] | None,
        user_id: UUID | None,
        authenticated: bool,
        trace_id: str | None,
        session_id: str | None,
    ) -> list[dict[str, Any]]:
        """Resolve the multi-path fused set into the broad path's entity payload.

        Runs the shared core (path="broad"), then maps its ordered fused items back
        to ``{name, type, description, mentions}`` entity dicts in fused-rank order:
        entity items resolve to themselves; turn items (surfaced by the lexical arm)
        expand to the entities they discuss. Deduped by name (first — highest fused
        rank — wins), optionally filtered to ``entity_types``, and capped to ``limit``.

        Args:
            query_text: The recall query.
            limit: Max entities to return.
            entity_types: Optional type filter (applied in Python; None = all types).
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity.
            trace_id: Request trace id for event correlation.
            session_id: Session id for event correlation.

        Returns:
            Entity dicts ordered by fused rank, in the broad payload shape.
        """
        recall = await self._multipath_fused_recall(
            query_text,
            path="broad",
            trace_id=trace_id,
            session_id=session_id,
            user_id=user_id,
            authenticated=authenticated,
        )
        if not recall.items or not self.driver:
            return []

        entity_ids = [it.item_id for it in recall.items if it.kind == "entity"]
        turn_ids = [it.item_id for it in recall.items if it.kind == "turn"]
        vis_e, vis_params = _build_visibility_filter("e", user_id, authenticated)

        by_entity_id: dict[str, dict[str, Any]] = {}
        by_turn_id: dict[str, list[dict[str, Any]]] = {}
        try:
            async with self.driver.session() as session:
                if entity_ids:
                    r = await session.run(
                        f"""
                        UNWIND $ids AS eid
                        MATCH (e:Entity) WHERE elementId(e) = eid AND {vis_e}
                        OPTIONAL MATCH (e)<-[:DISCUSSES]-(mt:Turn)
                        RETURN eid AS id, e.name AS name, e.entity_type AS type,
                               e.description AS description, count(mt) AS mentions
                        """,
                        ids=entity_ids,
                        **vis_params,
                    )
                    for row in await r.data():
                        by_entity_id[row["id"]] = {
                            "name": row["name"],
                            "type": row["type"],
                            "description": row["description"],
                            "mentions": row["mentions"],
                        }
                if turn_ids:
                    r = await session.run(
                        f"""
                        UNWIND $ids AS tid
                        MATCH (t:Turn {{turn_id: tid}})-[:DISCUSSES]->(e:Entity)
                        WHERE {vis_e}
                        OPTIONAL MATCH (e)<-[:DISCUSSES]-(mt:Turn)
                        RETURN tid AS id, e.name AS name, e.entity_type AS type,
                               e.description AS description, count(mt) AS mentions
                        """,
                        ids=turn_ids,
                        **vis_params,
                    )
                    for row in await r.data():
                        by_turn_id.setdefault(row["id"], []).append(
                            {
                                "name": row["name"],
                                "type": row["type"],
                                "description": row["description"],
                                "mentions": row["mentions"],
                            }
                        )
        except Exception as exc:
            log.warning(
                "multipath_broad_resolve_failed",
                error=str(exc),
                trace_id=trace_id,
                session_id=session_id,
            )
            return []

        ordered: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for item in recall.items:
            resolved = (
                [by_entity_id[item.item_id]]
                if item.kind == "entity" and item.item_id in by_entity_id
                else by_turn_id.get(item.item_id, [])
            )
            for ent in resolved:
                name = ent.get("name")
                if not name or name in seen_names:
                    continue
                if entity_types and ent.get("type") not in entity_types:
                    continue
                seen_names.add(name)
                ordered.append(ent)
                if len(ordered) >= limit:
                    return ordered
        return ordered

    @staticmethod
    def _turn_node_from_node(node: Any) -> TurnNode:
        """Build a TurnNode from a raw Neo4j Turn node (shared resolver helper)."""
        turn_id = node.get("turn_id") or node.get("conversation_id", "")
        return TurnNode(
            turn_id=turn_id,
            trace_id=node.get("trace_id"),
            session_id=node.get("session_id"),
            sequence_number=node.get("sequence_number", 0),
            timestamp=datetime.fromisoformat(node.get("timestamp", datetime.utcnow().isoformat())),
            summary=node.get("summary"),
            user_message=node.get("user_message", ""),
            assistant_response=node.get("assistant_response"),
            key_entities=node.get("key_entities", []),
            properties=orjson.loads(node.get("properties", "{}"))
            if isinstance(node.get("properties"), str)
            else node.get("properties", {}),
        )

    async def _multipath_query_memory(
        self,
        query: "MemoryQuery",
        query_text: str,
        *,
        access_context: AccessContext,
        trace_id: str | None,
        session_id: str | None,
        user_id: UUID | None,
        authenticated: bool,
    ) -> "MemoryQueryResult":
        """Entity-name path convergence onto the multi-path core (FRE-724).

        Runs the shared fused+reranked core (path="entity") and resolves its
        ordered items into the ``MemoryQueryResult`` turn set: turn items map
        straight to their TurnNode, entity items expand to their most-recent turns
        (bounded by ``recall_per_entity_turn_cap``). Relevance scores are the fused
        rank normalised to (0, 1] so downstream consumers keep a monotonic ordering
        signal. Turns are deduped by turn_id and sliced to ``query.limit``.

        Args:
            query: The original memory query (for the result limit + access event).
            query_text: The recall query text.
            access_context: Typed access context (ADR-0042).
            trace_id: Request trace id for event correlation.
            session_id: Session id for event correlation.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity.

        Returns:
            MemoryQueryResult with fused+reranked conversations and rank-derived
            relevance scores.
        """
        recall = await self._multipath_fused_recall(
            query_text,
            path="entity",
            trace_id=trace_id,
            session_id=session_id,
            user_id=user_id,
            authenticated=authenticated,
        )
        conversations: list[TurnNode] = []
        relevance_scores: dict[str, float] = {}
        if recall.items and self.driver:
            turns_by_entity, turns_by_id = await self._resolve_fused_turns(
                recall.items,
                per_entity_cap=get_settings().recall_per_entity_turn_cap,
                user_id=user_id,
                authenticated=authenticated,
                trace_id=trace_id,
            )
            seen: set[str] = set()
            total = len(recall.items)
            for position, item in enumerate(recall.items):
                resolved = (
                    [turns_by_id[item.item_id]]
                    if item.kind == "turn" and item.item_id in turns_by_id
                    else turns_by_entity.get(item.item_id, [])
                )
                # FRE-658: the fused arms take no recency predicate, so an explicit
                # caller-supplied hard window is enforced here as a post-recall
                # filter (dropped before the limit fill, so in-window turns are not
                # crowded out). No-op when hard_recency_days is None (AC-1a).
                resolved = _filter_turns_by_hard_recency(resolved, query.hard_recency_days)
                for turn in resolved:
                    if turn.turn_id in seen:
                        continue
                    seen.add(turn.turn_id)
                    conversations.append(turn)
                    relevance_scores[turn.turn_id] = (total - position) / total
                    if len(conversations) >= query.limit:
                        break
                if len(conversations) >= query.limit:
                    break

        accessed_entity_ids = list(query.entity_names or [])
        for conversation in conversations:
            accessed_entity_ids.extend(conversation.key_entities or [])
        accessed_entity_ids = list(dict.fromkeys(accessed_entity_ids))

        log.info(
            "memory_query_completed",
            query_params=["multipath"],
            result_count=len(conversations),
            trace_id=trace_id,
            session_id=session_id,
        )
        if settings.freshness_enabled and accessed_entity_ids and trace_id:
            event = MemoryAccessedEvent(
                entity_ids=accessed_entity_ids,
                relationship_ids=[],
                access_context=access_context,
                query_type="query_memory_multipath",
                trace_id=trace_id,
                session_id=session_id,
                source_component="memory.service",
            )
            bus = get_event_bus()
            try:
                await bus.publish(STREAM_MEMORY_ACCESSED, event)
            except Exception as pub_exc:
                log.warning(
                    "memory_access_event_publish_failed",
                    error=str(pub_exc),
                    event_id=event.event_id,
                    trace_id=trace_id,
                )
        return MemoryQueryResult(
            conversations=conversations,
            relevance_scores=relevance_scores,
        )

    async def _resolve_fused_turns(
        self,
        items: Sequence[FusedResult],
        *,
        per_entity_cap: int,
        user_id: UUID | None,
        authenticated: bool,
        trace_id: str | None,
    ) -> tuple[dict[str, list[TurnNode]], dict[str, TurnNode]]:
        """Resolve a fused set into TurnNodes for the entity-name path.

        Args:
            items: The capped fused set.
            per_entity_cap: Max most-recent turns to expand per entity item.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity.
            trace_id: Request trace id for event correlation.

        Returns:
            Tuple of (entity elementId -> its expanded TurnNodes, turn_id -> TurnNode).
        """
        by_entity: dict[str, list[TurnNode]] = {}
        by_turn: dict[str, TurnNode] = {}
        if not self.driver or not items:
            return by_entity, by_turn
        entity_ids = [it.item_id for it in items if it.kind == "entity"]
        turn_ids = [it.item_id for it in items if it.kind == "turn"]
        vis_t, vis_params = _build_visibility_filter("t", user_id, authenticated)
        try:
            async with self.driver.session() as session:
                if turn_ids:
                    r = await session.run(
                        f"""
                        UNWIND $ids AS tid
                        MATCH (t:Turn {{turn_id: tid}}) WHERE {vis_t}
                        RETURN t
                        """,
                        ids=turn_ids,
                        **vis_params,
                    )
                    for row in await r.values():
                        if row and row[0]:
                            node = self._turn_node_from_node(row[0])
                            by_turn[node.turn_id] = node
                if entity_ids:
                    r = await session.run(
                        f"""
                        UNWIND $ids AS eid
                        MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)
                        WHERE elementId(e) = eid AND {vis_t}
                        WITH eid, t ORDER BY t.timestamp DESC
                        WITH eid, collect(t)[0..$cap] AS turns
                        UNWIND turns AS t
                        RETURN eid AS eid, t
                        """,
                        ids=entity_ids,
                        cap=per_entity_cap,
                        **vis_params,
                    )
                    for row in await r.values():
                        eid, node_raw = row[0], row[1]
                        if node_raw:
                            by_entity.setdefault(eid, []).append(
                                self._turn_node_from_node(node_raw)
                            )
        except Exception as exc:
            log.warning(
                "multipath_resolve_turns_failed",
                error=str(exc),
                trace_id=trace_id,
            )
        return by_entity, by_turn

    async def multi_query_recall_arm(
        self,
        query_text: str,
        *,
        limit: int | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
    ) -> list[RankedResult]:
        """Multi-query paraphrase recall arm (ADR-0104 / FRE-723).

        Fans the original query plus up to (multipath_paraphrase_count - 1)
        local-model paraphrases through the dense vector arm, and RRF-fuses
        the per-variant ranked lists into one. Direct mitigation for the
        open-vocabulary miss (e.g. "vision" vs. "perception") without
        write-side canonicalization. Feature-gated OFF
        (``multiquery_arm_enabled``); flag-dark until FRE-724. Degrades to
        the dense arm on the original query alone if paraphrase generation
        fails or returns nothing — never hard-fails recall.

        Args:
            query_text: Free-text query.
            limit: Max fused hits; defaults to ``multipath_arm_top_k``.
            trace_id: Request trace id for event correlation.
            session_id: Session id for event correlation.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity.

        Returns:
            Ranked, RRF-fused list of RankedResult (best-first). item_id is
            Entity elementId. Empty when the arm is gated off, disconnected,
            or the query is empty.
        """
        current_settings = get_settings()
        if not current_settings.multiquery_arm_enabled:
            return []
        if not self.connected or not self.driver or not query_text.strip():
            return []

        top_k = limit if limit is not None else current_settings.multipath_arm_top_k
        paraphrase_count = max(current_settings.multipath_paraphrase_count - 1, 0)
        try:
            paraphrases = await generate_query_paraphrases(
                query_text, paraphrase_count, trace_id=trace_id, session_id=session_id
            )
        except Exception as exc:
            # Defense in depth: generate_query_paraphrases already fails open
            # internally, but this arm's own contract ("must never hard-fail
            # recall") does not get to depend on a collaborator never
            # regressing that guarantee.
            log.warning(
                "multiquery_paraphrase_call_failed",
                error=str(exc),
                trace_id=trace_id,
                session_id=session_id,
            )
            paraphrases = []
        variants = [query_text, *paraphrases]

        vis_frag, vis_params = _build_visibility_filter("node", user_id, authenticated)
        arm_rankings: list[list[RankedResult]] = []
        try:
            async with self.driver.session() as session:
                for variant in variants:
                    # Per-variant isolation: one variant's embedding/ANN
                    # failure must not zero out the other variants' results,
                    # matching query_memory's existing per-call isolation.
                    try:
                        embedding = await generate_embedding(variant, mode="query")
                        ranked = await self._dense_vector_search_ranked(
                            session, embedding, top_k, vis_frag, vis_params
                        )
                    except Exception as exc:
                        log.warning(
                            "multiquery_variant_search_failed",
                            error=str(exc),
                            trace_id=trace_id,
                            session_id=session_id,
                        )
                        continue
                    if ranked:
                        arm_rankings.append(ranked)
        except Exception as exc:
            # Session acquisition/release failure (transient
            # ServiceUnavailable, pool exhaustion, etc.) must not hard-fail
            # recall — matches lexical_recall_arm / structural_recall_arm,
            # which both wrap their whole session block (master gate finding,
            # 2026-07-02). Falls through to fusion with whatever arm_rankings
            # was accumulated before the failure (empty if it failed on
            # acquisition, before any variant ran).
            log.error(
                "multiquery_session_failed",
                error=str(exc),
                trace_id=trace_id,
                session_id=session_id,
            )

        fused = reciprocal_rank_fusion(arm_rankings, k=current_settings.multipath_rrf_k)
        # reciprocal_rank_fusion returns list[FusedResult]; this arm's contract
        # is list[RankedResult] (matching lexical_recall_arm and the arm
        # interface FRE-724 consumes) — re-rank the fused order into
        # RankedResult.
        ranked_fused = [
            RankedResult(item_id=f.item_id, rank=i + 1) for i, f in enumerate(fused[:top_k])
        ]
        log.info(
            "multi_query_recall_arm_completed",
            arm="multi_query",
            variant_count=len(variants),
            paraphrase_count=len(paraphrases),
            hit_count=len(ranked_fused),
            trace_id=trace_id,
            session_id=session_id,
        )
        return ranked_fused

    async def query_memory_broad(
        self,
        entity_types: list[str] | None = None,
        recency_days: int = 90,
        limit: int = 20,
        access_context: AccessContext = AccessContext.SEARCH,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
        query_text: str | None = None,
    ) -> dict[str, Any]:
        """Broad memory recall: return entities and session summaries (ADR-0025).

        Used for recall-intent queries ("what have I asked about?") where
        there are no specific entity names to search for.

        Args:
            entity_types: Optional filter e.g. ["Location", "Person"]. None = all types.
            recency_days: How far back to look.
            limit: Maximum entities to return.
            access_context: Typed context where the query originated (ADR-0042).
            trace_id: Optional request trace identifier for event correlation.
            session_id: Optional session identifier for event correlation.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity (FRE-229).
            query_text: Optional original user query text (ADR-0100 FRE-654). When
                provided and relevance_bounded_recall_enabled is on, the entity
                candidate set is the union of the recency window and the
                vector-relevant entities across all time (the 90-day cutoff is
                demoted to a ranking signal, not a hard gate). Default off / None
                reproduces legacy recency-only behaviour exactly.

        Returns:
            Dict with keys:
              - entities: list of {name, type, mentions, description}. Note: under
                the relevance-bounded path, ``mentions`` for a vector-surfaced
                out-of-window entity counts all-time turns (an ordering hint, not a
                gate), whereas non-vector entities count within the recency window.
              - sessions: list of {session_id, dominant_entities, turn_count, started_at}
              - turns_summary: list of recent turn summaries
        """
        if not self.connected or not self.driver:
            return {"entities": [], "sessions": [], "turns_summary": []}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=recency_days)).isoformat()
        turn_vis_frag, vis_params = _build_visibility_filter("t", user_id, authenticated)
        sess_vis_frag, _ = _build_visibility_filter("s", user_id, authenticated)

        current_settings = get_settings()
        relevance_bounded = current_settings.relevance_bounded_recall_enabled and bool(query_text)

        try:
            async with self.driver.session() as db_session:
                # Multi-path recall (ADR-0104 / FRE-724): behind
                # multipath_recall_enabled, the entity set is produced by the shared
                # fused+reranked core and resolved back to the broad payload shape.
                # Flag off reproduces the ADR-0100 / legacy entity path below,
                # byte-for-byte.
                if current_settings.multipath_recall_enabled and query_text:
                    entities = await self._multipath_broad_entities(
                        query_text,
                        limit=limit,
                        entity_types=entity_types,
                        user_id=user_id,
                        authenticated=authenticated,
                        trace_id=trace_id,
                        session_id=session_id,
                    )
                else:
                    # Relevance-keyed candidate entities (ADR-0100): vector top-k over
                    # entity_embedding, floor-filtered. Empty when the flag is off, no
                    # query_text, or the embedding is unavailable — in which case the
                    # entity query below collapses to the legacy recency-only path.
                    relevant_entity_names: list[str] = []
                    entity_scores: dict[str, float] = {}
                    if relevance_bounded and query_text:
                        try:
                            query_embedding = await generate_embedding(query_text, mode="query")
                            if any(x != 0.0 for x in query_embedding):
                                rows = await self._query_entity_vector_candidates(
                                    db_session,
                                    query_embedding,
                                    current_settings.proactive_memory_vector_top_k,
                                )
                                relevant_entity_names, entity_scores = _filter_entities_by_floor(
                                    rows, current_settings.recall_similarity_floor
                                )
                        except Exception as vec_exc:
                            log.warning(
                                "broad_vector_search_failed",
                                error=str(vec_exc),
                                trace_id=trace_id,
                            )

                    # Entities: legacy recency-only, or (flag on) recency-window UNION
                    # vector-relevant entities across all time, ranked by relevance.
                    if relevance_bounded:
                        entity_type_clause = (
                            "AND e.entity_type IN $entity_types\n" if entity_types else ""
                        )
                        entity_q = f"""
                            MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)
                            WHERE {turn_vis_frag}
                              {entity_type_clause}
                              AND (t.timestamp >= $cutoff OR e.name IN $relevant_entity_names)
                            WITH e, count(t) AS mentions,
                                 coalesce($entity_scores[e.name], 0.0) AS escore
                            RETURN e.name AS name, e.entity_type AS type,
                                   e.description AS description, mentions
                            ORDER BY escore DESC, mentions DESC LIMIT $limit
                        """
                        params: dict[str, Any] = {
                            "cutoff": cutoff,
                            "limit": limit,
                            "relevant_entity_names": relevant_entity_names,
                            "entity_scores": entity_scores,
                            **vis_params,
                        }
                        if entity_types:
                            params["entity_types"] = entity_types
                        r = await db_session.run(entity_q, **params)
                    elif entity_types:
                        entity_q = f"""
                            MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)
                            WHERE {turn_vis_frag}
                              AND e.entity_type IN $entity_types
                              AND t.timestamp >= $cutoff
                            RETURN e.name as name, e.entity_type as type,
                                   e.description as description,
                                   count(t) as mentions
                            ORDER BY mentions DESC LIMIT $limit
                        """
                        r = await db_session.run(
                            entity_q,
                            entity_types=entity_types,
                            cutoff=cutoff,
                            limit=limit,
                            **vis_params,
                        )
                    else:
                        entity_q = f"""
                            MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)
                            WHERE {turn_vis_frag}
                              AND t.timestamp >= $cutoff
                            RETURN e.name as name, e.entity_type as type,
                                   e.description as description,
                                   count(t) as mentions
                            ORDER BY mentions DESC LIMIT $limit
                        """
                        r = await db_session.run(entity_q, cutoff=cutoff, limit=limit, **vis_params)
                    entities = await r.data()

                # Recent sessions with dominant topics
                session_q = f"""
                    MATCH (s:Session)
                    WHERE {sess_vis_frag}
                      AND s.started_at >= $cutoff
                    RETURN s.session_id as session_id,
                           s.dominant_entities as dominant_entities,
                           s.turn_count as turn_count,
                           s.started_at as started_at
                    ORDER BY s.started_at DESC LIMIT 10
                """
                r = await db_session.run(session_q, cutoff=cutoff, **vis_params)
                sessions = await r.data()

                # Recent turn summaries
                turn_q = f"""
                    MATCH (t:Turn)
                    WHERE {turn_vis_frag}
                      AND t.timestamp >= $cutoff
                    RETURN t.summary as summary, t.key_entities as entities,
                           t.timestamp as ts
                    ORDER BY t.timestamp DESC LIMIT 10
                """
                r = await db_session.run(turn_q, cutoff=cutoff, **vis_params)
                turns = await r.data()

                payload = {
                    "entities": entities,
                    "sessions": sessions,
                    "turns_summary": turns,
                }

                # Publish memory access event (Phase 4 / ADR-0042)
                if settings.freshness_enabled and trace_id and entities:
                    accessed_entity_ids = [
                        e["name"] for e in entities if isinstance(e, dict) and e.get("name")
                    ]
                    relationship_element_ids: list[str] = []
                    if accessed_entity_ids:
                        rel_result = await db_session.run(
                            """
                            MATCH (e:Entity)<-[rel:DISCUSSES]-(t:Turn)
                            WHERE e.name IN $names AND t.timestamp >= $cutoff
                            RETURN collect(DISTINCT elementId(rel)) AS ids
                            """,
                            names=accessed_entity_ids,
                            cutoff=cutoff,
                        )
                        rel_rec = await rel_result.single()
                        raw_ids = rel_rec.get("ids") if rel_rec else None
                        if raw_ids:
                            relationship_element_ids = [str(x) for x in raw_ids if x is not None]
                    if accessed_entity_ids:
                        event = MemoryAccessedEvent(
                            entity_ids=accessed_entity_ids,
                            relationship_ids=relationship_element_ids,
                            access_context=access_context,
                            query_type="query_memory_broad",
                            trace_id=trace_id,
                            session_id=session_id,
                            source_component="memory.service",
                        )
                        bus = get_event_bus()
                        try:
                            await bus.publish(STREAM_MEMORY_ACCESSED, event)
                            log.debug(
                                "memory_access_event_published",
                                trace_id=trace_id,
                                entity_count=len(accessed_entity_ids),
                                access_context=access_context.value,
                            )
                        except Exception as pub_exc:
                            log.warning(
                                "memory_access_event_publish_failed",
                                error=str(pub_exc),
                                event_id=event.event_id,
                                trace_id=trace_id,
                            )

                return payload

        except Exception as e:
            log.error(
                "query_memory_broad_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
                session_id=session_id,
            )
            return {"entities": [], "sessions": [], "turns_summary": []}

    def _log_query_quality_metrics(
        self,
        query: MemoryQuery,
        relevance_scores: dict[str, float],
        feedback_key: str | None,
        query_text: str | None,
        trace_id: str | None = None,
    ) -> None:
        """Emit memory query quality metrics and implicit feedback signal."""
        result_count = len(relevance_scores)
        avg_relevance = sum(relevance_scores.values()) / result_count if result_count > 0 else 0.0
        max_relevance = max(relevance_scores.values(), default=0.0)
        min_relevance = min(relevance_scores.values(), default=0.0)
        query_signature = self._build_query_signature(query, query_text)
        state_key = feedback_key or "global"
        previous_state = self._query_feedback_by_key.get(state_key)
        implicit_rephrase = self._detect_implicit_rephrase(previous_state, query_signature)

        log.info(
            "memory_query_quality_metrics",
            query_type=self._classify_query_type(query),
            result_count=result_count,
            avg_relevance_score=round(avg_relevance, 4),
            max_relevance_score=round(max_relevance, 4),
            min_relevance_score=round(min_relevance, 4),
            entity_filter_count=len(query.entity_names),
            entity_type_filter_count=len(query.entity_types),
            trace_filter_count=len(query.trace_ids),
            conversation_filter_count=len(query.conversation_ids),
            recency_days=query.recency_days,
            implicit_rephrase_detected=implicit_rephrase,
            previous_result_count=(previous_state or {}).get("result_count"),
            trace_id=trace_id,
        )
        self._query_feedback_by_key[state_key] = {
            "signature": query_signature,
            "result_count": result_count,
            "timestamp": datetime.now(timezone.utc),
        }

    def _classify_query_type(self, query: MemoryQuery) -> str:
        """Classify query shape for analytics aggregation."""
        if query.entity_names:
            return "entity_name_lookup"
        if query.entity_types:
            return "entity_type_lookup"
        if query.conversation_ids:
            return "conversation_lookup"
        if query.trace_ids:
            return "trace_lookup"
        return "recent_conversations"

    def _build_query_signature(self, query: MemoryQuery, query_text: str | None) -> str:
        """Create normalized signature for implicit feedback tracking."""
        normalized_text = (query_text or "").strip().lower()
        entity_names = ",".join(sorted(name.lower() for name in query.entity_names))
        entity_types = ",".join(sorted(entity_type.lower() for entity_type in query.entity_types))
        conversation_ids = ",".join(sorted(query.conversation_ids))
        trace_ids = ",".join(sorted(query.trace_ids))
        return (
            f"text={normalized_text}|entities={entity_names}|types={entity_types}|"
            f"conversations={conversation_ids}|traces={trace_ids}|recency={query.recency_days}"
        )

    def _detect_implicit_rephrase(
        self,
        previous_state: dict[str, Any] | None,
        current_signature: str,
    ) -> bool:
        """Detect likely rephrase from sequential query behavior."""
        if not previous_state:
            return False

        previous_signature = str(previous_state.get("signature", ""))
        previous_result_count = int(previous_state.get("result_count", 0) or 0)
        previous_timestamp = previous_state.get("timestamp")
        if not isinstance(previous_timestamp, datetime):
            return False

        recency_seconds = (datetime.now(timezone.utc) - previous_timestamp).total_seconds()
        if recency_seconds > 600:  # 10 minutes
            return False
        if previous_signature == current_signature:
            return False
        return previous_result_count <= 1

    async def _calculate_relevance_scores(
        self,
        conversations: list[TurnNode],
        query: MemoryQuery,
        vector_scores: dict[str, float] | None = None,
        reranker_scores: dict[str, float] | None = None,
        trace_id: str | None = None,
    ) -> dict[str, float]:
        """Calculate relevance/plausibility scores for conversations.

        Scoring factors depend on which signals are available:

        Base weights (no vector, no reranker):
        1. Recency: 0-0.4
        2. Entity match: 0-0.4
        3. Entity importance: 0-0.2

        Hybrid (vector only):
        1. Recency: 0-0.3
        2. Entity match: 0-0.3
        3. Entity importance: 0-0.15
        4. Vector similarity: 0-0.25

        Full pipeline (vector + reranker):
        1. Recency: 0-0.20
        2. Entity match: 0-0.20
        3. Entity importance: 0-0.10
        4. Vector similarity: 0-0.15
        5. Reranker score: 0-0.35

        Full pipeline + freshness (all signals including access data):
        1. Recency: 0-0.15
        2. Entity match: 0-0.20
        3. Entity importance: 0-0.05
        4. Vector similarity: 0-0.15
        5. Reranker score: 0-0.30
        6. Freshness: 0-0.15

        Args:
            conversations: List of conversations to score.
            query: Original query with entity filters.
            vector_scores: Optional dict mapping entity name to cosine similarity
                score (0-1) from vector index search.
            reranker_scores: Optional dict mapping turn_id to normalized reranker
                relevance score (0-1) from cross-attention reranking.
            trace_id: Optional request trace identifier for log correlation
                (ADR-0074 §I3) on intermediate fetch-failure warnings.

        Returns:
            Dict mapping conversation_id to relevance score (0.0-1.0).
        """
        if not conversations:
            return {}

        scores: dict[str, float] = {}

        # Normalize optional score dicts to concrete dicts (simplifies type narrowing)
        _vector_scores: dict[str, float] = vector_scores if vector_scores is not None else {}
        _reranker_scores: dict[str, float] = reranker_scores if reranker_scores is not None else {}

        # Normalize all timestamps to naive UTC to avoid mixed tz comparisons
        now = datetime.utcnow()

        def _to_naive_utc(dt: datetime) -> datetime:
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt

        # Find oldest conversation for recency normalization
        oldest_timestamp = min(_to_naive_utc(c.timestamp) for c in conversations)
        time_range = (now - oldest_timestamp).total_seconds()

        # Get entity importance scores if querying by entities
        entity_importance: dict[str, float] = {}
        if query.entity_names and self.driver:
            ent_vis_frag, ent_vis_params = _build_visibility_filter(
                "e", query.user_id, query.authenticated
            )
            try:
                async with self.driver.session() as session:
                    result = await session.run(
                        f"""
                        MATCH (e:Entity)
                        WHERE e.name IN $entity_names
                          AND {ent_vis_frag}
                        RETURN e.name as name, e.mention_count as mentions
                        """,
                        entity_names=query.entity_names,
                        **ent_vis_params,
                    )
                    async for record in result:
                        name = record["name"]
                        mentions = record.get("mentions", 0)
                        # Normalize to 0-1 (cap at 100 mentions)
                        entity_importance[name] = min(mentions / 100.0, 1.0)
            except Exception as e:
                log.warning("entity_importance_fetch_failed", error=str(e), trace_id=trace_id)

        # Fetch freshness data when access tracking is enabled (ADR-0042 Step 5)
        # entity_name -> freshness score in [0.0, 1.0]
        freshness_scores: dict[str, float] = {}
        current_settings = get_settings()
        if current_settings.freshness_enabled and query.entity_names and self.driver:
            from personal_agent.memory.freshness import compute_freshness  # noqa: PLC0415

            fresh_vis_frag, fresh_vis_params = _build_visibility_filter(
                "e", query.user_id, query.authenticated
            )
            try:
                async with self.driver.session() as session:
                    result = await session.run(
                        f"""
                        MATCH (e:Entity)
                        WHERE e.name IN $entity_names
                          AND e.access_count IS NOT NULL
                          AND e.access_count > 0
                          AND {fresh_vis_frag}
                        RETURN e.name AS name,
                               e.last_accessed_at AS last_accessed_at,
                               e.access_count AS access_count
                        """,
                        entity_names=query.entity_names,
                        **fresh_vis_params,
                    )
                    async for record in result:
                        raw_ts = record.get("last_accessed_at")
                        last_accessed_at: datetime | None = None
                        if raw_ts is not None:
                            try:
                                last_accessed_at = datetime.fromisoformat(str(raw_ts))
                            except (ValueError, TypeError):
                                last_accessed_at = None
                        fs = compute_freshness(
                            last_accessed_at=last_accessed_at,
                            access_count=int(record.get("access_count") or 0),
                            half_life_days=current_settings.freshness_half_life_days,
                            alpha=current_settings.freshness_frequency_boost_alpha,
                            max_boost=current_settings.freshness_frequency_boost_max,
                        )
                        freshness_scores[record["name"]] = fs
            except Exception as e:
                log.warning("freshness_scores_fetch_failed", error=str(e), trace_id=trace_id)

        # Determine weight scheme based on available signals
        use_vector = bool(_vector_scores)
        use_reranker = bool(_reranker_scores)
        use_freshness = current_settings.freshness_enabled and bool(freshness_scores)
        # freshness_weight from config; only active when access data is available.
        # When active, all other weights are scaled by (1 - w_freshness) so they
        # sum to 1.0 regardless of which signals are present (graceful degradation).
        w_freshness_cfg = current_settings.freshness_relevance_weight if use_freshness else 0.0
        w_scale = 1.0 - w_freshness_cfg  # redistribution factor for non-freshness signals
        if use_vector and use_reranker:
            w_recency = 0.20 * w_scale
            w_entity_match = 0.20 * w_scale
            w_importance = 0.10 * w_scale
            w_vector = 0.15 * w_scale
            w_reranker = 0.35 * w_scale
        elif use_vector:
            w_recency = 0.30 * w_scale
            w_entity_match = 0.30 * w_scale
            w_importance = 0.15 * w_scale
            w_vector = 0.25 * w_scale
            w_reranker = 0.0
        else:
            w_recency = 0.40 * w_scale
            w_entity_match = 0.40 * w_scale
            w_importance = 0.20 * w_scale
            w_vector = 0.0
            w_reranker = 0.0

        # Calculate scores for each conversation
        for conv in conversations:
            # Per-conversation vector overlap check: if this conversation has
            # no entities matching the vector results, fall back to non-hybrid
            # weights to avoid score deflation (the 0.25 vector slot would be 0).
            conv_has_vector_hit = False
            best_vector_score = 0.0
            if use_vector and conv.key_entities:
                entity_vector_scores = [
                    _vector_scores[entity]
                    for entity in conv.key_entities
                    if entity in _vector_scores
                ]
                if entity_vector_scores:
                    conv_has_vector_hit = True
                    best_vector_score = max(entity_vector_scores)

            # Check if this conversation has a reranker score
            conv_reranker_score = _reranker_scores.get(conv.turn_id, 0.0)
            conv_has_reranker = use_reranker and conv.turn_id in _reranker_scores

            if conv_has_vector_hit:
                cw_recency, cw_entity, cw_importance, cw_vector = (
                    w_recency,
                    w_entity_match,
                    w_importance,
                    w_vector,
                )
                cw_reranker = w_reranker if conv_has_reranker else 0.0
            else:
                # Non-hybrid weights for this conversation (redistribute reranker weight)
                if conv_has_reranker:
                    cw_recency, cw_entity, cw_importance, cw_vector = (
                        0.25 * w_scale,
                        0.25 * w_scale,
                        0.15 * w_scale,
                        0.0,
                    )
                    cw_reranker = 0.35 * w_scale
                else:
                    cw_recency, cw_entity, cw_importance, cw_vector = (
                        0.40 * w_scale,
                        0.40 * w_scale,
                        0.20 * w_scale,
                        0.0,
                    )
                    cw_reranker = 0.0

            score = 0.0

            # 1. Recency score
            if time_range > 0:
                age_seconds = (now - _to_naive_utc(conv.timestamp)).total_seconds()
                recency_ratio = 1.0 - (age_seconds / time_range)
                score += recency_ratio * cw_recency
            else:
                score += cw_recency  # All same timestamp

            # 2. Entity match score
            if query.entity_names:
                matched_entities = set(query.entity_names) & set(conv.key_entities)
                match_ratio = len(matched_entities) / len(query.entity_names)
                score += match_ratio * cw_entity
            else:
                score += cw_entity * 0.5  # No entity filter, give neutral score

            # 3. Entity importance score
            if entity_importance:
                matched_importances = [
                    entity_importance.get(entity, 0.0)
                    for entity in conv.key_entities
                    if entity in entity_importance
                ]
                if matched_importances:
                    avg_importance = sum(matched_importances) / len(matched_importances)
                    score += avg_importance * cw_importance

            # 4. Vector similarity score (hybrid mode only)
            if conv_has_vector_hit:
                score += best_vector_score * cw_vector

            # 5. Reranker score (cross-attention relevance)
            if conv_has_reranker:
                score += conv_reranker_score * cw_reranker

            # 6. Freshness score (access recency × frequency, ADR-0042)
            #    + StalenessTier multiplier (ADR-0060 §D5)
            # Uses max freshness score across matched query entities for this conversation.
            # When no freshness data is available for any of this conversation's entities,
            # the factor is skipped and the redistributed weight stays with the other signals.
            if use_freshness and conv.key_entities:
                conv_freshness_scores = [
                    freshness_scores[e]
                    for e in conv.key_entities
                    if e in freshness_scores and freshness_scores[e] > 0.0
                ]
                if conv_freshness_scores:
                    best_freshness = max(conv_freshness_scores)
                    if current_settings.freshness_tier_reranking_enabled:
                        from personal_agent.memory.freshness import (  # noqa: PLC0415
                            staleness_tier_from_freshness_score,
                        )

                        tier = staleness_tier_from_freshness_score(best_freshness)
                        tier_factor = current_settings.freshness_tier_factors.get(tier.value, 1.0)
                        best_freshness *= tier_factor
                    score += best_freshness * w_freshness_cfg

            scores[conv.turn_id] = min(score, 1.0)  # Cap at 1.0

        return scores

    async def get_related_conversations(
        self, entity_names: list[str], limit: int = 10
    ) -> list[TurnNode]:
        """Get conversations related to given entities.

        Args:
            entity_names: List of entity names to search for
            limit: Maximum number of conversations to return

        Returns:
            List of related conversations
        """
        query = MemoryQuery(entity_names=entity_names, limit=limit)
        result = await self.query_memory(query)
        return result.conversations

    async def get_user_interests(
        self,
        limit: int = 20,
        user_id: UUID | None = None,
        authenticated: bool = False,
        trace_id: str | None = None,
    ) -> list[EntityNode]:
        """Get entities the user frequently mentions (interest profile).

        Args:
            limit: Maximum number of entities to return
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity (FRE-229).
            trace_id: Optional request trace identifier for log correlation
                (ADR-0074 §I3).

        Returns:
            List of entities sorted by mention frequency
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected", trace_id=trace_id)
            return []

        vis_frag, vis_params = _build_visibility_filter("e", user_id, authenticated)
        try:
            async with self.driver.session() as session:
                result = await session.run(
                    f"""
                    MATCH (e:Entity)
                    WHERE e.mention_count > 0
                      AND {vis_frag}
                    RETURN e
                    ORDER BY e.mention_count DESC, e.last_seen DESC
                    LIMIT $limit
                    """,
                    limit=limit,
                    **vis_params,
                )

                entities = []
                async for record in result:
                    entities.append(_entity_node_from_record(record["e"]))

                log.info("user_interests_retrieved", count=len(entities), trace_id=trace_id)
                return entities
        except Exception as e:
            log.error(
                "user_interests_query_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
            )
            return []

    async def promote_entity(
        self,
        entity_name: str,
        confidence: float,
        source_turn_ids: list[str],
        trace_id: str = "",
    ) -> bool:
        """Promote an entity to semantic memory.

        Sets memory_type='semantic', confidence, promoted_at on the Entity node.

        Args:
            entity_name: The entity to promote.
            confidence: Confidence score for the semantic fact.
            source_turn_ids: Turn IDs supporting this promotion.
            trace_id: Request trace identifier.

        Returns:
            True if the entity was found and promoted.
        """
        if not self.driver:
            log.warning(
                "promote_entity_no_driver",
                entity_name=entity_name,
                trace_id=trace_id,
            )
            return False

        query = """
        MATCH (e:Entity {name: $name})
        SET e.memory_type = 'semantic',
            e.confidence = $confidence,
            e.promoted_at = datetime(),
            e.source_turn_ids = $source_turn_ids
        RETURN e.name AS name, e.entity_type AS entity_type,
               e.mention_count AS mention_count
        """

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    query,
                    name=entity_name,
                    confidence=confidence,
                    source_turn_ids=source_turn_ids,
                )
                record = await result.single()
                if record is None:
                    log.debug(
                        "promote_entity_not_found",
                        entity_name=entity_name,
                        trace_id=trace_id,
                    )
                    return False

                log.info(
                    "promote_entity_success",
                    entity_name=entity_name,
                    entity_type=record["entity_type"],
                    confidence=confidence,
                    trace_id=trace_id,
                )
                return True
        except Exception:
            log.warning(
                "promote_entity_neo4j_error",
                entity_name=entity_name,
                trace_id=trace_id,
                exc_info=True,
            )
            return False

    async def get_promotion_candidates(
        self,
        min_mentions: int = 1,
        exclude_already_promoted: bool = True,
    ) -> Sequence[PromotionCandidate]:
        """Query Neo4j for entities eligible for episodic→semantic promotion.

        Args:
            min_mentions: Minimum mention count to include an entity.
            exclude_already_promoted: If True, skip entities already promoted
                to semantic memory.

        Returns:
            Sequence of PromotionCandidate ordered by mention count descending.
        """
        if not self.driver:
            log.warning("get_promotion_candidates_no_driver")
            return []

        where_clause = "WHERE e.mention_count >= $min_mentions"
        if exclude_already_promoted:
            where_clause += " AND (e.memory_type IS NULL OR e.memory_type <> 'semantic')"

        query = f"""
        MATCH (e:Entity)
        {where_clause}
        OPTIONAL MATCH (e)<-[:DISCUSSES]-(t:Turn)
        WITH e, collect(t.turn_id) AS turn_ids
        RETURN e.name AS name,
               e.entity_type AS entity_type,
               coalesce(e.mention_count, 1) AS mention_count,
               e.first_seen AS first_seen,
               e.last_seen AS last_seen,
               e.description AS description,
               turn_ids
        ORDER BY e.mention_count DESC
        """

        try:
            async with self.driver.session() as session:
                result = await session.run(query, min_mentions=min_mentions)
                records = await result.data()

            now = datetime.now(timezone.utc)
            candidates: list[PromotionCandidate] = []
            for row in records:
                first_seen = row.get("first_seen")
                last_seen = row.get("last_seen")
                # Neo4j returns its own DateTime type — convert to timezone-aware Python datetime
                if hasattr(first_seen, "to_native"):
                    first_seen = first_seen.to_native()
                elif isinstance(first_seen, str):
                    first_seen = datetime.fromisoformat(first_seen)
                if isinstance(first_seen, datetime) and first_seen.tzinfo is None:
                    first_seen = first_seen.replace(tzinfo=timezone.utc)
                if hasattr(last_seen, "to_native"):
                    last_seen = last_seen.to_native()
                elif isinstance(last_seen, str):
                    last_seen = datetime.fromisoformat(last_seen)
                if isinstance(last_seen, datetime) and last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                candidates.append(
                    PromotionCandidate(
                        entity_name=row["name"],
                        entity_type=row.get("entity_type") or "unknown",
                        mention_count=row["mention_count"],
                        first_seen=first_seen or now,
                        last_seen=last_seen or now,
                        source_turn_ids=[t for t in (row.get("turn_ids") or []) if t],
                        description=row.get("description"),
                    )
                )

            log.info(
                "promotion_candidates_queried",
                total=len(candidates),
                min_mentions=min_mentions,
                exclude_promoted=exclude_already_promoted,
            )
            return candidates

        except Exception:
            log.warning("get_promotion_candidates_failed", exc_info=True)
            return []

    async def aggregate_graph_staleness(self) -> GraphStalenessSummary | None:
        """Compute staleness tier counts and related metrics over the full graph.

        Returns:
            Aggregated summary, or ``None`` when Neo4j is not connected.
        """
        if not self.connected or not self.driver:
            log.warning("aggregate_graph_staleness_no_driver")
            return None
        return await aggregate_graph_staleness(self.driver, get_settings())
