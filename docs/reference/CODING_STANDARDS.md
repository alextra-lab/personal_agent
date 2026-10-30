# Coding Standards for Personal Agent

> **Purpose**: Python coding conventions, patterns, and anti-patterns for the Personal Agent project
> **Audience**: AI assistants, future contributors, project owner
> **Version**: 1.1
> **Date**: 2025-12-29 (examples corrected 2026-10-30, FRE-1707)

> **About the code examples**: The examples illustrate the rules. A name that an example
> cites with a `src/` path, or imports from `personal_agent`, is a real symbol on `main`.
> Other names (for example `Planner`, `TaskGraph`, `execute_tool`) are illustrations.

---

## Philosophy

These standards serve three goals:

1. **Human readability**: Code should be clear to humans reviewing and maintaining it
2. **AI comprehension**: Patterns should be consistent for AI assistants to learn and follow
3. **System reliability**: Standards enforce safety, observability, and testability

**Quality over cleverness.** If a pattern is hard to explain, it's probably wrong for this project.

### Git workflow

- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`) — small, logical commits.
- **Branches:** `feat/`, `fix/`, `docs/` as appropriate for larger work.
- **Pre-push:** `uv run pytest`, `ruff check` / `ruff format`, `mypy` where applicable; see the "Commands" section of the root [`CLAUDE.md`](../../CLAUDE.md) for the `make` targets.

### Path privacy in docs and examples

Do not commit machine-specific filesystem paths (e.g. absolute paths under your OS account home, or home-relative directory layouts unique to one machine). Use placeholders such as `<project-root>` and `$HOME`. See [PATH_PRIVACY.md](PATH_PRIVACY.md) and run `uv run python scripts/check_no_personal_paths.py` before pushing.

---

## Python Version & Compatibility

- **Target**: Python 3.12+
- **Features allowed**: Type unions with `|`, `match/case`, structural pattern matching, PEP 695 type parameters
- **No backward compatibility**: We control the environment (the Debian VPS and the gateway container), no need to support older Python

---

## Code Style (Base: PEP 8 + Enhancements)

### Line Length
- **Maximum**: 100 characters
- **Rationale**: Balance readability with modern wide screens
- **Ruff config**: `line-length = 100` (already in `pyproject.toml`)

### Indentation
- **4 spaces** (no tabs)
- **Continuation lines**: Align with opening delimiter or use hanging indent

```python
# Good: Aligned with opening delimiter
result = some_function(arg1, arg2,
                       arg3, arg4)

# Good: Hanging indent (4 spaces)
result = some_function(
    arg1, arg2,
    arg3, arg4,
)
```

### Imports
**Order** (enforced by ruff):
1. Standard library
2. Third-party packages
3. Local application imports (absolute from `personal_agent`)
4. Type-only imports (in `if TYPE_CHECKING:` block)

```python
import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext

if TYPE_CHECKING:
    from personal_agent.orchestrator import Orchestrator
```

**Absolute imports only**: Use `from personal_agent.module import X`, not relative imports.

### String Quotes
- **Double quotes** for strings (`"hello"`)
- **Rationale**: Consistency with ruff formatter default
- **Exception**: Triple-quoted docstrings use double quotes (`"""..."""`)

### Trailing Commas
- **Use in multi-line collections**: Makes diffs cleaner

```python
# Good
tools = [
    "filesystem_read",
    "filesystem_write",
    "web_search",  # ← Trailing comma
]

# Bad (no trailing comma)
tools = [
    "filesystem_read",
    "filesystem_write",
    "web_search"
]
```

---

## Naming Conventions

### Modules and Packages
- **snake_case**: `mode_manager.py`, `trace_context.py`
- **Short and descriptive**: `executor.py` > `orchestrator_execution_engine.py`
- **No abbreviations unless universal**: `http`, `url`, `id` are OK; `mgr`, `ctx` are not

### Classes
- **PascalCase**: `ModeManager`, `TraceContext`, `LiteLLMClient`
- **Nouns or noun phrases**: Classes represent things
- **Avoid prefixes**: `ModeManager` > `CModeManager` or `ModeManagerClass`

### Functions and Methods
- **snake_case**: `check_mode_transition()`, `execute_tool()`
- **Verbs or verb phrases**: Functions do things
- **Boolean-returning functions**: Use `is_`, `has_`, `can_` prefixes

```python
def is_valid_mode(mode: str) -> bool: ...
def has_permission(tool: str, mode: OperationalMode) -> bool: ...
def can_execute_tool(tool: str, ctx: TraceContext) -> bool: ...
```

### Variables
- **snake_case**: `trace_id`, `session_state`, `llm_response`
- **Descriptive**: `user_message` > `msg`, `model_response` > `resp`
- **Avoid single letters** except loop indices (`i`, `j`, `k`) and common math variables (`x`, `y`)

### Constants
- **UPPER_SNAKE_CASE**: `DEFAULT_TIMEOUT`, `MAX_RETRIES`, `NORMAL_MODE`
- **Module-level only**: Class constants follow class naming

```python
# Module-level constant
DEFAULT_TIMEOUT = 30.0

# Enum (not a constant, uses PascalCase)
class OperationalMode(Enum):
    NORMAL = "normal"
    ALERT = "alert"
    LOCKDOWN = "lockdown"
```

### Private Members
- **Single underscore prefix**: `_internal_state`, `_calculate_score()`
- **Rationale**: Signals "internal use", not enforced by Python
- **No double underscore** unless name mangling is truly needed (rare)

---

## Type Hints (Mandatory for Public APIs)

### Coverage Requirements
- ✅ **Always type hint**: Public functions, public class methods, complex private functions
- ⚠️ **Optional**: Simple private helpers, test functions, script one-liners
- ✅ **Return types**: Always annotate, including `-> None`

### Modern Syntax (Python 3.10+)
```python
# Good: Use | for unions
def process_message(msg: str | None) -> dict[str, Any]: ...

# Bad: Old-style typing.Union
from typing import Union, Dict, Any
def process_message(msg: Union[str, None]) -> Dict[str, Any]: ...
```

### Generic Types
```python
from collections.abc import Sequence, Mapping, Iterable

# Good: Use collections.abc
def process_items(items: Sequence[str]) -> list[str]: ...
def merge_configs(configs: Mapping[str, Any]) -> dict[str, Any]: ...

# Bad: Concrete types when generics are better
def process_items(items: list[str]) -> list[str]: ...  # Too specific
```

### Pydantic Models (Preferred for Data)
Use Pydantic for structured data, not raw dicts/TypedDicts:

```python
# Good: Pydantic model
from pydantic import BaseModel, Field

class TaskConfig(BaseModel):
    max_iterations: int = Field(default=10, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)
    mode: OperationalMode

# Bad: Raw dict with TypedDict
from typing import TypedDict

class TaskConfig(TypedDict):
    max_iterations: int
    timeout_seconds: float
    mode: str
```

**Rationale**: Pydantic provides validation, serialization, and better error messages.

### Type Aliases
For complex types used multiple times:

```python
from typing import TypeAlias

# Define once
ToolName: TypeAlias = str
ToolArgs: TypeAlias = dict[str, Any]
ToolResult: TypeAlias = dict[str, Any]

# Use throughout
def execute_tool(name: ToolName, args: ToolArgs) -> ToolResult: ...
```

### `Any` and `Unknown` Types
- **Avoid `Any` when possible**: It defeats type checking
- **Use `object` for "anything"**: If you truly need to accept any type
- **Use `Any` only for**: Interfacing with untyped libraries, dynamic data (LLM responses)

```python
# Acceptable: LLM response is inherently dynamic
def parse_llm_response(response: dict[str, Any]) -> ParsedOutput: ...

# Bad: Lazy typing
def process_data(data: Any) -> Any: ...  # Too vague
```

---

## Docstrings (Google Style)

### When to Document
- ✅ **All public modules**: Top-level module docstring
- ✅ **All public classes**: Purpose, responsibilities, example
- ✅ **All public functions/methods**: Args, returns, raises, examples for complex logic
- ⚠️ **Complex private functions**: If logic is non-obvious
- ❌ **Trivial getters/setters**: Don't document the obvious

### Module Docstrings
```python
"""Orchestrator execution engine.

This module implements the deterministic state machine for task execution.
It coordinates cognitive modules (planner, critic, executor) and maintains
session state with full observability.

Key classes:
    Orchestrator: Main execution loop
    TaskGraph: Graph definition with channels and edges
    State: Session state container

Example:
    >>> from personal_agent.orchestrator import Orchestrator
    >>> orchestrator = Orchestrator(config)
    >>> result = await orchestrator.execute(task)
"""
```

### Class Docstrings
```python
class ModeManager:
    """Manages operational mode state and transitions.

    ModeManager implements the state machine for operational modes (NORMAL,
    ALERT, DEGRADED, LOCKDOWN, RECOVERY). It evaluates sensor inputs,
    applies transition rules, and emits telemetry for all mode changes.

    This is the "endocrine system" of the agent—long-term behavior regulation.

    Attributes:
        current_mode: Current operational mode
        transition_history: Recent mode transitions for analysis

    Example:
        >>> mode_mgr = ModeManager(config)
        >>> mode_mgr.check_transition(sensor_data)
        >>> if mode_mgr.current_mode == OperationalMode.ALERT:
        ...     apply_heightened_scrutiny()
    """
```

### Function/Method Docstrings
```python
# Illustration of the docstring shape. execute_tool is not a real function.
# ToolError and ToolExecutionError are real: src/personal_agent/tools/contract.py.
def execute_tool(
    name: str,
    args: dict[str, Any],
    ctx: TraceContext,
) -> dict[str, Any]:
    """Execute a tool with governance checks and sandboxing.

    Args:
        name: Tool name (must be registered in tool registry)
        args: Tool-specific arguments (validated against tool schema)
        ctx: Trace context for observability

    Returns:
        Tool execution result with standard structure:
            - success: bool
            - output: Any (tool-specific)
            - error: str | None

    Raises:
        ToolError: If the tool reports a typed failure with a code from its code set
        ToolExecutionError: If tool execution fails for another reason

    Example:
        >>> result = execute_tool(
        ...     "filesystem_read",
        ...     {"path": "/tmp/test.txt"},
        ...     trace_ctx,
        ... )
        >>> if result["success"]:
        ...     print(result["output"])
    """
```

### Inline Comments
Use sparingly, for "why" not "what":

```python
# Good: Explains why
# Retry with exponential backoff to handle transient LLM service issues
for attempt in range(MAX_RETRIES):
    try:
        return await llm_client.generate(prompt)
    except ServiceUnavailable:
        await asyncio.sleep(2 ** attempt)

# Bad: States the obvious
# Loop through attempts
for attempt in range(MAX_RETRIES):
    # Try to generate
    try:
        # Return the result
        return await llm_client.generate(prompt)
```

---

## Error Handling

### Exception Hierarchy
Define project-specific exceptions. The project has no single base class. Each exception
subclasses the built-in error that matches its meaning. The project exceptions live in many
modules. The three below show the pattern:

```python
# src/personal_agent/exceptions.py — cross-cutting errors (extract)
class MissingIdentityError(ValueError):
    """Raised when an event sink is asked to write a row without identity."""

class UnknownSessionError(ValueError):
    """Raised when a transport event is appended for a session that does not exist."""

# src/personal_agent/tools/contract.py — tool failures
class ToolExecutionError(Exception):
    """Base class of every tool failure. Raise a typed ToolError subclass instead."""

class ToolError(ToolExecutionError, Generic[CodeT]):
    """A tool failure with a code from the tool's closed code set (ADR-0156 D6.1)."""

# src/personal_agent/llm_client/types.py — LLM client failures
class LLMClientError(Exception):
    """Base exception for all LLM client errors."""

class LLMTimeout(LLMClientError):
    """Raised when an LLM request times out."""
```

### Exception Raising
```python
# Good: Specific exception with context
from personal_agent.exceptions import UnknownSessionError

if session is None:
    raise UnknownSessionError(
        f"Session '{session_id}' does not exist. "
        "No event sequence number can be issued."
    )

# Bad: Generic exception
raise Exception("Tool not found")

# Bad: Bare string
raise "Tool not found"  # SyntaxError in Python 3
```

### Exception Catching
```python
# Good: Specific exceptions (ToolError, ToolExecutionError: tools/contract.py)
try:
    result = execute_tool(name, args, ctx)
except ToolError as e:
    log.warning("tool_failed", tool=name, code=e.code, retryable=e.retryable)
    return {"success": False, "error": e.message}
except ToolExecutionError as e:
    log.error("tool_execution_failed", tool=name, error=str(e))
    return {"success": False, "error": str(e)}

# Bad: Bare except
try:
    result = execute_tool(name, args, ctx)
except:  # Catches KeyboardInterrupt, SystemExit, etc.
    return {"error": "Something went wrong"}

# Acceptable: Re-raise after logging
try:
    result = execute_tool(name, args, ctx)
except Exception as e:
    log.error("unexpected_error", error=str(e))
    raise  # Re-raise for caller to handle
```

### Fail-Safe Defaults
Errors should degrade gracefully:

```python
# Good: Fallback to safe default
def get_mode_threshold(mode: OperationalMode, metric: str) -> float:
    """Get threshold for metric in mode, with safe default."""
    try:
        return config.modes[mode].thresholds[metric]
    except KeyError:
        log.warning(
            "threshold_missing",
            mode=mode,
            metric=metric,
            fallback=DEFAULT_THRESHOLD,
        )
        return DEFAULT_THRESHOLD  # Conservative default

# Bad: Crash on missing config
def get_mode_threshold(mode: OperationalMode, metric: str) -> float:
    return config.modes[mode].thresholds[metric]  # KeyError if missing
```

---

## Logging & Observability

### Structured Logging (structlog)
Always use structured logging, never `print()`:

```python
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

# Good: Structured with context
log.info(
    "tool_executed",
    tool=name,
    args=args,
    success=result["success"],
    duration_ms=duration,
    trace_id=ctx.trace_id,
    session_id=ctx.session_id,
)

# Bad: String formatting
print(f"Tool {name} executed with {args}")

# Bad: Unstructured log
log.info(f"Tool {name} executed successfully")
```

### Log Levels
| Level | When to Use | Example |
|-------|-------------|---------|
| `DEBUG` | Detailed diagnostic info | "Entering function with args..." |
| `INFO` | Normal operation events | "Tool executed successfully" |
| `WARNING` | Unexpected but recoverable | "Threshold missing, using default" |
| `ERROR` | Errors requiring attention | "Tool execution failed" |
| `CRITICAL` | System stability threatened | "Mode transition to LOCKDOWN" |

### Trace Context
Always pass `TraceContext` through async call chains:

```python
from personal_agent.telemetry import TraceContext

async def orchestrator_step(state: State, ctx: TraceContext) -> State:
    """Execute one orchestrator step."""
    log.info("step_start", step=state.current_step, trace_id=ctx.trace_id)

    # Pass context to sub-calls
    plan = await planner.generate_plan(state, ctx)
    result = await executor.execute(plan, ctx)

    log.info("step_complete", trace_id=ctx.trace_id, result=result)
    return state.updated_with(result)
```

### Redaction
Never log secrets or PII:

```python
# Good: Redact sensitive fields
log.info(
    "llm_request",
    model=model_name,
    prompt_length=len(prompt),
    api_key="<redacted>",  # Don't log actual key
)

# Bad: Logging API key
log.info("llm_request", api_key=api_key)
```

---

## Async/Await Conventions

### When to Use Async
- ✅ **I/O-bound operations**: LLM calls, file I/O, web requests
- ✅ **Orchestrator execution**: State machine steps
- ✅ **Tool execution**: Most tools involve I/O
- ❌ **CPU-bound work**: Parsing, validation, pure computation (use sync)

### Function Naming
Async functions don't need special naming:

```python
# Good: Same name as sync version would have
async def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    ...

# Bad: Redundant "async" suffix
async def execute_tool_async(name: str, args: dict[str, Any]) -> dict[str, Any]:
    ...
```

### Async Context Managers
Prefer async context managers for resources:

```python
# Good: Async context manager
async with httpx.AsyncClient() as client:
    response = await client.get(url)

# Bad: Manual setup/teardown
client = httpx.AsyncClient()
try:
    response = await client.get(url)
finally:
    await client.aclose()
```

### Mixing Sync and Async
Use `asyncio.to_thread()` for sync functions in async context:

```python
import asyncio

# Sync function (e.g., from library)
def expensive_sync_operation(data: str) -> str:
    # CPU-intensive or blocking I/O
    return process(data)

# Call from async context
async def async_workflow(data: str) -> str:
    result = await asyncio.to_thread(expensive_sync_operation, data)
    return result
```

---

## Data Classes & Models

### Pydantic Models (Preferred)
Use Pydantic for all configuration, API payloads, and validated data:

```python
from pydantic import BaseModel, Field, field_validator
from enum import Enum

class OperationalMode(str, Enum):
    NORMAL = "normal"
    ALERT = "alert"
    DEGRADED = "degraded"
    LOCKDOWN = "lockdown"
    RECOVERY = "recovery"

class ModeConfig(BaseModel):
    """Configuration for an operational mode."""

    mode: OperationalMode
    max_parallel_tools: int = Field(ge=1, le=10)
    cpu_threshold: float = Field(ge=0.0, le=100.0)
    require_approval: list[str] = Field(default_factory=list)

    @field_validator("require_approval")
    @classmethod
    def validate_tool_names(cls, v: list[str]) -> list[str]:
        """Ensure tool names are valid."""
        # Validation logic here
        return v
```

### Dataclasses (For Simple Immutable Data)
Use standard `@dataclass` for simple, immutable data structures:

```python
# Abridged from src/personal_agent/telemetry/trace.py
from dataclasses import dataclass
from uuid import UUID

@dataclass(frozen=True)  # Immutable
class TraceContext:
    """Lightweight trace context for request correlation."""

    trace_id: str
    parent_span_id: str | None = None
    user_id: UUID | None = None
    session_id: str | None = None
    kind: str = "user"
```

**When to use what**:
- **Pydantic**: Config, API payloads, anything needing validation/serialization
- **Dataclass**: Internal data structures, simple value objects
- **NamedTuple**: Lightweight immutable tuples (rare, prefer dataclass)

---

## Configuration Management

**Spec**: `architecture_decisions/ADR-0007-unified-configuration-management.md`

### Unified Configuration Access

All configuration must be accessed through the unified `AppConfig` class. **Never access environment variables directly**.

```python
from personal_agent.config import settings

# Good: Use settings singleton
log_level = settings.log_level
base_url = settings.slm_base_url  # str | None: no default, an origin without /v1
timeout = settings.llm_timeout_seconds

# Bad: Direct environment variable access
import os
log_level = os.getenv("LOG_LEVEL")  # ❌ Never do this
log_level = os.environ.get("LOG_LEVEL", "INFO")  # ❌ Never do this
```

### Type-Safe Configuration

All configuration values are type-safe and validated via Pydantic:

```python
from personal_agent.config import settings

# Type-safe access (mypy knows the types)
timeout: int = settings.llm_timeout_seconds  # int
debug: bool = settings.debug  # bool
log_format: Literal["json", "console"] = settings.log_format  # Literal
```

### Dependency Injection Pattern

Components should accept `AppConfig` as a parameter for testability:

```python
# Illustration: LocalModelProbe is not a real class. AppConfig and settings are real.
from personal_agent.config import AppConfig, settings

class LocalModelProbe:
    def __init__(self, config: AppConfig | None = None):
        """Initialize the probe with configuration.

        Args:
            config: Configuration (defaults to settings singleton)
        """
        self.config = config or settings
        self.base_url = self.config.slm_base_url
        self.timeout = self.config.llm_timeout_seconds
```

### Configuration Precedence

Configuration is loaded in this order (later sources override earlier ones):

1. **Default values** (hardcoded in `AppConfig`)
2. **YAML configuration files** (`config/governance/*.yaml`, `config/models.yaml`)
3. **Environment variables** (from `.env` files or system environment)

**Precedence rule**: Environment variables > YAML files > Defaults

### Testing Configuration

Override configuration in tests via dependency injection:

```python
from personal_agent.config import AppConfig

def test_local_model_probe_timeout():
    # Create test configuration
    test_config = AppConfig(
        slm_base_url="http://test-server:1234",
        llm_timeout_seconds=5,
    )

    # Pass to component (LocalModelProbe: the illustration above)
    probe = LocalModelProbe(config=test_config)
    assert probe.timeout == 5
```

### Integration with Domain Configs

The unified `AppConfig` provides paths for domain-specific config loaders:

```python
from personal_agent.brainstem.mode_manager import ModeManager
from personal_agent.config import load_governance_config, settings

# AppConfig provides the path
governance_config = load_governance_config(settings.governance_config_path)

# Use both app settings and domain configs
manager = ModeManager(governance_config=governance_config)
poll_interval = settings.brainstem_sensor_poll_interval_seconds
```

### Anti-Patterns

```python
# Bad: Direct environment access
import os
api_key = os.getenv("API_KEY")  # ❌ Use settings instead

# Bad: Hardcoded configuration
base_url = "http://localhost:1234/v1"  # ❌ Use settings.slm_base_url

# Bad: Creating separate config objects
class MyConfig:
    def __init__(self):
        self.timeout = os.getenv("TIMEOUT")  # ❌ Use unified AppConfig

# Good: Use unified settings
from personal_agent.config import settings
api_key = settings.anthropic_api_key  # ✅ Type-safe, validated (one key per provider)
base_url = settings.slm_base_url  # ✅ From config
timeout = settings.llm_timeout_seconds  # ✅ Validated
```

See `src/personal_agent/config/AGENTS.md` for detailed patterns.

---

## Testing Patterns

### Test File Organization
New tests mirror `src/`: `tests/personal_agent/<module>/test_<file>.py`. The older
`tests/test_<module>/` directories still hold tests.

```
tests/
├── conftest.py              # Shared fixtures
├── personal_agent/          # New tests: mirror src/personal_agent/
│   ├── telemetry/
│   │   └── test_otel_bootstrap.py
│   └── orchestrator/
│       └── test_executor_grounding.py
├── test_telemetry/          # Older layout
│   ├── test_logger.py
│   └── test_trace.py
├── test_orchestrator/
│   ├── test_executor.py
│   └── test_session.py
└── integration/
    └── test_e2e_flows.py
```

### Test Naming
```python
# Pattern: test_<component>_<scenario>_<expected>

def test_mode_manager_cpu_threshold_exceeded_transitions_to_alert():
    """Test mode transition when CPU threshold is exceeded."""
    # Arrange
    mode_mgr = ModeManager(config)
    sensor_data = {"cpu_percent": 90.0}

    # Act
    mode_mgr.check_transition(sensor_data)

    # Assert
    assert mode_mgr.current_mode == OperationalMode.ALERT
```

### Fixtures (pytest)
```python
# conftest.py
import pytest
from personal_agent.telemetry import TraceContext

@pytest.fixture
def trace_ctx() -> TraceContext:
    """Provide test trace context."""
    return TraceContext(
        trace_id="test-trace-123",
        session_id="test-session-789",
    )

@pytest.fixture
def mock_llm_client(monkeypatch):
    """Mock LLM client for testing."""
    from personal_agent.llm_client.litellm_client import LiteLLMClient

    async def mock_respond(self, role, messages, **kwargs):
        return {"role": "assistant", "content": "Mocked LLM response"}

    monkeypatch.setattr(LiteLLMClient, "respond", mock_respond)
```

### Async Tests
```python
import pytest

@pytest.mark.asyncio
async def test_orchestrator_execute_completes_successfully(trace_ctx):
    """Test orchestrator execution completes."""
    orchestrator = Orchestrator(config)
    task = Task(goal="Test task")

    result = await orchestrator.execute(task, trace_ctx)

    assert result.success is True
    assert result.output is not None
```

### Mocking LLM Calls
```python
# Use recorded responses for integration tests
@pytest.fixture
def recorded_llm_responses():
    """Load recorded LLM responses from fixtures."""
    return {
        "plan_prompt": "Step 1: ...\nStep 2: ...",
        "critic_prompt": "The plan looks good.",
    }

async def test_planner_with_recorded_response(recorded_llm_responses, monkeypatch):
    """Test planner with recorded LLM response."""
    async def mock_respond(self, role, messages, **kwargs):
        prompt = messages[-1]["content"]
        content = recorded_llm_responses.get(prompt, "Default response")
        return {"role": "assistant", "content": content}

    monkeypatch.setattr(
        "personal_agent.llm_client.litellm_client.LiteLLMClient.respond", mock_respond
    )

    planner = Planner(llm_client)
    plan = await planner.generate_plan(task)

    assert plan.steps is not None
```

---

## Common Patterns

### Builder Pattern (for Complex Objects)
```python
from __future__ import annotations
from typing import Self

class TaskGraphBuilder:
    """Builder for TaskGraph construction."""

    def __init__(self) -> None:
        self._channels: list[str] = []
        self._edges: list[tuple[str, str]] = []

    def add_channel(self, name: str) -> Self:
        """Add a channel to the graph."""
        self._channels.append(name)
        return self

    def add_edge(self, from_ch: str, to_ch: str) -> Self:
        """Add an edge between channels."""
        self._edges.append((from_ch, to_ch))
        return self

    def build(self) -> TaskGraph:
        """Build the TaskGraph."""
        return TaskGraph(channels=self._channels, edges=self._edges)

# Usage
graph = (
    TaskGraphBuilder()
    .add_channel("planner")
    .add_channel("executor")
    .add_edge("planner", "executor")
    .build()
)
```

### Context Manager Pattern (for Resources)
```python
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

@asynccontextmanager
async def session_context(
    session_id: str,
) -> AsyncIterator[Session]:
    """Provide session with automatic cleanup."""
    session = Session(session_id)
    await session.initialize()
    try:
        yield session
    finally:
        await session.cleanup()

# Usage
async with session_context("session-123") as session:
    result = await orchestrator.execute(task, session)
```

### Factory Pattern (for Component Creation)
```python
from typing import Protocol

class ToolExecutor(Protocol):
    """Protocol for tool execution."""

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        ...

class ToolExecutorFactory:
    """Factory for creating tool executors."""

    @staticmethod
    def create(config: ExecutorConfig) -> ToolExecutor:
        """Create tool executor based on config."""
        if config.sandboxing_enabled:
            return SandboxedToolExecutor(config)
        return DirectToolExecutor(config)
```

---

## Anti-Patterns (Don't Do This)

### God Objects
```python
# Bad: Class that does everything
class Agent:
    def plan(self): ...
    def execute(self): ...
    def evaluate(self): ...
    def log(self): ...
    def check_permissions(self): ...
    def manage_mode(self): ...
    # ... 50 more methods

# Good: Single responsibility
class Planner:
    def generate_plan(self, task: Task) -> Plan: ...

class Executor:
    def execute_plan(self, plan: Plan) -> Result: ...
```

### Stringly-Typed Code
```python
# Bad: Using strings for types
def transition_mode(new_mode: str) -> None:
    if new_mode == "alert":  # Typo-prone
        ...

# Good: Use enums
from enum import Enum

class OperationalMode(Enum):
    ALERT = "alert"

def transition_mode(new_mode: OperationalMode) -> None:
    if new_mode == OperationalMode.ALERT:  # Type-safe
        ...
```

### Mutable Default Arguments
```python
# Bad: Mutable default
def add_item(item: str, items: list[str] = []) -> list[str]:
    items.append(item)
    return items

# Good: Use None and create new list
def add_item(item: str, items: list[str] | None = None) -> list[str]:
    if items is None:
        items = []
    items.append(item)
    return items

# Better: Use dataclass with factory
from dataclasses import dataclass, field

@dataclass
class Config:
    items: list[str] = field(default_factory=list)
```

### Bare `except`
```python
# Bad: Catches everything, including KeyboardInterrupt
try:
    result = risky_operation()
except:
    log.error("Failed")

# Good: Specific exceptions
try:
    result = risky_operation()
except (OperationError, ValueError) as e:
    log.error("operation_failed", error=str(e))
    raise
```

### Global Mutable State
```python
# Bad: Global mutable state
current_mode = "normal"

def transition_mode(new_mode: str):
    global current_mode
    current_mode = new_mode

# Good: State in objects
class ModeManager:
    def __init__(self):
        self._current_mode = OperationalMode.NORMAL

    def transition(self, new_mode: OperationalMode) -> None:
        self._current_mode = new_mode
```

### Direct Environment Variable Access
```python
# Bad: Direct environment variable access
import os
api_key = os.getenv("API_KEY")
log_level = os.environ.get("LOG_LEVEL", "INFO")
base_url = os.getenv("BASE_URL", "http://localhost:1234")

# Good: Use unified configuration manager
from personal_agent.config import settings
api_key = settings.anthropic_api_key
log_level = settings.log_level
base_url = settings.slm_base_url
```

**Rationale**: Direct `os.getenv()` access:
- Bypasses validation and type safety
- Makes testing difficult (hard to mock)
- Violates single source of truth principle
- No centralized precedence rules
- Creates inconsistent configuration patterns

---


## Code Review Checklist

Before submitting code, verify:

### Functionality
- [ ] Code does what it's supposed to do
- [ ] Edge cases handled
- [ ] Error cases handled gracefully

### Style & Conventions
- [ ] Follows PEP 8 + project conventions
- [ ] Naming is clear and consistent
- [ ] No magic numbers (use named constants)
- [ ] Line length ≤ 100 characters

### Type Hints & Documentation
- [ ] Public APIs have type hints
- [ ] Complex functions have type hints
- [ ] All public classes/functions have docstrings
- [ ] Docstrings follow Google style

### Testing
- [ ] Unit tests written and passing
- [ ] Integration tests for complex workflows
- [ ] Test coverage >80% for new code
- [ ] LLM calls mocked in unit tests

### Observability
- [ ] Structured logging for important events
- [ ] Trace context passed through async calls
- [ ] Errors logged with context
- [ ] No sensitive data in logs

### Security & Safety
- [ ] No hard-coded secrets
- [ ] No direct environment variable access (use `settings`)
- [ ] Input validation for external data
- [ ] Governance checks for high-risk operations
- [ ] Mode awareness for mode-sensitive behavior

### Linting & Type Checking
- [ ] `ruff check src/` passes
- [ ] `ruff format src/` applied
- [ ] `mypy src/personal_agent` passes

---

## Tools Configuration

All tooling is configured in `pyproject.toml`:

- **Ruff**: Linting + formatting (line length 100, Google docstrings)
- **Mypy**: Type checking (strict mode)
- **Pytest**: Testing (coverage >80%)

See `pyproject.toml` for full configuration.

---

## Summary: Quick Reference

| Aspect | Standard |
|--------|----------|
| **Line length** | 100 characters |
| **Indentation** | 4 spaces |
| **Quotes** | Double quotes (`"..."`) |
| **Imports** | Absolute from `personal_agent`, stdlib → 3rd party → local |
| **Naming** | `snake_case` functions/vars, `PascalCase` classes, `UPPER_SNAKE` constants |
| **Type hints** | Mandatory for public APIs, use modern syntax (`|` not `Union`) |
| **Docstrings** | Google style, all public classes/functions |
| **Logging** | Structured (structlog), always include `trace_id` |
| **Exceptions** | Project-specific hierarchy, specific catches |
| **Async** | Use for I/O-bound, pass `TraceContext` |
| **Data models** | Pydantic for config/APIs, dataclass for internal |
| **Configuration** | Always use `settings` from `personal_agent.config`, never `os.getenv()` |
| **Testing** | pytest, >80% coverage, mock LLM calls |

---

**These standards exist to make code readable, maintainable, and AI-assistant-friendly. Follow them consistently for the best results.**

---

## Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-12-29 | Initial coding standards document |
| 1.1 | 2026-10-30 | FRE-1707: code examples name real symbols on `main` (exceptions, `LiteLLMClient`, `slm_base_url`, `TraceContext`, `load_governance_config`). The rules did not change. |
