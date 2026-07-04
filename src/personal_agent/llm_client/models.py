"""Pydantic models for LLM client configuration.

This module defines the schema for model configuration loaded from config/models.yaml.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ToolCallingStrategy(str, Enum):
    """How the agent presents tools to a given model.

    NATIVE:          Pass tools in the OpenAI ``tools`` array and expect
                     structured ``tool_calls`` in the response.  Works only when
                     the model's chat template renders tools correctly *and* the
                     model was fine-tuned on that format (e.g. Qwen3.5 via
                     LM Studio).
    PROMPT_INJECTED: Render tool definitions as text inside the system prompt
                     and parse tool invocations from the model's free-text
                     output.  Use this for models whose chat template does not
                     support tools or whose native tool output is unreliable.
    DISABLED:        No tool calling at all (e.g. the router model).
    """

    NATIVE = "native"
    PROMPT_INJECTED = "prompt"
    DISABLED = "disabled"


class ModelDefinition(BaseModel):
    """Configuration for a single model.

    Applies to both local models (LM Studio, vLLM, Ollama) and cloud models
    (Anthropic Claude, OpenAI). Cloud-specific fields (provider, max_tokens) are
    optional and ignored for local models; local-specific fields (quantization,
    endpoint) are optional and ignored for cloud models (ADR-0031).

    Attributes:
        id: Model identifier. For local models this is the LM Studio slug
            (e.g., "qwen3.5-35b-a3b"). For cloud models this is the provider's
            model name (e.g., "claude-sonnet-4-5-20250514", "o4-mini").
        provider: Cloud provider name. "anthropic", "openai", etc. dispatch to LiteLLMClient.
            None means local model via LocalLLMClient.
        max_tokens: Maximum output tokens for this model. Primarily useful for
            cloud models where output length is billed per token. None = provider
            default / LocalLLMClient call-site default.
        endpoint: Optional base URL override for this model. If None, uses
            settings.llm_base_url. Not used for cloud models (they use provider SDK).
        provider_type: Endpoint classification for concurrency control (ADR-0029).
            "local" = single-GPU servers (strict concurrency), "managed" = self-hosted
            multi-GPU clusters (moderate control), "cloud" = OpenAI/Anthropic/etc
            (pass-through). Auto-detected from endpoint if omitted.
        context_length: Maximum context length for this model.
        quantization: Quantization level (e.g., "8bit", "4bit", "5bit"). None for
            cloud models where quantization is managed by the provider.
        max_concurrency: Maximum concurrent requests for this model.
        default_timeout: Default timeout in seconds for requests to this model.
        temperature: Default sampling temperature (None uses backend default).
        top_p: Top-p nucleus sampling probability (None uses backend default).
        top_k: Top-k sampling — number of highest-probability tokens to keep. Not in the
            standard OpenAI spec; passed via extra_body for vLLM/LM Studio backends.
        presence_penalty: Presence penalty to reduce repetition. Positive values discourage
            token reuse. Passed in the top-level payload (standard OpenAI field).
        supports_function_calling: Whether model/backend supports OpenAI-style function calling.
            If False, tools are not passed to the model. Defaults to True.
        disable_thinking: If True, inject chat_template_kwargs enable_thinking=False via
            extra_body on every request. Hard-disables thinking for Qwen3.5+ models.
            Mutually exclusive with thinking_budget_tokens.
        thinking_budget_tokens: Cap on the number of thinking tokens the model may generate.
            Passed as thinking_budget in extra_body. None means unlimited.
            Mutually exclusive with disable_thinking.
        supports_vision: Whether this model/deployment accepts image content blocks
            (ADR-0101 §5). A deployment property, not inferred — set explicitly per
            model definition. Defaults to False.
    """

    id: str = Field(..., description="Model identifier")
    provider: str | None = Field(
        None,
        description=(
            "Cloud provider for dispatch (ADR-0031). "
            "'anthropic', 'openai', etc. = LiteLLMClient. "
            "None = local model via LocalLLMClient."
        ),
    )
    max_tokens: int | None = Field(
        None,
        ge=1,
        description=(
            "Maximum output tokens. Primarily used for cloud models where output length "
            "is billed per token. None = provider default."
        ),
    )
    endpoint: str | None = Field(None, description="Optional base URL override (local models)")
    provider_type: str | None = Field(
        None,
        description=(
            "Endpoint classification for concurrency control (ADR-0029). "
            "'local' = single-GPU (strict), 'managed' = multi-GPU cluster, "
            "'cloud' = OpenAI/Anthropic (pass-through). Auto-detected if omitted."
        ),
    )
    context_length: int = Field(..., ge=1, description="Maximum context length")
    quantization: str | None = Field(
        None,
        description=(
            "Quantization level (e.g., '8bit', '4bit'). "
            "None for cloud models where quantization is provider-managed."
        ),
    )
    max_concurrency: int = Field(..., ge=1, description="Maximum concurrent requests")
    min_concurrency: int = Field(
        default=1,
        ge=1,
        description=(
            "Floor for adaptive concurrency control (ADR-0033). "
            "Brainstem cannot reduce effective concurrency below this value. "
            "Must be <= max_concurrency."
        ),
    )
    default_timeout: int = Field(..., ge=1, description="Default timeout in seconds")
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="Default sampling temperature for this model (None uses backend default).",
    )
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = Field(
        default=None,
        description=(
            "FRE-766: discrete reasoning-effort hint for reasoning models (GPT-5 family). "
            "One of low/medium/high/xhigh; None uses the provider default (medium for GPT-5). "
            "Forwarded to litellm.acompletion by LiteLLMClient. Claude uses adaptive thinking, "
            "not this hint — leave None for Anthropic models."
        ),
    )
    top_p: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Top-p nucleus sampling probability (None uses backend default).",
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        description="Top-k sampling — passed via extra_body (not standard OpenAI).",
    )
    presence_penalty: float | None = Field(
        default=None,
        ge=-2.0,
        le=2.0,
        description="Presence penalty to reduce token repetition.",
    )
    min_p: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Min-p sampling — passed via extra_body (llama.cpp / vLLM extension).",
    )
    repetition_penalty: float | None = Field(
        default=None,
        ge=0.0,
        description="Repetition penalty — passed via extra_body (llama.cpp / vLLM extension).",
    )
    supports_function_calling: bool = Field(
        True,
        description=(
            "DEPRECATED — use tool_calling_strategy instead.  Kept for backward "
            "compatibility; ignored when tool_calling_strategy is set explicitly."
        ),
    )
    supports_vision: bool = Field(
        False,
        description=(
            "Whether this model/deployment accepts image content blocks (ADR-0101 §5). "
            "A deployment property, not inferred — set explicitly per model definition."
        ),
    )
    input_cost_per_token: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "USD cost per input token (ADR-0101 §8b / FRE-691). Config-owned pricing "
            "registered into litellm.model_cost at startup so cloud cost is deterministic "
            "and non-zero, independent of litellm's shipped registry. None = rely on the "
            "litellm registry (local/free models leave this unset). Image (vision) tokens "
            "are billed as ordinary input tokens on this rate."
        ),
    )
    output_cost_per_token: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "USD cost per output token (ADR-0101 §8b / FRE-691). See input_cost_per_token."
        ),
    )
    tool_calling_strategy: ToolCallingStrategy | None = Field(
        default=None,
        description=(
            "How to present tools to this model.  'native' = OpenAI tools array, "
            "'prompt' = inject tools into the system prompt as text, "
            "'disabled' = no tool calling.  When None the strategy is derived "
            "from supports_function_calling for backward compatibility."
        ),
    )
    parallel_tool_calls: bool = Field(
        default=True,
        description=(
            "Include parallel_tool_calls=True in the chat completions payload, "
            "allowing the model to emit multiple tool calls in a single response turn. "
            "Only active when tool_calling_strategy=NATIVE. Requires llama.cpp >= build "
            "with QwenLM/#1831 Qwen3.x template fixes (FRE-232). "
            "Set False for models whose chat template does not handle parallel calls."
        ),
    )
    disable_thinking: bool = Field(
        default=False,
        description=(
            "If True, inject chat_template_kwargs enable_thinking=False via extra_body. "
            "Hard-disables thinking for Qwen3.5+ models. "
            "Mutually exclusive with thinking_budget_tokens."
        ),
    )
    thinking_budget_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Cap on thinking tokens; passed as thinking_budget in extra_body. "
            "None = unlimited. Mutually exclusive with disable_thinking."
        ),
    )

    @model_validator(mode="after")
    def _thinking_fields_exclusive(self) -> "ModelDefinition":
        """Ensure disable_thinking and thinking_budget_tokens are not both set."""
        if self.disable_thinking and self.thinking_budget_tokens is not None:
            raise ValueError(
                "disable_thinking and thinking_budget_tokens are mutually exclusive: "
                "a model cannot have thinking both disabled and budgeted."
            )
        return self

    @model_validator(mode="after")
    def _min_max_concurrency(self) -> "ModelDefinition":
        """Ensure min_concurrency does not exceed max_concurrency."""
        if self.min_concurrency > self.max_concurrency:
            raise ValueError(
                f"min_concurrency ({self.min_concurrency}) must be <= "
                f"max_concurrency ({self.max_concurrency})"
            )
        return self

    @model_validator(mode="after")
    def _derive_tool_calling_strategy(self) -> "ModelDefinition":
        """Derive tool_calling_strategy from supports_function_calling when not set."""
        if self.tool_calling_strategy is None:
            self.tool_calling_strategy = (
                ToolCallingStrategy.NATIVE
                if self.supports_function_calling
                else ToolCallingStrategy.DISABLED
            )
        return self

    @property
    def effective_tool_strategy(self) -> ToolCallingStrategy:
        """Return the resolved tool calling strategy (never None)."""
        if self.tool_calling_strategy is not None:
            return self.tool_calling_strategy
        return (
            ToolCallingStrategy.NATIVE
            if self.supports_function_calling
            else ToolCallingStrategy.DISABLED
        )


class ModelConfig(BaseModel):
    """Complete model configuration.

    This represents the structure of config/models.yaml after loading and validation.
    All model identity and call parameters live here (ADR-0031). Only secrets (API keys)
    and operational controls (budgets, feature flags) belong in settings.py / .env.

    Cognitive-pipeline role assignment (entity extraction, Captain's Log, insights,
    compressor, embedding, reranker) lives ONLY in config/model_roles.yaml (ADR-0099
    D1 stage 2, FRE-650) — resolved via
    :func:`personal_agent.config.model_loader.resolve_role_model_key`, not a field
    on this model. There is no fallback: an absent matrix or undeclared role raises.

    Attributes:
        models: Dictionary mapping role names to their configuration. Includes both
            local model roles (primary, sub_agent) and cloud model
            entries (e.g. claude_sonnet, openai_o4_mini).
    """

    models: dict[str, ModelDefinition] = Field(..., description="Model configurations by role")
