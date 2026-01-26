"""Setup DSPy with LM Studio configuration.

This script configures DSPy to work with LM Studio's OpenAI-compatible endpoint.
"""

import dspy

from personal_agent.config import settings
from personal_agent.config.model_loader import load_model_config


def configure_dspy(model_name: str | None = None) -> None:
    """Configure DSPy to use LM Studio OpenAI-compatible endpoint.

    Args:
        model_name: Model identifier. If None, uses 'standard' model from models.yaml.
                    Use exact LM Studio model name (e.g., 'qwen/qwen3-4b-2507').

    This sets up DSPy's language model adapter to use the local LM Studio instance.
    Uses dspy.OpenAI which routes through LiteLLM for OpenAI-compatible endpoints.

    Note: Based on Perplexity research, key requirements:
    - Use dspy.LM with model format "openai/model-name"
    - api_base must include /v1 (passed as kwarg)
    - api_key must be set (even if dummy like 'lm-studio', passed as kwarg)
    - Use exact model name from LM Studio
    """
    # Load model from config if not specified
    if model_name is None:
        try:
            config = load_model_config()
            standard_model = config.models.get("standard")
            if standard_model:
                model_name = standard_model.id
                print(f"📋 Using 'standard' model from config: {model_name}")
            else:
                # Fallback to a known model
                model_name = "qwen/qwen3-4b-2507"
                print(f"⚠️  'standard' model not found in config, using: {model_name}")
        except Exception as e:
            # Fallback if config loading fails
            model_name = "qwen/qwen3-4b-2507"
            print(f"⚠️  Config load failed ({e}), using fallback: {model_name}")

    # DSPy expects OpenAI-compatible API
    # LM Studio provides /v1/chat/completions endpoint
    base_url = settings.llm_base_url

    # Ensure base_url includes /v1 (required by LiteLLM)
    if not base_url.endswith("/v1"):
        if base_url.endswith("/v1/"):
            base_url = base_url.rstrip("/")
        else:
            base_url = f"{base_url.rstrip('/')}/v1"

    # Configure DSPy with OpenAI-compatible LM Studio endpoint
    # Use dspy.LM with "openai/" prefix and pass api_base/api_key as kwargs
    lm = dspy.LM(
        model=f"openai/{model_name}",  # Format: "openai/model-name" for OpenAI-compatible
        api_base=base_url,  # Must include /v1, passed as kwarg to LiteLLM
        api_key="lm-studio",  # Dummy key required (LiteLLM rejects empty keys)
        model_type="chat",  # Use 'chat' for chat models
    )

    dspy.configure(lm=lm)
    print(f"✅ DSPy configured with LM Studio at {base_url}")
    print(f"   Using model: {model_name}")


def test_basic_signature() -> str:
    """Test basic DSPy signature to verify setup works.

    Returns:
        Response from DSPy to verify it's working.
    """

    # Simple test signature
    class AnswerQuestion(dspy.Signature):
        """Answer a simple question."""

        question: str = dspy.InputField()
        answer: str = dspy.OutputField()

    # Create predictor
    predictor = dspy.Predict(AnswerQuestion)

    # Test
    result = predictor(question="What is 2+2?")
    return result.answer


if __name__ == "__main__":
    print("Setting up DSPy with LM Studio...")
    configure_dspy()

    print("\nTesting basic signature...")
    try:
        answer = test_basic_signature()
        print(f"✅ DSPy test successful! Answer: {answer}")
    except Exception as e:
        print(f"❌ DSPy test failed: {e}")
        import traceback

        traceback.print_exc()
        raise
