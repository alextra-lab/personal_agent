"""Pydantic models for LLM client configuration.

This module defines the schema for model configuration loaded from config/models.yaml.
"""

from pydantic import BaseModel, Field, model_validator


class ModelDefinition(BaseModel):
    """Configuration for a single model.

    Attributes:
        id: Model identifier (e.g., "qwen/qwen3-4b-thinking-2507").
        endpoint: Optional base URL override for this model. If None, uses
            settings.llm_base_url.
        context_length: Maximum context length for this model.
        quantization: Quantization level (e.g., "8bit", "4bit", "5bit").
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
    endpoint: str | None = Field(None, description="Optional base URL override")
    context_length: int = Field(..., ge=1, description="Maximum context length")
    quantization: str = Field(..., description="Quantization level")
    max_concurrency: int = Field(..., ge=1, description="Maximum concurrent requests")
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
        True, description="Whether model supports native function calling"
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


class ModelConfig(BaseModel):
    """Complete model configuration.

    This represents the structure of config/models.yaml after loading and validation.

    Attributes:
        models: Dictionary mapping model role names (e.g., "router", "reasoning", "coding")
            to their configuration.
        entity_extraction_role: Which role under models is used for entity extraction
            (Phase 2.2). Must be a key in models or "claude".
    """

    models: dict[str, ModelDefinition] = Field(..., description="Model configurations by role")
    entity_extraction_role: str = Field(
        default="reasoning",
        description="Role used for entity extraction: a key in models or 'claude'",
    )

    @model_validator(mode="after")
    def _validate_entity_extraction_role(self) -> "ModelConfig":
        """Ensure entity_extraction_role is a key in models or 'claude'."""
        valid = set(self.models.keys()) | {"claude"}
        if self.entity_extraction_role not in valid:
            raise ValueError(
                f"entity_extraction_role must be one of {sorted(valid)}, "
                f"got: {self.entity_extraction_role!r}"
            )
        return self
