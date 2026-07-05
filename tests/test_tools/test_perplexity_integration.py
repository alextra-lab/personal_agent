"""Integration tests for perplexity_query tool — require a live Perplexity API key.

Run with:
    PERPLEXITY_INTEGRATION=1 uv run pytest tests/test_tools/test_perplexity_integration.py -v
"""

import os

import pytest

from personal_agent.telemetry.trace import TraceContext

pytestmark = pytest.mark.integration


_CTX = TraceContext.new_trace()


@pytest.fixture(autouse=True)
def require_perplexity() -> None:
    """Skip all tests in this module unless PERPLEXITY_INTEGRATION=1."""
    if not os.environ.get("PERPLEXITY_INTEGRATION"):
        pytest.skip("PERPLEXITY_INTEGRATION not set — skipping integration tests")


@pytest.mark.asyncio
async def test_reason_mode_smoke() -> None:
    """Reason mode resolves to a live, non-deprecated model and returns an answer.

    FRE-796: reason mode previously mapped to the retired "sonar-reasoning"
    id, which failed every live call with an HTTP 400. This smoke test
    exercises the real API to prove the current mapping actually works.
    """
    from personal_agent.tools.perplexity import perplexity_query_executor

    result = await perplexity_query_executor(
        query="What year was the Perplexity Sonar API launched?",
        mode="reason",
        ctx=_CTX,
    )

    assert result["model"] == "sonar-reasoning-pro"
    assert result["answer"], "Expected a non-empty answer from live reason-mode query"
    assert result["citations"], "Expected non-empty citations from live reason-mode query"
