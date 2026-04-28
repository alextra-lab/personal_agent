"""FRE-284 — Seshat skill-doc URL drift detector.

Parses the 'Works Now' sections of the four Seshat skill docs and asserts that
every documented path exists in the live FastAPI gateway router.

This test catches future drift cheaply: if a gateway route is renamed or removed,
or if a skill doc adds an example for a path that was never implemented, this
test fails before any eval run is contaminated.

No infrastructure required — uses the FastAPI app object directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SKILL_DIR = Path(__file__).parent.parent.parent / "docs" / "skills"

# Regex: match /api/v1/... paths in curl examples inside "Works Now" sections.
# Captures the path component after $SESHAT_API_URL or http://localhost:...
_PATH_PATTERN = re.compile(
    r'(?:"\$SESHAT_API_URL|"http://localhost:[0-9]+/api/v1)'
    r'(/[a-zA-Z0-9/_{}?=&.-]*)',
)


def _extract_api_paths(doc_path: Path) -> list[str]:
    """Extract all /api/v1/... paths from a skill doc.

    Stops reading at the first '🚫 Planned' or 'not implemented' section header
    so that we only test the 'Works Now' content.
    """
    content = doc_path.read_text()
    # Truncate at the Planned section so we don't test those paths.
    planned_idx = re.search(r"##.*(Planned|not implemented|🚫)", content, re.IGNORECASE)
    if planned_idx:
        content = content[: planned_idx.start()]

    paths: list[str] = []
    for match in _PATH_PATTERN.finditer(content):
        raw = match.group(1).split("?")[0].rstrip("/")  # strip query params and trailing slash
        if raw:
            paths.append("/api/v1" + raw)
    return paths


def _get_gateway_route_paths() -> set[str]:
    """Collect all registered paths from the FastAPI gateway app."""
    from personal_agent.gateway.app import create_gateway_router
    from fastapi import FastAPI

    app = FastAPI()
    router = create_gateway_router()
    app.include_router(router)

    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            # Normalise {param} → {param} (already normalised); collect as-is.
            paths.add(path)
    return paths


# ---------------------------------------------------------------------------
# Parameterised test
# ---------------------------------------------------------------------------

_SESHAT_DOCS = [
    "seshat-knowledge.md",
    "seshat-observations.md",
    "seshat-sessions.md",
    # seshat-delegate.md excluded: its entire commands section is Planned.
]


@pytest.fixture(scope="module")
def gateway_paths() -> set[str]:
    return _get_gateway_route_paths()


@pytest.mark.parametrize("doc_name", _SESHAT_DOCS)
def test_seshat_doc_paths_exist_in_gateway(
    doc_name: str, gateway_paths: set[str]
) -> None:
    """Every /api/v1/... path in the 'Works Now' section must be a registered route."""
    doc_path = _SKILL_DIR / doc_name
    assert doc_path.exists(), f"Skill doc not found: {doc_path}"

    extracted = _extract_api_paths(doc_path)
    if not extracted:
        pytest.skip(f"No API paths found in {doc_name}")

    missing: list[str] = []
    for path in extracted:
        # Normalise {placeholder} for matching: /api/v1/sessions/{session_id}
        # should match /api/v1/sessions/{session_id} in the router.
        # We also check for prefix matches for parameterised routes.
        base = re.sub(r"\{[^}]+\}", "{param}", path)
        normalised_routes = {re.sub(r"\{[^}]+\}", "{param}", r) for r in gateway_paths}
        if base not in normalised_routes:
            missing.append(path)

    assert not missing, (
        f"Paths documented in {doc_name} but not registered in the gateway router:\n"
        + "\n".join(f"  {p}" for p in missing)
        + "\nEither update the skill doc (remove/mark Planned) or implement the route."
    )
