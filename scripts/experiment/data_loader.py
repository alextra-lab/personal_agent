"""Load real telemetry data and generate synthetic episodes for the experiment."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import orjson

from personal_agent.memory.models import TurnNode
from personal_agent.memory.protocol import Episode


def load_real_episodes(
    telemetry_dir: Path,
    max_episodes: int = 50,
) -> list[Episode]:
    """Parse evaluation telemetry JSON files into Episode objects.

    Scans run-* directories for evaluation_results.json files and extracts
    conversation turns as Episode objects.

    Args:
        telemetry_dir: Path to telemetry/evaluation/ directory.
        max_episodes: Maximum number of episodes to return.

    Returns:
        List of Episode objects parsed from real evaluation data.
    """
    episodes: list[Episode] = []

    for run_dir in sorted(telemetry_dir.glob("run-*")):
        results_file = run_dir / "evaluation_results.json"
        if not results_file.exists():
            continue

        data = orjson.loads(results_file.read_bytes())
        paths = data.get("paths", [])

        for path in paths:
            session_id = path.get("session_id", str(uuid.uuid4()))
            turns = path.get("turns", [])

            for turn in turns:
                user_msg = turn.get("user_message", "")
                response = turn.get("response_text", "")
                trace_id = turn.get("trace_id", str(uuid.uuid4()))

                if not user_msg or not response:
                    continue

                episode = Episode(
                    turn_id=trace_id,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    user_message=user_msg,
                    assistant_response=response,
                )
                episodes.append(episode)

                if len(episodes) >= max_episodes:
                    return episodes

    return episodes


# -- Synthetic data generation ------------------------------------------------

# Entity clusters with deliberate name variations for dedup testing
ENTITY_CLUSTERS: list[dict[str, Any]] = [
    {
        "canonical": "Neo4j",
        "type": "Technology",
        "variations": ["Neo4j", "neo4j", "Neo4J", "neo4j graph database"],
        "description": "Graph database for knowledge storage",
    },
    {
        "canonical": "Claude Code",
        "type": "Technology",
        "variations": ["Claude Code", "claude-code", "CC", "Claude Code CLI"],
        "description": "Anthropic CLI for AI-assisted development",
    },
    {
        "canonical": "Python",
        "type": "Technology",
        "variations": ["Python", "python", "Python 3.12", "CPython"],
        "description": "Programming language used for the agent",
    },
    {
        "canonical": "Elasticsearch",
        "type": "Technology",
        "variations": ["Elasticsearch", "ElasticSearch", "elastic search", "ES"],
        "description": "Search and analytics engine for telemetry",
    },
    {
        "canonical": "Machine Learning",
        "type": "Concept",
        "variations": ["Machine Learning", "ML", "machine learning", "deep learning"],
        "description": "AI training and inference methodology",
    },
    {
        "canonical": "FastAPI",
        "type": "Technology",
        "variations": ["FastAPI", "fast api", "fastapi", "Fast API"],
        "description": "Python web framework for the agent service",
    },
    {
        "canonical": "Graphiti",
        "type": "Technology",
        "variations": ["Graphiti", "graphiti", "Graphiti by Zep", "graphiti-core"],
        "description": "Temporal knowledge graph framework",
    },
    {
        "canonical": "Memory Consolidation",
        "type": "Concept",
        "variations": [
            "Memory Consolidation",
            "memory consolidation",
            "consolidation",
            "episodic to semantic promotion",
        ],
        "description": "Process of promoting episodic memories to semantic knowledge",
    },
    {
        "canonical": "Docker",
        "type": "Technology",
        "variations": ["Docker", "docker", "Docker Compose", "docker-compose"],
        "description": "Container runtime for infrastructure services",
    },
    {
        "canonical": "Cognitive Architecture",
        "type": "Concept",
        "variations": [
            "Cognitive Architecture",
            "cognitive architecture",
            "cognitive agent architecture",
            "agent architecture",
        ],
        "description": "Biologically-inspired agent design framework",
    },
]

# Conversation templates that reference entities naturally
CONVERSATION_TEMPLATES: list[dict[str, str]] = [
    {
        "user": "How does {e1} handle entity deduplication compared to {e2}?",
        "assistant": "The {e1} approach uses {e3} for dedup while {e2} relies on name-based MERGE in the graph database. The key difference is that {e1} adds vector similarity matching before the LLM reasoning step.",
    },
    {
        "user": "What's the current status of the {e1} integration?",
        "assistant": "The {e1} integration is operational. It connects to {e2} for storage and uses {e3} for the extraction pipeline. We recently improved the query performance.",
    },
    {
        "user": "Can you explain how {e1} works in our system?",
        "assistant": "In our {e2}, {e1} serves as the foundation for persistent knowledge. When a conversation is processed, entities are extracted via {e3} and stored as nodes with relationships.",
    },
    {
        "user": "I want to compare {e1} and {e2} for our memory backend.",
        "assistant": "Good question. {e1} offers built-in temporal queries and entity resolution, while {e2} gives us more control over the schema. Both use {e3} under the hood, so the infrastructure cost is similar.",
    },
    {
        "user": "What did we discuss about {e1} last week?",
        "assistant": "Last week we explored how {e1} handles {e2} at scale. The main finding was that {e3} performance degrades above 10K entities without proper indexing.",
    },
    {
        "user": "How should we test the {e1} pipeline?",
        "assistant": "For testing {e1}, I recommend creating fixtures with known entities using {e2}, then verifying extraction quality with {e3}. Run the full pipeline end-to-end with synthetic data first.",
    },
    {
        "user": "Is {e1} better than our current approach for handling temporal data?",
        "assistant": "For temporal queries, {e1} has a clear advantage: it uses a bi-temporal model with valid_at and invalid_at timestamps on every edge. Our current {e2} only tracks recency via {e3} timestamps on turn nodes.",
    },
    {
        "user": "Tell me about the relationship between {e1} and {e2}.",
        "assistant": "{e1} and {e2} are complementary in our architecture. {e1} provides the high-level patterns while {e2} handles the low-level storage. {e3} bridges the two with its extraction pipeline.",
    },
]


def generate_synthetic_episodes(
    count: int = 500,
    days_span: int = 30,
) -> list[Episode]:
    """Generate synthetic episodes with known entities and temporal anchors.

    Creates realistic multi-topic conversations with deliberate entity name
    variations for dedup testing and known temporal references for verification.

    Args:
        count: Number of episodes to generate.
        days_span: Time span in days for the generated episodes.

    Returns:
        List of Episode objects with ground truth in properties.
    """
    episodes: list[Episode] = []
    base_time = datetime.now(timezone.utc) - timedelta(days=days_span)
    session_id = str(uuid.uuid4())

    for i in range(count):
        # Pick 3 entity clusters and a random variation from each
        clusters = random.sample(ENTITY_CLUSTERS, k=min(3, len(ENTITY_CLUSTERS)))
        e1_var = random.choice(clusters[0]["variations"])
        e2_var = random.choice(clusters[1]["variations"])
        e3_var = random.choice(clusters[2]["variations"])

        # Pick a conversation template and fill it
        template = random.choice(CONVERSATION_TEMPLATES)
        user_msg = template["user"].format(e1=e1_var, e2=e2_var, e3=e3_var)
        assistant_msg = template["assistant"].format(e1=e1_var, e2=e2_var, e3=e3_var)

        # Distribute timestamps across the time span
        offset = timedelta(seconds=i * (days_span * 86400 / count))
        timestamp = base_time + offset

        # Rotate session_id every 10 episodes
        if i > 0 and i % 10 == 0:
            session_id = str(uuid.uuid4())

        episode = Episode(
            turn_id=str(uuid.uuid4()),
            session_id=session_id,
            timestamp=timestamp,
            user_message=user_msg,
            assistant_response=assistant_msg,
            entities=[c["canonical"] for c in clusters],
        )
        episodes.append(episode)

    return episodes


def get_ground_truth() -> dict[str, Any]:
    """Return ground truth for precision/recall measurement.

    Returns:
        Dict with canonical entities, expected dedup mappings, and
        expected relationships.
    """
    return {
        "canonical_entities": {c["canonical"]: c for c in ENTITY_CLUSTERS},
        "variation_to_canonical": {
            var: c["canonical"]
            for c in ENTITY_CLUSTERS
            for var in c["variations"]
        },
        "total_canonical_count": len(ENTITY_CLUSTERS),
    }
