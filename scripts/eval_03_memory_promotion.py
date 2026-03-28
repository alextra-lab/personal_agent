#!/usr/bin/env python3
"""EVAL-03: Memory promotion quality evaluation.

Evaluates the episodic→semantic promotion pipeline by:
  1. Seeding 10+ entity-rich conversations covering people, projects,
     technologies, and decisions
  2. Triggering consolidation directly (bypasses scheduler idle gates)
  3. Querying Neo4j for extraction and promotion metrics
  4. Running memory recall turns to evaluate response quality
  5. Printing a structured report

Usage:
    uv run python scripts/eval_03_memory_promotion.py

Requirements:
    - Agent service running at http://localhost:9000
    - Neo4j at bolt://localhost:7687
    - Infrastructure: ./scripts/init-services.sh
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog

# Resolve project root so imports work when run as a script
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

log = structlog.get_logger(__name__)

AGENT_URL = "http://localhost:9000"
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "neo4j_dev_password"

# ---------------------------------------------------------------------------
# Conversation seeds — 5 scenarios × 2 seeding turns = 10 seed turns
# Each scenario is followed by a recall turn to evaluate memory quality.
# ---------------------------------------------------------------------------
SCENARIOS: list[dict] = [
    {
        "name": "DataForge Project",
        "seed_turns": [
            (
                "I'm building a service called DataForge. It uses Apache Flink "
                "for stream processing and stores results in ClickHouse."
            ),
            (
                "The project lead is Priya Sharma. We're targeting "
                "a throughput of 50,000 events per second on GCP. "
                "DataForge also integrates with Grafana for real-time monitoring "
                "and uses Kafka as the ingestion layer before Flink."
            ),
        ],
        "recall_turn": "What do you remember about the DataForge project and its tech stack?",
        "expected_entities": ["DataForge", "Apache Flink", "ClickHouse", "Priya Sharma", "Kafka"],
    },
    {
        "name": "ML Infrastructure",
        "seed_turns": [
            (
                "Our ML team is building SentinelML, a model training pipeline "
                "using PyTorch and MLflow for experiment tracking. "
                "The team lead is Dr. Amara Osei."
            ),
            (
                "SentinelML runs on Kubernetes with GPU node pools. "
                "The inference endpoint uses TorchServe behind an Istio service mesh "
                "and we're evaluating moving to AWS SageMaker."
            ),
        ],
        "recall_turn": "Tell me about the ML infrastructure project we discussed.",
        "expected_entities": ["SentinelML", "PyTorch", "Dr. Amara Osei", "TorchServe"],
    },
    {
        "name": "Team Tech Decisions",
        "seed_turns": [
            (
                "Our backend team (led by Marcus Webb) decided to migrate from Django "
                "to FastAPI after benchmarking showed 3x throughput improvement. "
                "The migration targets Q3 2026."
            ),
            (
                "For the database layer we chose PostgreSQL with pgvector extension "
                "over MongoDB because our data is mostly relational. "
                "Marcus also wants to add Redis for session caching."
            ),
        ],
        "recall_turn": "What technology decisions did we discuss for the backend?",
        "expected_entities": ["FastAPI", "Marcus Webb", "PostgreSQL", "pgvector", "Redis"],
    },
    {
        "name": "Research Findings",
        "seed_turns": [
            (
                "I've been researching vector databases for our semantic search feature. "
                "Compared Qdrant, Weaviate, and Pinecone. "
                "Qdrant wins on latency, Pinecone on managed simplicity."
            ),
            (
                "We decided to go with Qdrant because we need on-prem deployment. "
                "The engineer implementing it is Yuki Tanaka. "
                "She's building a hybrid search combining BM25 and dense vectors."
            ),
        ],
        "recall_turn": "What did we decide about the vector database and who is building it?",
        "expected_entities": ["Qdrant", "Yuki Tanaka", "Weaviate", "Pinecone"],
    },
    {
        "name": "Architecture Proposal",
        "seed_turns": [
            (
                "We're designing a new event-driven architecture using Apache Kafka "
                "for the event bus and CloudEvents specification for event schemas. "
                "Project codename is Project Heron."
            ),
            (
                "Project Heron will replace the current monolith (called LegacyCore) "
                "by 2027. The architect is Sofia Reyes. "
                "We're using Avro for serialization and Confluent Schema Registry."
            ),
        ],
        "recall_turn": "Summarize what we discussed about the architecture migration project.",
        "expected_entities": ["Project Heron", "Sofia Reyes", "LegacyCore", "Confluent Schema Registry"],
    },
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class RecallEvaluation:
    scenario_name: str
    recall_response: str
    expected_entities: list[str]
    entities_found: list[str] = field(default_factory=list)
    entities_missing: list[str] = field(default_factory=list)

    def compute(self) -> None:
        resp_lower = self.recall_response.lower()
        for e in self.expected_entities:
            if e.lower() in resp_lower:
                self.entities_found.append(e)
            else:
                self.entities_missing.append(e)

    @property
    def recall_rate(self) -> float:
        if not self.expected_entities:
            return 0.0
        return len(self.entities_found) / len(self.expected_entities)


@dataclass
class Neo4jStats:
    total_entities: int
    episodic_entities: int
    semantic_entities: int
    entity_names: list[str]
    promoted_names: list[str]

    @property
    def extraction_count(self) -> int:
        return self.total_entities

    @property
    def promotion_rate(self) -> float:
        if not self.total_entities:
            return 0.0
        return self.semantic_entities / self.total_entities


# ---------------------------------------------------------------------------
# Agent API helpers
# ---------------------------------------------------------------------------
async def create_session(client: httpx.AsyncClient) -> str:
    resp = await client.post(f"{AGENT_URL}/sessions", json={}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["session_id"]


async def send_message(client: httpx.AsyncClient, session_id: str, message: str) -> str:
    resp = await client.post(
        f"{AGENT_URL}/chat",
        params={"message": message, "session_id": session_id},
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("response", data.get("message", "")))


# ---------------------------------------------------------------------------
# Neo4j query helpers
# ---------------------------------------------------------------------------
async def query_neo4j_stats(driver) -> Neo4jStats:  # type: ignore[type-arg]
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (e:Entity)
            RETURN e.name AS name,
                   coalesce(e.memory_type, 'episodic') AS memory_type
            ORDER BY e.mention_count DESC
            """
        )
        rows = await result.data()

    entity_names = [r["name"] for r in rows]
    promoted = [r["name"] for r in rows if r["memory_type"] == "semantic"]
    episodic = [r["name"] for r in rows if r["memory_type"] != "semantic"]

    return Neo4jStats(
        total_entities=len(rows),
        episodic_entities=len(episodic),
        semantic_entities=len(promoted),
        entity_names=entity_names,
        promoted_names=promoted,
    )


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------
async def run() -> None:
    print("\n" + "=" * 70)
    print("EVAL-03: Memory Promotion Quality Evaluation")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70 + "\n")

    # --- Pre-flight checks ---
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{AGENT_URL}/health", timeout=5.0)
            health = resp.json()
            if health.get("status") != "healthy":
                print(f"✗ Agent not healthy: {health}")
                sys.exit(1)
            neo4j_status = health.get("components", {}).get("neo4j", "unknown")
            print(f"✓ Agent healthy — neo4j: {neo4j_status}")
        except Exception as e:
            print(f"✗ Agent unreachable: {e}")
            sys.exit(1)

    try:
        from neo4j import AsyncGraphDatabase
    except ModuleNotFoundError:
        print("✗ neo4j Python package not installed")
        sys.exit(1)

    neo4j_driver = AsyncGraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
    )
    try:
        await neo4j_driver.verify_connectivity()
        print("✓ Neo4j connected\n")
    except Exception as e:
        print(f"✗ Neo4j unreachable: {e}")
        sys.exit(1)

    # --- Baseline Neo4j state ---
    baseline = await query_neo4j_stats(neo4j_driver)
    print(f"[Baseline] Entities in Neo4j: {baseline.total_entities} total, "
          f"{baseline.semantic_entities} semantic")
    print()

    # -----------------------------------------------------------------------
    # Phase 1: Seed conversations
    # -----------------------------------------------------------------------
    print("─" * 70)
    print("Phase 1: Seeding entity-rich conversations")
    print("─" * 70)

    all_recall_evals: list[RecallEvaluation] = []
    seeded_sessions: list[str] = []

    async with httpx.AsyncClient() as client:
        for i, scenario in enumerate(SCENARIOS, 1):
            print(f"\n[{i}/{len(SCENARIOS)}] {scenario['name']}")
            session_id = await create_session(client)
            seeded_sessions.append(session_id)

            for j, turn in enumerate(scenario["seed_turns"], 1):
                print(f"  Turn {j}: {turn[:60]}...")
                await send_message(client, session_id, turn)
                await asyncio.sleep(1.0)  # Give ES time to index

        print(f"\n✓ {len(SCENARIOS)} scenarios × 2 turns = {len(SCENARIOS) * 2} seed turns sent")

    # -----------------------------------------------------------------------
    # Phase 2: Trigger consolidation (bypasses scheduler idle gates)
    # -----------------------------------------------------------------------
    print("\n" + "─" * 70)
    print("Phase 2: Triggering consolidation (direct call)")
    print("─" * 70)

    from personal_agent.second_brain.consolidator import SecondBrainConsolidator

    consolidator = SecondBrainConsolidator()
    await consolidator.memory_service.connect()
    print("  Consolidating last 2 days of captures (limit 200)...")

    consolidation_result: dict = {}
    try:
        consolidation_result = await consolidator.consolidate_recent_captures(
            days=2, limit=200
        )
        print(f"  ✓ Captures processed : {consolidation_result['captures_processed']}")
        print(f"    Captures skipped   : {consolidation_result['captures_skipped']}")
        print(f"    Turns created      : {consolidation_result['turns_created']}")
        print(f"    Entities created   : {consolidation_result['entities_created']}")
        print(f"    Entities promoted  : {consolidation_result.get('entities_promoted', 0)}")
    except Exception as e:
        print(f"  ✗ Consolidation failed: {e}")
        import traceback
        traceback.print_exc()

    await asyncio.sleep(2.0)  # Allow Neo4j writes to flush

    # -----------------------------------------------------------------------
    # Phase 3: Query Neo4j for extraction + promotion metrics
    # -----------------------------------------------------------------------
    print("\n" + "─" * 70)
    print("Phase 3: Neo4j extraction and promotion metrics")
    print("─" * 70)

    post_consolidation = await query_neo4j_stats(neo4j_driver)
    new_entities = post_consolidation.total_entities - baseline.total_entities
    new_promoted = post_consolidation.semantic_entities - baseline.semantic_entities

    print(f"\n  Total entities    : {post_consolidation.total_entities} "
          f"(+{new_entities} new)")
    print(f"  Semantic entities : {post_consolidation.semantic_entities} "
          f"(+{new_promoted} new)")
    print(f"  Promotion rate    : {post_consolidation.promotion_rate:.1%}")

    # Check which seeded scenario entities made it into Neo4j
    print("\n  Entity extraction coverage (seeded entities):")
    all_seeded_entities: list[str] = []
    for scenario in SCENARIOS:
        all_seeded_entities.extend(scenario["expected_entities"])

    found_in_graph = []
    missing_from_graph = []
    entity_names_lower = [n.lower() for n in post_consolidation.entity_names]
    for entity in all_seeded_entities:
        if entity.lower() in entity_names_lower:
            found_in_graph.append(entity)
        else:
            missing_from_graph.append(entity)

    extraction_rate = len(found_in_graph) / len(all_seeded_entities) if all_seeded_entities else 0
    print(f"  Seeded entities found in Neo4j: "
          f"{len(found_in_graph)}/{len(all_seeded_entities)} ({extraction_rate:.1%})")
    if missing_from_graph:
        print(f"  Missing: {', '.join(missing_from_graph)}")

    print("\n  Promoted (semantic) entities:")
    if post_consolidation.promoted_names:
        for name in post_consolidation.promoted_names[:20]:
            marker = "★" if name in found_in_graph else " "
            print(f"    {marker} {name}")
    else:
        print("    (none yet)")

    # -----------------------------------------------------------------------
    # Phase 4: Memory recall evaluation
    # -----------------------------------------------------------------------
    print("\n" + "─" * 70)
    print("Phase 4: Memory recall quality evaluation")
    print("─" * 70)

    async with httpx.AsyncClient() as client:
        for i, (scenario, session_id) in enumerate(
            zip(SCENARIOS, seeded_sessions, strict=True), 1
        ):
            print(f"\n[{i}] {scenario['name']}")
            print(f"  Recall: {scenario['recall_turn'][:60]}...")
            response = await send_message(client, session_id, scenario["recall_turn"])
            print(f"  Response: {response[:200]}...")

            evaluation = RecallEvaluation(
                scenario_name=scenario["name"],
                recall_response=response,
                expected_entities=scenario["expected_entities"],
            )
            evaluation.compute()
            all_recall_evals.append(evaluation)

            print(f"  Entities found   : {evaluation.entities_found}")
            print(f"  Entities missing : {evaluation.entities_missing}")
            print(f"  Recall rate      : {evaluation.recall_rate:.0%}")

    # -----------------------------------------------------------------------
    # Phase 5: Summary report
    # -----------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("EVAL-03 Summary")
    print("=" * 70)

    avg_recall = (
        sum(e.recall_rate for e in all_recall_evals) / len(all_recall_evals)
        if all_recall_evals else 0.0
    )

    print(f"""
  Extraction quality
  ──────────────────
  Seeded entities extracted : {len(found_in_graph)}/{len(all_seeded_entities)} ({extraction_rate:.1%})
  Missing entities          : {', '.join(missing_from_graph) or 'none'}

  Promotion quality
  ─────────────────
  Total entities in Neo4j   : {post_consolidation.total_entities}
  Promoted to semantic      : {post_consolidation.semantic_entities} ({post_consolidation.promotion_rate:.1%})
  New this run              : +{new_promoted}

  Recall quality
  ──────────────
  Average entity recall rate: {avg_recall:.1%}
  Per-scenario results:""")

    for e in all_recall_evals:
        print(f"    {e.scenario_name:<30} {e.recall_rate:.0%} "
              f"({len(e.entities_found)}/{len(e.expected_entities)})")

    print(f"""
  Questions to answer (FRE-148)
  ─────────────────────────────
  Entity extraction rate    : {extraction_rate:.1%}
  Entity promotion rate     : {post_consolidation.promotion_rate:.1%}
  Avg recall accuracy       : {avg_recall:.1%}
""")

    await neo4j_driver.close()

    # -----------------------------------------------------------------------
    # Phase 6: Write raw data for research report
    # -----------------------------------------------------------------------
    output_dir = project_root / "telemetry" / "evaluation" / "eval-03-memory-promotion"
    output_dir.mkdir(parents=True, exist_ok=True)

    import json
    data = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "baseline": {
            "total_entities": baseline.total_entities,
            "semantic_entities": baseline.semantic_entities,
        },
        "post_consolidation": {
            "total_entities": post_consolidation.total_entities,
            "semantic_entities": post_consolidation.semantic_entities,
            "promoted_names": post_consolidation.promoted_names,
        },
        "extraction": {
            "seeded_entities": all_seeded_entities,
            "found_in_graph": found_in_graph,
            "missing_from_graph": missing_from_graph,
            "extraction_rate": extraction_rate,
        },
        "consolidation_result": consolidation_result,
        "recall_evaluations": [
            {
                "scenario": e.scenario_name,
                "recall_rate": e.recall_rate,
                "found": e.entities_found,
                "missing": e.entities_missing,
                "response_excerpt": e.recall_response[:500],
            }
            for e in all_recall_evals
        ],
        "summary": {
            "extraction_rate": extraction_rate,
            "promotion_rate": post_consolidation.promotion_rate,
            "avg_recall_rate": avg_recall,
        },
    }

    json_path = output_dir / "results.json"
    json_path.write_text(json.dumps(data, indent=2, default=str))
    print(f"  Raw data written to: {json_path}")
    print()


if __name__ == "__main__":
    asyncio.run(run())
