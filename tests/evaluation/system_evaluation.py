"""System-wide evaluation script for Day 26-28: Evaluation & Refinement.

This script runs multiple tasks across different scenarios (chat, coding, system health),
collects telemetry data, and generates a comprehensive evaluation report.

Usage:
    python tests/evaluation/system_evaluation.py [--scenarios=all|chat|coding|health]
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.metrics import (
    get_trace_events,
)

log = get_logger(__name__)

app = typer.Typer()


# ============================================================================
# Test Scenarios
# ============================================================================

CHAT_SCENARIOS = [
    {
        "name": "simple_greeting",
        "query": "Hello! How are you today?",
        "channel": Channel.CHAT,
        "expected_routing": None,  # Router handles simple greetings directly (no delegation)
        "timeout": 10,
    },
    {
        "name": "factual_question",
        "query": "What is Python and why is it popular?",
        "channel": Channel.CHAT,
        "expected_routing": "standard",  # Router delegates to STANDARD (lowercase enum value)
        "timeout": 60,  # Increased from 30s
    },
    {
        "name": "deep_reasoning",
        "query": "Think deeply: What are the philosophical implications of artificial intelligence achieving consciousness?",
        "channel": Channel.CHAT,
        "expected_routing": "reasoning",  # Escalates to REASONING (lowercase enum value)
        "timeout": 90,  # Increased from 60s for deep reasoning
    },
]

CODING_SCENARIOS = [
    {
        "name": "code_explanation",
        "query": "Explain what this Python code does: def fib(n): return n if n <= 1 else fib(n-1) + fib(n-2)",
        "channel": Channel.CODE_TASK,
        "expected_routing": None,  # CODE_TASK bypasses router, goes directly to model
        "timeout": 90,  # Increased from 60s
    },
    {
        "name": "code_review",
        "query": "Review this Python code for potential bugs:\n\n```python\nx = input('Enter number: ')\nresult = 10 / x\nprint(result)\n```\n\nWhat could go wrong?",
        "channel": Channel.CODE_TASK,
        "expected_routing": None,  # CODE_TASK bypasses router
        "timeout": 120,  # Increased from 60s (uses tools)
    },
]

SYSTEM_HEALTH_SCENARIOS = [
    {
        "name": "system_metrics",
        "query": "What is my Mac's current health status?",
        "channel": Channel.SYSTEM_HEALTH,
        "expected_routing": None,  # SYSTEM_HEALTH bypasses router, uses REASONING
        "timeout": 180,  # Increased from 60s (4+ LLM calls with 14b model)
        "expects_tool_calls": True,
        "expected_tools": ["system_metrics_snapshot"],
    },
    {
        "name": "resource_check",
        "query": "Check if my system resources are being used efficiently.",
        "channel": Channel.SYSTEM_HEALTH,
        "expected_routing": None,  # SYSTEM_HEALTH bypasses router
        "timeout": 180,  # Increased from 60s (4+ LLM calls with 14b model)
        "expects_tool_calls": True,
        "expected_tools": ["system_metrics_snapshot"],
    },
]

ALL_SCENARIOS = {
    "chat": CHAT_SCENARIOS,
    "coding": CODING_SCENARIOS,
    "health": SYSTEM_HEALTH_SCENARIOS,
}


# ============================================================================
# Evaluation Logic
# ============================================================================


async def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Run a single test scenario and collect metrics."""
    log.info(
        "evaluation_scenario_started",
        scenario=scenario["name"],
        query=scenario["query"][:50],
    )

    start_time = time.time()

    try:
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id=f"eval-{scenario['name']}",
            user_message=scenario["query"],
            channel=scenario["channel"],
            mode=None,  # Let orchestrator query brainstem
        )

        elapsed_ms = int((time.time() - start_time) * 1000)

        # Collect trace events for analysis
        trace_id = result["trace_id"]
        trace_events = get_trace_events(trace_id)

        # Extract key metrics from trace
        routing_decision = None
        tool_calls = []
        model_calls = 0

        for event in trace_events:
            if event.get("event") == "routing_decision":
                routing_decision = event.get("target_model")
            elif event.get("event") == "tool_call_started":
                tool_calls.append(event.get("tool_name"))
            elif event.get("event") == "model_call_completed":
                model_calls += 1

        # Validate expectations
        expected_routing = scenario.get("expected_routing")
        if expected_routing is None:
            # No routing validation needed (channel bypasses router)
            routing_matches = True
        else:
            routing_matches = routing_decision == expected_routing

        tool_check_passed = True
        if scenario.get("expects_tool_calls"):
            expected_tools = set(scenario.get("expected_tools", []))
            actual_tools = set(tool_calls)
            tool_check_passed = expected_tools.issubset(actual_tools)

        # Check for errors in reply
        reply = result.get("reply", "")
        has_error = reply.startswith("Error:") if reply else True

        success = (
            not has_error
            and routing_matches
            and tool_check_passed
            and elapsed_ms < (scenario["timeout"] * 1000)
        )

        log.info(
            "evaluation_scenario_completed",
            scenario=scenario["name"],
            success=success,
            elapsed_ms=elapsed_ms,
            routing_decision=routing_decision,
            tool_calls=tool_calls,
            model_calls=model_calls,
            trace_id=trace_id,
        )

        return {
            "scenario": scenario["name"],
            "success": success,
            "elapsed_ms": elapsed_ms,
            "trace_id": trace_id,
            "routing_decision": routing_decision,
            "routing_matches": routing_matches,
            "tool_calls": tool_calls,
            "tool_check_passed": tool_check_passed,
            "model_calls": model_calls,
            "error": None,
            "response_length": len(reply),
        }

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        trace_id = f"error-{scenario['name']}"
        log.error(
            "evaluation_scenario_failed",
            scenario=scenario["name"],
            error=str(e),
            elapsed_ms=elapsed_ms,
            trace_id=trace_id,
            exc_info=True,
        )

        return {
            "scenario": scenario["name"],
            "success": False,
            "elapsed_ms": elapsed_ms,
            "trace_id": trace_id,
            "routing_decision": None,
            "routing_matches": False,
            "tool_calls": [],
            "tool_check_passed": False,
            "model_calls": 0,
            "error": str(e),
            "response_length": 0,
        }


async def run_evaluation(
    scenario_types: list[str],
) -> dict[str, Any]:
    """Run evaluation across specified scenario types."""
    log.info("evaluation_started", scenario_types=scenario_types)

    results = []
    for scenario_type in scenario_types:
        scenarios = ALL_SCENARIOS.get(scenario_type, [])
        log.info(f"Running {len(scenarios)} {scenario_type} scenarios...")

        for scenario in scenarios:
            result = await run_scenario(scenario)
            results.append(result)

            # Delay between scenarios to allow GPU/CPU cooldown and reduce thermal throttling
            await asyncio.sleep(5)  # Increased from 1s to 5s

    # Wait for background tasks (Captain's Log reflections) to complete
    try:
        from personal_agent.captains_log.background import wait_for_background_tasks

        await wait_for_background_tasks()
    except Exception as e:
        log.warning("background_task_wait_failed", error=str(e))

    # Aggregate metrics
    total_scenarios = len(results)
    successful = sum(1 for r in results if r["success"])
    failed = total_scenarios - successful

    avg_latency = (
        sum(r["elapsed_ms"] for r in results) / total_scenarios if total_scenarios > 0 else 0
    )
    max_latency = max((r["elapsed_ms"] for r in results), default=0)

    # Only calculate routing accuracy for CHAT scenarios (others bypass router)
    chat_scenarios = [r for r in results if r["scenario"] in [s["name"] for s in CHAT_SCENARIOS]]
    routing_accuracy = (
        sum(1 for r in chat_scenarios if r["routing_matches"]) / len(chat_scenarios)
        if len(chat_scenarios) > 0
        else 1.0  # 100% if no routing validation needed
    )

    tool_accuracy = (
        sum(1 for r in results if r["tool_check_passed"]) / total_scenarios
        if total_scenarios > 0
        else 0
    )

    total_model_calls = sum(r["model_calls"] for r in results)
    total_tool_calls = sum(len(r["tool_calls"]) for r in results)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_scenarios": total_scenarios,
        "successful": successful,
        "failed": failed,
        "success_rate": successful / total_scenarios if total_scenarios > 0 else 0,
        "avg_latency_ms": avg_latency,
        "max_latency_ms": max_latency,
        "routing_accuracy": routing_accuracy,
        "tool_accuracy": tool_accuracy,
        "total_model_calls": total_model_calls,
        "total_tool_calls": total_tool_calls,
        "results": results,
    }

    log.info("evaluation_completed", summary={k: v for k, v in summary.items() if k != "results"})

    return summary


def generate_report(summary: dict[str, Any], output_path: Path) -> None:
    """Generate evaluation report with timestamped filenames."""
    # Generate timestamp for filenames
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Write JSON data with timestamp
    json_path = output_path / f"evaluation_results_{timestamp}.json"
    json_latest = output_path / "evaluation_results.json"
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    # Create symlink/copy to "latest" file for convenience
    with open(json_latest, "w") as f:
        json.dump(summary, f, indent=2)

    # Write human-readable report with timestamp
    report_path = output_path / f"evaluation_report_{timestamp}.md"
    report_latest = output_path / "evaluation_report.md"
    with open(report_path, "w") as f:
        f.write("# System Evaluation Report\n\n")
        f.write(f"**Date**: {summary['timestamp']}\n\n")
        f.write("## Summary\n\n")
        f.write(f"- **Total Scenarios**: {summary['total_scenarios']}\n")
        f.write(f"- **Successful**: {summary['successful']}\n")
        f.write(f"- **Failed**: {summary['failed']}\n")
        f.write(f"- **Success Rate**: {summary['success_rate']:.1%}\n\n")

        f.write("## Performance Metrics\n\n")
        f.write(f"- **Average Latency**: {summary['avg_latency_ms']:.0f}ms\n")
        f.write(f"- **Max Latency**: {summary['max_latency_ms']:.0f}ms\n")
        f.write(f"- **Routing Accuracy**: {summary['routing_accuracy']:.1%}\n")
        f.write(f"- **Tool Accuracy**: {summary['tool_accuracy']:.1%}\n")
        f.write(f"- **Total Model Calls**: {summary['total_model_calls']}\n")
        f.write(f"- **Total Tool Calls**: {summary['total_tool_calls']}\n\n")

        f.write("## Detailed Results\n\n")
        f.write("| Scenario | Success | Latency (ms) | Routing | Tool Calls | Error |\n")
        f.write("|----------|---------|--------------|---------|------------|---------|\n")

        for result in summary["results"]:
            success_icon = "✅" if result["success"] else "❌"
            routing = result["routing_decision"] or "N/A"
            tools = ", ".join(result["tool_calls"]) if result["tool_calls"] else "-"
            error = result["error"][:30] + "..." if result["error"] else "-"

            f.write(
                f"| {result['scenario']} | {success_icon} | {result['elapsed_ms']} | {routing} | {tools} | {error} |\n"
            )

        f.write("\n## Trace IDs for Analysis\n\n")
        f.write("Use these trace IDs to investigate issues:\n\n")
        for result in summary["results"]:
            f.write(f"- **{result['scenario']}**: `{result['trace_id']}`\n")

        f.write("\n## Recommendations\n\n")

        # Generate recommendations based on findings
        if summary["success_rate"] < 0.8:
            f.write(
                f"⚠️ **Low success rate** ({summary['success_rate']:.1%}): Investigate failed scenarios\n\n"
            )

        if summary["avg_latency_ms"] > 10000:
            f.write(
                f"⚠️ **High latency** ({summary['avg_latency_ms']:.0f}ms): Optimize model calls or tool execution\n\n"
            )

        if summary["routing_accuracy"] < 0.9:
            f.write(
                f"⚠️ **Poor routing accuracy** ({summary['routing_accuracy']:.1%}): Review router prompt and decision logic\n\n"
            )

        if summary["tool_accuracy"] < 1.0:
            f.write(
                f"⚠️ **Tool selection issues** ({summary['tool_accuracy']:.1%}): Review tool calling logic\n\n"
            )

        f.write("\n---\n\n")
        f.write("*Generated by system_evaluation.py*\n")

    # Also write to "latest" file for convenience
    with open(report_latest, "w") as f:
        # Copy the same content
        with open(report_path, "r") as src:
            f.write(src.read())

    print("\n✅ Evaluation complete!")
    print(f"📊 Timestamped results: {json_path}")
    print(f"📄 Timestamped report: {report_path}")
    print(f"🔗 Latest results: {json_latest}")
    print(f"🔗 Latest report: {report_latest}")


# ============================================================================
# CLI Interface
# ============================================================================


@app.command()
def main(
    scenarios: str = typer.Option(
        "all",
        help="Scenarios to run: all, chat, coding, health, or comma-separated list",
    ),
    output_dir: str = typer.Option(
        "telemetry/evaluation",
        help="Directory to write evaluation results",
    ),
) -> None:
    """Run system evaluation across multiple scenarios."""
    # Parse scenario types
    if scenarios == "all":
        scenario_types = ["chat", "coding", "health"]
    else:
        scenario_types = [s.strip() for s in scenarios.split(",")]

    # Validate scenario types
    invalid = [s for s in scenario_types if s not in ALL_SCENARIOS]
    if invalid:
        print(f"❌ Invalid scenario types: {invalid}")
        print(f"Valid types: {list(ALL_SCENARIOS.keys())}")
        raise typer.Exit(1)

    # Create output directory
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Run evaluation
    print(f"🚀 Running evaluation for scenarios: {', '.join(scenario_types)}")
    print(f"📁 Output directory: {output_path}")
    print()

    summary = asyncio.run(run_evaluation(scenario_types))

    # Generate report
    generate_report(summary, output_path)

    # Exit with error code if any scenarios failed
    if summary["failed"] > 0:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
