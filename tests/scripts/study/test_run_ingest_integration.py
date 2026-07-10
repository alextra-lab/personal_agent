"""Integration test for the ADR-0114 corpus ingest driver (FRE-839).

Runs against the REAL study Neo4j sandbox (`make study-infra-up`, real
frozen corpus already loaded — see `scripts/study/README.md`) but with the
categorizer's LLM call mocked — never a real paid call in CI/test. Proves
schema application, alias resolution against real frozen `Entity` data, and
`MEMBER_OF` recomputation end-to-end.

Requires: `make study-infra-up` already running, `STUDY_NEO4J_PASSWORD` in
`.env` matching the running container's credential, and the real corpus
already exported (see `scripts/study/snapshots/snapshot_manifest.json`).

Not idempotent across repeated real-corpus runs: `run_ingest`'s
episode-dedup (`_episode_already_processed`) correctly skips a session
that already has an `Episode` node from a prior run of this exact test —
by design (the same guard that makes a real `--execute-full` re-run safe).
Re-running this test against a sandbox it already populated will report
`assertions_written == 0`, not a regression; delete the 3 `Episode`/
`Mention` nodes (and any `Category`/`MembershipAssertion`s this test
created) to reset for a fresh run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from scripts.study.neo4j_types import Neo4jDriver
from scripts.study.run_ingest import run_ingest
from scripts.study.writer import ProposedMembership


@pytest_asyncio.fixture
async def study_driver() -> Neo4jDriver:  # type: ignore[misc]
    from neo4j import AsyncGraphDatabase
    from scripts.study.config import StudySettings

    settings = StudySettings()
    driver = AsyncGraphDatabase.driver(  # fre-375-allow: connects to the isolated ADR-0114 study sandbox (StudySettings, bolt://localhost:7691), never prod — mirrors test_verify_isolation.py's runtime probes
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        await driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"study Neo4j not reachable ({exc}) — run `make study-infra-up` first")
    yield driver
    await driver.close()


def _fake_categorize(concepts: list[tuple[str, str]]) -> list[ProposedMembership]:
    """A deterministic stand-in for the real (paid) categorizer call."""
    return [
        ProposedMembership(
            concept_name=name, kind=kind, category_name="test category", proposed_confidence=0.75
        )
        for name, kind in concepts[:2]  # keep the fixture small/fast
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_ingest_end_to_end_against_real_corpus_mocked_llm(
    study_driver: Neo4jDriver,
) -> None:
    async def _mock_categorize_conversation(conversation_text, concepts, *, seed, trace_id=None):
        return _fake_categorize(concepts)

    with patch(
        "scripts.study.run_ingest.categorize_conversation",
        new=AsyncMock(side_effect=_mock_categorize_conversation),
    ):
        summary = await run_ingest(study_driver, limit=3, seed=1)

    assert summary["sessions_processed"] == 3
    assert summary["sessions_failed"] == 0
    # Code-review finding (FRE-839): `>= 0` is tautological and can never
    # fail — this asserts the actual expected count. The real corpus's first
    # 3 sessions (by started_at) each have well over 2 discussed entities
    # (39/65/21, confirmed live against the frozen corpus), so
    # `_fake_categorize`'s `concepts[:2]` deterministically yields exactly 2
    # memberships per session — 6 total. A regression that breaks the
    # DISCUSSES-edge query, the categorizer wiring, or the writer would move
    # this number, and this assertion would actually catch it.
    assert summary["assertions_written"] == 6

    async with study_driver.session() as session:
        result = await session.run("MATCH (c:Concept) RETURN count(c) AS c")
        record = await result.single()
        assert record["c"] > 0

        result = await session.run(
            "MATCH (c:Concept)-[m:MEMBER_OF]->(:Category) RETURN count(m) AS c"
        )
        record = await result.single()
        assert record["c"] > 0
