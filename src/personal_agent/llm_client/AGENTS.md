# LLM Client

Unified LLM dispatch layer for local and cloud model calls (ADR-0141). Every
call — local (llama.cpp/MLX via the SLM tunnel) and cloud (Anthropic, OpenAI,
OVH) — dispatches through `LiteLLMClient` / `litellm.acompletion()`. There is
one client class, not one per placement.

## Responsibilities

- Single dispatch path for every provider and placement
- Egress guard on every outbound call (ADR-0132)
- Cost-gate reservation for cloud placement; skipped for local (self-hosted, free)
- Process-wide inference concurrency control (ADR-0029/ADR-0141 D3)
- Emit telemetry for all LLM calls
- Handle retries and error recovery

## Structure

```
llm_client/
├── __init__.py          # Public exports
├── factory.py            # get_llm_client() / get_llm_client_for_key() — the construction door
├── litellm_client.py      # LiteLLMClient — the one dispatch class, both placements
├── concurrency.py         # InferenceConcurrencyController + process-wide singleton
├── dspy_adapter.py        # DSPy integration for structured outputs (ADR-0010)
├── adapters.py            # Response/request adapters
├── history_sanitiser.py   # tool_call/tool_result consistency before dispatch
├── prompt_identity.py     # Prompt identity derivation (ADR-0078)
├── cost_tracker.py        # Cost/telemetry recording
├── cost_estimator.py      # Pre-call cost estimation
├── models.py              # Model/provider configuration types
├── telemetry.py           # Canonical model_call_started/completed emit helpers
└── types.py                # Response types, ModelRole, error taxonomy
```

## Usage

### Standard LLM Calls

```python
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.types import ModelRole
from personal_agent.telemetry.trace import TraceContext

client = get_llm_client(role_name="primary")  # resolves placement + endpoint from the catalog
trace_ctx = TraceContext.new_trace()

response = await client.respond(
    role=ModelRole.PRIMARY,
    messages=[{"role": "user", "content": "Analyze this task: ..."}],
    trace_ctx=trace_ctx,
)
```

Use `get_llm_client(role_name=...)` for a factory role name resolved through
the current selection + binding (`config/model_roles.yaml`). Use
`get_llm_client_for_key(model_key, budget_role=...)` for a **trusted-config**
key already resolved elsewhere (it skips the user-selection guardrail and
fails loudly on an unknown key).

### Structured Outputs with DSPy (ADR-0010)

DSPy configuration is independent of the client above — `configure_dspy_lm()`
resolves a `dspy.LM` directly from a role or model key:

```python
import dspy
from personal_agent.llm_client.dspy_adapter import configure_dspy_lm
from personal_agent.llm_client.types import ModelRole

class ExtractUser(dspy.Signature):
    """Extract user information from text."""
    text: str = dspy.InputField(desc="Text containing user information")
    name: str = dspy.OutputField(desc="User's name")
    age: int = dspy.OutputField(desc="User's age")

lm = configure_dspy_lm(role=ModelRole.PRIMARY)
dspy.configure(lm=lm)

predictor = dspy.ChainOfThought(ExtractUser)
result = predictor(text="Alice is 30 years old")

assert result.name == "Alice"
assert result.age == 30
```

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
- See `dspy_adapter.py` for configuration details
- See ADR-0010 for decision rationale (selective adoption for Captain's Log)

## Placement (ADR-0141)

Placement decides parameter shape and cost-gate applicability, not which
client class handles the call:

| | Local | Cloud |
|---|---|---|
| Dispatch | litellm's OpenAI-compatible route (`openai/{model_id}`, `api_base` set) | litellm's native provider route |
| Sampler params | catalog values flattened onto the wire via `extra_body` | provider-native kwargs |
| `max_tokens` | omit-means-unbounded if the catalog declares none (D5) | `or 8192` default applies |
| Cost gate | skipped — self-hosted, free | reserve/commit/refund (ADR-0065) |
| Concurrency | process-wide controller, GPU sub-limit | process-wide controller, cloud safety-valve ceiling |

The catalog/telemetry provider name (`slm_local`) and the litellm dispatch
prefix (`openai/`) are different things — the prefix never leaves the client.

## Egress Guard (ADR-0132 D2)

Every dispatch route is guarded: a pre-dispatch check
(`check_egress_or_raise`, raises `EgressBlockedError` directly) plus a
per-route injected hook (`AsyncOpenAI(http_client=create_guarded_http_client())`
on the OpenAI-SDK route). See `tests/personal_agent/llm_client/test_local_via_litellm.py::TestEgressGuardOnTheLocalRoute`
and `tests/test_security/test_egress_seams.py::TestLlmClientSeam` for the
seeded-negative proofs.

## Retries

```python
MAX_RETRIES = 3

for attempt in range(MAX_RETRIES):
    try:
        response = await client.respond(role=role, messages=messages, trace_ctx=trace_ctx)
        break
    except LLMClientError as e:
        if attempt < MAX_RETRIES - 1:
            wait_time = 2 ** attempt
            await asyncio.sleep(wait_time)
        else:
            raise
```

`max_retries` is also accepted directly by `respond()` — most producers pass
it explicitly rather than looping themselves.

## Telemetry

Canonical `model_call_started` / `model_call_completed` events are emitted by
the client itself (`telemetry.py`), not by callers — see
`emit_model_call_started` / `emit_model_call_completed`.

## Config

- Local endpoint: the deployment's declared `endpoint`, or the provider's
  `base_url` (`config/models.yaml`) — no default (ADR-0132 D4)
- Cloud credentials: the provider's declared `auth_env` (`config/models.yaml`),
  resolved from settings — never passed manually per call

## Dependencies

- `litellm`: unified dispatch to every provider
- `openai`: SDK used on the OpenAI-compatible route (local + `openai` cloud)
- `telemetry`: Logging
- `pydantic`: Response validation
- `dspy`: Structured outputs via signatures and modules (ADR-0010)

## Search

```bash
rg -n "get_llm_client|LiteLLMClient" src/
rg -n "LLMClientError" src/
```

## Critical

- Timeout handling — LLM calls can be slow; the role timeout is a
  **generation** budget, not a connect/write/pool one (see `litellm_client.py`)
- **Never send secrets/PII** in prompts
- Never call `litellm.acompletion()`/`litellm.completion()` outside this
  package (ast-grep tombstone, ADR-0141 AC-6) — dispatch through the factory
  or `LiteLLMClient` directly

## Testing

- `tests/personal_agent/llm_client/test_local_via_litellm.py` — local-placement
  dispatch parity, through the real litellm path (transport-level mocking only)
- `tests/personal_agent/llm_client/test_litellm_*.py` — cloud-placement dispatch,
  egress guard, cost-gate wiring
- Mock at the `litellm.acompletion` boundary, or the transport
  (`httpx.AsyncHTTPTransport.handle_async_request`) for wire-shape assertions
- Test error handling (timeout, connection refused, 5xx)
- Test retry logic

## Pre-PR

```bash
pytest tests/personal_agent/llm_client/ tests/test_llm_client/ -v
mypy src/personal_agent/llm_client/
ruff check src/personal_agent/llm_client/
```
