"""Telemetry CLI for the personal agent.

This module provides telemetry query and trace analysis. For chat, use the
service-backed CLI instead:

    uv run agent chat "Your message here"
    uv run agent chat "New topic" --new
"""

import json
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from personal_agent.telemetry import (
    get_logger,
    get_request_latency_breakdown,
    get_trace_events,
    query_events,
)

app = typer.Typer(help="Personal Agent telemetry (query, trace). For chat use: uv run agent chat")
console = Console()
log = get_logger(__name__)

# Telemetry subcommand group
telemetry_app = typer.Typer(help="Telemetry analysis and query tools")
app.add_typer(telemetry_app, name="telemetry")


@telemetry_app.command("query")
def telemetry_query(
    event: Optional[str] = typer.Option(
        None, "--event", "-e", help="Filter by event name (e.g., model_call_completed)"
    ),
    last: Optional[str] = typer.Option(
        None,
        "--last",
        "-l",
        help="Time window (e.g., 1h, 30m, 2d, 45s)",
    ),
    component: Optional[str] = typer.Option(
        None, "--component", "-c", help="Filter by component name"
    ),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Maximum number of results"),
    json_output: bool = typer.Option(
        False, "--json", help="Output as JSON instead of formatted table"
    ),
) -> None:
    """Query telemetry logs with flexible filters.

    Examples:
        agent telemetry query --event=model_call_completed --last=1h
        agent telemetry query --component=orchestrator --last=30m --limit=10
        agent telemetry query --event=tool_call_completed --json
    """
    try:
        entries = query_events(
            event=event,
            window_str=last,
            component=component,
            limit=limit,
        )

        if json_output:
            # Output as JSON array
            console.print(json.dumps(entries, indent=2))
        else:
            # Output as formatted table
            if not entries:
                console.print("[yellow]No matching log entries found.[/yellow]")
                return

            table = Table(title=f"Telemetry Query Results ({len(entries)} entries)")
            table.add_column("Timestamp", style="cyan")
            table.add_column("Event", style="green")
            table.add_column("Component", style="blue")
            table.add_column("Trace ID", style="magenta", overflow="fold")
            table.add_column("Details", style="white", overflow="fold")

            for entry in entries[:100]:  # Limit display to 100 rows
                timestamp = entry.get("timestamp", "N/A")
                event_name = entry.get("event", "N/A")
                component_name = entry.get("component", "N/A")
                trace_id = entry.get("trace_id", "N/A")
                # Create a summary of other fields
                details_dict = {
                    k: v
                    for k, v in entry.items()
                    if k not in ("timestamp", "event", "component", "trace_id", "logger", "level")
                }
                details = json.dumps(details_dict)[:100] if details_dict else ""

                table.add_row(
                    timestamp[:19] if len(timestamp) > 19 else timestamp,
                    event_name,
                    component_name,
                    trace_id[:36] if len(trace_id) > 36 else trace_id,
                    details,
                )

            console.print(table)
            if len(entries) > 100:
                console.print(
                    f"\n[yellow]Showing first 100 of {len(entries)} entries. Use --limit to control output.[/yellow]"
                )

    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        raise typer.Exit(1) from e


@telemetry_app.command("trace")
def telemetry_trace(
    trace_id: str = typer.Argument(..., help="Trace ID to reconstruct"),
    json_output: bool = typer.Option(
        False, "--json", help="Output as JSON instead of formatted timeline"
    ),
) -> None:
    """Reconstruct full execution trace for a given trace_id.

    Examples:
        agent telemetry trace abc-123-def-456
        agent telemetry trace abc-123-def-456 --json
    """
    try:
        entries = get_trace_events(trace_id)

        if not entries:
            console.print(f"[yellow]No log entries found for trace_id: {trace_id}[/yellow]")
            raise typer.Exit(0)

        if json_output:
            # Output as JSON array
            console.print(json.dumps(entries, indent=2))
        else:
            # Output as formatted timeline
            console.print(f"\n[bold blue]Trace Reconstruction: {trace_id}[/bold blue]")
            console.print(f"[dim]Found {len(entries)} log entries[/dim]\n")

            table = Table(title="Execution Timeline")
            table.add_column("Time", style="cyan")
            table.add_column("Event", style="green")
            table.add_column("Component", style="blue")
            table.add_column("Details", style="white", overflow="fold")

            for entry in entries:
                timestamp = entry.get("timestamp", "N/A")
                event_name = entry.get("event", "N/A")
                component_name = entry.get("component", "N/A")
                # Create a summary of other fields
                details_dict = {
                    k: v
                    for k, v in entry.items()
                    if k not in ("timestamp", "event", "component", "trace_id", "logger", "level")
                }
                details = json.dumps(details_dict)[:150] if details_dict else ""

                table.add_row(
                    timestamp[:19] if len(timestamp) > 19 else timestamp,
                    event_name,
                    component_name,
                    details,
                )

            console.print(table)

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e


@telemetry_app.command("trace-breakdown")
def telemetry_trace_breakdown(
    trace_id: str = typer.Argument(..., help="Trace ID for latency breakdown"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON instead of table"),
) -> None:
    """Show request-to-reply latency breakdown for a trace_id.

    Traces from request_received to reply_ready and breaks down time by phase:
    entry_to_task, init, planning, llm_call, tool_execution, synthesis,
    task_to_reply. Use this to see which step is taking the most time.

    Examples:
        agent telemetry trace-breakdown abc-123-def-456
        agent telemetry trace-breakdown abc-123-def-456 --json
    """
    try:
        breakdown = get_request_latency_breakdown(trace_id)

        if not breakdown:
            console.print(
                f"[yellow]No latency breakdown for trace_id: {trace_id}. "
                "Ensure the trace has request_received and reply_ready events "
                "(run a request after this change), or use 'agent telemetry trace' "
                "to inspect raw log entries.[/yellow]"
            )
            raise typer.Exit(0)

        if json_output:
            console.print(json.dumps(breakdown, indent=2))
        else:
            console.print(
                f"\n[bold blue]Request-to-reply latency breakdown: {trace_id}[/bold blue]\n"
            )
            table = Table(title="Phase durations")
            table.add_column("Phase", style="cyan")
            table.add_column("Duration (ms)", style="green")
            table.add_column("Description", style="white", overflow="fold")
            for row in breakdown:
                phase = row.get("phase", "")
                dur = row.get("duration_ms")
                desc = row.get("description", "")
                dur_str = f"{dur:.2f}" if dur is not None else "—"
                table.add_row(phase, dur_str, desc)
            console.print(table)
            total = next(
                (r["duration_ms"] for r in breakdown if r.get("phase") == "total_request_to_reply"),
                None,
            )
            if total is not None:
                console.print(f"\n[bold]Total request → reply: {total:.2f} ms[/bold]")

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e


if __name__ == "__main__":
    app()
