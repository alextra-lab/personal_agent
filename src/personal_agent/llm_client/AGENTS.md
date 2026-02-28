# LLM Client

Model abstraction layer for local LLM interactions (Qwen via LM Studio).

**Spec**: `../../docs/architecture/LOCAL_LLM_CLIENT_SPEC_v0.1.md`

## Responsibilities

- Abstract OpenAI-compatible API calls
- Handle retries and error recovery
- Emit telemetry for all LLM calls

## Structure

```
llm_client/
├── __init__.py          # Exports: LocalLLMClient
├── client.py            # LocalLLMClient class
├── dspy_adapter.py      # DSPy integration for structured outputs (ADR-0010)
├── adapters.py          # API adapters
├── models.py            # Model configuration types
└── types.py             # Response types
```

## Usage

### Standard LLM Calls

```python
from personal_agent.llm_client import LocalLLMClient
from personal_agent.llm_client.types import ModelRole

client = LocalLLMClient(base_url="http://localhost:1234/v1")
trace_ctx = TraceContext.new_trace()

response = await client.respond(
    role=ModelRole.REASONING,
    messages=[{"role": "user", "content": "Analyze this task: ..."}],
    trace_ctx=trace_ctx,
)
```

### Structured Outputs with DSPy (ADR-0010)

Use DSPy signatures and modules for structured LLM outputs:

```python
import dspy
from personal_agent.llm_client import LocalLLMClient, ModelRole

# 1. Define DSPy signature (schema)
class ExtractUser(dspy.Signature):
    """Extract user information from text."""
    text: str = dspy.InputField(desc="Text containing user information")
    name: str = dspy.OutputField(desc="User's name")
    age: int = dspy.OutputField(desc="User's age")

# 2. Configure DSPy with LocalLLMClient
client = LocalLLMClient()
lm = client.get_dspy_lm(role=ModelRole.REASONING)
dspy.configure(lm=lm)

# 3. Create predictor (use ChainOfThought for complex reasoning)
predictor = dspy.ChainOfThought(ExtractUser)

# 4. Execute and get structured output
result = predictor(text="Alice is 30 years old")

# 5. Access validated fields
assert result.name == "Alice"
assert result.age == 30
```

**Benefits of DSPy structured outputs:**

- ✅ Signature-based schema definition (cleaner than JSON prompts)
- ✅ Type-safe output fields (validated by DSPy)
- ✅ ChainOfThought adds explicit reasoning (improves quality)
- ✅ No manual JSON parsing or validation
- ✅ Works with LM Studio's OpenAI-compatible endpoint

**DSPy Module Types:**

- `dspy.Predict`: Basic prediction (fast, no reasoning trace)
- `dspy.ChainOfThought`: Adds step-by-step reasoning (recommended for Captain's Log)
- `dspy.ReAct`: Tool-augmented reasoning (NOT recommended per E-008 Test Case C)

**When to use:**

- Captain's Log reflection generation (primary use case per ADR-0010)
- Planning outputs with reasoning
- Complex structured outputs requiring explanation
- Cases where schema validation + reasoning are both needed

**Implementation Notes:**

- Based on E-008 prototype evaluation (100% reliability, ~30-40% code reduction)
- DSPy configured via `client.get_dspy_lm(role)` for consistency
- See `dspy_adapter.py` for configuration details
- See ADR-0010 for decision rationale (selective adoption for Captain's Log)

**Example: Captain's Log Reflection (Day 31-32)**

```python
import dspy
from personal_agent.llm_client import LocalLLMClient, ModelRole

class GenerateReflection(dspy.Signature):
    """Generate structured reflection on task execution."""
    user_message: str = dspy.InputField()
    steps_count: int = dspy.InputField()
    final_state: str = dspy.InputField()

    rationale: str = dspy.OutputField(desc="Analysis of execution")
    proposed_change_what: str = dspy.OutputField(desc="What to change (empty if none)")
    proposed_change_why: str = dspy.OutputField(desc="Why it helps")
    supporting_metrics: str = dspy.OutputField(desc="Comma-separated metrics")

# Configure and use
client = LocalLLMClient()
lm = client.get_dspy_lm(role=ModelRole.REASONING)
dspy.configure(lm=lm)

reflection_generator = dspy.ChainOfThought(GenerateReflection)
result = reflection_generator(
    user_message="What is Python?",
    steps_count=3,
    final_state="COMPLETED",
)

print(f"Rationale: {result.rationale}")
print(f"Change: {result.proposed_change_what}")
```

## Retries

```python
MAX_RETRIES = 3

for attempt in range(MAX_RETRIES):
    try:
        response = await client.generate(prompt, ctx=trace_ctx)
        break
    except LLMClientError as e:
        if attempt < MAX_RETRIES - 1:
            wait_time = 2 ** attempt
            await asyncio.sleep(wait_time)
        else:
            raise
```

## Telemetry

```python
log.info("llm_request", model=model_name, prompt_length=len(prompt), trace_id=ctx.trace_id)
response = await client.generate(prompt, ctx=ctx)
log.info("llm_response", response_length=len(response), duration_ms=duration, trace_id=ctx.trace_id)
```

## Config

- Base URL: `http://localhost:1234/v1` (LM Studio default)
- Model: Qwen-based (configured in LM Studio)
- **No API key needed** - local only

## Dependencies

- `httpx`: Async HTTP calls
- `telemetry`: Logging
- `pydantic`: Response validation
- `dspy`: Structured outputs via signatures and modules (ADR-0010)

## Search

```bash
rg -n "LocalLLMClient|client\.generate" src/
rg -n "LLMClientError" src/
```

## Critical

- **Local only** - no cloud fallback (local-first principle)
- Timeout handling - LLM calls can be slow, set reasonable timeouts
- **No API key needed** - LM Studio runs locally
- **Never send secrets/PII** in prompts

## Testing

- Mock httpx responses
- Test error handling (timeout, connection refused, 5xx)
- Test retry logic
- Use recorded responses for integration tests

## LM Studio Setup

```bash
# 1. Download: https://lmstudio.ai
# 2. Download Qwen model from UI
# 3. Start local server (http://localhost:1234)
# 4. Test: curl http://localhost:1234/v1/models
```

## Pre-PR

```bash
pytest tests/test_llm_client/ -v
mypy src/personal_agent/llm_client/
ruff check src/personal_agent/llm_client/
```
