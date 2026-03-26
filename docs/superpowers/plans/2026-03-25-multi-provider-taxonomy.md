# Implementation Plan: Multi-Provider Model Taxonomy (ADR-0033 Rev 2)

**Date**: 2026-03-26
**ADR**: ADR-0033 (Multi-Provider Model Taxonomy, LiteLLM Integration & Delegation Architecture)
**Branch**: `feat/multi-provider-taxonomy`
**Estimated tasks**: 10

---

## Summary

| # | Task | Tier | Model | Files Modified | Test Command |
|---|------|------|-------|---------------|-------------|
| 1 | Add litellm dependency | T3 | Haiku | `pyproject.toml` | `uv run python -c "import litellm; print(litellm.__version__)"` |
| 2 | Clean ModelRole enum | T2 | Sonnet | `types.py` | `uv run pytest tests/personal_agent/llm_client/test_types.py -v` |
| 3 | Add min_concurrency to ModelDefinition | T2 | Sonnet | `models.py` | `uv run pytest tests/personal_agent/llm_client/test_models.py -v` |
| 4 | Restructure models.yaml | T2 | Sonnet | `models.yaml`, `models-baseline.yaml` | `uv run python -c "from personal_agent.config import load_model_config; ..."` |
| 5 | Build LiteLLMClient | T2 | Sonnet | `litellm_client.py` (new) | `uv run pytest tests/personal_agent/llm_client/test_litellm_client.py -v` |
| 6 | Update factory for two-path dispatch | T2 | Sonnet | `factory.py`, `__init__.py` | `uv run pytest tests/personal_agent/llm_client/test_factory.py -v` |
| 7 | Wire sub-agent client isolation | T2 | Sonnet | `expansion.py`, `executor.py` | `uv run pytest tests/personal_agent/orchestrator/test_expansion.py -v` |
| 8 | Update executor: PRIMARY role + remove ROUTER dead code | T2 | Sonnet | `executor.py`, `routing.py` | `uv run pytest tests/personal_agent/orchestrator/ -v` |
| 9 | Rename all call sites (src + tests) | T2 | Sonnet | ~30 files | `uv run pytest -v` |
| 10 | Delete ClaudeClient + full verification | T2 | Sonnet | `claude.py`, test files | `uv run pytest && uv run mypy src/ && uv run ruff check src/` |

---

## Task 1: Add litellm Dependency

**Command**: `uv add litellm`

This adds LiteLLM to `pyproject.toml`. LiteLLM bundles optional dependencies for each provider (Anthropic SDK, Google GenAI, etc.) and exposes a single `acompletion()` interface.

**Verify**:
```bash
uv run python -c "import litellm; print(litellm.__version__)"
```

---

## Task 2: Clean ModelRole Enum

**File**: `src/personal_agent/llm_client/types.py`

**Changes**:
- Replace all four existing members with `PRIMARY` and `SUB_AGENT`
- Update `from_str()` — simpler now (only two valid values)
- Update module docstring

```python
class ModelRole(str, Enum):
    """Model roles mapping to entries in config/models.yaml.

    Tier 1 (Primary): The orchestrator brain — reasoning, tool calling, decomposition.
    Tier 2 (Sub-Agent): Focused single-task completion — no thinking, fast inference.

    See ADR-0033 for the two-tier taxonomy rationale.
    """

    PRIMARY = "primary"          # Tier 1: orchestrator brain
    SUB_AGENT = "sub_agent"      # Tier 2: focused task completion

    @classmethod
    def from_str(cls, value: str) -> "ModelRole | None":
        """Convert string to ModelRole enum (case-insensitive).

        Args:
            value: String representation (case-insensitive).

        Returns:
            ModelRole enum or None if invalid.
        """
        value_lower = value.lower()
        for role in cls:
            if role.value == value_lower:
                return role
        return None
```

**Test**: `tests/personal_agent/llm_client/test_types.py` — rewrite to test only PRIMARY and SUB_AGENT.

```python
def test_primary_role_exists() -> None:
    assert ModelRole.PRIMARY.value == "primary"

def test_sub_agent_role_exists() -> None:
    assert ModelRole.SUB_AGENT.value == "sub_agent"

def test_from_str_primary() -> None:
    assert ModelRole.from_str("primary") == ModelRole.PRIMARY

def test_from_str_sub_agent() -> None:
    assert ModelRole.from_str("sub_agent") == ModelRole.SUB_AGENT

def test_from_str_invalid_returns_none() -> None:
    assert ModelRole.from_str("nonexistent") is None

def test_only_two_members() -> None:
    """Enum has exactly two members — no deprecated roles."""
    assert len(ModelRole) == 2
```

**Verify**: `uv run pytest tests/personal_agent/llm_client/test_types.py -v`

---

## Task 3: Add min_concurrency to ModelDefinition

**File**: `src/personal_agent/llm_client/models.py`

**Changes**:
- Add `min_concurrency` field with default 1
- Add validator: `min_concurrency <= max_concurrency`

```python
# Add after max_concurrency field:
min_concurrency: int = Field(
    default=1,
    ge=1,
    description=(
        "Floor for adaptive concurrency control (ADR-0033). "
        "Brainstem cannot reduce effective concurrency below this value. "
        "Must be <= max_concurrency."
    ),
)

# Add validator:
@model_validator(mode="after")
def _min_max_concurrency(self) -> "ModelDefinition":
    """Ensure min_concurrency does not exceed max_concurrency."""
    if self.min_concurrency > self.max_concurrency:
        raise ValueError(
            f"min_concurrency ({self.min_concurrency}) must be <= "
            f"max_concurrency ({self.max_concurrency})"
        )
    return self
```

**Test**: `tests/personal_agent/llm_client/test_models.py`

```python
def test_min_concurrency_default() -> None:
    md = ModelDefinition(id="test", context_length=4096, max_concurrency=2, default_timeout=30)
    assert md.min_concurrency == 1

def test_min_concurrency_valid() -> None:
    md = ModelDefinition(
        id="test", context_length=4096, max_concurrency=3, min_concurrency=2, default_timeout=30
    )
    assert md.min_concurrency == 2

def test_min_exceeds_max_raises() -> None:
    with pytest.raises(ValidationError, match="min_concurrency"):
        ModelDefinition(
            id="test", context_length=4096, max_concurrency=1, min_concurrency=3, default_timeout=30
        )
```

**Verify**: `uv run pytest tests/personal_agent/llm_client/test_models.py -v`

---

## Task 4: Restructure models.yaml

**File**: `config/models.yaml`

**Changes**:
- Rename `reasoning` → `primary` (orchestrator brain)
- Rename `standard` → `sub_agent` (focused task completion, tuned parameters)
- Remove `coding` entry (coding is delegation — ADR-0033 D5)
- Remove commented `router` entry (dead code)
- Add `min_concurrency` to all model entries
- Add `delegation_targets` section (schema defined, commented, not parsed yet)
- Keep `claude_sonnet`, experimental models

```yaml
# Model configuration (ADR-0031 + ADR-0033: two-tier taxonomy + LiteLLM)
#
# ── Model Taxonomy (ADR-0033) ──────────────────────────────────────
#   PRIMARY:    Orchestrator brain (reasoning, tool calling, decomposition)
#   SUB_AGENT:  Focused task completion (no thinking, fast, bounded output)
#
# ── Rules (ADR-0031) ───────────────────────────────────────────────
#   "Which model does X use?"  → This file (role assignment + model def)
#   "What is my API key?"      → .env (secret, gitignored)
#   "How much can I spend?"    → .env (operational runtime control)

# ── Process-to-model assignments ────────────────────────────────────
entity_extraction_role: claude_sonnet
captains_log_role: claude_sonnet
insights_role: claude_sonnet

models:
  # ── Tier 1: Primary Agent ───────────────────────────────────────
  primary:
    # Orchestrator brain: deep reasoning, tool calling, decomposition planning.
    # Thinking enabled with bounded budget. Single concurrent (GPU-limited).
    # Renamed from "reasoning" (ADR-0033).
    id: "unsloth/qwen3.5-35-A3B"
    context_length: 64000
    quantization: "8bit"
    min_concurrency: 1
    max_concurrency: 1
    default_timeout: 180
    temperature: 0.6
    top_p: 0.95
    top_k: 20
    thinking_budget_tokens: 3000
    supports_function_calling: true
    tool_calling_strategy: "native"
    provider_type: "local"
    endpoint: "http://localhost:8000/v1"

  # ── Tier 2: Sub-Agent ───────────────────────────────────────────
  sub_agent:
    # Focused single-task completion for HYBRID expansion sub-agents.
    # No thinking. Lower temperature for deterministic output. Bounded tokens.
    # Renamed from "standard" (ADR-0033). Parameters tuned for focused tasks.
    id: "unsloth/qwen3.5-9b"
    context_length: 32768
    quantization: "8bit"
    min_concurrency: 1
    max_concurrency: 3
    default_timeout: 90
    temperature: 0.4
    top_p: 0.8
    top_k: 20
    presence_penalty: 0.5
    max_tokens: 2048
    disable_thinking: true
    supports_function_calling: true
    tool_calling_strategy: "native"
    provider_type: "local"
    endpoint: "http://localhost:8000/v1"

  # ── Experimental / A/B Testing ──────────────────────────────────
  reasoning_heavy:
    id: "qwen3.5-35b-a3b"
    context_length: 64000
    quantization: "8bit"
    min_concurrency: 1
    max_concurrency: 1
    default_timeout: 180
    temperature: 0.6
    top_p: 0.95
    top_k: 20
    thinking_budget_tokens: 3000
    supports_function_calling: true
    provider_type: "local"
    endpoint: "http://localhost:8000/v1"

  coding_large_context:
    id: "qwen/qwen3-coder-next"
    context_length: 128000
    quantization: "4bit"
    min_concurrency: 1
    max_concurrency: 1
    default_timeout: 60
    temperature: 0.2

  # ── Cloud Models (ADR-0031) ─────────────────────────────────────
  claude_sonnet:
    id: "claude-sonnet-4-6"
    provider: "anthropic"
    provider_type: "cloud"
    max_tokens: 8192
    context_length: 200000
    min_concurrency: 1
    max_concurrency: 10
    default_timeout: 60

  # openai_o4_mini:
  #   id: "o4-mini"
  #   provider: "openai"
  #   provider_type: "cloud"
  #   max_tokens: 8192
  #   context_length: 128000
  #   min_concurrency: 1
  #   max_concurrency: 10
  #   default_timeout: 60

  # gemini_pro:
  #   id: "gemini-2.5-pro"
  #   provider: "google"
  #   provider_type: "cloud"
  #   max_tokens: 8192
  #   context_length: 1000000
  #   min_concurrency: 1
  #   max_concurrency: 10
  #   default_timeout: 120

# ── Delegation Targets (Slice 3 — schema defined, not invoked) ────
# These are AGENTS, not models. They have their own runtimes, models,
# and execution environments. Invoked via DelegationPackage, not respond().
# See ADR-0033 D5 for interface contract design.
#
# delegation_targets:
#   claude_code:
#     description: "Claude Code CLI for coding and refactoring tasks"
#     interface: "cli"
#     command: "claude"
#     auth_method: "api_key"
#     auth_env_var: "ANTHROPIC_API_KEY"
#     model: "claude-sonnet-4-6"
#     max_turns: 20
#     timeout_seconds: 600
#     cost_model: "per_token"
#     estimated_cost_per_task_usd: 0.50
#     capabilities: ["code_generation", "refactoring", "testing", "debugging", "file_editing"]
#     input_format: "text"
#     output_format: "text+diffs"
#     requires_working_directory: true
#     sandboxed: false
#
#   codex:
#     description: "OpenAI Codex for sandboxed code execution"
#     interface: "api"
#     base_url: "https://api.openai.com/v1"
#     auth_method: "api_key"
#     auth_env_var: "OPENAI_API_KEY"
#     model: "codex-mini"
#     timeout_seconds: 300
#     cost_model: "per_task"
#     capabilities: ["code_generation", "code_execution"]
#     input_format: "text+files"
#     output_format: "text+artifacts"
#     requires_working_directory: false
#     sandboxed: true
#
#   deep_research:
#     description: "Gemini for deep research with grounding"
#     interface: "api"
#     auth_method: "api_key"
#     auth_env_var: "GOOGLE_API_KEY"
#     model: "gemini-2.5-pro"
#     timeout_seconds: 900
#     cost_model: "per_token"
#     capabilities: ["research", "analysis", "long_context", "grounding"]
#     input_format: "text"
#     output_format: "text+citations"
#     requires_working_directory: false
#     sandboxed: false
```

**File**: `config/models-baseline.yaml`

Update to use `primary` and `sub_agent` keys (remove `coding`):

```yaml
# Foundation model baseline: all roles → Claude Sonnet (ADR-0033 taxonomy)
entity_extraction_role: claude_sonnet
captains_log_role: claude_sonnet
insights_role: claude_sonnet

models:
  primary:
    id: "claude-sonnet-4-6"
    provider: "anthropic"
    provider_type: "cloud"
    max_tokens: 8192
    context_length: 200000
    min_concurrency: 1
    max_concurrency: 10
    default_timeout: 60
    tool_calling_strategy: "native"

  sub_agent:
    id: "claude-sonnet-4-6"
    provider: "anthropic"
    provider_type: "cloud"
    max_tokens: 4096
    context_length: 200000
    min_concurrency: 1
    max_concurrency: 10
    default_timeout: 60

  claude_sonnet:
    id: "claude-sonnet-4-6"
    provider: "anthropic"
    provider_type: "cloud"
    max_tokens: 8192
    context_length: 200000
    min_concurrency: 1
    max_concurrency: 10
    default_timeout: 60
```

**Verify**: `uv run python -c "from personal_agent.config import load_model_config; c = load_model_config(); print(sorted(c.models.keys()))"`

Expected: `['claude_sonnet', 'coding_large_context', 'primary', 'reasoning_heavy', 'sub_agent']`

---

## Task 5: Build LiteLLMClient

**File**: `src/personal_agent/llm_client/litellm_client.py` (new)

Replaces `ClaudeClient`. Uses `litellm.acompletion()` for all cloud providers.

```python
"""LiteLLM-backed client for all cloud LLM providers.

Uses litellm.acompletion() to transparently handle message/tool format
conversion across Anthropic, OpenAI, Google, Mistral, and other providers.

Replaces the per-provider ClaudeClient/GeminiClient approach (ADR-0033).
Our wrapper adds: cost tracking, budget enforcement, telemetry, concurrency.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import litellm
import structlog

if TYPE_CHECKING:
    from personal_agent.llm_client.types import LLMResponse, ModelRole, ToolCall
    from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger()

# Suppress litellm's verbose default logging
litellm.suppress_debug_info = True


class LiteLLMClient:
    """Cloud LLM client backed by LiteLLM.

    Handles all cloud providers (Anthropic, OpenAI, Google, Mistral, etc.)
    through a single interface. LiteLLM manages message format conversion,
    tool calling translation, and provider-specific API differences.

    Our wrapper adds:
    - Cost tracking via CostTrackerService
    - Weekly budget enforcement
    - Telemetry emission (structlog)
    - Concurrency bounds from ModelDefinition

    Args:
        model_id: Provider model identifier (e.g., "claude-sonnet-4-6").
        provider: Provider name (e.g., "anthropic", "openai", "google").
        max_tokens: Default maximum output tokens.
    """

    def __init__(
        self,
        model_id: str,
        provider: str = "anthropic",
        max_tokens: int = 8192,
    ) -> None:
        self.model_id = model_id
        self.provider = provider
        self.max_tokens = max_tokens
        # LiteLLM model string: "provider/model_id"
        self._litellm_model = f"{provider}/{model_id}"

    @property
    def model_configs(self) -> dict[str, Any]:
        """Expose model configs for executor compatibility."""
        from personal_agent.config import load_model_config

        config = load_model_config()
        return config.models

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
        priority: Any = None,
        priority_timeout: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Make an LLM call via LiteLLM to any cloud provider.

        LiteLLM handles message/tool format conversion transparently.
        This method adds cost tracking, budget enforcement, and telemetry.

        Args:
            role: Model role for config lookup.
            messages: OpenAI-format messages (LiteLLM converts as needed).
            tools: OpenAI-format tool definitions (LiteLLM converts as needed).
            tool_choice: Tool selection strategy.
            response_format: Response format constraint.
            system_prompt: System prompt (prepended to messages if provided).
            max_tokens: Max output tokens override.
            temperature: Temperature override.
            timeout_s: Request timeout override.
            max_retries: Max retry count override.
            reasoning_effort: Reasoning effort hint (provider-specific).
            trace_ctx: Trace context for telemetry.
            previous_response_id: Previous response ID for conversation continuity.
            priority: Request priority (unused for cloud).
            priority_timeout: Priority timeout (unused for cloud).
            **kwargs: Additional provider-specific parameters.

        Returns:
            Normalized LLMResponse.

        Raises:
            LLMClientError: On API failure after retries.
        """
        from personal_agent.llm_client.types import LLMResponse as LLMResponseType
        from personal_agent.llm_client.types import ToolCall as ToolCallType

        effective_max_tokens = max_tokens or self.max_tokens
        trace_id = str(trace_ctx.trace_id) if trace_ctx else str(uuid4())

        # Budget enforcement
        from personal_agent.cost_tracking.service import CostTrackerService

        cost_tracker = CostTrackerService()
        if not await cost_tracker.check_budget():
            from personal_agent.llm_client.types import LLMClientError

            raise LLMClientError("Weekly cloud API budget exceeded")

        # Prepend system prompt if provided
        api_messages = list(messages)
        if system_prompt:
            api_messages = [{"role": "system", "content": system_prompt}, *api_messages]

        # Build litellm kwargs
        litellm_kwargs: dict[str, Any] = {
            "model": self._litellm_model,
            "messages": api_messages,
            "max_tokens": effective_max_tokens,
        }
        if tools:
            litellm_kwargs["tools"] = tools
        if tool_choice is not None:
            litellm_kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            litellm_kwargs["response_format"] = response_format
        if temperature is not None:
            litellm_kwargs["temperature"] = temperature
        if timeout_s is not None:
            litellm_kwargs["timeout"] = timeout_s
        if max_retries is not None:
            litellm_kwargs["num_retries"] = max_retries

        start_time = time.monotonic()
        log.info(
            "litellm_request_start",
            model=self._litellm_model,
            trace_id=trace_id,
            max_tokens=effective_max_tokens,
        )

        response = await litellm.acompletion(**litellm_kwargs)

        elapsed = time.monotonic() - start_time

        # Extract response data (litellm returns OpenAI-format ModelResponse)
        choice = response.choices[0]
        message = choice.message
        content = message.content or ""

        # Parse tool calls
        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(
                    ToolCallType(
                        id=tc.id or str(uuid4()),
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    )
                )

        # Usage
        usage: dict[str, Any] = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        # Cost tracking
        cost = litellm.completion_cost(completion_response=response)
        if cost > 0:
            await cost_tracker.record_cost(
                model=self._litellm_model,
                cost_usd=cost,
                tokens=usage.get("total_tokens", 0),
            )

        response_id = response.id if hasattr(response, "id") else None

        log.info(
            "litellm_request_complete",
            model=self._litellm_model,
            trace_id=trace_id,
            elapsed_s=round(elapsed, 2),
            tokens=usage.get("total_tokens"),
            cost_usd=round(cost, 6) if cost else None,
            tool_calls=len(tool_calls),
        )

        return LLMResponseType(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            reasoning_trace=None,
            usage=usage,
            response_id=response_id,
            raw=response.model_dump() if hasattr(response, "model_dump") else {},
        )
```

**Test**: `tests/personal_agent/llm_client/test_litellm_client.py` (new)

```python
"""Tests for LiteLLMClient (ADR-0033)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.types import ModelRole


class TestLiteLLMClientInit:
    def test_constructor_sets_model(self) -> None:
        client = LiteLLMClient(model_id="claude-sonnet-4-6", provider="anthropic")
        assert client.model_id == "claude-sonnet-4-6"
        assert client._litellm_model == "anthropic/claude-sonnet-4-6"

    def test_constructor_openai_provider(self) -> None:
        client = LiteLLMClient(model_id="o4-mini", provider="openai")
        assert client._litellm_model == "openai/o4-mini"

    def test_constructor_google_provider(self) -> None:
        client = LiteLLMClient(model_id="gemini-2.5-pro", provider="google")
        assert client._litellm_model == "google/gemini-2.5-pro"


class TestLiteLLMClientRespond:
    @pytest.mark.asyncio
    async def test_respond_basic(self) -> None:
        """Basic completion returns normalized LLMResponse."""
        client = LiteLLMClient(model_id="claude-sonnet-4-6", provider="anthropic")

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Hello!"
        mock_response.choices[0].message.tool_calls = None
        mock_response.usage = MagicMock(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )
        mock_response.id = "resp_123"
        mock_response.model_dump.return_value = {}

        with (
            patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response),
            patch("litellm.completion_cost", return_value=0.001),
            patch(
                "personal_agent.cost_tracking.service.CostTrackerService.check_budget",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "personal_agent.cost_tracking.service.CostTrackerService.record_cost",
                new_callable=AsyncMock,
            ),
        ):
            result = await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "Hi"}],
            )

        assert result["content"] == "Hello!"
        assert result["role"] == "assistant"
        assert result["usage"]["total_tokens"] == 15

    @pytest.mark.asyncio
    async def test_respond_with_tool_calls(self) -> None:
        """Tool calls are parsed from litellm response."""
        client = LiteLLMClient(model_id="claude-sonnet-4-6", provider="anthropic")

        mock_tc = MagicMock()
        mock_tc.id = "call_abc"
        mock_tc.function.name = "search"
        mock_tc.function.arguments = '{"query": "test"}'

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = ""
        mock_response.choices[0].message.tool_calls = [mock_tc]
        mock_response.usage = MagicMock(
            prompt_tokens=20, completion_tokens=10, total_tokens=30
        )
        mock_response.id = "resp_456"
        mock_response.model_dump.return_value = {}

        with (
            patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response),
            patch("litellm.completion_cost", return_value=0.002),
            patch(
                "personal_agent.cost_tracking.service.CostTrackerService.check_budget",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "personal_agent.cost_tracking.service.CostTrackerService.record_cost",
                new_callable=AsyncMock,
            ),
        ):
            result = await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "search for test"}],
                tools=[{"type": "function", "function": {"name": "search"}}],
            )

        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "search"

    @pytest.mark.asyncio
    async def test_respond_budget_exceeded_raises(self) -> None:
        """Budget exceeded raises LLMClientError."""
        client = LiteLLMClient(model_id="claude-sonnet-4-6", provider="anthropic")

        with patch(
            "personal_agent.cost_tracking.service.CostTrackerService.check_budget",
            new_callable=AsyncMock,
            return_value=False,
        ):
            from personal_agent.llm_client.types import LLMClientError

            with pytest.raises(LLMClientError, match="budget exceeded"):
                await client.respond(
                    role=ModelRole.PRIMARY,
                    messages=[{"role": "user", "content": "Hi"}],
                )
```

**Verify**: `uv run pytest tests/personal_agent/llm_client/test_litellm_client.py -v`

---

## Task 6: Update Factory for Two-Path Dispatch

**File**: `src/personal_agent/llm_client/factory.py`

**Changes**:
- Dispatch on `provider_type`: `"local"` → `LocalLLMClient`, else → `LiteLLMClient`
- Remove Anthropic-specific import of `ClaudeClient`
- Default role changes from `"standard"` to `"primary"`
- Update `LLMClient` Protocol docstring

```python
def get_llm_client(role_name: str = "primary") -> Any:
    """Return appropriate LLM client based on provider_type in models.yaml.

    Dispatch logic (ADR-0033):
        provider_type == "local"  →  LocalLLMClient (GPU concurrency, thinking budget)
        provider_type == "cloud"  →  LiteLLMClient  (litellm.acompletion(), all providers)

    Args:
        role_name: Model role key in models.yaml (e.g., "primary", "sub_agent").

    Returns:
        LLM client implementing the LLMClient protocol.
    """
    config = load_model_config()
    model_def = config.models.get(role_name)

    if model_def is None:
        log.warning("model_role_not_found", role=role_name, fallback="LocalLLMClient")
        from personal_agent.llm_client.client import LocalLLMClient
        return LocalLLMClient()

    if model_def.provider_type == "local" or model_def.provider_type is None:
        from personal_agent.llm_client.client import LocalLLMClient
        return LocalLLMClient()

    # Cloud provider — use LiteLLM
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    return LiteLLMClient(
        model_id=model_def.id,
        provider=model_def.provider or "openai",
        max_tokens=model_def.max_tokens or 8192,
    )
```

**Test**: `tests/personal_agent/llm_client/test_factory.py` (new, consolidating factory tests)

```python
"""Tests for get_llm_client factory (ADR-0033)."""

from unittest.mock import MagicMock, patch

from personal_agent.llm_client.factory import get_llm_client


class TestGetLLMClientFactory:
    def _mock_config(self, models: dict) -> MagicMock:
        config = MagicMock()
        config.models = models
        return config

    def test_local_provider_returns_local_client(self) -> None:
        model = MagicMock(provider_type="local", provider=None)
        config = self._mock_config({"primary": model})
        with patch("personal_agent.llm_client.factory.load_model_config", return_value=config):
            client = get_llm_client("primary")
        from personal_agent.llm_client.client import LocalLLMClient
        assert isinstance(client, LocalLLMClient)

    def test_cloud_anthropic_returns_litellm(self) -> None:
        model = MagicMock(provider_type="cloud", provider="anthropic", id="claude-sonnet-4-6", max_tokens=8192)
        config = self._mock_config({"claude_sonnet": model})
        with patch("personal_agent.llm_client.factory.load_model_config", return_value=config):
            client = get_llm_client("claude_sonnet")
        from personal_agent.llm_client.litellm_client import LiteLLMClient
        assert isinstance(client, LiteLLMClient)
        assert client._litellm_model == "anthropic/claude-sonnet-4-6"

    def test_cloud_google_returns_litellm(self) -> None:
        model = MagicMock(provider_type="cloud", provider="google", id="gemini-2.5-pro", max_tokens=8192)
        config = self._mock_config({"gemini": model})
        with patch("personal_agent.llm_client.factory.load_model_config", return_value=config):
            client = get_llm_client("gemini")
        from personal_agent.llm_client.litellm_client import LiteLLMClient
        assert isinstance(client, LiteLLMClient)
        assert client._litellm_model == "google/gemini-2.5-pro"

    def test_unknown_role_falls_back_to_local(self) -> None:
        config = self._mock_config({})
        with patch("personal_agent.llm_client.factory.load_model_config", return_value=config):
            client = get_llm_client("nonexistent")
        from personal_agent.llm_client.client import LocalLLMClient
        assert isinstance(client, LocalLLMClient)

    def test_none_provider_type_returns_local(self) -> None:
        model = MagicMock(provider_type=None, provider=None)
        config = self._mock_config({"primary": model})
        with patch("personal_agent.llm_client.factory.load_model_config", return_value=config):
            client = get_llm_client("primary")
        from personal_agent.llm_client.client import LocalLLMClient
        assert isinstance(client, LocalLLMClient)
```

**Verify**: `uv run pytest tests/personal_agent/llm_client/test_factory.py -v`

---

## Task 7: Wire Sub-Agent Client Isolation

**File**: `src/personal_agent/orchestrator/expansion.py`

**Changes**:
- `execute_hybrid()` creates its own client via factory instead of accepting one as a parameter
- `parse_decomposition_plan()` assigns `ModelRole.SUB_AGENT` to all specs

```python
# In execute_hybrid() — remove llm_client parameter:
async def execute_hybrid(
    specs: Sequence[SubAgentSpec],
    trace_id: str,
    max_concurrent: int | None = None,
) -> list[SubAgentResult]:
    """Execute sub-agents concurrently within expansion budget.

    Creates a dedicated sub-agent LLM client via factory (ADR-0033: client isolation).
    Sub-agents always use the sub_agent model config, never the primary's client.
    """
    from personal_agent.llm_client.factory import get_llm_client

    sub_agent_client = get_llm_client(role_name="sub_agent")
    # ... rest uses sub_agent_client
```

**File**: `src/personal_agent/orchestrator/executor.py`

Remove `llm_client` from `execute_hybrid()` call site.

**File**: `src/personal_agent/orchestrator/sub_agent_types.py`

Update default: `model_role: ModelRole = ModelRole.SUB_AGENT`

**Test**: `tests/personal_agent/orchestrator/test_expansion.py`

```python
@pytest.mark.asyncio
async def test_execute_hybrid_uses_sub_agent_client(mock_factory):
    """execute_hybrid creates its own client via factory for sub_agent role."""
    mock_factory.assert_called_once_with(role_name="sub_agent")

def test_sub_agent_spec_default_role():
    """SubAgentSpec defaults to ModelRole.SUB_AGENT."""
    spec = SubAgentSpec(task="test")
    assert spec.model_role == ModelRole.SUB_AGENT
```

**Verify**: `uv run pytest tests/personal_agent/orchestrator/test_expansion.py -v`

---

## Task 8: Update Executor — PRIMARY Role + Remove ROUTER Dead Code

**File**: `src/personal_agent/orchestrator/executor.py`

This is the largest single task. Two changes:

### 8a: Remove ROUTER dead code

All `if model_role == ModelRole.ROUTER:` branches are dead since the gateway handles intent classification. Remove:
- Router-specific system prompt construction
- Router-specific tool filtering
- Router response parsing → re-routing logic
- Router timeout special-casing
- Router fallback logic

The executor becomes simpler: it always receives a gateway-classified request and calls the PRIMARY model directly.

### 8b: Replace ModelRole references

```python
# _determine_initial_model_role():
def _determine_initial_model_role(ctx: ExecutionContext) -> ModelRole:
    # CODE_TASK: primary agent decides whether to handle or delegate (Slice 3)
    # All channels route to PRIMARY — the gateway already classified intent
    return ModelRole.PRIMARY

# step_init (line ~1052):
model_role = ModelRole.PRIMARY  # was: ModelRole.REASONING

# step_select_model_role (line ~1935):
ctx.selected_model_role = last_llm_role or ModelRole.PRIMARY  # was: ModelRole.REASONING
```

**File**: `src/personal_agent/orchestrator/routing.py`

Simplify `resolve_role()` — with only PRIMARY and SUB_AGENT, routing is straightforward:

```python
def resolve_role(requested_role: ModelRole) -> ModelRole:
    """Map requested role to actual runtime role.

    With the two-tier taxonomy (ADR-0033), this is simple:
    PRIMARY stays PRIMARY unless reasoning is disabled (falls back to SUB_AGENT).
    """
    if requested_role == ModelRole.PRIMARY:
        if not getattr(settings, "enable_reasoning_role", True):
            return ModelRole.SUB_AGENT
        return ModelRole.PRIMARY
    return requested_role
```

Update routing rules table: `"target_model"` values change from `ModelRole.STANDARD`/`CODING`/`REASONING` to `ModelRole.PRIMARY`/`ModelRole.SUB_AGENT` based on task complexity.

**Verify**: `uv run pytest tests/personal_agent/orchestrator/ -v`

---

## Task 9: Rename All Remaining Call Sites

Mechanical find-and-replace across src/ and tests/. The mapping:

| Old | New | Count (approx) |
|-----|-----|-----------------|
| `ModelRole.REASONING` | `ModelRole.PRIMARY` | ~25 in src, ~30 in tests |
| `ModelRole.STANDARD` | `ModelRole.SUB_AGENT` | ~10 in src, ~15 in tests |
| `ModelRole.ROUTER` | (deleted — code paths removed in Task 8) | ~20 in src, ~25 in tests |
| `ModelRole.CODING` | `ModelRole.PRIMARY` | ~3 in src, ~8 in tests |
| `"standard"` (in factory default) | `"primary"` | ~2 |
| `"reasoning"` (in config lookups) | `"primary"` | ~5 |

**Files with most changes** (from grep results):
- `src/personal_agent/orchestrator/executor.py` — ~20 references (mostly ROUTER removal)
- `src/personal_agent/orchestrator/routing.py` — ~10 references
- `src/personal_agent/llm_client/client.py` — timeout defaults dict
- `src/personal_agent/captains_log/reflection.py` — REASONING → PRIMARY
- `src/personal_agent/second_brain/entity_extraction.py` — REASONING → PRIMARY
- `src/personal_agent/llm_client/dspy_adapter.py` — REASONING → PRIMARY
- `tests/test_llm_client/test_client.py` — ~20 ROUTER references
- `tests/test_orchestrator/test_routing.py` — ~10 references
- `tests/test_orchestrator/test_routing_delegation.py` — ~15 references

**Approach**: Use targeted `Edit` operations, not blind find-replace. Each file is read, context verified, then edited. Tests are updated to match new role names.

**Verify**: `uv run pytest -v` (full suite)

---

## Task 10: Delete ClaudeClient + Full Verification

**File deletions**:
- `src/personal_agent/llm_client/claude.py` — replaced by `LiteLLMClient`
- `tests/personal_agent/llm_client/test_claude_respond.py` — replaced by `test_litellm_client.py`

**File updates**:
- `src/personal_agent/llm_client/__init__.py` — remove `ClaudeClient` from `__all__` and `__getattr__`; add `LiteLLMClient`

**Full verification suite**:

```bash
# All tests pass
uv run pytest

# Type checking clean
uv run mypy src/

# Linting clean
uv run ruff check src/

# Formatting clean
uv run ruff format --check src/

# Config loads correctly
uv run python -c "
from personal_agent.config import load_model_config
c = load_model_config()
print('Models:', sorted(c.models.keys()))
print('Primary ID:', c.models['primary'].id)
print('Sub-agent ID:', c.models['sub_agent'].id)
print('Min concurrency (primary):', c.models['primary'].min_concurrency)
print('Max concurrency (sub_agent):', c.models['sub_agent'].max_concurrency)
"
```

Expected output:
```
Models: ['claude_sonnet', 'coding_large_context', 'primary', 'reasoning_heavy', 'sub_agent']
Primary ID: unsloth/qwen3.5-35-A3B
Sub-agent ID: unsloth/qwen3.5-9b
Min concurrency (primary): 1
Max concurrency (sub_agent): 3
```

---

## Deferred to Slice 3

| Item | What's Ready Now | What's Deferred |
|------|-----------------|-----------------|
| **Adaptive concurrency** | `min_concurrency` / `max_concurrency` bounds on all models | Brainstem feedback loop adjusting within bounds |
| **Delegation orchestrator** | `delegation_targets` schema in models.yaml, `DelegationPackage`/`DelegationOutcome` data structures | `DelegationOrchestrator` that invokes targets per interface contract |
| **Claude Code delegation** | Schema: interface=cli, command=claude, capabilities, I/O format | CLI invocation, result parsing, cost tracking |
| **DelegationTarget model** | YAML schema (commented) | Pydantic model to parse and validate |

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Large diff (~170 call sites) introduces bugs | Medium | Medium | Full test suite + mypy + manual review |
| LiteLLM doesn't handle a provider edge case | Low | Medium | Pin version; fallback to direct API if critical |
| ROUTER removal breaks an edge path | Low | High | Gateway always runs; add assertion to verify |
| models.yaml restructure breaks evaluation harness | Medium | Medium | Run evaluation after changes |
| litellm dependency size/bloat | Low | Low | Check installed size; tree-shake if needed |

---

## Dependencies

- **New**: `litellm` package (`uv add litellm`)
- No database migrations
- No infrastructure changes
- No new environment variables (litellm reads existing `ANTHROPIC_API_KEY`, etc.)
