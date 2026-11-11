"""Integration test for the SearXNG container — requires it running.

Run with:
    docker compose up searxng -d
    SEARXNG_INTEGRATION=1 uv run pytest tests/test_tools/test_web_search_integration.py -v

Skip automatically unless SEARXNG_INTEGRATION=1 is set.

FRE-1766: ``web_search`` no longer calls SearXNG. It calls Exa and Brave directly, and each
live call is paid, so its executor tests here were removed; ``tests/test_tools/test_web_search.py``
covers it with recorded responses. SearXNG stays deployed until FRE-1763 T2 removes it.
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


def test_searxng_health_check() -> None:
    """SearXNG /healthz endpoint returns 200."""
    resp = httpx.get(f"{SEARXNG_URL}/healthz", timeout=5)
    assert resp.status_code == 200, f"SearXNG healthz returned {resp.status_code}"
