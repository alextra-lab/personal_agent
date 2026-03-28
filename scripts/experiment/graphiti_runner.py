"""Run experiment scenarios against Graphiti."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# Disable Graphiti telemetry before importing
os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"

from graphiti_core import Graphiti
from graphiti_core.llm_client.config import LLMConfig as GraphitiLLMConfig
from graphiti_core.nodes import EpisodeType
from graphiti_core.search.search_config_recipes import (
    NODE_HYBRID_SEARCH_RRF,
)

from personal_agent.memory.protocol import Episode

from .config import ExperimentConfig, LLMConfig
from .data_loader import ENTITY_CLUSTERS, get_ground_truth
from .metrics import (
    CostTracker,
    DedupMetrics,
    RetrievalMetrics,
    ScenarioResult,
    TimingResult,
    timed,
)


def _create_graphiti_llm_client(
    llm_config: LLMConfig,
) -> Any:
    """Create the appropriate Graphiti LLM client based on config."""
    if "claude" in llm_config.medium_model:
        from graphiti_core.llm_client.anthropic_client import AnthropicClient

        return AnthropicClient(
            config=GraphitiLLMConfig(
                model=llm_config.medium_model,
                small_model=llm_config.small_model,
            )
        )
    else:
        from graphiti_core.llm_client.openai_client import OpenAIClient

        return OpenAIClient(
            config=GraphitiLLMConfig(
                model=llm_config.medium_model,
                small_model=llm_config.small_model,
            )
        )


def _create_graphiti_embedder(llm_config: LLMConfig) -> Any:
    """Create OpenAI embedder (used regardless of LLM provider)."""
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

    return OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            model=llm_config.embedder_model,
            embedding_dim=llm_config.embedding_dim,
        )
    )


async def create_graphiti_client(config: ExperimentConfig) -> Graphiti:
    """Create and initialize a Graphiti client connected to the experiment Neo4j."""
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    driver = Neo4jDriver(
        uri=config.graphiti_neo4j_uri,
        user=config.graphiti_neo4j_user,
        password=config.graphiti_neo4j_password,
    )

    llm_client = _create_graphiti_llm_client(config.llm_config)
    embedder = _create_graphiti_embedder(config.llm_config)

    graphiti = Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
    )

    await graphiti.build_indices_and_constraints()
    return graphiti


def _get_token_usage(graphiti: Graphiti) -> dict[str, int]:
    """Extract token usage from Graphiti's tracker."""
    try:
        usage = graphiti.token_tracker.get_total_usage()
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }
    except Exception:
        return {"input_tokens": 0, "output_tokens": 0}


async def ingest_episodes(
    graphiti: Graphiti,
    episodes: list[Episode],
    timing: TimingResult,
) -> None:
    """Add episodes to Graphiti, recording timing per episode."""
    for episode in episodes:
        body = f"User: {episode.user_message}\nAssistant: {episode.assistant_response}"
        async with timed(timing):
            await graphiti.add_episode(
                name=f"turn_{episode.turn_id[:8]}",
                episode_body=body,
                source=EpisodeType.message,
                source_description="Evaluation conversation",
                reference_time=episode.timestamp,
                group_id=episode.session_id,
            )


async def run_scenario_1_episodic(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 1: Episodic Memory — Store + Retrieve."""
    ingest_timing = TimingResult(label="graphiti_ingest")
    query_timing = TimingResult(label="graphiti_query")
    retrieval = RetrievalMetrics()

    # Ingest
    await ingest_episodes(graphiti, episodes, ingest_timing)

    # Query by each canonical entity name
    ground_truth = get_ground_truth()
    for canonical in ground_truth["canonical_entities"]:
        start = time.perf_counter()
        async with timed(query_timing):
            # Edge search for facts about this entity
            results = await graphiti.search(canonical)
        latency = (time.perf_counter() - start) * 1000

        # Expected: episodes that mention this canonical entity
        expected_ids = {ep.turn_id for ep in episodes if canonical in ep.entities}
        # Graphiti returns edges, not episodes — map source_node_uuid to check coverage
        returned_ids = {str(r.uuid) for r in results} if results else set()

        retrieval.add_query(
            query_text=canonical,
            expected_ids=expected_ids,
            returned_ids=returned_ids,
            latency_ms=latency,
        )

    token_usage = _get_token_usage(graphiti)

    return {
        "ingest": ingest_timing.to_dict(),
        "query": query_timing.to_dict(),
        "retrieval": retrieval.to_dict(),
        "token_usage": token_usage,
    }


async def run_scenario_2_semantic(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 2: Semantic Memory — Consolidation Quality.

    Examine what Graphiti auto-extracted after ingestion.
    """
    # Search for all entities (node search)
    nodes = await graphiti.search("*", config=NODE_HYBRID_SEARCH_RRF)

    entities = []
    for node in nodes:
        entities.append({
            "name": getattr(node, "name", str(node)),
            "summary": getattr(node, "summary", ""),
            "labels": getattr(node, "labels", []),
        })

    # Search for facts (edge search)
    edges = await graphiti.search("knowledge relationships")
    facts = []
    for edge in edges[:20]:
        facts.append({
            "fact": getattr(edge, "fact", str(edge)),
            "valid_at": str(getattr(edge, "valid_at", "")),
            "invalid_at": str(getattr(edge, "invalid_at", "")),
        })

    token_usage = _get_token_usage(graphiti)

    return {
        "entity_count": len(entities),
        "entities": entities[:20],
        "fact_count": len(facts),
        "facts": facts[:10],
        "token_usage": token_usage,
        "notes": "Graphiti auto-extracts entities and facts on add_episode — no separate consolidation step.",
    }


async def run_scenario_3_temporal(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 3: Temporal Queries."""
    query_timing = TimingResult(label="graphiti_temporal")
    results = []

    # Retrieve recent episodes at different time windows
    now = datetime.now(timezone.utc)
    for days in [7, 14, 30]:
        async with timed(query_timing):
            recent = await graphiti.retrieve_episodes(
                reference_time=now,
                last_n=50,
            )

        # Also test edge search with temporal semantics
        temporal_edges = await graphiti.search(f"discussed in the last {days} days")

        results.append({
            "recency_days": days,
            "episodes_retrieved": len(recent) if recent else 0,
            "temporal_edge_results": len(temporal_edges) if temporal_edges else 0,
        })

    token_usage = _get_token_usage(graphiti)

    return {
        "query_timing": query_timing.to_dict(),
        "temporal_queries": results,
        "token_usage": token_usage,
        "notes": "Graphiti supports bi-temporal model (valid_at/invalid_at) + episode retrieval by reference_time.",
    }


async def run_scenario_4_dedup(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 4: Entity Deduplication.

    Ingest episodes with entity name variations via conversation text
    and count how many unique entities Graphiti creates.
    """
    dedup = DedupMetrics()
    ground_truth = get_ground_truth()
    ingest_timing = TimingResult(label="graphiti_dedup_ingest")

    # Create episodes that naturally mention entity variations
    variation_count = 0
    for cluster in ENTITY_CLUSTERS:
        for i, variation in enumerate(cluster["variations"]):
            body = (
                f"User: Tell me about {variation} and how it works.\n"
                f"Assistant: {variation} is {cluster['description']}. "
                f"It's commonly used in our project infrastructure."
            )
            async with timed(ingest_timing):
                await graphiti.add_episode(
                    name=f"dedup_test_{cluster['canonical']}_{i}",
                    episode_body=body,
                    source=EpisodeType.message,
                    source_description="Dedup test conversation",
                    reference_time=datetime.now(timezone.utc),
                    group_id="dedup_test",
                )
            variation_count += 1

    # Count unique entities Graphiti created
    all_nodes = await graphiti.search("*", config=NODE_HYBRID_SEARCH_RRF)
    unique_count = len(all_nodes) if all_nodes else 0

    dedup.raw_mentions = variation_count
    dedup.unique_entities_created = unique_count
    dedup.expected_canonical = ground_truth["total_canonical_count"]

    token_usage = _get_token_usage(graphiti)

    return {
        "dedup": dedup.to_dict(),
        "ingest_timing": ingest_timing.to_dict(),
        "token_usage": token_usage,
        "notes": "Graphiti uses three-tier dedup: vector similarity + BM25 + LLM reasoning.",
    }


async def run_scenario_5_lifecycle(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 5: Consolidation Lifecycle.

    With Graphiti, add_episode handles extraction + dedup + edge creation
    in one step. There is no separate promotion/consolidation step.
    """
    ingest_timing = TimingResult(label="graphiti_lifecycle_ingest")

    # Ingest 10 episodes
    await ingest_episodes(graphiti, episodes[:10], ingest_timing)

    # Check what Graphiti produced — entities, edges, communities
    nodes = await graphiti.search("*", config=NODE_HYBRID_SEARCH_RRF)
    edges = await graphiti.search("relationships")

    token_usage = _get_token_usage(graphiti)

    return {
        "ingest_timing": ingest_timing.to_dict(),
        "entities_created": len(nodes) if nodes else 0,
        "facts_created": len(edges) if edges else 0,
        "token_usage": token_usage,
        "lifecycle_steps": [
            "1. add_episode() — extracts entities, resolves/deduplicates, creates edges",
            "2. No separate promotion step needed",
            "3. Entities are immediately searchable via hybrid search",
            "4. Contradiction detection auto-invalidates superseded facts",
        ],
        "notes": "Graphiti collapses working→episodic→semantic into a single add_episode call.",
    }


async def run_scenario_6_scaling(
    graphiti: Graphiti,
    episodes: list[Episode],
) -> dict[str, Any]:
    """Scenario 6: Scaling."""
    ingest_timing = TimingResult(label="graphiti_scale_ingest")
    checkpoints = [100, 250, 500]
    checkpoint_results = []

    for i, episode in enumerate(episodes):
        body = f"User: {episode.user_message}\nAssistant: {episode.assistant_response}"
        async with timed(ingest_timing):
            await graphiti.add_episode(
                name=f"scale_{episode.turn_id[:8]}",
                episode_body=body,
                source=EpisodeType.message,
                source_description="Scaling test",
                reference_time=episode.timestamp,
                group_id=episode.session_id,
            )

        if (i + 1) in checkpoints:
            query_timing = TimingResult(label=f"graphiti_query_at_{i+1}")
            for query_text in ["Neo4j", "Python", "FastAPI"]:
                async with timed(query_timing):
                    await graphiti.search(query_text)

            token_usage = _get_token_usage(graphiti)

            checkpoint_results.append({
                "episodes_ingested": i + 1,
                "ingest_mean_ms": round(ingest_timing.mean, 2),
                "query": query_timing.to_dict(),
                "token_usage": token_usage,
            })

    return {
        "ingest_total": ingest_timing.to_dict(),
        "checkpoints": checkpoint_results,
    }


async def clear_graphiti_data(graphiti: Graphiti) -> None:
    """Clear all data from the experiment Neo4j instance."""
    driver = graphiti.driver
    if driver is None:
        return
    async with driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
