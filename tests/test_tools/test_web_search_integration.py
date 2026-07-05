"""Integration tests for web_search tool — require running SearXNG container.

Run with:
    docker compose up searxng -d
    SEARXNG_INTEGRATION=1 uv run pytest tests/test_tools/test_web_search_integration.py -v

Skip automatically unless SEARXNG_INTEGRATION=1 is set.
"""

import os

import httpx
import pytest

from personal_agent.telemetry.trace import TraceContext

pytestmark = pytest.mark.integration


_CTX = TraceContext.new_trace()


@pytest.fixture(autouse=True)
def require_searxng() -> None:
    """Skip all tests in this module unless SEARXNG_INTEGRATION=1."""
    if not os.environ.get("SEARXNG_INTEGRATION"):
        pytest.skip("SEARXNG_INTEGRATION not set — skipping integration tests")


SEARXNG_URL = "http://localhost:8888"


@pytest.mark.asyncio
async def test_web_search_executor_smoke() -> None:
    """Smoke test: executor returns non-empty results for a broad query."""
    from personal_agent.tools.web import web_search_executor

    result = await web_search_executor(query="python programming", ctx=_CTX)

    assert isinstance(result, dict)
    assert result["result_count"] > 0, "Expected live results from SearXNG"
    assert result["results"][0]["url"].startswith("http")
    assert result["results"][0]["title"] != ""
    assert len(result["results"]) == result["result_count"]


@pytest.mark.asyncio
async def test_web_search_category_routing_it() -> None:
    """Category routing: 'it' category returns results from technical engines."""
    from personal_agent.tools.web import web_search_executor

    result = await web_search_executor(query="asyncio event loop", categories="it", ctx=_CTX)

    assert isinstance(result, dict)
    assert result["result_count"] > 0, "Expected results for IT category query"
    assert len(result["results"]) == result["result_count"]
    # Results should come from technical engines (SO, GitHub, MDN, etc.)
    engines_used = {r["engine"] for r in result["results"] if r.get("engine")}
    assert len(engines_used) > 0, "Expected engine attribution in results"


@pytest.mark.asyncio
async def test_web_search_general_category_excludes_chefkoch() -> None:
    """FRE-796 regression: default 'general' category must not surface chefkoch recipes.

    chefkoch was previously misconfigured with categories: general, so any
    general-category query whose text loosely matched German recipe titles
    (e.g. "American ...") returned off-topic recipe results — reproduced
    live with the exact query below, which returned "Creamy tomato pasta"
    and "Cheeseburger" among the results before the fix.
    """
    from personal_agent.tools.web import web_search_executor

    result = await web_search_executor(
        query=(
            "did the US repay France for American Revolution war debt "
            "French Revolution financial crisis"
        ),
        categories="general",
        ctx=_CTX,
    )

    assert isinstance(result, dict)
    engines_used = {r["engine"] for r in result["results"] if r.get("engine")}
    assert "chefkoch" not in engines_used, (
        f"chefkoch recipe engine leaked into general-category results: {engines_used}"
    )


def test_searxng_health_check() -> None:
    """SearXNG /healthz endpoint returns 200."""
    resp = httpx.get(f"{SEARXNG_URL}/healthz", timeout=5)
    assert resp.status_code == 200, f"SearXNG healthz returned {resp.status_code}"
