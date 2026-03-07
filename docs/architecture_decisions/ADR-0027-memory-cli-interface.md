# ADR-0027: Memory CLI Interface

**Status**: Accepted  
**Date**: 2026-03-07  
**Deciders**: Project owner  

---

## Context

ADR-0025 and ADR-0026 address how the agent itself accesses memory graph data at runtime. This ADR addresses a separate, human-facing need: **the ability for the developer/user to query the memory graph directly from the terminal, without starting the full agent service or writing Cypher**.

Current options for inspecting memory data:

| Method | Friction | Requires |
|---|---|---|
| Neo4j Browser (localhost:7474) | GUI, manual Cypher | Neo4j running, browser |
| `python tests/manual/cleanup_graph_noise.py` | Script, hardcoded query | venv activation |
| `uv run agent "what have I asked about?"` | Routes through full LLM | Service running, LLM loaded |

None of these are suitable for quick, scriptable, offline inspection. The developer needs a single command like:

```bash
uv run agent memory search "greek islands"
uv run agent memory entities --type Location
uv run agent memory sessions --last 5
uv run agent memory stats
```

This aligns with the "CLIs are the agent interface" philosophy discussed in the previous session — small, composable, structured-output commands that bypass protocol overhead for direct data access.

---

## Decision

Add a `memory` sub-command group to the existing Typer CLI (`src/personal_agent/ui/service_cli.py`) that connects directly to Neo4j via `MemoryService` and outputs results in both human-readable (Rich table) and machine-parseable (`--json`) formats.

The `memory` commands do **not** require the agent service to be running. They connect directly to Neo4j using `settings.neo4j_uri`, `settings.neo4j_user`, and `settings.neo4j_password`.

---

## Implementation

### New file: `src/personal_agent/ui/memory_cli.py`

```python
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
    return asyncio.get_event_loop().run_until_complete(coro)


async def _get_memory_service():
    """Connect to MemoryService using configured Neo4j credentials."""
    from personal_agent.memory.service import MemoryService

    svc = MemoryService()
    await svc.connect()
    if not svc.connected:
        console.print("[red]Cannot connect to Neo4j. Check settings.neo4j_uri and credentials.[/red]")
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

    async def _run_search():
        svc = await _get_memory_service()
        try:
            from personal_agent.memory.models import MemoryQuery

            entity_names = [
                w.strip('",.:;!?()')
                for w in query.split()
                if len(w) > 2 and w[0].isupper()
            ]

            q = MemoryQuery(
                entity_names=entity_names or [],
                entity_types=list(entity_type),
                limit=limit,
                recency_days=days if days > 0 else None,
            )
            result = await svc.query_memory(q, query_text=query)

            if json_output:
                data = {
                    "query": query,
                    "turns": [
                        {
                            "turn_id": t.turn_id,
                            "timestamp": t.timestamp.isoformat(),
                            "session_id": t.session_id,
                            "user_message": t.user_message,
                            "summary": t.summary,
                            "key_entities": t.key_entities,
                        }
                        for t in result.conversations
                    ],
                }
                console.print_json(json.dumps(data))
            else:
                if not result.conversations:
                    console.print("[yellow]No turns matched your query.[/yellow]")
                    return

                table = Table(title=f"Memory Search: {query!r}", show_lines=True)
                table.add_column("Timestamp", style="dim", width=20)
                table.add_column("Session", style="cyan", width=10)
                table.add_column("User Message", max_width=60)
                table.add_column("Key Entities", style="green", max_width=40)

                for t in result.conversations:
                    table.add_row(
                        t.timestamp.strftime("%Y-%m-%d %H:%M"),
                        (t.session_id or "")[:8],
                        t.user_message[:200],
                        ", ".join(t.key_entities[:5]),
                    )
                console.print(table)
        finally:
            await svc.close()

    _run(_run_search())


@memory_app.command("entities")
def memory_entities(
    entity_type: list[str] = typer.Option(
        [], "--type", "-t", help="Filter by entity type (repeatable)"
    ),
    days: int = typer.Option(90, "--days", "-d", help="Only entities seen in last N days (0 = all)"),
    limit: int = typer.Option(30, "--limit", "-n", help="Max entities to show"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    sort_by: str = typer.Option("mentions", "--sort", help="Sort by: mentions | name | last_seen"),
) -> None:
    """List entities in the memory graph, sorted by frequency."""

    async def _run_entities():
        svc = await _get_memory_service()
        try:
            cutoff = ""
            if days > 0:
                cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

            sort_clause = {
                "mentions": "ORDER BY mentions DESC",
                "name": "ORDER BY e.name",
                "last_seen": "ORDER BY e.last_seen DESC",
            }.get(sort_by, "ORDER BY mentions DESC")

            type_filter = "WHERE e.entity_type IN $types " if entity_type else ""
            time_filter = " AND t.timestamp >= $cutoff " if cutoff else ""

            cypher = f"""
                MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)
                {type_filter}{'WHERE' if not type_filter and time_filter else 'AND' if time_filter else ''}
                {time_filter.replace('AND', '', 1).replace('WHERE', '', 1) if not type_filter and time_filter else time_filter}
                RETURN e.name as name, e.entity_type as type,
                       e.description as description,
                       count(t) as mentions, max(t.timestamp) as last_seen
                {sort_clause}
                LIMIT $limit
            """
            # Use the simpler direct API
            broad = await svc.query_memory_broad(
                entity_types=list(entity_type) or None,
                recency_days=days if days > 0 else 3650,
                limit=limit,
            )
            entities = broad.get("entities", [])

            if json_output:
                console.print_json(json.dumps({"entities": entities}))
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
                        (e.get("description") or "")[:80],
                    )
                console.print(table)
        finally:
            await svc.close()

    _run(_run_entities())


@memory_app.command("sessions")
def memory_sessions(
    last: int = typer.Option(10, "--last", "-n", help="Show last N sessions"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List recent conversation sessions with their dominant topics."""

    async def _run_sessions():
        svc = await _get_memory_service()
        try:
            broad = await svc.query_memory_broad(
                recency_days=3650,
                limit=100,
            )
            sessions = broad.get("sessions", [])[:last]

            if json_output:
                console.print_json(json.dumps({"sessions": sessions}))
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
                    entities = ", ".join((s.get("dominant_entities") or [])[:5])
                    started = s.get("started_at", "")
                    if started:
                        try:
                            started = datetime.fromisoformat(started).strftime("%Y-%m-%d %H:%M")
                        except ValueError:
                            pass
                    table.add_row(
                        str(s.get("session_id", ""))[:12],
                        started,
                        str(s.get("turn_count", 0)),
                        entities,
                    )
                console.print(table)
        finally:
            await svc.close()

    _run(_run_sessions())


@memory_app.command("stats")
def memory_stats(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Print high-level statistics about the memory graph."""

    async def _run_stats():
        svc = await _get_memory_service()
        try:
            from personal_agent.config import settings

            if not svc.driver:
                console.print("[red]No driver connected.[/red]")
                raise typer.Exit(1)

            async with svc.driver.session() as db:
                counts = {}
                for label in ("Turn", "Session", "Entity"):
                    r = await db.run(f"MATCH (n:{label}) RETURN count(n) as c")
                    row = await r.single()
                    counts[label.lower() + "s"] = row["c"] if row else 0

                r = await db.run("MATCH ()-[r]->() RETURN type(r) as t, count(r) as c ORDER BY c DESC LIMIT 10")
                rel_data = await r.data()

                r = await db.run(
                    "MATCH (t:Turn) RETURN min(t.timestamp) as first, max(t.timestamp) as last"
                )
                date_row = await r.single()

            stats: dict[str, Any] = {
                "graph_nodes": counts,
                "relationship_counts": {row["t"]: row["c"] for row in rel_data},
                "date_range": {
                    "first_turn": date_row["first"] if date_row else None,
                    "last_turn": date_row["last"] if date_row else None,
                },
                "neo4j_uri": settings.neo4j_uri,
            }

            if json_output:
                console.print_json(json.dumps(stats))
            else:
                console.print("\n[bold]Memory Graph Statistics[/bold]")
                console.print(f"  Neo4j:    [cyan]{settings.neo4j_uri}[/cyan]")
                console.print(f"  Turns:    [yellow]{counts['turns']}[/yellow]")
                console.print(f"  Sessions: [yellow]{counts['sessions']}[/yellow]")
                console.print(f"  Entities: [yellow]{counts['entities']}[/yellow]")

                if date_row and date_row["first"]:
                    console.print(
                        f"  History:  {date_row['first'][:10]} → {date_row['last'][:10]}"
                    )

                if rel_data:
                    console.print("\n[bold]Relationship counts:[/bold]")
                    for row in rel_data[:8]:
                        console.print(f"  [{row['t']}]: {row['c']}")
        finally:
            await svc.close()

    _run(_run_stats())
```

### Register in `src/personal_agent/ui/service_cli.py`

Add the `memory` sub-command group to the existing Typer `app`:

```python
# At the top of service_cli.py, with existing imports:
from personal_agent.ui.memory_cli import memory_app

# After existing sub-commands (e.g. after `app.add_typer(session_app, name="session")`):
app.add_typer(memory_app, name="memory")
```

### Resulting CLI surface

```
Usage: agent memory [COMMAND]

Commands:
  search    Search the memory graph by text
  entities  List entities by frequency / type
  sessions  List recent conversation sessions
  stats     High-level graph statistics

Examples:
  agent memory search "greek islands"
  agent memory search "greek islands" --type Location --json
  agent memory entities --type Location --limit 20
  agent memory entities --sort mentions
  agent memory sessions --last 7
  agent memory sessions --json | jq '.[].dominant_entities'
  agent memory stats
  agent memory stats --json
```

---

## JSON output contract

All commands support `--json` for machine-readable output. This enables shell pipelines:

```bash
# Find the 5 most mentioned locations
uv run agent memory entities --type Location --json \
  | jq '.entities | sort_by(.mentions) | reverse | .[0:5] | .[] | .name'

# Count entities per type
uv run agent memory stats --json | jq '.graph_nodes'

# Get all session IDs as a list
uv run agent memory sessions --json | jq '.sessions[].session_id'
```

---

## Relation to ADR-0025 and ADR-0026

| | ADR-0025 | ADR-0026 | ADR-0027 |
|---|---|---|---|
| Actor | Agent (automatic) | Agent (tool call) | Human developer |
| Interface | `step_init` injection | LLM tool call | Terminal command |
| Requires service? | Yes | Yes | No |
| Requires LLM? | No | No | No |
| Use case | Auto-context for recall queries | In-task memory lookup | Inspection, debugging, scripting |

---

## Design Principles (CLI Army Pattern)

Following the pattern discussed in the previous session (Phil Rentier / OpenClaw article):

1. **One command per action** — `search`, `entities`, `sessions`, `stats` are separate sub-commands, not flags.
2. **`--json` on every command** — structured output for programmatic consumption.
3. **Exit codes** — `0` on success, `1` on connection failure or no results.
4. **No service dependency** — connects directly to Neo4j. The agent service does not need to be running.
5. **Composable** — all `--json` outputs can be piped through `jq`, stored in files, or used in shell scripts.

---

## Alternatives Considered

**A. Route through the agent service API (`/memory/search`).**
Rejected: requires the service to be running, adds HTTP overhead, and the service startup involves LLM client initialisation that is irrelevant for graph queries.

**B. Separate `agent-memory` binary (standalone entry point).**
Rejected: a sub-command of the existing `agent` entry point is more discoverable (`agent memory --help`) and avoids adding a second `[project.scripts]` entry.

**C. REPL / interactive mode with Cypher pass-through.**
Out of scope for this ADR. Could be added as `agent memory repl` later.

---

## Consequences

**Positive:**
- Developer can inspect graph state in seconds from the terminal, without GUI or scripts
- `--json` output enables shell-based monitoring and testing
- No service or LLM dependency — fast startup (< 500ms)
- Adds to the CLIs-as-agent-interfaces posture endorsed by the project

**Negative:**
- Connects directly to Neo4j, bypassing the service layer — requires `settings.neo4j_uri` to be reachable from the terminal environment
- The `query_memory_broad` method (added in ADR-0025) is a dependency; both ADRs must be implemented together

---

## Acceptance Criteria

- [ ] `uv run agent memory --help` shows all 4 sub-commands
- [ ] `uv run agent memory stats` prints Turn/Session/Entity counts without the service running
- [ ] `uv run agent memory entities --type Location --json | jq '.entities[0].name'` returns a location name
- [ ] `uv run agent memory search "greek islands"` returns a Rich table of matching turns
- [ ] All commands return exit code `1` when Neo4j is unreachable and print a useful error
- [ ] `uv run agent memory sessions --json` produces valid JSON parseable by `jq`
