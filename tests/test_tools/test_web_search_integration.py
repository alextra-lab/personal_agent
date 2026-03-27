"""Integration tests for web_search tool — require running SearXNG container.

Run with:
    docker compose up searxng -d
    SEARXNG_INTEGRATION=1 uv run pytest tests/test_tools/test_web_search_integration.py -v

Skip automatically unless SEARXNG_INTEGRATION=1 is set.
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.integration


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

    result = await web_search_executor(query="python programming")

    assert isinstance(result, dict)
    assert result["result_count"] > 0, "Expected live results from SearXNG"
    assert result["results"][0]["url"].startswith("http")
    assert result["results"][0]["title"] != ""
    assert len(result["results"]) == result["result_count"]


@pytest.mark.asyncio
async def test_web_search_category_routing_it() -> None:
    """Category routing: 'it' category returns results from technical engines."""
    from personal_agent.tools.web import web_search_executor

    result = await web_search_executor(query="asyncio event loop", categories="it")

    assert isinstance(result, dict)
    assert result["result_count"] > 0, "Expected results for IT category query"
    assert len(result["results"]) == result["result_count"]
    # Results should come from technical engines (SO, GitHub, MDN, etc.)
    engines_used = {r["engine"] for r in result["results"] if r.get("engine")}
    assert len(engines_used) > 0, "Expected engine attribution in results"


def test_searxng_health_check() -> None:
    """SearXNG /healthz endpoint returns 200."""
    resp = httpx.get(f"{SEARXNG_URL}/healthz", timeout=5)
    assert resp.status_code == 200, f"SearXNG healthz returned {resp.status_code}"
