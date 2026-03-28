#!/usr/bin/env python
"""Graphiti vs Seshat Experiment — EVAL-02 / FRE-147.

Compares Graphiti against the current Seshat Neo4j backend across 6 scenarios:
  1. Episodic Memory — Store + Retrieve
  2. Semantic Memory — Consolidation Quality
  3. Temporal Queries
  4. Entity Deduplication
  5. Consolidation Lifecycle
  6. Scaling

Usage:
    python scripts/graphiti_experiment.py --llm openai --scenarios 1,2,3,4,5,6
    python scripts/graphiti_experiment.py --llm both
    python scripts/graphiti_experiment.py --llm anthropic --scenarios 4 --episodes 20
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Disable Graphiti telemetry BEFORE any graphiti imports
os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"

# Add src to path for personal_agent imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from experiment.config import ANTHROPIC_CONFIG, OPENAI_CONFIG, ExperimentConfig, LLM_CONFIGS
from experiment.data_loader import generate_synthetic_episodes, load_real_episodes
from experiment.metrics import ScenarioResult
from experiment.report import print_summary, save_json_results, save_markdown_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Graphiti vs Seshat Experiment")
    parser.add_argument(
        "--llm",
        choices=["openai", "anthropic", "both"],
        default="openai",
        help="LLM provider for Graphiti (default: openai)",
    )
    parser.add_argument(
        "--scenarios",
        default="1,2,3,4,5,6",
        help="Comma-separated scenario numbers to run (default: all)",
    )
    parser.add_argument("--episodes", type=int, default=50, help="Episode count for quality tests")
    parser.add_argument("--scale-episodes", type=int, default=500, help="Episode count for scaling test")
    parser.add_argument(
        "--output",
        default="telemetry/evaluation/graphiti",
        help="Output directory for results",
    )
    parser.add_argument("--neo4j-uri", default="bolt://localhost:7687")
    parser.add_argument("--graphiti-neo4j-uri", default="bolt://localhost:7688")
    return parser.parse_args()


async def run_experiment(config: ExperimentConfig) -> dict[str, Any]:
    """Run the full experiment for a single LLM configuration."""
    from experiment import graphiti_runner, seshat_runner

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M") + f"-{config.llm}"
    print(f"\n{'='*60}")
    print(f"Starting experiment run: {run_id}")
    print(f"LLM: {config.llm_config.name}")
    print(f"Medium model: {config.llm_config.medium_model}")
    print(f"Small model: {config.llm_config.small_model}")
    print(f"Scenarios: {config.scenarios}")
    print(f"{'='*60}\n")

    # Load test data
    telemetry_dir = Path("telemetry/evaluation")
    real_episodes = load_real_episodes(telemetry_dir, max_episodes=config.episodes)
    print(f"Loaded {len(real_episodes)} real episodes from telemetry")

    if len(real_episodes) < config.episodes:
        # Supplement with synthetic if not enough real data
        supplement = generate_synthetic_episodes(
            count=config.episodes - len(real_episodes),
            days_span=30,
        )
        quality_episodes = real_episodes + supplement
        print(f"Supplemented with {len(supplement)} synthetic episodes")
    else:
        quality_episodes = real_episodes

    scale_episodes = generate_synthetic_episodes(count=config.scale_episodes, days_span=30)
    print(f"Generated {len(scale_episodes)} synthetic episodes for scaling")

    # Connect backends
    seshat_service = await seshat_runner.create_seshat_service(config)
    graphiti_client = await graphiti_runner.create_graphiti_client(config)
    print("Connected to both backends\n")

    scenarios: dict[str, Any] = {}

    # Run scenarios
    if 1 in config.scenarios:
        print("--- Scenario 1: Episodic Memory — Store + Retrieve ---")
        seshat_s1 = await seshat_runner.run_scenario_1_episodic(seshat_service, quality_episodes)
        await graphiti_runner.clear_graphiti_data(graphiti_client)
        graphiti_s1 = await graphiti_runner.run_scenario_1_episodic(graphiti_client, quality_episodes)
        scenarios["episodic_retrieval"] = {"seshat": seshat_s1, "graphiti": graphiti_s1}
        print("  Done.\n")

    if 2 in config.scenarios:
        print("--- Scenario 2: Semantic Memory — Consolidation Quality ---")
        seshat_s2 = await seshat_runner.run_scenario_2_semantic(seshat_service, quality_episodes)
        graphiti_s2 = await graphiti_runner.run_scenario_2_semantic(graphiti_client, quality_episodes)
        scenarios["semantic_consolidation"] = {"seshat": seshat_s2, "graphiti": graphiti_s2}
        print("  Done.\n")

    if 3 in config.scenarios:
        print("--- Scenario 3: Temporal Queries ---")
        seshat_s3 = await seshat_runner.run_scenario_3_temporal(seshat_service, quality_episodes)
        graphiti_s3 = await graphiti_runner.run_scenario_3_temporal(graphiti_client, quality_episodes)
        scenarios["temporal_queries"] = {"seshat": seshat_s3, "graphiti": graphiti_s3}
        print("  Done.\n")

    if 4 in config.scenarios:
        print("--- Scenario 4: Entity Deduplication ---")
        await graphiti_runner.clear_graphiti_data(graphiti_client)
        seshat_s4 = await seshat_runner.run_scenario_4_dedup(seshat_service, quality_episodes)
        graphiti_s4 = await graphiti_runner.run_scenario_4_dedup(graphiti_client, quality_episodes)
        scenarios["entity_dedup"] = {"seshat": seshat_s4, "graphiti": graphiti_s4}
        print("  Done.\n")

    if 5 in config.scenarios:
        print("--- Scenario 5: Consolidation Lifecycle ---")
        await graphiti_runner.clear_graphiti_data(graphiti_client)
        seshat_s5 = await seshat_runner.run_scenario_5_lifecycle(seshat_service, quality_episodes)
        graphiti_s5 = await graphiti_runner.run_scenario_5_lifecycle(graphiti_client, quality_episodes)
        scenarios["consolidation_lifecycle"] = {"seshat": seshat_s5, "graphiti": graphiti_s5}
        print("  Done.\n")

    if 6 in config.scenarios:
        print("--- Scenario 6: Scaling ---")
        await graphiti_runner.clear_graphiti_data(graphiti_client)
        seshat_s6 = await seshat_runner.run_scenario_6_scaling(seshat_service, scale_episodes)
        await graphiti_runner.clear_graphiti_data(graphiti_client)
        graphiti_s6 = await graphiti_runner.run_scenario_6_scaling(graphiti_client, scale_episodes)
        scenarios["scaling"] = {"seshat": seshat_s6, "graphiti": graphiti_s6}
        print("  Done.\n")

    # Assemble results
    results = {
        "run_id": run_id,
        "config": {
            "llm": config.llm,
            "llm_model": config.llm_config.medium_model,
            "small_model": config.llm_config.small_model,
            "embedder": config.llm_config.embedder_model,
            "episodes": config.episodes,
            "scale_episodes": config.scale_episodes,
        },
        "scenarios": scenarios,
    }

    # Clean up experiment data from Seshat (don't pollute production graph)
    await seshat_runner.clean_experiment_data(seshat_service)

    # Disconnect
    await seshat_service.disconnect()
    await graphiti_client.close()

    return results


def main() -> None:
    args = parse_args()
    scenarios = [int(s) for s in args.scenarios.split(",")]
    output_dir = Path(args.output)

    llm_configs = ["openai", "anthropic"] if args.llm == "both" else [args.llm]

    all_results = []
    for llm in llm_configs:
        config = ExperimentConfig(
            llm=llm,
            scenarios=scenarios,
            episodes=args.episodes,
            scale_episodes=args.scale_episodes,
            output_dir=output_dir,
            neo4j_uri=args.neo4j_uri,
            graphiti_neo4j_uri=args.graphiti_neo4j_uri,
        )

        results = asyncio.run(run_experiment(config))

        # Save outputs
        run_id = results["run_id"]
        json_path = save_json_results(results, output_dir, run_id)
        md_path = save_markdown_report(results, output_dir, run_id)
        print(f"\nResults saved: {json_path}")
        print(f"Report saved: {md_path}")

        print_summary(results)
        all_results.append(results)

    if len(all_results) > 1:
        print("\n" + "=" * 60)
        print("A/B COMPARISON: OpenAI vs Anthropic")
        print("=" * 60)
        print("See individual report files for detailed comparison.")
        print("Key: Compare dedup ratios, retrieval precision, and cost across runs.")


if __name__ == "__main__":
    main()
