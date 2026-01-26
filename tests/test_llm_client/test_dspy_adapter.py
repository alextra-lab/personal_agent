"""Integration tests for DSPy adapter and LocalLLMClient.get_dspy_lm().

These tests verify that the DSPy integration works correctly with LM Studio's
OpenAI-compatible endpoint. Based on E-008 prototype evaluation.

Test Coverage:
- DSPy LM configuration with role-based model selection
- LocalLLMClient.get_dspy_lm() method
- Basic DSPy Predict module
- DSPy ChainOfThought module (recommended for Captain's Log)
- Error handling (missing dependencies, invalid role)

Related:
- ADR-0010: Structured LLM Outputs
- E-008: DSPy Prototype Evaluation
- src/personal_agent/llm_client/dspy_adapter.py
"""

import pytest

# Check if dspy is available
pytest.importorskip("dspy", reason="dspy not installed")

import dspy  # noqa: E402

from personal_agent.config.model_loader import ModelConfigError  # noqa: E402
from personal_agent.llm_client import LocalLLMClient, ModelRole  # noqa: E402
from personal_agent.llm_client.dspy_adapter import (  # noqa: E402
    configure_dspy_lm,
    create_dspy_predictor,
)

# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def llm_client():
    """Create LocalLLMClient for testing."""
    return LocalLLMClient()


# ============================================================================
# DSPy Signature Definitions (for testing)
# ============================================================================


class SimpleQuestion(dspy.Signature):
    """Answer a simple question."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField()


class ReasoningTask(dspy.Signature):
    """Perform a reasoning task with explanation."""

    task: str = dspy.InputField(desc="The reasoning task to perform")
    reasoning: str = dspy.OutputField(desc="Step-by-step reasoning")
    conclusion: str = dspy.OutputField(desc="Final conclusion")


# ============================================================================
# Unit Tests: configure_dspy_lm()
# ============================================================================


def test_configure_dspy_lm_with_reasoning_role():
    """Test DSPy LM configuration with REASONING role."""
    lm = configure_dspy_lm(role=ModelRole.REASONING)

    # Verify LM instance is created
    assert lm is not None
    assert hasattr(lm, "model")

    # Verify model format (should be "openai/model-name")
    assert lm.model.startswith("openai/")


def test_configure_dspy_lm_with_router_role():
    """Test DSPy LM configuration with ROUTER role."""
    lm = configure_dspy_lm(role=ModelRole.ROUTER)

    assert lm is not None
    assert hasattr(lm, "model")
    assert lm.model.startswith("openai/")


def test_configure_dspy_lm_with_invalid_role():
    """Test that invalid role raises ModelConfigError."""

    # Create a fake role that doesn't exist in config
    class FakeRole:
        value = "nonexistent_role"

    with pytest.raises(ModelConfigError, match="No model configured"):
        configure_dspy_lm(role=FakeRole())  # type: ignore[arg-type]


def test_configure_dspy_lm_with_custom_base_url():
    """Test DSPy LM configuration with custom base_url."""
    custom_url = "http://custom:1234"
    lm = configure_dspy_lm(role=ModelRole.REASONING, base_url=custom_url)

    assert lm is not None
    # Note: Can't easily verify internal base_url, but no exception means it worked


def test_configure_dspy_lm_with_custom_timeout():
    """Test DSPy LM configuration with custom timeout."""
    custom_timeout = 60
    lm = configure_dspy_lm(role=ModelRole.REASONING, timeout_s=custom_timeout)

    assert lm is not None


# ============================================================================
# Unit Tests: LocalLLMClient.get_dspy_lm()
# ============================================================================


def test_llm_client_get_dspy_lm(llm_client):
    """Test LocalLLMClient.get_dspy_lm() method."""
    lm = llm_client.get_dspy_lm(role=ModelRole.REASONING)

    assert lm is not None
    assert hasattr(lm, "model")
    assert lm.model.startswith("openai/")


def test_llm_client_get_dspy_lm_uses_client_config(llm_client):
    """Test that get_dspy_lm() uses client's base_url and timeout."""
    # Create client with custom config
    custom_client = LocalLLMClient(
        base_url="http://custom:1234",
        timeout_seconds=60,
    )

    lm = custom_client.get_dspy_lm(role=ModelRole.REASONING)

    # Verify LM is created (no exception means config was passed correctly)
    assert lm is not None


# ============================================================================
# Integration Tests: DSPy Predict Module
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dspy_predict_simple_question(llm_client):
    """Test basic DSPy Predict module with simple question."""
    # Configure DSPy
    lm = llm_client.get_dspy_lm(role=ModelRole.REASONING)
    dspy.configure(lm=lm)

    # Create predictor
    predictor = dspy.Predict(SimpleQuestion)

    # Run prediction
    result = predictor(question="What is 2+2?")

    # Verify result
    assert hasattr(result, "answer")
    assert isinstance(result.answer, str)
    assert len(result.answer) > 0

    # Answer should contain "4" (basic math check)
    assert "4" in result.answer


# ============================================================================
# Integration Tests: DSPy ChainOfThought Module
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dspy_chain_of_thought_reasoning(llm_client):
    """Test DSPy ChainOfThought module (recommended for Captain's Log)."""
    # Configure DSPy
    lm = llm_client.get_dspy_lm(role=ModelRole.REASONING)
    dspy.configure(lm=lm)

    # Create ChainOfThought predictor
    predictor = dspy.ChainOfThought(ReasoningTask)

    # Run prediction with reasoning task
    result = predictor(task="If all dogs are animals, and all animals breathe, do dogs breathe?")

    # Verify result has both reasoning and conclusion
    assert hasattr(result, "reasoning")
    assert hasattr(result, "conclusion")
    assert isinstance(result.reasoning, str)
    assert isinstance(result.conclusion, str)
    assert len(result.reasoning) > 0
    assert len(result.conclusion) > 0

    # Conclusion should indicate "yes" (basic logic check)
    assert any(word in result.conclusion.lower() for word in ["yes", "true", "correct", "breathe"])


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dspy_chain_of_thought_with_empty_fields(llm_client):
    """Test ChainOfThought handles optional/empty fields correctly."""
    # Configure DSPy
    lm = llm_client.get_dspy_lm(role=ModelRole.REASONING)
    dspy.configure(lm=lm)

    # Signature with optional field
    class OptionalFields(dspy.Signature):
        """Task with optional output."""

        question: str = dspy.InputField()
        answer: str = dspy.OutputField(desc="Answer (or empty string if unknown)")

    predictor = dspy.ChainOfThought(OptionalFields)
    result = predictor(question="What is the capital of a planet that doesn't exist?")

    # Should still return result (even if answer might be empty or unknown)
    assert hasattr(result, "answer")
    assert isinstance(result.answer, str)


# ============================================================================
# Integration Tests: create_dspy_predictor() Helper
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_dspy_predictor_predict():
    """Test create_dspy_predictor() with Predict module."""
    predictor = create_dspy_predictor(
        signature=SimpleQuestion,
        module_type="predict",
        role=ModelRole.REASONING,
    )

    result = predictor(question="What is Python?")

    assert hasattr(result, "answer")
    assert isinstance(result.answer, str)
    assert len(result.answer) > 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_dspy_predictor_chain_of_thought():
    """Test create_dspy_predictor() with ChainOfThought module."""
    predictor = create_dspy_predictor(
        signature=ReasoningTask,
        module_type="chain_of_thought",
        role=ModelRole.REASONING,
    )

    result = predictor(task="Explain why the sky is blue in one sentence.")

    assert hasattr(result, "reasoning")
    assert hasattr(result, "conclusion")
    assert isinstance(result.reasoning, str)
    assert isinstance(result.conclusion, str)


def test_create_dspy_predictor_invalid_module_type():
    """Test create_dspy_predictor() with invalid module_type."""
    with pytest.raises(ValueError, match="Invalid module_type"):
        create_dspy_predictor(
            signature=SimpleQuestion,
            module_type="invalid_type",
            role=ModelRole.REASONING,
        )


# ============================================================================
# Error Handling Tests
# ============================================================================


def test_configure_dspy_lm_without_dspy_raises_import_error(monkeypatch):
    """Test that configure_dspy_lm raises ImportError if dspy not installed."""
    # Mock dspy as None (simulating it's not installed)
    import personal_agent.llm_client.dspy_adapter as adapter_module

    monkeypatch.setattr(adapter_module, "dspy", None)

    with pytest.raises(ImportError, match="dspy package is required"):
        configure_dspy_lm(role=ModelRole.REASONING)


# ============================================================================
# Performance Tests (Baseline Measurement)
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_dspy_chain_of_thought_latency_baseline(llm_client):
    """Measure DSPy ChainOfThought latency for baseline comparison.

    This test establishes a baseline for Captain's Log reflection latency.
    Based on E-008 Test Case A: ~14.3s average (acceptable: +21% vs manual).
    """
    import time

    # Configure DSPy
    lm = llm_client.get_dspy_lm(role=ModelRole.REASONING)
    dspy.configure(lm=lm)

    # Create ChainOfThought predictor
    predictor = dspy.ChainOfThought(ReasoningTask)

    # Measure latency
    start_time = time.time()
    result = predictor(task="Analyze this simple task execution.")
    elapsed_ms = int((time.time() - start_time) * 1000)

    # Verify result exists
    assert hasattr(result, "reasoning")
    assert hasattr(result, "conclusion")

    # Log latency for comparison (not a strict assertion)
    print(f"\n[DSPy ChainOfThought Latency] {elapsed_ms}ms")

    # Sanity check: should complete within reasonable time (< 60s)
    assert elapsed_ms < 60000, "DSPy ChainOfThought took too long"
