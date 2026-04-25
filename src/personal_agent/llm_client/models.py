"""Pydantic models for LLM client configuration.

This module defines the schema for model configuration loaded from config/models.yaml.
"""

from enum import Enum

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
    supports_function_calling: bool = Field(
        True,
        description=(
            "DEPRECATED — use tool_calling_strategy instead.  Kept for backward "
            "compatibility; ignored when tool_calling_strategy is set explicitly."
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

    Attributes:
        models: Dictionary mapping role names to their configuration. Includes both
            local model roles (primary, sub_agent) and cloud model
            entries (e.g. claude_sonnet, openai_o4_mini).
        entity_extraction_role: Role key used for Second Brain entity extraction.
            Must be a key in models. Cloud models are identified by their provider field.
        captains_log_role: Role key used for Captain's Log reflection generation.
            Must be a key in models. Defaults to "primary" (local fallback).
        insights_role: Role key used for Insights Engine analysis.
            Must be a key in models. Defaults to "primary" (local fallback).
    """

    models: dict[str, ModelDefinition] = Field(..., description="Model configurations by role")
    entity_extraction_role: str = Field(
        default="primary",
        description="Role used for entity extraction: a key in models",
    )
    captains_log_role: str = Field(
        default="primary",
        description="Role used for Captain's Log reflection: a key in models",
    )
    insights_role: str = Field(
        default="primary",
        description="Role used for Insights Engine LLM calls: a key in models",
    )

    @model_validator(mode="after")
    def _validate_process_roles(self) -> "ModelConfig":
        """Ensure all process role keys resolve to entries in models.

        For backward compatibility, the legacy sentinel value "claude" is accepted
        for entity_extraction_role and silently remapped to the first cloud model
        with provider="anthropic" if one exists, or left as-is for callers that
        handle the old sentinel themselves.
        """
        self.entity_extraction_role = self._resolve_role(
            "entity_extraction_role", self.entity_extraction_role
        )
        self.captains_log_role = self._resolve_role("captains_log_role", self.captains_log_role)
        self.insights_role = self._resolve_role("insights_role", self.insights_role)
        return self

    def _resolve_role(self, field_name: str, value: str) -> str:
        """Validate a role field and return a resolved value.

        Args:
            field_name: Name of the field being validated (for error messages).
            value: The role string from models.yaml.

        Returns:
            Resolved role string (a key in self.models).

        Raises:
            ValueError: If value is not a valid model key and cannot be resolved.
        """
        valid = set(self.models.keys())

        if value in valid:
            return value

        # Backward-compatible fallback: if the field was omitted and defaulted to
        # "primary", choose a viable role from the provided models dict.
        explicitly_set = field_name in self.model_fields_set
        if not explicitly_set and value == "primary":
            if self.models:
                return "primary" if "primary" in self.models else next(iter(self.models))
            return value

        # Legacy sentinel: "claude" was the old magic string for entity_extraction_role.
        # Remap to the first Anthropic cloud model in the registry if one exists.
        if value == "claude":
            anthropic_key = next(
                (k for k, m in self.models.items() if m.provider == "anthropic"),
                None,
            )
            if anthropic_key:
                return anthropic_key
            # No cloud model defined yet — keep sentinel for callers to handle gracefully.
            return value

        raise ValueError(f"{field_name} must be one of {sorted(valid)}, got: {value!r}")
