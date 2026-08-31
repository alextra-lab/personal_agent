"""FRE-1341 — /health echoes settings.build_fingerprint so a harness can assert freshness.

Calls the route function directly, the established pattern in this test dir for
`personal_agent.service.app` (see test_chat_selection_ordering.py, test_expansion_wiring.py)
rather than standing up the full FastAPI app/lifespan.
"""

from __future__ import annotations

import pytest

from personal_agent.service.app import health_check, settings


@pytest.mark.asyncio
async def test_health_echoes_build_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "build_fingerprint", "abc123fingerprint")
    response = await health_check()
    assert response["build_fingerprint"] == "abc123fingerprint"


@pytest.mark.asyncio
async def test_health_reports_none_outside_a_container_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "build_fingerprint", None)
    response = await health_check()
    assert response["build_fingerprint"] is None
