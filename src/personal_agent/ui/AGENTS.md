# UI

Command-line interfaces using Typer + Rich.

## Architecture

- **Primary CLI**: `uv run agent` (entrypoint in `pyproject.toml` → `service_cli.py`). Talks to the running Personal Agent service over HTTP. Use this for chat, session, memory.
- **Telemetry CLI**: `python -m personal_agent.ui.cli telemetry ...` for query/trace/trace-breakdown on local telemetry. No service required.

## Responsibilities

- **service_cli.py**: Chat, session, memory — HTTP client to the service.
- **cli.py**: Telemetry analysis (query events, trace reconstruction, latency breakdown).
- **approval.py**: Human approval workflows (e.g. governance).

## Structure

```
ui/
├── __init__.py          # Package exports
├── service_cli.py       # Agent CLI (chat, session, memory) — primary entrypoint
├── cli.py               # Telemetry-only (query, trace, trace-breakdown)
├── memory_cli.py        # Memory subcommand for agent
└── approval.py          # Human approval workflow
```

## Primary CLI (agent)

```bash
uv run agent chat "What is the current weather in Forcalquier?"
uv run agent chat "New topic" --new
uv run agent session
uv run agent memory --help
```

## Telemetry CLI

```bash
python -m personal_agent.ui.cli telemetry query --event=model_call_completed --last=1h
python -m personal_agent.ui.cli telemetry trace <trace_id>
python -m personal_agent.ui.cli telemetry trace-breakdown <trace_id>
```

## Rich Output

```python
from rich.console import Console
from rich.table import Table

console = Console()

# Formatted text
console.print("[bold blue]Status:[/bold blue] Running")

# Tables
table = Table(title="Session Status")
table.add_column("Field", style="cyan")
table.add_column("Value", style="green")
table.add_row("Session ID", session_id)
console.print(table)
```

## Human Approval

```python
from rich.prompt import Confirm

def request_approval(action: str, details: dict[str, Any]) -> bool:
    console.print(f"\n[yellow]Approval Required:[/yellow] {action}")
    for key, value in details.items():
        console.print(f"  {key}: {value}")
    return Confirm.ask("\nProceed?", default=False)
```

## Dependencies

- `typer`: CLI structure
- `rich`: Terminal formatting
- `service_cli`: httpx, config (service_url)
- `cli` (telemetry): telemetry module (query_events, get_trace_events, etc.)

## Search

```bash
rg -n "@app\.command" src/personal_agent/ui/
rg -n "request_approval|Confirm\.ask" src/personal_agent/ui/
rg -n "console\.print" src/personal_agent/ui/
```

## Critical

- **Never** use `print()` - use `console.print()`
- Catch exceptions, display user-friendly messages
- Return proper exit codes (0 success, 1 error)

## Testing

- Test CLI parsing with CliRunner
- Mock Confirm.ask for approval tests
- Test session lifecycle (start → status → stop)

## Entry Points

```bash
# Chat and session (service must be running)
uv run agent chat "Hello"

# Telemetry (local log analysis)
python -m personal_agent.ui.cli telemetry query --last=1h
```

## Pre-PR

```bash
pytest tests/test_ui/ -v
mypy src/personal_agent/ui/
ruff check src/personal_agent/ui/
```
