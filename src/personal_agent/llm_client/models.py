"""Pydantic models for LLM client configuration.

This module defines the schema for model configuration loaded from config/models.yaml.
"""

from pydantic import BaseModel, Field


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
        supports_function_calling: Whether model/backend supports OpenAI-style function calling.
            If False, tools are not passed to the model. Defaults to True.
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
    supports_function_calling: bool = Field(
        True, description="Whether model supports native function calling"
    )


class ModelConfig(BaseModel):
    """Complete model configuration.

    This represents the structure of config/models.yaml after loading and validation.

    Attributes:
        models: Dictionary mapping model role names (e.g., "router", "reasoning", "coding")
            to their configuration.
    """

    models: dict[str, ModelDefinition] = Field(..., description="Model configurations by role")
