"""DSPy adapter for structured LLM outputs.

This module provides integration between LocalLLMClient and DSPy for structured
outputs via Pydantic models. Based on E-008 prototype evaluation and ADR-0010.

Usage:
    from personal_agent.llm_client.dspy_adapter import configure_dspy_lm

    # Configure DSPy once at module level
    lm = configure_dspy_lm(role=ModelRole.REASONING)
    dspy.configure(lm=lm)

    # Use DSPy signatures
    class MySignature(dspy.Signature):
        input_field: str = dspy.InputField()
        output_field: str = dspy.OutputField()

    predictor = dspy.ChainOfThought(MySignature)
    result = predictor(input_field="value")

Design:
- DSPy handles structured output generation via signatures
- Uses LM Studio's OpenAI-compatible endpoint (/v1/chat/completions)
- Supports ModelRole-based model selection from models.yaml
- Thread-safe configuration (can create multiple dspy.LM instances)

Related:
- ADR-0010: Structured LLM Outputs via Pydantic Models
- E-008: DSPy Prototype Evaluation
- experiments/dspy_prototype/setup_dspy.py
"""

from typing import Any

from personal_agent.config import settings
from personal_agent.config.model_loader import ModelConfigError, load_model_config
from personal_agent.llm_client.types import ModelRole
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

try:
    import dspy  # type: ignore[import-untyped]
except ImportError:
    dspy = None  # type: ignore[assignment,unused-ignore]


def configure_dspy_lm(
    role: ModelRole,
    base_url: str | None = None,
    timeout_s: int | None = None,
) -> Any:
    """Configure and return a DSPy LM instance for the specified model role.

    This function creates a DSPy language model adapter configured to use
    LM Studio's OpenAI-compatible endpoint with the model specified for the
    given role in models.yaml.

    Args:
        role: Model role (ROUTER, REASONING, CODING, STANDARD) to lookup model.
        base_url: Optional LM Studio base URL. Defaults to settings.llm_base_url.
        timeout_s: Optional timeout in seconds. Defaults to settings.llm_timeout_seconds.

    Returns:
        Configured dspy.LM instance ready to use with dspy.configure().

    Raises:
        ImportError: If dspy package is not installed.
        ModelConfigError: If model configuration is missing for role.

    Example:
        >>> from personal_agent.llm_client.dspy_adapter import configure_dspy_lm
        >>> from personal_agent.llm_client.types import ModelRole
        >>> import dspy
        >>>
        >>> lm = configure_dspy_lm(role=ModelRole.REASONING)
        >>> dspy.configure(lm=lm)
        >>>
        >>> class MySignature(dspy.Signature):
        ...     question: str = dspy.InputField()
        ...     answer: str = dspy.OutputField()
        >>>
        >>> predictor = dspy.Predict(MySignature)
        >>> result = predictor(question="What is Python?")

    Notes:
        - DSPy requires OpenAI-compatible API endpoint
        - LM Studio provides /v1/chat/completions endpoint
        - api_key is set to "lm-studio" (dummy key required by LiteLLM)
        - Model format: "openai/model-name" for OpenAI-compatible endpoints
        - base_url must include /v1 suffix (required by LiteLLM routing)
    """
    if dspy is None:
        raise ImportError(
            "dspy package is required for structured outputs. Install with: uv add dspy>=3.1.0"
        )

    # Load model configuration
    try:
        model_configs = load_model_config()
    except ModelConfigError as e:
        log.error("model_config_load_failed", error=str(e), component="dspy_adapter")
        raise

    # Lookup model for role
    model_def = model_configs.models.get(role.value)
    if not model_def:
        raise ModelConfigError(
            f"No model configured for role '{role.value}'. "
            f"Available roles: {list(model_configs.models.keys())}"
        )

    model_id = model_def.id

    # Determine base URL (priority: explicit arg > model config > settings default)
    effective_base_url = base_url or model_def.endpoint or settings.llm_base_url

    # Ensure base_url includes /v1 (required by LiteLLM routing)
    if not effective_base_url.endswith("/v1"):
        if effective_base_url.endswith("/v1/"):
            effective_base_url = effective_base_url.rstrip("/")
        else:
            effective_base_url = f"{effective_base_url.rstrip('/')}/v1"

    # Determine timeout
    effective_timeout = timeout_s or settings.llm_timeout_seconds

    log.info(
        "dspy_lm_configured",
        role=role.value,
        model_id=model_id,
        base_url=effective_base_url,
        timeout_s=effective_timeout,
        component="dspy_adapter",
    )

    # Create DSPy LM instance
    # Format: "openai/model-name" for OpenAI-compatible endpoints
    # api_base and api_key passed as kwargs to LiteLLM
    lm = dspy.LM(
        model=f"openai/{model_id}",  # Format for OpenAI-compatible
        api_base=effective_base_url,  # Must include /v1
        api_key="lm-studio",  # Dummy key (LiteLLM requires non-empty)
        model_type="chat",  # Use 'chat' for chat models
        timeout=effective_timeout,
    )

    return lm


def create_dspy_predictor(
    signature: type,
    module_type: str = "chain_of_thought",
    role: ModelRole = ModelRole.REASONING,
    base_url: str | None = None,
    timeout_s: int | None = None,
) -> Any:
    """Create a configured DSPy predictor for the given signature.

    This is a convenience function that combines LM configuration and predictor
    creation in one call. Useful for one-off predictions.

    Args:
        signature: DSPy signature class defining input/output fields.
        module_type: Type of DSPy module. Options: "predict", "chain_of_thought", "react".
        role: Model role for model selection. Defaults to REASONING.
        base_url: Optional LM Studio base URL.
        timeout_s: Optional timeout in seconds.

    Returns:
        Configured DSPy predictor (Predict, ChainOfThought, or ReAct).

    Raises:
        ValueError: If invalid module_type is specified.
        ImportError: If dspy package is not installed.
        ModelConfigError: If model configuration is missing.

    Example:
        >>> import dspy
        >>> from personal_agent.llm_client.dspy_adapter import create_dspy_predictor
        >>>
        >>> class AnswerQuestion(dspy.Signature):
        ...     question: str = dspy.InputField()
        ...     answer: str = dspy.OutputField()
        >>>
        >>> predictor = create_dspy_predictor(AnswerQuestion, module_type="chain_of_thought")
        >>> result = predictor(question="What is Python?")
        >>> print(result.answer)

    Notes:
        - This function configures DSPy globally (dspy.configure())
        - For multiple predictors, consider calling configure_dspy_lm() once
        - ChainOfThought recommended for complex reasoning tasks
        - ReAct not recommended for tool execution (per E-008 Test Case C)
    """
    if dspy is None:
        raise ImportError("dspy package is required. Install with: uv add dspy>=3.1.0")

    # Configure DSPy with the specified model role
    lm = configure_dspy_lm(role=role, base_url=base_url, timeout_s=timeout_s)
    dspy.configure(lm=lm)

    # Create predictor based on module type
    if module_type == "predict":
        return dspy.Predict(signature)
    elif module_type == "chain_of_thought":
        return dspy.ChainOfThought(signature)
    elif module_type == "react":
        return dspy.ReAct(signature)
    else:
        raise ValueError(
            f"Invalid module_type '{module_type}'. Options: 'predict', 'chain_of_thought', 'react'"
        )
