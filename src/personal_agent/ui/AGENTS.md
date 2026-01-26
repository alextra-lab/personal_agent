# UI

Command-line interface using Typer + Rich.

## Responsibilities

- CLI command structure (Typer)
- Rich terminal formatting
- Human approval workflows
- Session management (start, stop, status)

## Structure

```
ui/
├── __init__.py          # Package exports
├── cli.py               # Main Typer CLI app
└── approval.py          # Human approval workflow
```

## CLI Commands

```python
import typer
from rich.console import Console

app = typer.Typer()
console = Console()

@app.command()
def start(
    task: str = typer.Argument(..., help="Task description"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Start the agent with a task."""
    console.print(f"[bold green]Starting:[/bold green] {task}")
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
- `orchestrator`: Task execution
- `brainstem`: Mode status

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

## Entry Point

```bash
python -m personal_agent.ui.cli start "Analyze this codebase"
python -m personal_agent.ui.cli status
python -m personal_agent.ui.cli stop
```

## Pre-PR

```bash
pytest tests/test_ui/ -v
mypy src/personal_agent/ui/
ruff check src/personal_agent/ui/
```
