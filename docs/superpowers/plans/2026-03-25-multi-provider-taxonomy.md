# Implementation Plan: Multi-Provider Model Taxonomy (ADR-0033)

**Date**: 2026-03-25
**ADR**: ADR-0033 (Multi-Provider Model Taxonomy & Adaptive Concurrency Bounds)
**Branch**: `feat/multi-provider-taxonomy`
**Estimated tasks**: 10

---

## Summary

| # | Task | Tier | Model | Files Modified | Test Command |
|---|------|------|-------|---------------|-------------|
| 1 | Update ModelRole enum | T2 | Sonnet | `types.py` | `uv run pytest tests/personal_agent/llm_client/test_types.py -v` |
| 2 | Add min_concurrency to ModelDefinition | T2 | Sonnet | `models.py` | `uv run pytest tests/personal_agent/llm_client/test_models.py -v` |
| 3 | Restructure models.yaml | T2 | Sonnet | `models.yaml`, `models-baseline.yaml` | `uv run python -c "from personal_agent.config import load_model_config; c = load_model_config(); print(list(c.models.keys()))"` |
| 4 | ModelConfig backward-compat aliases | T2 | Sonnet | `models.py` | `uv run pytest tests/personal_agent/llm_client/test_models.py -v` |
| 5 | Rename LocalLLMClient → OpenAICompatibleClient | T2 | Sonnet | `client.py`, `__init__.py` | `uv run pytest tests/personal_agent/llm_client/ -v` |
| 6 | Build GeminiClient skeleton | T2 | Sonnet | `gemini.py` (new), `__init__.py` | `uv run pytest tests/personal_agent/llm_client/test_gemini.py -v` |
| 7 | Update factory for three providers | T2 | Sonnet | `factory.py` | `uv run pytest tests/personal_agent/llm_client/test_claude_respond.py::TestGetLLMClientFactory -v` |
| 8 | Wire sub-agent client isolation | T2 | Sonnet | `expansion.py`, `sub_agent.py`, `executor.py` | `uv run pytest tests/personal_agent/orchestrator/test_expansion.py -v` |
| 9 | Update executor for PRIMARY role | T2 | Sonnet | `executor.py`, `routing.py` | `uv run pytest tests/personal_agent/orchestrator/ -v` |
| 10 | Verification | T2 | Sonnet | — | `uv run pytest && uv run mypy src/ && uv run ruff check src/` |

---

## Task 1: Update ModelRole Enum

**File**: `src/personal_agent/llm_client/types.py`

**Changes**:
- Add `PRIMARY = "primary"` and `SUB_AGENT = "sub_agent"` enum members
- Keep `REASONING`, `STANDARD`, `ROUTER` as deprecated members
- Update `from_str()` to handle migration aliases
- Update module docstring

```python
class ModelRole(str, Enum):
    """Model roles mapping to entries in config/models.yaml.

    Tier 1 (Primary): The orchestrator brain — reasoning, tool calling, decomposition.
    Tier 2 (Sub-Agent): Focused single-task completion — no thinking, fast inference.
    Specialist: Task-specific roles (coding) that map to dedicated model configs.

    Deprecated roles are kept for backward compatibility during migration (ADR-0033).
    """

    # ── Active roles (ADR-0033) ─────────────────────
    PRIMARY = "primary"          # Tier 1: orchestrator brain
    SUB_AGENT = "sub_agent"      # Tier 2: focused task completion
    CODING = "coding"            # Specialist: code generation

    # ── Deprecated (kept for deserialization + migration) ────
    REASONING = "reasoning"      # Use PRIMARY; maps to models["reasoning"] if present
    STANDARD = "standard"        # Use SUB_AGENT; maps to models["standard"] if present
    ROUTER = "router"            # Removed in Redesign v2

    @classmethod
    def from_str(cls, value: str) -> "ModelRole | None":
        """Convert string to ModelRole enum (case-insensitive).

        Handles migration aliases:
        - "reasoning" → PRIMARY (if models.yaml has "primary" entry)
        - "standard"  → SUB_AGENT (if models.yaml has "sub_agent" entry)

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

**Test**: `tests/personal_agent/llm_client/test_types.py` — add tests for new enum members and `from_str` with new values.

```python
def test_primary_role_exists() -> None:
    assert ModelRole.PRIMARY.value == "primary"

def test_sub_agent_role_exists() -> None:
    assert ModelRole.SUB_AGENT.value == "sub_agent"

def test_from_str_primary() -> None:
    assert ModelRole.from_str("primary") == ModelRole.PRIMARY

def test_from_str_sub_agent() -> None:
    assert ModelRole.from_str("sub_agent") == ModelRole.SUB_AGENT

def test_deprecated_reasoning_still_works() -> None:
    assert ModelRole.from_str("reasoning") == ModelRole.REASONING

def test_deprecated_standard_still_works() -> None:
    assert ModelRole.from_str("standard") == ModelRole.STANDARD
```

**Verify**: `uv run pytest tests/personal_agent/llm_client/test_types.py -v`

---

## Task 2: Add min_concurrency to ModelDefinition

**File**: `src/personal_agent/llm_client/models.py`

**Changes**:
- Add `min_concurrency` field with default 1
- Add validator: `min_concurrency <= max_concurrency`

```python
# Add after max_concurrency field (line ~109):
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

**Test**: `tests/personal_agent/llm_client/test_models.py` — add validation tests.

```python
def test_min_concurrency_default() -> None:
    """min_concurrency defaults to 1."""
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

## Task 3: Restructure models.yaml

**File**: `config/models.yaml`

**Changes**:
- Rename `reasoning` → `primary` (with updated comments)
- Rename `standard` → `sub_agent` (with tuned parameters)
- Add `min_concurrency` to all model entries
- Add `delegation_targets` section (structural, not yet parsed)
- Add `gemini_pro` cloud model entry (commented out, ready for activation)
- Keep `coding`, `claude_sonnet`, experimental models

```yaml
# Model configuration (ADR-0031 + ADR-0033: multi-provider taxonomy)
#
# ── Three-Tier Taxonomy (ADR-0033) ──────────────────────────────────
#   Tier 1 — PRIMARY:    Orchestrator brain (reasoning, tool calling, decomposition)
#   Tier 2 — SUB_AGENT:  Focused task completion (no thinking, fast, bounded output)
#   Tier 3 — DELEGATION: External agent targets (Slice 3, structure defined below)
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
    # Renamed from "reasoning" in ADR-0033.
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
    # Replaces overloaded "standard" role (ADR-0033).
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

  # ── Specialist Roles ────────────────────────────────────────────
  coding:
    # Code generation specialist. Low temperature for precision.
    id: "unsloth/qwen3.5-9b"
    context_length: 32768
    quantization: "8bit"
    min_concurrency: 1
    max_concurrency: 1
    default_timeout: 45
    temperature: 0.2
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

  # gemini_pro:
  #   # Google Gemini Pro — deep research and long-context analysis
  #   # Uncomment and set AGENT_GOOGLE_API_KEY to use
  #   id: "gemini-2.5-pro"
  #   provider: "google"
  #   provider_type: "cloud"
  #   max_tokens: 8192
  #   context_length: 1000000
  #   min_concurrency: 1
  #   max_concurrency: 10
  #   default_timeout: 120

  # openai_o4_mini:
  #   id: "o4-mini"
  #   provider: "openai"
  #   provider_type: "cloud"
  #   max_tokens: 8192
  #   context_length: 128000
  #   min_concurrency: 1
  #   max_concurrency: 10
  #   default_timeout: 60

# ── Tier 3: Delegation Targets (Slice 3 — structural only) ────────
# These define external agent interfaces, not direct LLM calls.
# Each references a model entry above for API config/credentials.
# Parsed for validation but not invoked until Slice 3 implementation.
#
# delegation_targets:
#   claude_code:
#     description: "Claude Code CLI for coding and refactoring tasks"
#     model_ref: "claude_sonnet"
#     interface: "cli"
#     capabilities: ["code_generation", "refactoring", "testing", "debugging"]
#   deep_research:
#     description: "SOTA model for deep research and long-context analysis"
#     model_ref: "gemini_pro"
#     interface: "api"
#     capabilities: ["research", "analysis", "long_context", "multi_document"]
#   codex:
#     description: "OpenAI Codex for code completion tasks"
#     model_ref: "openai_o4_mini"
#     interface: "api"
#     capabilities: ["code_generation", "code_completion"]
```

**File**: `config/models-baseline.yaml`

Update to use `primary` and `sub_agent` keys:

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

  coding:
    id: "claude-sonnet-4-6"
    provider: "anthropic"
    provider_type: "cloud"
    max_tokens: 8192
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

Expected output: `['claude_sonnet', 'coding', 'coding_large_context', 'primary', 'reasoning_heavy', 'sub_agent']`

---

## Task 4: ModelConfig Backward-Compat Aliases

**File**: `src/personal_agent/llm_client/models.py`

**Changes**: Update `ModelConfig._validate_process_roles` to create aliases so that code using `models["reasoning"]` or `models["standard"]` still works.

```python
@model_validator(mode="after")
def _create_migration_aliases(self) -> "ModelConfig":
    """Create backward-compatible aliases for renamed model roles (ADR-0033).

    Maps:
    - "reasoning" → "primary" (if "primary" exists and "reasoning" does not)
    - "standard"  → "sub_agent" (if "sub_agent" exists and "standard" does not)

    This allows old code using ModelRole.REASONING.value ("reasoning") to
    resolve against the new "primary" model config.
    """
    if "primary" in self.models and "reasoning" not in self.models:
        self.models["reasoning"] = self.models["primary"]
    if "sub_agent" in self.models and "standard" not in self.models:
        self.models["standard"] = self.models["sub_agent"]
    return self
```

**Test**: `tests/personal_agent/llm_client/test_models.py`

```python
def test_reasoning_alias_resolves_to_primary() -> None:
    """ModelConfig creates 'reasoning' alias pointing to 'primary' config."""
    config = ModelConfig(models={
        "primary": ModelDefinition(id="big", context_length=64000, max_concurrency=1, default_timeout=180),
    })
    assert "reasoning" in config.models
    assert config.models["reasoning"].id == "big"

def test_standard_alias_resolves_to_sub_agent() -> None:
    """ModelConfig creates 'standard' alias pointing to 'sub_agent' config."""
    config = ModelConfig(models={
        "primary": ModelDefinition(id="big", context_length=64000, max_concurrency=1, default_timeout=180),
        "sub_agent": ModelDefinition(id="small", context_length=32768, max_concurrency=3, default_timeout=90),
    })
    assert "standard" in config.models
    assert config.models["standard"].id == "small"

def test_explicit_reasoning_not_overwritten() -> None:
    """If both 'primary' and 'reasoning' exist, 'reasoning' keeps its own config."""
    config = ModelConfig(models={
        "primary": ModelDefinition(id="new", context_length=64000, max_concurrency=1, default_timeout=180),
        "reasoning": ModelDefinition(id="old", context_length=64000, max_concurrency=1, default_timeout=180),
    })
    assert config.models["reasoning"].id == "old"
```

**Verify**: `uv run pytest tests/personal_agent/llm_client/test_models.py -v`

---

## Task 5: Rename LocalLLMClient → OpenAICompatibleClient

**File**: `src/personal_agent/llm_client/client.py`

**Changes**:
- Rename class `LocalLLMClient` → `OpenAICompatibleClient`
- Add alias: `LocalLLMClient = OpenAICompatibleClient`
- Update class docstring

```python
class OpenAICompatibleClient:
    """Client for OpenAI-compatible LLM endpoints.

    Supports any endpoint implementing the OpenAI Chat Completions API:
    local inference servers (LM Studio, vLLM, Ollama, llama.cpp), cloud
    providers (OpenAI, Groq, Together, Fireworks, Mistral), and managed
    clusters.

    Renamed from LocalLLMClient in ADR-0033 to reflect actual scope.
    """
    ...

# Backward-compatible alias (ADR-0033 migration)
LocalLLMClient = OpenAICompatibleClient
```

**File**: `src/personal_agent/llm_client/__init__.py`

Update `__all__` and lazy imports:
- Add `"OpenAICompatibleClient"` to `__all__`
- Keep `"LocalLLMClient"` in `__all__` (alias)
- Update `__getattr__` to import from the right location

**Verify**: `uv run python -c "from personal_agent.llm_client import OpenAICompatibleClient, LocalLLMClient; assert OpenAICompatibleClient is LocalLLMClient; print('OK')"`

---

## Task 6: Build GeminiClient Skeleton

**File**: `src/personal_agent/llm_client/gemini.py` (new)

Implements the `LLMClient` protocol using the Google GenAI SDK. Skeleton with `respond()` that raises `NotImplementedError` with a clear message — the client is structurally complete but activation requires the `google-genai` dependency and an API key.

```python
"""Google Gemini LLM client.

Implements the LLMClient protocol for Google's Gemini model family.
Requires the google-genai SDK: `uv add google-genai`.

ADR-0033: Added as the third provider client (OpenAI-compatible, Anthropic, Google).
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from personal_agent.llm_client.types import LLMResponse, ModelRole

log = structlog.get_logger()


class GeminiClient:
    """Google Gemini API client.

    Provides the same respond() interface as OpenAICompatibleClient and
    ClaudeClient, enabling transparent provider dispatch via the factory.

    Model identity comes from config/models.yaml (provider: "google").
    API key comes from settings.google_api_key (.env).

    Args:
        model_id: Gemini model identifier (e.g., "gemini-2.5-pro").
        max_tokens: Maximum output tokens per request.
    """

    def __init__(
        self,
        model_id: str,
        max_tokens: int = 8192,
    ) -> None:
        self.model = model_id
        self.max_tokens = max_tokens
        # SDK client initialized lazily on first respond() call
        self._client: Any = None

    def _ensure_client(self) -> None:
        """Lazily initialize the Google GenAI client."""
        if self._client is not None:
            return
        try:
            import google.generativeai as genai  # type: ignore[import-untyped]
        except ImportError:
            raise ImportError(
                "google-genai package required for GeminiClient. "
                "Install with: uv add google-genai"
            ) from None

        from personal_agent.config import get_settings

        settings = get_settings()
        api_key = getattr(settings, "google_api_key", None)
        if not api_key:
            raise ValueError("AGENT_GOOGLE_API_KEY not configured in .env")

        genai.configure(api_key=api_key)
        self._client = genai.GenerativeModel(self.model)
        log.info("gemini_client_initialized", model=self.model)

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
        trace_ctx: Any | None = None,
        previous_response_id: str | None = None,
        priority: Any = None,
        priority_timeout: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Make an LLM call via Google Gemini API.

        Implements the LLMClient protocol. Converts OpenAI-format messages
        to Gemini format and returns a normalized LLMResponse.

        Raises:
            NotImplementedError: Until google-genai integration is complete.
        """
        raise NotImplementedError(
            "GeminiClient.respond() is structurally defined but not yet implemented. "
            "Activate by installing google-genai and implementing message/tool conversion. "
            "See ADR-0033 for the multi-provider roadmap."
        )
```

**File**: `src/personal_agent/llm_client/__init__.py` — add `GeminiClient` to `__all__` and `__getattr__`.

**Test**: `tests/personal_agent/llm_client/test_gemini.py` (new)

```python
"""Tests for GeminiClient skeleton (ADR-0033)."""

import pytest

from personal_agent.llm_client.gemini import GeminiClient
from personal_agent.llm_client.types import ModelRole


class TestGeminiClientInit:
    def test_constructor_sets_model(self) -> None:
        client = GeminiClient(model_id="gemini-2.5-pro")
        assert client.model == "gemini-2.5-pro"
        assert client.max_tokens == 8192

    def test_constructor_custom_max_tokens(self) -> None:
        client = GeminiClient(model_id="gemini-2.5-flash", max_tokens=4096)
        assert client.max_tokens == 4096


class TestGeminiClientRespond:
    @pytest.mark.asyncio
    async def test_respond_raises_not_implemented(self) -> None:
        client = GeminiClient(model_id="gemini-2.5-pro")
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hello"}],
            )
```

**Verify**: `uv run pytest tests/personal_agent/llm_client/test_gemini.py -v`

---

## Task 7: Update Factory for Three Providers

**File**: `src/personal_agent/llm_client/factory.py`

**Changes**:
- Add `"google"` provider dispatch to `GeminiClient`
- Accept `"openai"` as explicit provider (routes to `OpenAICompatibleClient`)
- Update docstring and protocol

```python
def get_llm_client(role_name: str = "primary") -> Any:
    """Return appropriate LLM client based on provider field in models.yaml.

    Dispatch logic (ADR-0033):
        provider == "anthropic"  →  ClaudeClient
        provider == "google"     →  GeminiClient
        provider is None/"openai" → OpenAICompatibleClient (local or cloud OpenAI-compat)

    Args:
        role_name: Model role key in models.yaml (e.g., "primary", "sub_agent", "coding").

    Returns:
        LLM client implementing the LLMClient protocol.
    """
    config = load_model_config()
    model_def = config.models.get(role_name)

    if model_def is None:
        log.warning("model_role_not_found", role=role_name, fallback="OpenAICompatibleClient")
        from personal_agent.llm_client.client import OpenAICompatibleClient
        return OpenAICompatibleClient()

    match model_def.provider:
        case "anthropic":
            from personal_agent.llm_client.claude import ClaudeClient
            return ClaudeClient(
                model_id=model_def.id,
                max_tokens=model_def.max_tokens or 8192,
            )
        case "google":
            from personal_agent.llm_client.gemini import GeminiClient
            return GeminiClient(
                model_id=model_def.id,
                max_tokens=model_def.max_tokens or 8192,
            )
        case _:  # None, "openai", or any OpenAI-compatible
            from personal_agent.llm_client.client import OpenAICompatibleClient
            return OpenAICompatibleClient()
```

**Test**: Update `tests/personal_agent/llm_client/test_claude_respond.py::TestGetLLMClientFactory`

```python
def test_factory_google_provider_returns_gemini(self, ...):
    """provider='google' returns GeminiClient."""
    # Mock model config with provider="google"
    ...
    client = get_llm_client("gemini_pro")
    assert isinstance(client, GeminiClient)

def test_factory_openai_explicit_returns_openai_compat(self, ...):
    """provider='openai' returns OpenAICompatibleClient."""
    ...
    client = get_llm_client("openai_model")
    assert isinstance(client, OpenAICompatibleClient)
```

**Verify**: `uv run pytest tests/personal_agent/llm_client/test_claude_respond.py::TestGetLLMClientFactory -v`

---

## Task 8: Wire Sub-Agent Client Isolation

**File**: `src/personal_agent/orchestrator/expansion.py`

**Changes**:
- `parse_decomposition_plan()` uses `ModelRole.SUB_AGENT` for all specs
- `execute_hybrid()` creates its own client via factory instead of accepting one

```python
# In parse_decomposition_plan():
from personal_agent.llm_client.types import ModelRole

specs.append(
    SubAgentSpec(
        task=task_text,
        context=[],
        output_format="markdown_summary",
        max_tokens=max_tokens,
        timeout_seconds=timeout,
        model_role=ModelRole.SUB_AGENT,  # Explicit tier 2 (was: default STANDARD)
    )
)

# In execute_hybrid():
async def execute_hybrid(
    specs: Sequence[SubAgentSpec],
    trace_id: str,
    max_concurrent: int | None = None,
) -> list[SubAgentResult]:
    """Execute sub-agents concurrently within expansion budget.

    Creates a dedicated sub-agent LLM client (ADR-0033: client isolation).
    Sub-agents always use the sub_agent model config, never the primary's client.
    """
    from personal_agent.llm_client.factory import get_llm_client

    sub_agent_client = get_llm_client(role_name="sub_agent")
    max_conc = max_concurrent or settings.expansion_budget_max
    semaphore = asyncio.Semaphore(max(1, max_conc))

    async def _run_with_semaphore(spec: SubAgentSpec) -> SubAgentResult:
        async with semaphore:
            return await run_sub_agent(
                spec=spec,
                llm_client=sub_agent_client,
                trace_id=trace_id,
            )

    # ... rest unchanged
```

**File**: `src/personal_agent/orchestrator/executor.py`

**Changes**: Remove `llm_client` parameter from `execute_hybrid()` call.

```python
# Before (line ~1517):
results = await execute_hybrid(
    specs=specs,
    llm_client=llm_client,  # ← primary client leaked to sub-agents
    trace_id=ctx.trace_id,
    max_concurrent=max_sub,
)

# After:
results = await execute_hybrid(
    specs=specs,
    trace_id=ctx.trace_id,
    max_concurrent=max_sub,
)
```

**Test**: `tests/personal_agent/orchestrator/test_expansion.py`

```python
@pytest.mark.asyncio
async def test_execute_hybrid_uses_sub_agent_client(mock_factory):
    """execute_hybrid creates its own client via factory, not using primary's."""
    # Verify get_llm_client("sub_agent") was called
    mock_factory.assert_called_once_with(role_name="sub_agent")

def test_parse_decomposition_plan_uses_sub_agent_role():
    """All parsed specs have ModelRole.SUB_AGENT, not STANDARD."""
    specs = parse_decomposition_plan("1. Task A\n2. Task B")
    for spec in specs:
        assert spec.model_role == ModelRole.SUB_AGENT
```

**Verify**: `uv run pytest tests/personal_agent/orchestrator/test_expansion.py -v`

---

## Task 9: Update Executor for PRIMARY Role

**File**: `src/personal_agent/orchestrator/executor.py`

**Changes**: Replace `ModelRole.REASONING` with `ModelRole.PRIMARY` in the gateway-driven path.

```python
# Before (line ~1046):
if ctx.gateway_output is not None and ctx.selected_model_role is None:
    model_role = ModelRole.REASONING
    ...

# After:
if ctx.gateway_output is not None and ctx.selected_model_role is None:
    model_role = ModelRole.PRIMARY
    ...
```

**File**: `src/personal_agent/orchestrator/routing.py`

**Changes**: Update `resolve_role()` to handle `PRIMARY` alongside `REASONING`.

```python
def resolve_role(requested_role: ModelRole) -> ModelRole:
    """Map requested role to actual runtime role."""
    role_upper = requested_role.value.upper()

    if role_upper == "ROUTER":
        router_cfg = (getattr(settings, "router_role", None) or "ROUTER").upper()
        if router_cfg == "STANDARD":
            return ModelRole.SUB_AGENT  # Updated from STANDARD
        return ModelRole.PRIMARY  # Router → Primary (no separate router model)

    if role_upper in ("REASONING", "PRIMARY"):
        if not getattr(settings, "enable_reasoning_role", True):
            return ModelRole.SUB_AGENT  # Fallback to sub-agent tier
        return ModelRole.PRIMARY

    return requested_role
```

**File**: `src/personal_agent/orchestrator/executor.py` — `_determine_initial_model_role()`

```python
def _determine_initial_model_role(ctx: ExecutionContext) -> ModelRole:
    if ctx.channel == Channel.CODE_TASK:
        return ModelRole.CODING
    elif ctx.channel == Channel.CHAT:
        return resolve_role(ModelRole.PRIMARY)  # was: ROUTER
    else:
        return resolve_role(ModelRole.PRIMARY)  # was: REASONING
```

**Verify**: `uv run pytest tests/personal_agent/orchestrator/ -v`

---

## Task 10: Verification

Run the full verification suite:

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
print('Reasoning alias:', 'reasoning' in c.models)
print('Standard alias:', 'standard' in c.models)
print('Min concurrency (primary):', c.models['primary'].min_concurrency)
print('Max concurrency (sub_agent):', c.models['sub_agent'].max_concurrency)
"
```

Expected output:
```
Models: ['claude_sonnet', 'coding', 'coding_large_context', 'primary', 'reasoning', 'reasoning_heavy', 'standard', 'sub_agent']
Primary ID: unsloth/qwen3.5-35-A3B
Sub-agent ID: unsloth/qwen3.5-9b
Reasoning alias: True
Standard alias: True
Min concurrency (primary): 1
Max concurrency (sub_agent): 3
```

---

## Deferred to Slice 3

The following items are **structurally prepared** by this plan but not implemented:

| Item | What's Ready | What's Deferred |
|------|-------------|-----------------|
| **Adaptive concurrency** | `min_concurrency` / `max_concurrency` bounds on all models | Brainstem feedback loop that adjusts within bounds |
| **Delegation targets** | `delegation_targets` section in models.yaml (commented) | Parsing, validation, invocation via DelegationPackage |
| **GeminiClient** | Class skeleton with `respond()` protocol | Message/tool format conversion, API integration |
| **Google API key** | — | `settings.google_api_key` in AppConfig + `.env.example` |

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| ModelRole rename breaks external scripts | Low | Medium | Backward-compat aliases in enum + ModelConfig |
| Sub-agent client creation adds latency | Low | Low | Client construction is lightweight; cache if measured |
| models.yaml restructure breaks evaluation harness | Medium | Medium | Run evaluation after changes to verify |
| GeminiClient import fails (no google-genai) | Expected | None | Lazy import; clear error message |

---

## Dependencies

- No new Python packages required (GeminiClient is lazy-import)
- No database migrations
- No infrastructure changes
- Backward compatible with existing evaluation harness (aliases ensure old role names work)
