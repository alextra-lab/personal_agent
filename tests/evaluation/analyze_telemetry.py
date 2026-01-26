"""Telemetry analysis script for identifying bottlenecks, errors, and policy violations.

This script analyzes telemetry logs to provide insights for system refinement.

Usage:
    python tests/evaluation/analyze_telemetry.py [--window=1h|24h|7d]
"""

import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from personal_agent.telemetry.metrics import query_events

app = typer.Typer()


def parse_time_window(window: str) -> int:
    """Parse time window string to seconds."""
    if window.endswith("h"):
        return int(window[:-1]) * 3600
    elif window.endswith("d"):
        return int(window[:-1]) * 86400
    elif window.endswith("m"):
        return int(window[:-1]) * 60
    else:
        raise ValueError(f"Invalid time window: {window}")


def analyze_performance(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze performance metrics from events."""
    model_calls = [e for e in events if e.get("event") == "model_call_completed"]
    tool_calls = [e for e in events if e.get("event") == "tool_call_completed"]
    tasks = [e for e in events if e.get("event") == "task_completed"]

    # Calculate latencies
    model_latencies = [e.get("duration_ms", 0) for e in model_calls]
    tool_latencies = [e.get("duration_ms", 0) for e in tool_calls]
    task_latencies = [e.get("duration_ms", 0) for e in tasks]

    # Model usage by role
    model_usage = Counter(e.get("model_role") for e in model_calls)

    # Tool usage
    tool_usage = Counter(e.get("tool_name") for e in tool_calls)

    return {
        "model_calls": {
            "total": len(model_calls),
            "avg_latency_ms": sum(model_latencies) / len(model_latencies) if model_latencies else 0,
            "max_latency_ms": max(model_latencies) if model_latencies else 0,
            "by_role": dict(model_usage),
        },
        "tool_calls": {
            "total": len(tool_calls),
            "avg_latency_ms": sum(tool_latencies) / len(tool_latencies) if tool_latencies else 0,
            "max_latency_ms": max(tool_latencies) if tool_latencies else 0,
            "by_tool": dict(tool_usage),
        },
        "tasks": {
            "total": len(tasks),
            "avg_latency_ms": sum(task_latencies) / len(task_latencies) if task_latencies else 0,
            "max_latency_ms": max(task_latencies) if task_latencies else 0,
        },
    }


def analyze_errors(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze errors and failures from events."""
    failures = [
        e
        for e in events
        if e.get("event")
        in [
            "task_failed",
            "model_call_failed",
            "tool_call_failed",
            "state_transition_failed",
        ]
    ]

    # Categorize errors
    error_types = Counter(e.get("event") for e in failures)
    error_reasons = Counter(e.get("error") or e.get("reason") for e in failures)

    # Find most common failure patterns
    failure_traces = defaultdict(list)
    for failure in failures:
        trace_id = failure.get("trace_id")
        if trace_id:
            failure_traces[trace_id].append(failure)

    return {
        "total_failures": len(failures),
        "by_type": dict(error_types),
        "common_reasons": dict(error_reasons.most_common(10)),
        "failed_traces": len(failure_traces),
    }


def analyze_routing(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze routing decisions."""
    routing_decisions = [e for e in events if e.get("event") == "routing_decision"]

    decisions = Counter(e.get("decision") for e in routing_decisions)
    targets = Counter(e.get("target_model") for e in routing_decisions)

    # Calculate delegation rate
    total_decisions = len(routing_decisions)
    delegations = sum(1 for e in routing_decisions if e.get("decision") == "DELEGATE")
    delegation_rate = delegations / total_decisions if total_decisions > 0 else 0

    return {
        "total_decisions": total_decisions,
        "delegation_rate": delegation_rate,
        "by_decision": dict(decisions),
        "by_target": dict(targets),
    }


def analyze_governance(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze governance enforcement."""
    mode_transitions = [e for e in events if e.get("event") == "mode_transition"]

    permission_denials = [e for e in events if e.get("event") == "permission_denied"]

    # Mode distribution
    mode_changes = Counter(f"{e.get('from_mode')} → {e.get('to_mode')}" for e in mode_transitions)

    # Denial reasons
    denial_reasons = Counter(e.get("reason") for e in permission_denials)

    return {
        "mode_transitions": len(mode_transitions),
        "mode_changes": dict(mode_changes),
        "permission_denials": len(permission_denials),
        "denial_reasons": dict(denial_reasons),
    }


def generate_recommendations(
    performance: dict[str, Any],
    errors: dict[str, Any],
    routing: dict[str, Any],
    governance: dict[str, Any],
) -> list[str]:
    """Generate recommendations based on analysis."""
    recommendations = []

    # Performance recommendations
    if performance["model_calls"]["avg_latency_ms"] > 5000:
        recommendations.append(
            f"⚠️ High model latency ({performance['model_calls']['avg_latency_ms']:.0f}ms avg): "
            "Consider optimizing prompts or using faster models"
        )

    if performance["tool_calls"]["avg_latency_ms"] > 1000:
        recommendations.append(
            f"⚠️ High tool latency ({performance['tool_calls']['avg_latency_ms']:.0f}ms avg): "
            "Investigate slow tools or add caching"
        )

    # Error recommendations
    if errors["total_failures"] > 0:
        failure_rate = errors["total_failures"] / max(performance["tasks"]["total"], 1)
        if failure_rate > 0.1:
            recommendations.append(
                f"⚠️ High failure rate ({failure_rate:.1%}): "
                "Review error logs and improve error handling"
            )

    # Routing recommendations
    if routing["delegation_rate"] > 0.8:
        recommendations.append(
            f"⚠️ High delegation rate ({routing['delegation_rate']:.1%}): "
            "Router is rarely handling queries directly - may need prompt tuning"
        )
    elif routing["delegation_rate"] < 0.2:
        recommendations.append(
            f"ℹ️ Low delegation rate ({routing['delegation_rate']:.1%}): "
            "Router is handling most queries - verify quality of simple responses"
        )

    # Governance recommendations
    if governance["permission_denials"] > 0:
        recommendations.append(
            f"ℹ️ Permission denials detected ({governance['permission_denials']}): "
            "Review tool policies if legitimate operations are being blocked"
        )

    if not recommendations:
        recommendations.append("✅ System is performing well with no major issues detected")

    return recommendations


@app.command()
def main(
    window: str = typer.Option(
        "1h",
        help="Time window to analyze (e.g., 1h, 24h, 7d)",
    ),
    output: str = typer.Option(
        "telemetry/evaluation/telemetry_analysis.md",
        help="Output file for analysis report",
    ),
) -> None:
    """Analyze telemetry data for bottlenecks and issues."""
    print(f"🔍 Analyzing telemetry for last {window}...")

    # Parse time window
    try:
        window_seconds = parse_time_window(window)
    except ValueError as e:
        print(f"❌ {e}")
        raise typer.Exit(1) from e

    # Query events
    cutoff_time = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    events = query_events(
        start_time=cutoff_time.isoformat(),
    )

    if not events:
        print(f"⚠️ No events found in the last {window}")
        print("Run some tasks to generate telemetry data first")
        raise typer.Exit(0)

    print(f"📊 Found {len(events)} events")

    # Analyze
    performance = analyze_performance(events)
    errors = analyze_errors(events)
    routing = analyze_routing(events)
    governance = analyze_governance(events)

    # Generate recommendations
    recommendations = generate_recommendations(performance, errors, routing, governance)

    # Write report
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        f.write("# Telemetry Analysis Report\n\n")
        f.write(f"**Time Window**: {window}\n")
        f.write(f"**Analysis Date**: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"**Total Events**: {len(events)}\n\n")

        f.write("## Performance Metrics\n\n")
        f.write("### Model Calls\n\n")
        f.write(f"- **Total**: {performance['model_calls']['total']}\n")
        f.write(f"- **Avg Latency**: {performance['model_calls']['avg_latency_ms']:.0f}ms\n")
        f.write(f"- **Max Latency**: {performance['model_calls']['max_latency_ms']:.0f}ms\n")
        f.write(f"- **By Role**: {performance['model_calls']['by_role']}\n\n")

        f.write("### Tool Calls\n\n")
        f.write(f"- **Total**: {performance['tool_calls']['total']}\n")
        f.write(f"- **Avg Latency**: {performance['tool_calls']['avg_latency_ms']:.0f}ms\n")
        f.write(f"- **Max Latency**: {performance['tool_calls']['max_latency_ms']:.0f}ms\n")
        f.write(f"- **By Tool**: {performance['tool_calls']['by_tool']}\n\n")

        f.write("### Tasks\n\n")
        f.write(f"- **Total**: {performance['tasks']['total']}\n")
        f.write(f"- **Avg Latency**: {performance['tasks']['avg_latency_ms']:.0f}ms\n")
        f.write(f"- **Max Latency**: {performance['tasks']['max_latency_ms']:.0f}ms\n\n")

        f.write("## Error Analysis\n\n")
        f.write(f"- **Total Failures**: {errors['total_failures']}\n")
        f.write(f"- **Failed Traces**: {errors['failed_traces']}\n")
        f.write(f"- **By Type**: {errors['by_type']}\n")
        f.write(f"- **Common Reasons**: {errors['common_reasons']}\n\n")

        f.write("## Routing Analysis\n\n")
        f.write(f"- **Total Decisions**: {routing['total_decisions']}\n")
        f.write(f"- **Delegation Rate**: {routing['delegation_rate']:.1%}\n")
        f.write(f"- **By Decision**: {routing['by_decision']}\n")
        f.write(f"- **By Target**: {routing['by_target']}\n\n")

        f.write("## Governance Analysis\n\n")
        f.write(f"- **Mode Transitions**: {governance['mode_transitions']}\n")
        f.write(f"- **Mode Changes**: {governance['mode_changes']}\n")
        f.write(f"- **Permission Denials**: {governance['permission_denials']}\n")
        f.write(f"- **Denial Reasons**: {governance['denial_reasons']}\n\n")

        f.write("## Recommendations\n\n")
        for rec in recommendations:
            f.write(f"- {rec}\n")

        f.write("\n---\n\n")
        f.write("*Generated by analyze_telemetry.py*\n")

    print("\n✅ Analysis complete!")
    print(f"📄 Report: {output_path}")

    # Print summary to console
    print("\n📊 Summary:")
    print(f"  Tasks: {performance['tasks']['total']}")
    print(f"  Model calls: {performance['model_calls']['total']}")
    print(f"  Tool calls: {performance['tool_calls']['total']}")
    print(f"  Failures: {errors['total_failures']}")
    print(f"  Delegation rate: {routing['delegation_rate']:.1%}")

    print("\n💡 Top Recommendations:")
    for i, rec in enumerate(recommendations[:3], 1):
        print(f"  {i}. {rec}")


if __name__ == "__main__":
    app()
