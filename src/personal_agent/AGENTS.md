# Personal Agent Package

**Tech**: Python 3.12+, Pydantic, structlog, httpx, typer

## Commands

```bash
pytest tests/ -v
mypy src/personal_agent
ruff check src/personal_agent && ruff format src/personal_agent
python -m personal_agent.ui.cli  # When implemented
```

## Module Structure

```
src/personal_agent/
├── config/         # Configuration (see config/AGENTS.md)
├── orchestrator/   # State machine (see orchestrator/AGENTS.md)
├── telemetry/      # Logging (see telemetry/AGENTS.md)
├── governance/     # Policies (see governance/AGENTS.md)
├── llm_client/     # LLM calls (see llm_client/AGENTS.md)
├── brainstem/      # Modes (see brainstem/AGENTS.md)
├── tools/          # Tool execution (see tools/AGENTS.md)
├── ui/             # CLI (see ui/AGENTS.md)
└── exceptions.py   # Exception hierarchy
```

## File Organization

```
component/
├── __init__.py     # Public exports
├── core.py         # Main logic
├── types.py        # Pydantic models
└── helpers.py      # Internal utils
```

## Naming

- Modules: `snake_case.py`
- Classes: `PascalCase`
- Functions: `snake_case`
- Constants: `UPPER_SNAKE_CASE`

## Type Hints

Use modern syntax:

```python
def execute_tool(name: str, args: dict[str, Any], ctx: TraceContext) -> ToolResult:
    ...
```

**Never** use old `typing.Union` - use `|` instead.

Use Pydantic for data:

```python
from pydantic import BaseModel, Field

class ToolConfig(BaseModel):
    name: str
    timeout: float = Field(gt=0, default=30.0)
```

## Docstrings

Google style, mandatory for public APIs:

```python
def execute_tool(name: str, args: dict[str, Any], ctx: TraceContext) -> dict[str, Any]:
    """Execute tool with governance checks.

    Args:
        name: Tool name (must be registered)
        args: Tool-specific arguments
        ctx: Trace context

    Returns:
        {success: bool, output: Any, error: str | None}

    Raises:
        ToolNotFoundError: Tool not registered
        PermissionDeniedError: Governance denied
    """
```

## Error Handling

Project-specific exceptions:

```python
from personal_agent.exceptions import ToolExecutionError

try:
    result = execute_tool(name, args, ctx)
except ToolExecutionError as e:
    log.error("tool_failed", tool=name, error=str(e), trace_id=ctx.trace_id)
    raise
```

**Never** use bare `except:`.

## Logging

Structured only:

```python
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
log.info("tool_executed", tool=name, success=True, trace_id=ctx.trace_id)
```

**Never** use `print()` or string formatting in logs.

## Async

Use for I/O, pass TraceContext:

```python
async def execute_tool(name: str, args: dict[str, Any], ctx: TraceContext) -> ToolResult:
    log.info("tool_start", tool=name, trace_id=ctx.trace_id)
    return await tool_registry.execute(name, args)
```

For sync in async context:

```python
import asyncio
result = await asyncio.to_thread(blocking_function, data)
```

## Search

```bash
rg -n "^def \w+" src/personal_agent/              # Functions
rg -n "^class \w+" src/personal_agent/            # Classes
rg -n "^async def" src/personal_agent/            # Async functions
rg -n "class \w+\(BaseModel\)" src/personal_agent/ # Pydantic models
```

## Configuration

**Always use unified configuration manager** (ADR-0007):

```python
from personal_agent.config import settings

# All configuration access goes through settings
log_level = settings.log_level
base_url = settings.llm_base_url
timeout = settings.llm_timeout_seconds
```

**Never access environment variables directly:**
- ❌ `os.getenv("LOG_LEVEL")`
- ❌ `os.environ["LOG_LEVEL"]`
- ✅ `settings.log_level`

See `config/AGENTS.md` for detailed patterns.

## Critical

- Check mode before high-risk operations
- Always pass TraceContext through call chains
- **Never log secrets/PII** - redact first
- Use absolute imports: `from personal_agent.module import X`
- **Always use `settings` for configuration** - never `os.getenv()` or `os.environ`

## Project Context

- **Vision & Philosophy**: See `../../docs/VISION_DOC.md`
- **Directory Structure**: See `../../docs/PROJECT_DIRECTORY_STRUCTURE.md`
- **Coding Standards**: See `../../docs/CODING_STANDARDS.md` (also enforced via Cursor rules)

## Before Implementing

1. Read `../../docs/architecture/COMPONENT_NAME_SPEC_v0.X.md`
2. Check `../../docs/architecture_decisions/ADR-*.md`
3. Review component AGENTS.md
4. Understand mode interactions in `../../docs/architecture/HOMEOSTASIS_MODEL.md`

## Pre-PR

```bash
pytest tests/ --cov=src/personal_agent --cov-report=term-missing
ruff check src/personal_agent && ruff format src/personal_agent
mypy src/personal_agent
```
