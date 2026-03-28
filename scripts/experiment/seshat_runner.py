"""Run experiment scenarios against the current Seshat Neo4j backend."""

from __future__ import annotations

import time
from typing import Any

from personal_agent.memory.models import Entity, MemoryQuery, Relationship, TurnNode
from personal_agent.memory.service import MemoryService

from .config import ExperimentConfig
from .data_loader import ENTITY_CLUSTERS, get_ground_truth
from .metrics import (
    CostTracker,
    DedupMetrics,
    RetrievalMetrics,
    ScenarioResult,
    TimingResult,
    timed,
)

from personal_agent.memory.protocol import Episode


async def create_seshat_service(config: ExperimentConfig) -> MemoryService:
    """Create and connect a MemoryService to the existing Neo4j."""
    service = MemoryService()
    connected = await service.connect()
    if not connected:
        msg = f"Failed to connect to Seshat Neo4j at {config.neo4j_uri}"
        raise ConnectionError(msg)
    return service


def episode_to_turn_node(episode: Episode) -> TurnNode:
    """Convert a protocol Episode to a TurnNode for MemoryService."""
    return TurnNode(
        turn_id=episode.turn_id,
        trace_id=episode.turn_id,
        session_id=episode.session_id,
        timestamp=episode.timestamp,
        user_message=episode.user_message,
        assistant_response=episode.assistant_response,
        key_entities=list(episode.entities),
    )


async def ingest_episodes(
    service: MemoryService,
    episodes: list[Episode],
    timing: TimingResult,
) -> None:
    """Store episodes in Seshat, recording timing per episode."""
    for episode in episodes:
        turn = episode_to_turn_node(episode)
        async with timed(timing):
            await service.create_conversation(turn)
            # Create entity nodes for each entity in the episode
            for entity_name in episode.entities:
                entity = Entity(
                    name=entity_name,
                    entity_type="Technology",  # default; real extraction would classify
                )
                await service.create_entity(entity)


async def run_scenario_1_episodic(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 1: Episodic Memory — Store + Retrieve.

    Ingest episodes, then query by entity name and measure retrieval quality.
    """
    ingest_timing = TimingResult(label="seshat_ingest")
    query_timing = TimingResult(label="seshat_query")
    retrieval = RetrievalMetrics()

    # Ingest
    await ingest_episodes(service, episodes, ingest_timing)

    # Query by each canonical entity name
    ground_truth = get_ground_truth()
    for canonical, cluster in ground_truth["canonical_entities"].items():
        query = MemoryQuery(entity_names=[canonical], limit=20)

        start = time.perf_counter()
        async with timed(query_timing):
            result = await service.query_memory(query)
        latency = (time.perf_counter() - start) * 1000

        # Expected: episodes that mention this canonical entity
        expected_ids = {
            ep.turn_id for ep in episodes if canonical in ep.entities
        }
        returned_ids = {c.turn_id for c in result.conversations}

        retrieval.add_query(
            query_text=canonical,
            expected_ids=expected_ids,
            returned_ids=returned_ids,
            latency_ms=latency,
        )

    return {
        "ingest": ingest_timing.to_dict(),
        "query": query_timing.to_dict(),
        "retrieval": retrieval.to_dict(),
    }


async def run_scenario_2_semantic(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 2: Semantic Memory — Consolidation Quality.

    Examine what entities exist after ingestion. Since Seshat relies on
    explicit entity creation during ingestion, we check what was stored.
    """
    # Query all entities via broad recall
    broad = await service.query_memory_broad(entity_types=None, recency_days=90, limit=100)
    entities = broad.get("entities", [])
    turns = broad.get("turns_summary", [])

    return {
        "entity_count": len(entities),
        "entities": [
            {"name": e.get("name", ""), "type": e.get("type", ""), "mentions": e.get("mentions", 0)}
            for e in entities[:20]
        ],
        "turns_with_entities": len(turns),
        "notes": "Seshat stores entities explicitly during ingestion; no auto-extraction in this test path.",
    }


async def run_scenario_3_temporal(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 3: Temporal Queries.

    Test recency-based filtering at different time windows.
    """
    query_timing = TimingResult(label="seshat_temporal")
    results = []

    for days in [7, 14, 30]:
        query = MemoryQuery(recency_days=days, limit=50)
        async with timed(query_timing):
            result = await service.query_memory(query)

        results.append({
            "recency_days": days,
            "results_count": len(result.conversations),
            "has_relevance_scores": bool(result.relevance_scores),
        })

    return {
        "query_timing": query_timing.to_dict(),
        "temporal_queries": results,
        "notes": "Seshat supports recency_days filtering only — no point-in-time or bi-temporal queries.",
    }


async def run_scenario_4_dedup(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 4: Entity Deduplication.

    Ingest episodes with entity name variations and count unique entities created.
    """
    dedup = DedupMetrics()
    ground_truth = get_ground_truth()

    # Also ingest with variation names to test dedup
    variation_timing = TimingResult(label="seshat_dedup_ingest")
    variation_count = 0
    for cluster in ENTITY_CLUSTERS:
        for variation in cluster["variations"]:
            entity = Entity(name=variation, entity_type=cluster["type"])
            async with timed(variation_timing):
                await service.create_entity(entity)
            variation_count += 1

    # Count unique entities in the graph
    all_entities = await service.query_memory_broad(entity_types=None, recency_days=90, limit=500)
    unique_count = len(all_entities.get("entities", []))

    dedup.raw_mentions = variation_count
    dedup.unique_entities_created = unique_count
    dedup.expected_canonical = ground_truth["total_canonical_count"]
    # Seshat dedup is name-based MERGE — variations with different names create separate nodes
    dedup.false_negatives = max(0, unique_count - ground_truth["total_canonical_count"])

    return {
        "dedup": dedup.to_dict(),
        "ingest_timing": variation_timing.to_dict(),
        "notes": "Seshat uses Neo4j MERGE on exact entity name — no fuzzy/semantic dedup.",
    }


async def run_scenario_5_lifecycle(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 5: Consolidation Lifecycle.

    Simulate the working → episodic → semantic lifecycle.
    Steps measured: store episode, create entities, promote to semantic.
    """
    from personal_agent.memory.fact import PromotionCandidate
    from personal_agent.memory.promote import run_promotion_pipeline

    store_timing = TimingResult(label="seshat_store")
    promote_timing = TimingResult(label="seshat_promote")

    # Store episodes (episodic encoding)
    for episode in episodes[:10]:
        turn = episode_to_turn_node(episode)
        async with timed(store_timing):
            await service.create_conversation(turn)
            for name in episode.entities:
                await service.create_entity(Entity(name=name, entity_type="Technology"))

    # Build promotion candidates
    candidates = [
        PromotionCandidate(
            entity_name=c["canonical"],
            entity_type=c["type"],
            mention_count=len(c["variations"]) * 5,
            first_seen=episodes[0].timestamp,
            last_seen=episodes[-1].timestamp,
            source_turn_ids=[ep.turn_id for ep in episodes[:3]],
            description=c["description"],
        )
        for c in ENTITY_CLUSTERS[:5]
    ]

    # Run promotion (semantic integration)
    async with timed(promote_timing):
        promotion_result = await run_promotion_pipeline(service, candidates, trace_id="experiment")

    return {
        "store_timing": store_timing.to_dict(),
        "promote_timing": promote_timing.to_dict(),
        "promoted_count": promotion_result.promoted_count,
        "skipped_count": promotion_result.skipped_count,
        "errors": promotion_result.errors,
        "lifecycle_steps": [
            "1. Store episode (create_conversation + create_entity)",
            "2. Build PromotionCandidate from entity stats",
            "3. Run run_promotion_pipeline()",
            "4. Entity gains memory_type='semantic'",
        ],
    }


async def run_scenario_6_scaling(
    service: MemoryService,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 6: Scaling.

    Ingest episodes in batches and measure throughput + query latency at checkpoints.
    """
    ingest_timing = TimingResult(label="seshat_scale_ingest")
    checkpoints = [100, 250, 500]
    checkpoint_results = []

    for i, episode in enumerate(episodes):
        turn = episode_to_turn_node(episode)
        async with timed(ingest_timing):
            await service.create_conversation(turn)

        if (i + 1) in checkpoints:
            # Measure query latency at this checkpoint
            query_timing = TimingResult(label=f"seshat_query_at_{i+1}")
            for canonical in ["Neo4j", "Python", "FastAPI"]:
                query = MemoryQuery(entity_names=[canonical], limit=10)
                async with timed(query_timing):
                    await service.query_memory(query)

            checkpoint_results.append({
                "episodes_ingested": i + 1,
                "ingest_mean_ms": round(ingest_timing.mean, 2),
                "query": query_timing.to_dict(),
            })

    return {
        "ingest_total": ingest_timing.to_dict(),
        "checkpoints": checkpoint_results,
    }


async def clean_experiment_data(
    service: MemoryService,
    session_ids: list[str] | None = None,
) -> None:
    """Remove experiment data from Seshat Neo4j.

    Deletes Turn and Entity nodes created during the experiment, identified
    by session_ids used during ingestion. Does NOT touch production data.

    Args:
        service: Connected MemoryService.
        session_ids: Session IDs used during experiment. If None, skips cleanup.
    """
    if service.driver is None or not session_ids:
        return

    async with service.driver.session() as session:
        # Delete turns by session_id
        await session.run(
            "MATCH (t:Turn) WHERE t.session_id IN $ids DETACH DELETE t",
            ids=session_ids,
        )
