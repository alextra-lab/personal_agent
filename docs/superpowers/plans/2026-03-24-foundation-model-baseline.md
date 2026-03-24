# Foundation Model Baseline — Implementation Plan

> **Date**: 2026-03-24
> **Author**: Opus (Architect)
> **Executor**: Sonnet or Haiku
> **Linear Issue**: TBD
> **Purpose**: Run the same 25 evaluation CPs against Claude Sonnet to establish a capability ceiling and validate the evaluation dataset

---

## Goal

Add a **provider dispatch** layer so the orchestrator can use Claude API instead of the local SLM. Then run the evaluation harness against Sonnet to produce a foundation baseline that answers:

1. Which CPs are **dataset problems** (Sonnet fails them too)?
2. Which CPs are **model capability gaps** (Sonnet passes, Qwen fails)?
3. Which CPs are **infrastructure/latency issues** (both pass but Qwen is 10x slower)?

---

## Architecture

```
models.yaml (provider field)
       │
   ┌───▼────────────┐
   │ get_llm_client()│  new factory in llm_client/__init__.py
   └───┬─────────┬───┘
       │         │
  ┌────▼──┐  ┌──▼────────┐
  │Local  │  │Claude     │
  │Client │  │Client     │
  │       │  │.respond() │  ← NEW method
  └───┬───┘  └──┬────────┘
      │         │
  llama.cpp   Anthropic SDK
  Qwen        Sonnet/Haiku
```

No changes to the orchestrator's calling code. The factory returns a client that implements `respond() → LLMResponse`.

---

## Task Summary

| # | Task | File(s) | Model | Est. |
|---|------|---------|-------|------|
| 1 | Add `respond()` to ClaudeClient | `src/personal_agent/llm_client/claude.py` | Sonnet | 15 min |
| 2 | Add `get_llm_client()` factory | `src/personal_agent/llm_client/factory.py` (new) | Sonnet | 10 min |
| 3 | Wire factory into executor | `src/personal_agent/orchestrator/executor.py` | Sonnet | 5 min |
| 4 | Create baseline model config | `config/models-baseline.yaml` (new) | Haiku | 5 min |
| 5 | Unit tests | `tests/personal_agent/llm_client/test_claude_respond.py` (new) | Sonnet | 15 min |
| 6 | Integration smoke test | manual | Sonnet | 5 min |

---

## Task 1: Add `respond()` to ClaudeClient

**File**: `src/personal_agent/llm_client/claude.py`

Add a `respond()` method that matches `LocalLLMClient.respond()`'s signature and return type (`LLMResponse`). This method handles:

- OpenAI→Anthropic message format conversion
- OpenAI→Anthropic tool format conversion
- Anthropic→`LLMResponse` response normalization
- Cost tracking (already wired)
- Budget enforcement (already wired)

### Code

Add these imports at the top of `claude.py`:

```python
import json
from uuid import uuid4
```

Add this helper method and the `respond()` method to the `ClaudeClient` class, after `chat_completion()`:

```python
    @staticmethod
    def _convert_tools_to_anthropic(
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert OpenAI-format tool definitions to Anthropic format.

        Args:
            tools: OpenAI-format tool definitions.

        Returns:
            Anthropic-format tool definitions.
        """
        anthropic_tools: list[dict[str, Any]] = []
        for tool in tools:
            func = tool.get("function", {})
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return anthropic_tools

    @staticmethod
    def _convert_messages_to_anthropic(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI-format messages to Anthropic format.

        Extracts system message (separate parameter in Anthropic API) and
        converts tool-result messages to Anthropic's tool_result content blocks.

        Args:
            messages: OpenAI-format message list.

        Returns:
            Tuple of (system_text, anthropic_messages).
        """
        system_text: str | None = None
        anthropic_msgs: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                # Anthropic takes system as a separate parameter
                system_text = content
                continue

            if role == "tool":
                # Convert OpenAI tool result to Anthropic tool_result block
                anthropic_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": content or "",
                    }],
                })
                continue

            if role == "assistant" and msg.get("tool_calls"):
                # Convert OpenAI assistant tool_calls to Anthropic tool_use blocks
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    args_str = func.get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", str(uuid4())),
                        "name": func.get("name", ""),
                        "input": args,
                    })
                anthropic_msgs.append({"role": "assistant", "content": blocks})
                continue

            if role in ("user", "assistant"):
                anthropic_msgs.append({"role": role, "content": content or ""})

        return system_text, anthropic_msgs

    async def respond(
        self,
        role: "ModelRole",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        reasoning_effort: str | None = None,
        trace_ctx: "TraceContext | None" = None,
        previous_response_id: str | None = None,
        priority: Any = None,
        priority_timeout: float | None = None,
    ) -> "LLMResponse":
        """Make an LLM call via Anthropic API, matching LocalLLMClient interface.

        Converts OpenAI-format messages/tools to Anthropic format, calls the API,
        and returns a normalized LLMResponse.

        Args:
            role: Model role (ignored — ClaudeClient always uses self.model).
            messages: OpenAI-format message list.
            tools: Optional OpenAI-format tool definitions.
            tool_choice: Tool choice ("auto", "none", "any", or specific).
            response_format: Ignored (Anthropic uses different mechanism).
            system_prompt: Optional system prompt (prepended to messages).
            max_tokens: Max output tokens.
            temperature: Sampling temperature.
            timeout_s: Request timeout (passed to httpx via SDK).
            max_retries: Ignored (Anthropic SDK has its own retry logic).
            reasoning_effort: Ignored (Anthropic doesn't support this).
            trace_ctx: Trace context for telemetry.
            previous_response_id: Ignored (Anthropic doesn't support this).
            priority: Ignored (no local concurrency control needed).
            priority_timeout: Ignored.

        Returns:
            LLMResponse with normalized structure.
        """
        from personal_agent.llm_client.types import LLMResponse

        await self._check_weekly_budget()

        # Convert messages
        system_from_msgs, anthropic_msgs = self._convert_messages_to_anthropic(messages)
        system = system_prompt or system_from_msgs

        # Build create params
        create_params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": anthropic_msgs,
        }
        if system:
            create_params["system"] = system
        if temperature is not None:
            create_params["temperature"] = temperature
        if tools:
            create_params["tools"] = self._convert_tools_to_anthropic(tools)
            # Map tool_choice
            if tool_choice == "none":
                pass  # Anthropic doesn't have "none" — just don't send tool_choice
            elif tool_choice == "auto" or tool_choice is None:
                create_params["tool_choice"] = {"type": "auto"}
            elif tool_choice == "any":
                create_params["tool_choice"] = {"type": "any"}
            elif isinstance(tool_choice, dict) and "function" in tool_choice:
                create_params["tool_choice"] = {
                    "type": "tool",
                    "name": tool_choice["function"]["name"],
                }

        # Ensure cost tracker is connected
        if self.cost_tracker.pool is None:
            await self.cost_tracker.connect()

        trace_id = trace_ctx.trace_id if trace_ctx else None

        t0 = time.monotonic()
        response = await self.client.messages.create(**create_params)
        latency_ms = int((time.monotonic() - t0) * 1000)

        # Extract text content and tool calls
        content_text = ""
        tool_calls: list[dict[str, str]] = []
        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input),
                })

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost_usd = _estimate_cost(self.model, input_tokens, output_tokens)

        # Record cost
        await self.cost_tracker.record_api_call(
            provider="anthropic",
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            trace_id=trace_id,
            purpose="orchestrator",
            latency_ms=latency_ms,
        )

        log.info(
            "claude_respond_completed",
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            tool_calls_count=len(tool_calls),
            trace_id=trace_id,
            component="claude_client",
        )

        return LLMResponse(
            role="assistant",
            content=content_text,
            tool_calls=tool_calls,
            reasoning_trace=None,
            usage={
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            response_id=response.id,
            raw={"stop_reason": response.stop_reason},
        )
```

**Import note**: Add `from __future__ import annotations` at top of file (after docstring) so the string-quoted type hints work.

---

## Task 2: Add `get_llm_client()` Factory

**File**: `src/personal_agent/llm_client/factory.py` (NEW)

```python
"""LLM client factory — dispatches to LocalLLMClient or ClaudeClient based on provider.

The factory reads model configuration from models.yaml and returns the
appropriate client for the given role's provider field.

Usage:
    client = get_llm_client()
    response = await client.respond(role=ModelRole.STANDARD, messages=[...])
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Any

from personal_agent.llm_client.models import load_model_config

if TYPE_CHECKING:
    from personal_agent.llm_client.types import LLMResponse, ModelRole
    from personal_agent.telemetry.context import TraceContext


class LLMClient(Protocol):
    """Protocol for LLM clients (LocalLLMClient and ClaudeClient)."""

    async def respond(
        self,
        role: ModelRole,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        reasoning_effort: str | None = None,
        trace_ctx: TraceContext | None = None,
        previous_response_id: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse: ...


def get_llm_client(role_name: str = "standard") -> LLMClient:
    """Return the appropriate LLM client for a given role's provider.

    Reads the provider field from models.yaml for the specified role.
    If provider is "anthropic", returns a ClaudeClient.
    Otherwise returns a LocalLLMClient (for local/null providers).

    Args:
        role_name: The model role name to check (default: "standard").

    Returns:
        An LLM client instance matching the role's provider.
    """
    config = load_model_config()
    model_def = config.models.get(role_name)

    if model_def and model_def.provider == "anthropic":
        from personal_agent.llm_client.claude import ClaudeClient

        return ClaudeClient(
            model_id=model_def.id,
            max_tokens=model_def.max_tokens or 8192,
        )

    from personal_agent.llm_client.client import LocalLLMClient

    return LocalLLMClient()
```

**Export**: Add to `src/personal_agent/llm_client/__init__.py`:

```python
from personal_agent.llm_client.factory import LLMClient, get_llm_client
```

And add `"LLMClient"` and `"get_llm_client"` to the `__all__` list.

---

## Task 3: Wire Factory into Executor

**File**: `src/personal_agent/orchestrator/executor.py`

**Change**: Replace line 1131:

```python
# BEFORE:
llm_client = LocalLLMClient()

# AFTER:
from personal_agent.llm_client.factory import get_llm_client
llm_client = get_llm_client(role_name=model_role.value)
```

**Also**: The executor accesses `llm_client.model_configs` on line 1143. `ClaudeClient` doesn't have this attribute. Add a `model_configs` property to `ClaudeClient`:

```python
    @property
    def model_configs(self) -> dict[str, Any]:
        """Expose model configs for executor compatibility.

        Returns:
            Dict mapping role names to ModelDefinition objects.
        """
        from personal_agent.llm_client.models import load_model_config
        config = load_model_config()
        return config.models
```

---

## Task 4: Create Baseline Model Config

**File**: `config/models-baseline.yaml` (NEW)

Copy `config/models.yaml` and change the `standard` and `reasoning` roles to use Claude:

```yaml
# Foundation model baseline configuration
# Usage: AGENT_MODEL_CONFIG_PATH=config/models-baseline.yaml uv run uvicorn ...
#
# Swaps primary agent roles to Claude Sonnet for evaluation baseline.
# Entity extraction, Captain's Log, and Insights remain on claude_sonnet.

entity_extraction_role: claude_sonnet
captains_log_role: claude_sonnet
insights_role: claude_sonnet

models:
  standard:
    id: "claude-sonnet-4-6"
    provider: "anthropic"
    provider_type: "cloud"
    max_tokens: 8192
    context_length: 200000
    max_concurrency: 10
    default_timeout: 60
    tool_calling_strategy: "native"

  reasoning:
    id: "claude-sonnet-4-6"
    provider: "anthropic"
    provider_type: "cloud"
    max_tokens: 8192
    context_length: 200000
    max_concurrency: 10
    default_timeout: 120
    tool_calling_strategy: "native"

  coding:
    id: "claude-sonnet-4-6"
    provider: "anthropic"
    provider_type: "cloud"
    max_tokens: 8192
    context_length: 200000
    max_concurrency: 10
    default_timeout: 60
    tool_calling_strategy: "native"

  claude_sonnet:
    id: "claude-sonnet-4-6"
    provider: "anthropic"
    provider_type: "cloud"
    max_tokens: 8192
    context_length: 200000
    max_concurrency: 10
    default_timeout: 60
```

**Config loading**: Already fully supported. `settings.model_config_path` defaults to `config/models.yaml` and is overridable via the `AGENT_MODEL_CONFIG_PATH` env var (prefix `AGENT_` + field name). No code changes needed.

```bash
# Start agent with baseline config
AGENT_MODEL_CONFIG_PATH=config/models-baseline.yaml \
  uv run uvicorn personal_agent.service.app:app --reload --port 9000
```

---

## Task 5: Unit Tests

**File**: `tests/personal_agent/llm_client/test_claude_respond.py` (NEW)

```python
"""Unit tests for ClaudeClient.respond() method."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.claude import ClaudeClient


class TestConvertToolsToAnthropic:
    """Test OpenAI → Anthropic tool format conversion."""

    def test_basic_tool(self) -> None:
        openai_tools = [{
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "Search memory",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }]
        result = ClaudeClient._convert_tools_to_anthropic(openai_tools)
        assert len(result) == 1
        assert result[0]["name"] == "search_memory"
        assert result[0]["description"] == "Search memory"
        assert result[0]["input_schema"]["properties"]["query"]["type"] == "string"

    def test_empty_tools(self) -> None:
        assert ClaudeClient._convert_tools_to_anthropic([]) == []


class TestConvertMessagesToAnthropic:
    """Test OpenAI → Anthropic message format conversion."""

    def test_extracts_system(self) -> None:
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        system, anthropic_msgs = ClaudeClient._convert_messages_to_anthropic(msgs)
        assert system == "You are helpful."
        assert len(anthropic_msgs) == 1
        assert anthropic_msgs[0]["role"] == "user"

    def test_tool_result_conversion(self) -> None:
        msgs = [
            {"role": "tool", "tool_call_id": "tc_123", "content": "result data"},
        ]
        _, anthropic_msgs = ClaudeClient._convert_messages_to_anthropic(msgs)
        assert anthropic_msgs[0]["role"] == "user"
        block = anthropic_msgs[0]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "tc_123"

    def test_assistant_tool_calls_conversion(self) -> None:
        msgs = [{
            "role": "assistant",
            "content": "Let me search.",
            "tool_calls": [{
                "id": "tc_456",
                "function": {"name": "search", "arguments": '{"q": "test"}'},
            }],
        }]
        _, anthropic_msgs = ClaudeClient._convert_messages_to_anthropic(msgs)
        blocks = anthropic_msgs[0]["content"]
        assert blocks[0]["type"] == "text"
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["name"] == "search"
        assert blocks[1]["input"] == {"q": "test"}


class TestClaudeRespond:
    """Test the respond() method with mocked Anthropic API."""

    @pytest.fixture()
    def mock_anthropic(self) -> MagicMock:
        """Create a mock Anthropic response."""
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "Hello from Claude"

        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50

        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.usage = mock_usage
        mock_response.id = "msg_123"
        mock_response.stop_reason = "end_turn"

        return mock_response

    @pytest.mark.asyncio()
    @patch("personal_agent.llm_client.claude.get_settings")
    async def test_respond_returns_llm_response(
        self, mock_settings: MagicMock, mock_anthropic: MagicMock
    ) -> None:
        mock_settings.return_value.anthropic_api_key = "test-key"
        mock_settings.return_value.cloud_weekly_budget_usd = 100.0

        with patch.object(ClaudeClient, "_check_weekly_budget", new_callable=AsyncMock):
            client = ClaudeClient(model_id="claude-sonnet-4-6")
            client.client.messages.create = AsyncMock(return_value=mock_anthropic)
            client.cost_tracker = MagicMock()
            client.cost_tracker.pool = True  # skip connect
            client.cost_tracker.record_api_call = AsyncMock()

            from personal_agent.llm_client.types import ModelRole

            response = await client.respond(
                role=ModelRole.STANDARD,
                messages=[{"role": "user", "content": "Hello"}],
            )

            assert response["role"] == "assistant"
            assert response["content"] == "Hello from Claude"
            assert response["tool_calls"] == []
            assert response["usage"]["prompt_tokens"] == 100
            assert response["usage"]["completion_tokens"] == 50

    @pytest.mark.asyncio()
    @patch("personal_agent.llm_client.claude.get_settings")
    async def test_respond_with_tool_calls(
        self, mock_settings: MagicMock
    ) -> None:
        mock_settings.return_value.anthropic_api_key = "test-key"
        mock_settings.return_value.cloud_weekly_budget_usd = 100.0

        mock_text = MagicMock()
        mock_text.type = "text"
        mock_text.text = "Searching..."

        mock_tool = MagicMock()
        mock_tool.type = "tool_use"
        mock_tool.id = "tu_789"
        mock_tool.name = "search_memory"
        mock_tool.input = {"query": "databases"}

        mock_usage = MagicMock()
        mock_usage.input_tokens = 200
        mock_usage.output_tokens = 100

        mock_response = MagicMock()
        mock_response.content = [mock_text, mock_tool]
        mock_response.usage = mock_usage
        mock_response.id = "msg_456"
        mock_response.stop_reason = "tool_use"

        with patch.object(ClaudeClient, "_check_weekly_budget", new_callable=AsyncMock):
            client = ClaudeClient(model_id="claude-sonnet-4-6")
            client.client.messages.create = AsyncMock(return_value=mock_response)
            client.cost_tracker = MagicMock()
            client.cost_tracker.pool = True
            client.cost_tracker.record_api_call = AsyncMock()

            from personal_agent.llm_client.types import ModelRole

            response = await client.respond(
                role=ModelRole.STANDARD,
                messages=[{"role": "user", "content": "Search"}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "search_memory",
                        "description": "Search",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }],
            )

            assert len(response["tool_calls"]) == 1
            assert response["tool_calls"][0]["name"] == "search_memory"
            assert response["tool_calls"][0]["id"] == "tu_789"


class TestGetLLMClientFactory:
    """Test the factory function."""

    @patch("personal_agent.llm_client.factory.load_model_config")
    def test_returns_claude_for_anthropic_provider(self, mock_config: MagicMock) -> None:
        from personal_agent.llm_client.models import ModelDefinition
        mock_model = ModelDefinition(
            id="claude-sonnet-4-6",
            provider="anthropic",
            context_length=200000,
        )
        mock_config.return_value.models = {"standard": mock_model}

        with patch("personal_agent.llm_client.claude.get_settings") as ms:
            ms.return_value.anthropic_api_key = "test-key"
            ms.return_value.cloud_weekly_budget_usd = 100.0

            from personal_agent.llm_client.factory import get_llm_client
            client = get_llm_client(role_name="standard")
            assert type(client).__name__ == "ClaudeClient"

    @patch("personal_agent.llm_client.factory.load_model_config")
    def test_returns_local_for_null_provider(self, mock_config: MagicMock) -> None:
        from personal_agent.llm_client.models import ModelDefinition
        mock_model = ModelDefinition(
            id="qwen3.5-9b",
            provider=None,
            context_length=32768,
        )
        mock_config.return_value.models = {"standard": mock_model}

        from personal_agent.llm_client.factory import get_llm_client
        client = get_llm_client(role_name="standard")
        assert type(client).__name__ == "LocalLLMClient"
```

---

## Task 6: Integration Smoke Test

### Verify unit tests pass:

```bash
uv run pytest tests/personal_agent/llm_client/test_claude_respond.py -v
```

**Expected output**: All tests pass (6-8 tests).

### Run type checker:

```bash
uv run mypy src/personal_agent/llm_client/claude.py src/personal_agent/llm_client/factory.py
```

### Run linter:

```bash
uv run ruff check src/personal_agent/llm_client/claude.py src/personal_agent/llm_client/factory.py
uv run ruff format src/personal_agent/llm_client/claude.py src/personal_agent/llm_client/factory.py
```

### Live smoke test (manual — requires AGENT_ANTHROPIC_API_KEY):

```bash
# Start agent with baseline config
AGENT_MODEL_CONFIG_PATH=config/models-baseline.yaml \
  uv run uvicorn personal_agent.service.app:app --reload --port 9000

# Send a single message
curl -s "http://localhost:9000/chat?message=Hello&session_id=test-baseline" | jq .

# Verify Claude is being used (check agent log for "claude_respond_completed")
```

### Run evaluation baseline:

```bash
# With agent running on baseline config:
uv run python -m tests.evaluation.harness.run \
  --output-dir telemetry/evaluation/run-foundation-baseline
```

---

## Success Criteria

1. `uv run pytest tests/personal_agent/llm_client/test_claude_respond.py -v` — all pass
2. `uv run mypy src/personal_agent/llm_client/claude.py src/personal_agent/llm_client/factory.py` — clean
3. `uv run ruff check src/ && uv run ruff format --check src/` — clean
4. `AGENT_MODEL_CONFIG_PATH=config/models-baseline.yaml` agent starts without errors
5. `curl localhost:9000/chat?message=Hello&session_id=test` returns a response from Claude
6. Agent log shows `claude_respond_completed` events (not `model_call_completed` from LocalLLMClient)
7. Evaluation harness completes all 25 CPs and writes results to `telemetry/evaluation/run-foundation-baseline/`

---

## Cost Estimate

25 CPs × ~2-4 turns each × ~1000 tokens avg = ~150K-250K tokens
At Sonnet pricing ($3/Mtok input, $15/Mtok output): **~$1-3 per run**

Ensure `AGENT_CLOUD_WEEKLY_BUDGET_USD` is set high enough (≥$15 to cover baseline + entity extraction).

---

## What NOT to Change

- `LocalLLMClient` — untouched, still handles all local models
- `config/models.yaml` — untouched, still the default for local development
- Orchestrator logic — no changes to routing, expansion, or tool execution
- Telemetry — same events, same ES pipeline
