"""Format experiment results as JSON and markdown."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson


def save_json_results(
    results: dict[str, Any],
    output_dir: Path,
    run_id: str,
) -> Path:
    """Save full results as timestamped JSON.

    Args:
        results: Complete experiment results dict.
        output_dir: Directory for output files.
        run_id: Unique run identifier.

    Returns:
        Path to the saved JSON file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{run_id}.json"
    path.write_bytes(orjson.dumps(results, option=orjson.OPT_INDENT_2))
    return path


def format_markdown_report(results: dict[str, Any]) -> str:
    """Format results as markdown tables for pasting into the experiment report.

    Args:
        results: Complete experiment results dict.

    Returns:
        Markdown string with comparison tables.
    """
    config = results.get("config", {})
    scenarios = results.get("scenarios", {})

    lines = [
        f"## Experiment Run: {results.get('run_id', 'unknown')}",
        "",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**LLM:** {config.get('llm', 'unknown')} (medium: {config.get('llm_model', '?')}, small: {config.get('small_model', '?')})",
        f"**Embedder:** {config.get('embedder', 'unknown')}",
        f"**Episodes:** {config.get('episodes', '?')} quality, {config.get('scale_episodes', '?')} scaling",
        "",
    ]

    # Scenario 1: Episodic Retrieval
    if "episodic_retrieval" in scenarios:
        s = scenarios["episodic_retrieval"]
        lines.extend([
            "### Scenario 1: Episodic Memory — Store + Retrieve",
            "",
            "| Metric | Seshat | Graphiti |",
            "|--------|--------|---------|",
        ])
        seshat = s.get("seshat", {})
        graphiti = s.get("graphiti", {})

        si = seshat.get("ingest", {})
        gi = graphiti.get("ingest", {})
        lines.append(f"| Ingest p50 (ms) | {si.get('p50_ms', '-')} | {gi.get('p50_ms', '-')} |")

        sq = seshat.get("query", {})
        gq = graphiti.get("query", {})
        lines.append(f"| Query p50 (ms) | {sq.get('p50_ms', '-')} | {gq.get('p50_ms', '-')} |")
        lines.append(f"| Query p95 (ms) | {sq.get('p95_ms', '-')} | {gq.get('p95_ms', '-')} |")

        sr = seshat.get("retrieval", {})
        gr = graphiti.get("retrieval", {})
        lines.append(f"| Avg Precision | {sr.get('avg_precision', '-')} | {gr.get('avg_precision', '-')} |")
        lines.append(f"| Avg Recall | {sr.get('avg_recall', '-')} | {gr.get('avg_recall', '-')} |")
        lines.append("")

    # Scenario 4: Entity Dedup
    if "entity_dedup" in scenarios:
        s = scenarios["entity_dedup"]
        lines.extend([
            "### Scenario 4: Entity Deduplication",
            "",
            "| Metric | Seshat | Graphiti |",
            "|--------|--------|---------|",
        ])
        sd = s.get("seshat", {}).get("dedup", {})
        gd = s.get("graphiti", {}).get("dedup", {})
        lines.append(f"| Raw Mentions | {sd.get('raw_mentions', '-')} | {gd.get('raw_mentions', '-')} |")
        lines.append(f"| Unique Entities | {sd.get('unique_entities_created', '-')} | {gd.get('unique_entities_created', '-')} |")
        lines.append(f"| Dedup Ratio | {sd.get('dedup_ratio', '-')} | {gd.get('dedup_ratio', '-')} |")
        lines.append(f"| Expected Canonical | {sd.get('expected_canonical', '-')} | {gd.get('expected_canonical', '-')} |")
        lines.append("")

    # Scenario 6: Scaling
    if "scaling" in scenarios:
        s = scenarios["scaling"]
        lines.extend([
            "### Scenario 6: Scaling",
            "",
            "| Checkpoint | Seshat Ingest (ms) | Graphiti Ingest (ms) | Seshat Query p50 | Graphiti Query p50 |",
            "|------------|-------------------|---------------------|-----------------|-------------------|",
        ])
        s_checks = s.get("seshat", {}).get("checkpoints", [])
        g_checks = s.get("graphiti", {}).get("checkpoints", [])
        for sc, gc in zip(s_checks, g_checks):
            ep = sc.get("episodes_ingested", "?")
            si = sc.get("ingest_mean_ms", "-")
            gi = gc.get("ingest_mean_ms", "-")
            sq = sc.get("query", {}).get("p50_ms", "-")
            gq = gc.get("query", {}).get("p50_ms", "-")
            lines.append(f"| {ep} | {si} | {gi} | {sq} | {gq} |")
        lines.append("")

    # Cost comparison
    cost = results.get("cost", {})
    if cost:
        lines.extend([
            "### Cost Comparison",
            "",
            "| Metric | Seshat | Graphiti |",
            "|--------|--------|---------|",
            f"| LLM Input Tokens | {cost.get('seshat', {}).get('input_tokens', '-')} | {cost.get('graphiti', {}).get('input_tokens', '-')} |",
            f"| LLM Output Tokens | {cost.get('seshat', {}).get('output_tokens', '-')} | {cost.get('graphiti', {}).get('output_tokens', '-')} |",
            f"| Estimated Cost (USD) | ${cost.get('seshat', {}).get('estimated_cost_usd', '-')} | ${cost.get('graphiti', {}).get('estimated_cost_usd', '-')} |",
            "",
        ])

    return "\n".join(lines)


def save_markdown_report(
    results: dict[str, Any],
    output_dir: Path,
    run_id: str,
) -> Path:
    """Save markdown report fragment.

    Args:
        results: Complete experiment results dict.
        output_dir: Directory for output files.
        run_id: Unique run identifier.

    Returns:
        Path to the saved markdown file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{run_id}-report.md"
    path.write_text(format_markdown_report(results))
    return path


def print_summary(results: dict[str, Any]) -> None:
    """Print a console summary of the experiment results."""
    print("\n" + "=" * 70)
    print("GRAPHITI EXPERIMENT RESULTS")
    print("=" * 70)
    print(format_markdown_report(results))
    print("=" * 70)
