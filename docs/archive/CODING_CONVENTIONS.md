# Personal Agent

Python AI agent with biologically-inspired architecture. Pre-implementation (specs complete, no code yet).

**Stack**: Python 3.12+, Pydantic, structlog, local LLMs (Qwen via LM Studio)

## Setup

```bash
uv sync && source .venv/bin/activate
pytest tests/ --cov=src/personal_agent --cov-report=term-missing
ruff check src/ && ruff format src/ && mypy src/personal_agent
```

## Code Style

- PEP 8, line length 100
- Type hints mandatory for public APIs
- Google-style docstrings for public classes/functions
- **Never** use `print()` - use `structlog` with `trace_id`
- Async for I/O, pass `TraceContext` through call chains

## Commits & Branches

- Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`
- Feature branches: `feat/`, `fix/`, `docs/`
- Small logical commits

## Security

- **Never commit secrets** - use `.env` (gitignored)
- **Never log API keys/PII** - redact in structured logs

## Configuration Management

**Spec**: `architecture_decisions/ADR-0007-unified-configuration-management.md`

- **Always use** `from personal_agent.config import settings` for configuration
- **Never use** `os.getenv()` or `os.environ` directly - use `settings` instead
- **Never access** environment variables directly in code
- **Type-safe access**: All config values are validated and typed via Pydantic

**Configuration access pattern:**

```python
from personal_agent.config import settings

# Correct: Use settings
log_level = settings.log_level
base_url = settings.llm_base_url
timeout = settings.llm_timeout_seconds

# Wrong: Direct environment access
import os
log_level = os.getenv("LOG_LEVEL")  # ❌ Never do this
```

## Component Index

See subdirectory AGENTS.md for detailed patterns:

- `src/personal_agent/` - Package conventions
- `src/personal_agent/config/` - Configuration management (ADR-0007)
- `src/personal_agent/orchestrator/` - State machine patterns
- `src/personal_agent/telemetry/` - Logging patterns
- `src/personal_agent/governance/` - Policy enforcement
- `src/personal_agent/llm_client/` - LLM interaction
- `src/personal_agent/tools/` - Tool execution
- `src/personal_agent/brainstem/` - Mode management
- `src/personal_agent/ui/` - CLI patterns
- `tests/` - Testing strategies

## Architecture

Read these before implementing:

- `architecture/HOMEOSTASIS_MODEL.md` - Control loop architecture
- `architecture/COMPONENT_NAME_SPEC_v0.X.md` - Component design
- `architecture_decisions/ADR-*.md` - Key decisions
  - **ADR-0007**: Unified Configuration Management (required reading)
- `docs/VISION_DOC.md` - Philosophy

## Quick Find

```bash
rg -n "^def \w+" src/                           # Find function
rg -n "^class \w+" src/                         # Find class
rg -n "from personal_agent\.\w+ import" src/    # Find imports
find tests/ -name "test_*.py"                   # Find tests
```

## Non-Negotiables

- Every important behavior needs control loop: Sensor → Control Center → Effector → Feedback
- State machines are explicit, traceable, testable
- All operations log with `trace_id`
- Check current mode before high-risk operations (NORMAL/ALERT/DEGRADED/LOCKDOWN/RECOVERY)
- No cloud dependencies for core reasoning
- Explicit approval for destructive actions

## Common Patterns

**Error handling:**

```python
from personal_agent.exceptions import ToolExecutionError
if not tool_exists(name):
    raise ToolExecutionError(f"Tool '{name}' not found. Available: {list(registry.keys())}")
```

**Logging:**

```python
from personal_agent.telemetry import get_logger
log = get_logger(__name__)
log.info("tool_executed", tool=name, success=True, duration_ms=123, trace_id=ctx.trace_id)
```

**Never:**

- Bare `except:` (catches KeyboardInterrupt)
- God objects
- Global mutable state
- Magic strings (use enums)
- Operations without logging
- `os.getenv()` or `os.environ` - use `settings` from `personal_agent.config` instead

## Pre-PR Checklist

- [ ] Tests pass
- [ ] Type hints added
- [ ] Structured logging integrated
- [ ] `ruff check` clean
- [ ] `ruff format` applied
- [ ] `mypy` passes
- [ ] Specs/ADRs updated if behavior changed

## When Unsure

Check ADRs first, then ask project owner. **Never guess**.
