"""CLI interface for the personal agent.

This module provides a Typer-based command-line interface for interacting
with the orchestrator and agent capabilities.
"""

import asyncio
import json
import uuid
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from personal_agent.orchestrator import Channel, Orchestrator, OrchestratorResult
from personal_agent.telemetry import get_trace_events, query_events

app = typer.Typer(help="Personal AI Agent - Local AI collaborator")
console = Console()

# Create telemetry subcommand group
telemetry_app = typer.Typer(help="Telemetry analysis and query tools")
app.add_typer(telemetry_app, name="telemetry")


@app.command(name="chat")
def chat_command(
    message: str = typer.Argument(..., help="User message to send to the agent"),
    session_id: Optional[str] = typer.Option(
        None, "--session-id", help="Session ID for multi-turn conversations"
    ),
) -> None:
    """Chat with the agent (Q&A, general conversation).

    Examples:
        python -m personal_agent.ui.cli "What is Python?"
        python -m personal_agent.ui.cli "Hello" --session-id my-session
    """
    user_message = message

    # Generate session ID if not provided
    if not session_id:
        session_id = str(uuid.uuid4())

    # Run async orchestrator call
    result = asyncio.run(_handle_request(user_message, session_id, Channel.CHAT))

    # Display response
    console.print("\n[bold blue]Agent:[/bold blue]")
    console.print(Markdown(result["reply"]))

    # Optionally display trace ID for debugging
    if result.get("trace_id"):
        console.print(f"\n[dim]Trace ID: {result['trace_id']}[/dim]")


async def _handle_request(
    user_message: str, session_id: str, channel: Channel
) -> OrchestratorResult:
    """Handle a user request via orchestrator.

    Args:
        user_message: The user's message.
        session_id: Session identifier.
        channel: Communication channel.

    Returns:
        OrchestratorResult with reply, steps, and trace_id.
    """
    from personal_agent.captains_log.background import (
        get_background_task_count,
        wait_for_background_tasks,
    )
    from personal_agent.orchestrator.executor import _initialize_mcp_gateway

    # Initialize MCP gateway once (idempotent - won't re-initialize if already connected)
    await _initialize_mcp_gateway()

    orchestrator = Orchestrator()
    # Query current mode from brainstem (orchestrator will query if None)
    result = await orchestrator.handle_user_request(
        session_id=session_id,
        user_message=user_message,
        mode=None,  # Will query brainstem automatically
        channel=channel,
    )

    # Check if there are background tasks (like Captain's Log reflection)
    task_count = get_background_task_count()
    if task_count > 0:
        console.print(f"\n[dim]⏳ Completing {task_count} background task(s)...[/dim]")
        await wait_for_background_tasks()
        console.print("[dim]✓ Background tasks complete[/dim]\n")

    return result


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


if __name__ == "__main__":
    # Run the full app (includes chat and telemetry commands)
    # If no command provided and first arg is not a known command, treat as chat message
    import sys

    if len(sys.argv) > 1 and sys.argv[1] not in ["telemetry", "chat", "--help", "-h", "--version"]:
        # Treat as chat command with message (backward compatibility)
        # Insert "chat" command before the message
        sys.argv.insert(1, "chat")

    # Run normal Typer app for commands
    app()
