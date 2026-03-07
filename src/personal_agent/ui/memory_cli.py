"""Direct memory graph CLI — no agent service required.

Connects directly to Neo4j via MemoryService for fast, offline inspection.

Usage:
    uv run agent memory search "greek islands"
    uv run agent memory search "greek islands" --type Location --json
    uv run agent memory entities --type Location --limit 20
    uv run agent memory sessions --last 7
    uv run agent memory stats
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from personal_agent.telemetry import get_logger

log = get_logger(__name__)
console = Console()

memory_app = typer.Typer(help="Query the personal memory graph directly (no service required)")


def _run(coro: Any) -> Any:
    """Run a coroutine synchronously (Typer commands are sync)."""
    return asyncio.run(coro)


async def _get_memory_service() -> Any:
    """Connect to MemoryService using configured Neo4j credentials.

    Returns:
        Connected MemoryService instance.

    Raises:
        typer.Exit: Exit code 1 if Neo4j is unreachable.
    """
    from personal_agent.memory.service import MemoryService

    svc = MemoryService()
    await svc.connect()
    if not svc.connected:
        console.print(
            "[red]Cannot connect to Neo4j. Check settings.neo4j_uri and credentials.[/red]"
        )
        raise typer.Exit(1)
    return svc


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(..., help="Free-text search query"),
    entity_type: list[str] = typer.Option(
        [], "--type", "-t", help="Filter by entity type (repeatable). E.g. --type Location --type Person"
    ),
    days: int = typer.Option(90, "--days", "-d", help="Look back this many days (0 = all history)"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Search the memory graph by text, returning entities and matching turns."""
    from personal_agent.memory.models import MemoryQuery

    async def _run_search() -> None:
        svc = await _get_memory_service()
        try:
            entity_names = [
                w.strip('",.:;!?()')
                for w in query.split()
                if len(w) > 2 and w[0].isupper()
            ]
            q = MemoryQuery(
                entity_names=entity_names or [],
                entity_types=list(entity_type),
                limit=min(limit, 100),
                recency_days=days if days > 0 else None,
            )
            result = await svc.query_memory(q, query_text=query)

            if json_output:
                data: dict[str, Any] = {
                    "query": query,
                    "turns": [
                        {
                            "turn_id": t.turn_id,
                            "timestamp": t.timestamp.isoformat(),
                            "session_id": t.session_id,
                            "user_message": t.user_message,
                            "assistant_response": t.assistant_response,
                            "summary": t.summary,
                            "key_entities": t.key_entities,
                        }
                        for t in result.conversations
                    ],
                }
                console.print(json.dumps(data))
            else:
                if not result.conversations:
                    console.print("[yellow]No turns matched your query.[/yellow]")
                    return
                table = Table(title=f"Memory Search: {query!r}", show_lines=True)
                table.add_column("Timestamp", style="dim", width=20)
                table.add_column("Session", style="cyan", width=10)
                table.add_column("User Message", max_width=50)
                table.add_column("Summary / Response", max_width=60)
                table.add_column("Key Entities", style="green", max_width=30)
                for t in result.conversations:
                    summary_text = (t.summary or t.assistant_response or "")[:300]
                    table.add_row(
                        t.timestamp.strftime("%Y-%m-%d %H:%M"),
                        (t.session_id or "")[:8],
                        (t.user_message or "")[:200],
                        summary_text,
                        ", ".join((t.key_entities or [])[:5]),
                    )
                console.print(table)
        finally:
            await svc.disconnect()

    _run(_run_search())


@memory_app.command("entities")
def memory_entities(
    entity_type: list[str] = typer.Option(
        [], "--type", "-t", help="Filter by entity type (repeatable)"
    ),
    days: int = typer.Option(90, "--days", "-d", help="Only entities seen in last N days (0 = all)"),
    limit: int = typer.Option(30, "--limit", "-n", help="Max entities to show"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    sort_by: str = typer.Option(
        "mentions", "--sort", help="Sort by: mentions | name (last_seen not available from broad query)"
    ),
) -> None:
    """List entities in the memory graph, sorted by frequency or name."""

    async def _run_entities() -> None:
        svc = await _get_memory_service()
        try:
            broad = await svc.query_memory_broad(
                entity_types=list(entity_type) if entity_type else None,
                recency_days=days if days > 0 else 3650,
                limit=limit,
            )
            entities = list(broad.get("entities", []))

            if sort_by == "name":
                entities = sorted(entities, key=lambda e: (e.get("name") or "").lower())

            if json_output:
                console.print(json.dumps({"entities": entities}))
            else:
                if not entities:
                    console.print("[yellow]No entities found.[/yellow]")
                    return
                table = Table(title="Memory Entities", show_lines=False)
                table.add_column("Name", style="bold cyan")
                table.add_column("Type", style="green")
                table.add_column("Mentions", justify="right", style="yellow")
                table.add_column("Description", max_width=60)
                for e in entities:
                    table.add_row(
                        e.get("name", ""),
                        e.get("type", ""),
                        str(e.get("mentions", 0)),
                        ((e.get("description") or "")[:80]),
                    )
                console.print(table)
        finally:
            await svc.disconnect()

    _run(_run_entities())


@memory_app.command("sessions")
def memory_sessions(
    last: int = typer.Option(10, "--last", "-n", help="Show last N sessions"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List recent conversation sessions with their dominant topics."""

    async def _run_sessions() -> None:
        svc = await _get_memory_service()
        try:
            broad = await svc.query_memory_broad(
                recency_days=3650,
                limit=100,
            )
            sessions = list(broad.get("sessions", []))[:last]

            if json_output:
                console.print(json.dumps({"sessions": sessions}))
            else:
                if not sessions:
                    console.print("[yellow]No sessions found.[/yellow]")
                    return
                table = Table(title=f"Last {last} Sessions", show_lines=False)
                table.add_column("Session ID", style="dim", width=12)
                table.add_column("Started", style="cyan", width=18)
                table.add_column("Turns", justify="right", style="yellow")
                table.add_column("Dominant Topics", style="green")
                for s in sessions:
                    entities_str = ", ".join((s.get("dominant_entities") or [])[:5])
                    started = s.get("started_at", "")
                    if started:
                        try:
                            if isinstance(started, str):
                                started = datetime.fromisoformat(started).strftime("%Y-%m-%d %H:%M")
                            else:
                                started = str(started)[:16]
                        except (ValueError, TypeError):
                            started = str(started)[:16]
                    table.add_row(
                        str(s.get("session_id", ""))[:12],
                        started,
                        str(s.get("turn_count", 0)),
                        entities_str,
                    )
                console.print(table)
        finally:
            await svc.disconnect()

    _run(_run_sessions())


@memory_app.command("stats")
def memory_stats(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Print high-level statistics about the memory graph."""

    async def _run_stats() -> None:
        svc = await _get_memory_service()
        try:
            from personal_agent.config import settings

            if not svc.driver:
                console.print("[red]No driver connected.[/red]")
                raise typer.Exit(1)

            async with svc.driver.session() as db:
                counts: dict[str, int] = {}
                for label in ("Turn", "Session", "Entity"):
                    r = await db.run(f"MATCH (n:{label}) RETURN count(n) as c")
                    row = await r.single()
                    counts[label.lower() + "s"] = row["c"] if row else 0

                r = await db.run(
                    "MATCH ()-[r]->() RETURN type(r) as t, count(r) as c ORDER BY c DESC LIMIT 10"
                )
                rel_data = await r.data()

                r = await db.run(
                    "MATCH (t:Turn) RETURN min(t.timestamp) as first, max(t.timestamp) as last"
                )
                date_row = await r.single()

            first_ts = date_row["first"] if date_row else None
            last_ts = date_row["last"] if date_row else None

            def _serialize_ts(ts: Any) -> str | None:
                if ts is None:
                    return None
                if hasattr(ts, "isoformat"):
                    return ts.isoformat()
                return str(ts)

            stats: dict[str, Any] = {
                "graph_nodes": counts,
                "relationship_counts": {row["t"]: row["c"] for row in rel_data},
                "date_range": {
                    "first_turn": _serialize_ts(first_ts),
                    "last_turn": _serialize_ts(last_ts),
                },
                "neo4j_uri": settings.neo4j_uri,
            }

            if json_output:
                console.print(json.dumps(stats))
            else:
                console.print("\n[bold]Memory Graph Statistics[/bold]")
                console.print(f"  Neo4j:    [cyan]{settings.neo4j_uri}[/cyan]")
                console.print(f"  Turns:    [yellow]{counts.get('turns', 0)}[/yellow]")
                console.print(f"  Sessions: [yellow]{counts.get('sessions', 0)}[/yellow]")
                console.print(f"  Entities: [yellow]{counts.get('entities', 0)}[/yellow]")
                if first_ts and last_ts:
                    first_str = first_ts[:10] if isinstance(first_ts, str) else str(first_ts)[:10]
                    last_str = last_ts[:10] if isinstance(last_ts, str) else str(last_ts)[:10]
                    console.print(f"  History:  {first_str} → {last_str}")
                if rel_data:
                    console.print("\n[bold]Relationship counts:[/bold]")
                    for row in rel_data[:8]:
                        console.print(f"  [{row['t']}]: {row['c']}")
        finally:
            await svc.disconnect()

    _run(_run_stats())
