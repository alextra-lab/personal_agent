# Testing Standards

**Last updated**: March 2026 (FRE-104)
**Status**: 513 passed, 1 skipped, 0 failures (fast suite)

## Commands

```bash
make test               # Fast suite — no LLM server required (~71s)
make test-integration   # Integration suite — requires live LLM server
make test-all           # Full suite
uv run pytest -v        # Verbose, all tests
```

## Two-Tier Architecture

The test suite is split into two tiers to allow CI to run quickly without an LLM server.

### Tier 1: Unit/Mock Tests (`make test`)

- **Marker**: No marker (default)
- **LLM server**: Not required
- **Duration**: ~71 seconds
- **What they test**: Logic, state machines, data models, mocked HTTP interactions
- **Rule**: Every LLM call must be intercepted by `AsyncMock` or `patch`

### Tier 2: Integration Tests (`make test-integration`)

- **Marker**: `@pytest.mark.integration` + `@pytest.mark.requires_llm_server`
- **LLM server**: Required (LM Studio on `http://127.0.0.1:1234`)
- **Duration**: Minutes (depends on model size and concurrency)
- **What they test**: Real LLM outputs, entity extraction quality, DSPy structured outputs, full orchestrator E2E

## When to Mark as Integration

Mark a test `@pytest.mark.integration` whenever it directly or indirectly calls a live model:

| Pattern | Why it's live |
|---------|---------------|
| `extract_entities_and_relationships()` | `entity_extraction_role: reasoning` in `models.yaml` → hits qwen3.6-35b |
| `Orchestrator().handle_user_request()` | Calls LLM for routing + task + triggers reflection (see below) |
| `SecondBrainConsolidator._process_capture()` | Calls entity extraction internally |
| `generate_reflection_entry()` | Creates own `LocalLLMClient` → DSPy → reasoning model |
| `predictor(...)` (DSPy) | Any DSPy predictor execution calls the model |
| `llm_client.respond(...)` | Direct HTTP call to LM Studio |

```python
@pytest.mark.integration
@pytest.mark.requires_llm_server
@pytest.mark.asyncio
async def test_entity_extraction_python():
    result = await extract_entities_and_relationships(user_msg, assistant_msg)
    assert "entities" in result
```

## The Reflection Bypass Problem

**Problem**: Patching `personal_agent.orchestrator.executor.LocalLLMClient` is NOT enough to prevent live LLM calls when testing `Orchestrator().handle_user_request()`.

**Root cause**: After every task, `executor.py` calls `generate_reflection_entry()` which is defined in `personal_agent.captains_log.reflection`. That module creates its own `LocalLLMClient()` at line 144 — completely independent of the executor's client. It then calls `ModelRole.REASONING` via DSPy to generate a structured reflection.

**The reflection path**:
```
Orchestrator.handle_user_request()
  → executor._generate_reflection_background()
    → generate_reflection_entry()          # reflection.py — creates OWN LocalLLMClient
      → generate_reflection_dspy()         # reflection_dspy.py
        → llm_client.get_dspy_lm(role=ModelRole.REASONING)
          → dspy.configure(lm=...)
            → predictor(...)               # LIVE CALL to qwen3.6-35b
```

**Solution**: `tests/test_orchestrator/conftest.py` has an `autouse` fixture that patches `generate_reflection_entry` as an `AsyncMock` for all non-integration tests in that directory.

```python
# tests/test_orchestrator/conftest.py
@pytest.fixture(autouse=True)
def mock_reflection_for_unit_tests(request):
    if request.node.get_closest_marker("integration"):
        yield
        return
    with patch(
        "personal_agent.captains_log.reflection.generate_reflection_entry",
        new_callable=AsyncMock,
    ):
        yield
```

**If you add a new test directory** that calls `Orchestrator().handle_user_request()`, you must add an equivalent `conftest.py`.

## Safe vs. Live Operations

| Operation | Live Call? | Notes |
|-----------|-----------|-------|
| `dspy.LM(...)` | No | Object creation, no network |
| `dspy.configure(lm=...)` | No | Sets global state only |
| `configure_dspy_lm(role=...)` | No | Returns LM object |
| `LocalLLMClient()` | No | Loads config/models.yaml, no network |
| `predictor(question=...)` | **YES** | Calls model endpoint |
| `llm_client.respond(...)` | **YES** | HTTP POST to LM Studio |
| `extract_entities_and_relationships()` | **YES** | Via entity_extraction_role config |
| `Orchestrator().handle_user_request()` | **YES** | LLM + reflection (see above) |

## Model Routing in Tests

`config/models.yaml` `entity_extraction_role: reasoning` routes entity extraction to the `reasoning` role, which is currently `qwen3.6-35b-a3b`. This is the large thinking model with `max_concurrency: 1`.

Tests that hit entity extraction are therefore:
1. Slow (90-180s per call)
2. Serialised (only one at a time)
3. Consuming the shared reasoning model slot

This is why entity extraction tests are integration-only.

## Mocking Patterns

### Mocking the executor LLM client

```python
@patch("personal_agent.orchestrator.executor.LocalLLMClient")
async def test_executor_behavior(self, mock_llm_class):
    mock_llm = AsyncMock()
    mock_llm_class.return_value = mock_llm
    mock_llm.respond.return_value = {
        "role": "assistant",
        "content": "mocked reply",
        "tool_calls": None,
    }
    # reflection is handled automatically by conftest.py
    orchestrator = Orchestrator()
    result = await orchestrator.handle_user_request(...)
```

### Mocking a full LLM response with tool calls

```python
mock_llm.respond.side_effect = [
    {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "read_file", "arguments": '{"path": "/tmp/x"}'}}]},
    {"role": "assistant", "content": "Done", "tool_calls": None},
]
```

### DSPy unit tests (no live call)

```python
# Safe — only creates LM object, no network call
def test_configure_dspy_lm():
    lm = configure_dspy_lm(role=ModelRole.REASONING)
    assert lm.model.startswith("openai/")

# NOT safe — predictor execution calls the model
@pytest.mark.integration
@pytest.mark.requires_llm_server
async def test_dspy_predict():
    lm = llm_client.get_dspy_lm(role=ModelRole.REASONING)
    dspy.configure(lm=lm)
    predictor = dspy.Predict(MySignature)
    result = predictor(question="...")  # live call
```

## Test File Classification Reference

| File | Tier | Why |
|------|------|-----|
| `tests/test_second_brain/test_entity_extraction.py` | Integration | Live entity extraction (reasoning model) |
| `tests/test_second_brain/test_consolidation_e2e.py` | Integration | Live consolidation pipeline |
| `tests/test_orchestrator/test_orchestrator.py` | Integration | Unguarded Orchestrator() calls |
| `tests/test_llm_client/test_integration.py` | Integration | Live LLM client calls |
| `tests/test_llm_client/test_dspy_adapter.py` | Mixed | Unit tests + marked integration tests |
| `tests/test_mcp/test_e2e.py` | Integration | Requires Docker + MCP Gateway |
| All other `tests/test_*/` | Unit | Fully mocked |
| `tests/integration/test_e2e_flows.py` | Unit | Despite location — properly mocks LLM |
| `tests/manual/` | Manual only | Run explicitly, never via pytest |

## Debugging: "Why is my test hitting the reasoning model?"

1. **Check if you're calling `Orchestrator().handle_user_request()`** — always triggers reflection unless `conftest.py` is in place
2. **Check if you're calling `extract_entities_and_relationships()`** — routes to reasoning model
3. **Check if you have a DSPy predictor call (`predictor(...)`)** — not just `configure_dspy_lm()`
4. **Check the reflection bypass** — even with executor patched, reflection creates its own client
5. **Run with `-v` and watch which test hangs** — the reasoning model is `max_concurrency: 1`, so concurrent tests will queue

## History

- **Phase 2.2 (Jan 2026)**: Initial test suite written, 86% pass rate, entity extraction and E2E consolidation tests ran live against reasoning model without integration marks
- **FRE-104 (Mar 2026)**: Investigated slow/hanging tests. Discovered `Orchestrator()` tests were running unguarded, `test_entity_extraction.py` (11 live calls) and `test_consolidation_e2e.py` unguarded. Added integration marks, added `conftest.py` reflection bypass fix. Result: 513 passed, 1 skipped, 0 failures in 71s.
