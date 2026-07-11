"""Integration test for the ADR-0114 consolidator's GDS candidate generation (FRE-842).

Runs against the REAL study Neo4j sandbox (`make study-infra-up`, real
frozen corpus + FRE-839 ingest already loaded — see `scripts/study/README.md`)
— read-only, no writes. Proves the Category-Concept bipartite projection +
`gds.nodeSimilarity.stream` Cypher this ticket's plan hand-verified live
stays correct under the actual module code, not just a fake session.

Requires: `make study-infra-up` already running, `STUDY_NEO4J_PASSWORD` in
`.env` matching the running container's credential.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from scripts.study.consolidator import generate_candidates_gds
from scripts.study.neo4j_types import Neo4jDriver


@pytest_asyncio.fixture
async def study_driver() -> Neo4jDriver:  # type: ignore[misc]
    from neo4j import AsyncGraphDatabase
    from scripts.study.config import StudySettings

    settings = StudySettings()
    driver = AsyncGraphDatabase.driver(  # fre-375-allow: connects to the isolated ADR-0114 study sandbox (StudySettings, bolt://localhost:7691), never prod — mirrors test_run_ingest_integration.py's runtime probes
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        await driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"study Neo4j not reachable ({exc}) — run `make study-infra-up` first")
    yield driver
    await driver.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_candidates_gds_runs_against_real_sandbox(study_driver: Neo4jDriver) -> None:
    """Read-only: projects, streams, and drops against the real populated
    sandbox (818 Concepts / 1341 Categories / 1667 MEMBER_OF at last check)
    and must return normalized, deduped, self-pair-free candidate pairs.
    """
    candidates = await generate_candidates_gds(
        study_driver, graph_name="fre842_integration_test", top_k=5, similarity_cutoff=0.1
    )

    assert isinstance(candidates, list)
    seen_pairs: set[tuple[str, str]] = set()
    for candidate in candidates:
        assert candidate.category_a != candidate.category_b
        assert candidate.category_a <= candidate.category_b
        pair = (candidate.category_a, candidate.category_b)
        assert pair not in seen_pairs, "duplicate (a,b) pair returned"
        seen_pairs.add(pair)
        assert 0.0 <= candidate.jaccard <= 1.0
