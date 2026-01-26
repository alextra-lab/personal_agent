# Testing

**Philosophy**: Test-first, >80% coverage for core logic, 100% for safety-critical paths.

## Structure

```
tests/
├── conftest.py          # Shared fixtures
├── test_orchestrator/   # Orchestrator tests
├── test_telemetry/      # Telemetry tests
├── test_governance/     # Governance tests
├── test_llm_client/     # LLM client tests
├── test_tools/          # Tool execution tests
├── test_brainstem/      # Mode management tests
└── integration/         # End-to-end tests
```

## Commands

```bash
pytest tests/ -v
pytest tests/ --cov=src/personal_agent --cov-report=term-missing
pytest -m "not integration"                      # Skip slow tests
pytest tests/test_orchestrator/test_executor.py::test_name -v  # Single test
```

## Naming

Pattern: `test_<component>_<scenario>_<expected>`

```python
def test_mode_manager_cpu_threshold_exceeded_transitions_to_alert():
    """Test mode transition when CPU threshold is exceeded."""
    ...
```

## Async Tests

```python
import pytest

@pytest.mark.asyncio
async def test_orchestrator_async_execution():
    orchestrator = Orchestrator(config)
    result = await orchestrator.execute(task, trace_ctx)
    assert result.success is True
```

## Fixtures

```python
# conftest.py
import pytest
from personal_agent.telemetry import TraceContext
from datetime import datetime, timezone

@pytest.fixture
def trace_ctx() -> TraceContext:
    return TraceContext(
        trace_id="test-trace-123",
        span_id="test-span-456",
        session_id="test-session-789",
        timestamp=datetime.now(timezone.utc),
    )
```

## Mocking LLM

Unit tests - mock all LLM calls:

```python
@pytest.fixture
def mock_llm_client(monkeypatch):
    from personal_agent.llm_client import LocalLLMClient

    async def mock_generate(prompt: str) -> str:
        return "Mocked LLM response"

    monkeypatch.setattr(LocalLLMClient, "generate", mock_generate)
```

Integration tests - use recorded responses:

```python
@pytest.mark.integration
async def test_full_flow(recorded_responses, monkeypatch):
    def mock_generate(prompt: str) -> str:
        return recorded_responses.get(prompt, "Default")

    monkeypatch.setattr("personal_agent.llm_client.LocalLLMClient.generate", mock_generate)
```

## Parameterized Tests

```python
@pytest.mark.parametrize(
    "cpu_percent,expected_mode",
    [(70, "NORMAL"), (85, "ALERT"), (95, "DEGRADED")],
)
def test_mode_transitions(cpu_percent, expected_mode):
    mode_mgr = ModeManager(config)
    mode_mgr.check_transition({"cpu_percent": cpu_percent})
    assert mode_mgr.current_mode.value == expected_mode
```

## Markers

```python
@pytest.mark.integration
def test_full_workflow():
    ...

@pytest.mark.requires_llm_server
def test_llm_client_real_call():
    ...
```

Run: `pytest -m "not integration"`

## Coverage

```bash
pytest tests/ --cov=src/personal_agent --cov-report=html
open htmlcov/index.html
pytest tests/ --cov-fail-under=80  # Fail if <80%
```

**Targets**: Core >80%, safety-critical 100%, UI >60%.

## Search

```bash
find tests/ -name "test_*.py"                  # All test files
rg -n "@pytest.fixture" tests/                 # Fixtures
rg -n "@pytest.mark.parametrize" tests/        # Parameterized
rg -n "@pytest.mark.asyncio" tests/            # Async tests
```

## Critical

- **Never** test implementation details - test behavior
- Each test must be independent
- **Never** hit real LLM APIs in unit tests - mock them
- **Never** skip assertions

## Pre-PR

```bash
pytest tests/ -v
pytest tests/ --cov=src/personal_agent --cov-fail-under=80
pytest tests/ --strict-markers
```
