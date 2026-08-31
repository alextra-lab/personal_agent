"""Integration test for the FRE-1348 provenance backfill against a real Neo4j (:7688).

Exercises the real :class:`_Neo4jGraph` Cypher end to end against ADR-0098 Amendment A's own
acceptance criteria (AC-1 through AC-4): seeds legacy ``:Entity``/``:Claim`` nodes and a
legacy relationship whose ``provenance_state`` predates FRE-1346, plus on-disk fixture
captures whose ``fetch_url`` tool-result output demonstrably contains some of those items'
names/content, runs the real migration, and checks the graph.

Marked ``integration`` → skipped by ``make test``; run with the isolated test stack up
(``make test-infra-up``). Assertions are scoped to the seeded name prefix so pre-existing
test data neither fails this test nor is depended upon.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from scripts.migrate_fre1348_provenance_backfill import (
    _STRUCTURAL_RELATIONSHIP_TYPES,
    _VALID_STATES,
    _Neo4jGraph,
    run_backfill,
)

from personal_agent.captains_log.capture import TaskCapture, build_capture_index, write_capture
from personal_agent.grounding.containment import ContainmentOutcome, check_containment
from personal_agent.memory.service import MemoryService
from personal_agent.tools import get_default_registry

pytestmark = pytest.mark.integration

_TS = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
_REGISTRY = get_default_registry()
_RUN_ID = "fre1348-it"
_NOW = "2026-08-31T00:00:00+00:00"


@pytest_asyncio.fixture
async def driver():
    """Connect to the test Neo4j; skip if unavailable."""
    service = MemoryService()  # fre-375-allow: isolated test stack :7688
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")
    yield service.driver
    await service.disconnect()


@pytest.fixture
def captures_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from personal_agent.captains_log import capture as capture_mod

    d = tmp_path / "captures"
    d.mkdir()
    monkeypatch.setattr(capture_mod, "_get_captures_dir", lambda: d)
    return d


@pytest_asyncio.fixture
async def prefix(driver):
    """A unique fixture-name prefix, cleaned up on teardown even if the test fails.

    A trailing cleanup statement at the end of the test body only runs when every
    assertion above it passes — an assertion failure mid-test (as happened once during
    this ticket's own development) skips straight past it and leaves fixture nodes
    orphaned on the shared test substrate for every later test run to trip over.
    """
    value = f"FRE1348IT{uuid4().hex[:8]}"
    try:
        yield value
    finally:
        async with driver.session() as session:
            await session.run(
                "MATCH (x) WHERE x.name STARTS WITH $prefix OR x.claim_id STARTS WITH $prefix "
                "DETACH DELETE x",
                prefix=value,
            )
            await session.run(
                "MATCH (s:Source) WHERE s.referent STARTS WITH 'https://example.com/' "
                "AND s.retained_pointer CONTAINS $prefix DETACH DELETE s",
                prefix=value,
            )


def _write_fixture_capture(trace_id: str, *, fetches: list[tuple[str, str]]) -> None:
    """Write a fixture capture with one fetch_url tool_result per (url, content) pair."""
    write_capture(
        TaskCapture(
            trace_id=trace_id,
            session_id="sess-fre1348-it",
            timestamp=_TS,
            user_message="research",
            assistant_response="done",
            outcome="completed",
            user_id="00000000-0000-0000-0000-000000000000",
            tool_results=[
                {
                    "tool_name": "fetch_url",
                    "success": True,
                    "arguments": {"url": url},
                    "output": content,
                }
                for url, content in fetches
            ],
        )
    )


async def _seed_entity(
    driver, name: str, *, trace_id: str | None, state: str | None = "__unset__"
) -> None:
    async with driver.session() as session:
        if state == "__unset__":
            await session.run(
                "MERGE (e:Entity {name: $name}) SET e.originating_trace_id = $trace_id",
                name=name,
                trace_id=trace_id,
            )
        else:
            await session.run(
                "MERGE (e:Entity {name: $name}) "
                "SET e.originating_trace_id = $trace_id, e.provenance_state = $state",
                name=name,
                trace_id=trace_id,
                state=state,
            )


async def _seed_claim(driver, claim_id: str, content: str, *, trace_id: str | None) -> None:
    async with driver.session() as session:
        await session.run(
            "CREATE (cl:Claim {claim_id: $claim_id, content: $content, trace_id: $trace_id})",
            claim_id=claim_id,
            content=content,
            trace_id=trace_id,
        )


async def _seed_relationship(driver, source_name: str, target_name: str, rel_type: str) -> None:
    async with driver.session() as session:
        await session.run(
            "MERGE (a:Entity {name: $source}) "
            "MERGE (b:Entity {name: $target}) "
            f"MERGE (a)-[r:{rel_type}]->(b) SET r.weight = 1.0",
            source=source_name,
            target=target_name,
        )


async def _entity_state(driver, name: str) -> tuple[str | None, list[str]]:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (e:Entity {name: $name}) "
            "OPTIONAL MATCH (e)-[:SOURCED_FROM]->(s:Source) "
            "RETURN e.provenance_state AS state, collect(s.referent) AS referents",
            name=name,
        )
        rec = await result.single()
    return (rec["state"], rec["referents"]) if rec else (None, [])


async def _claim_state(driver, claim_id: str) -> tuple[str | None, list[str]]:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (cl:Claim {claim_id: $claim_id}) "
            "OPTIONAL MATCH (cl)-[:SOURCED_FROM]->(s:Source) "
            "RETURN cl.provenance_state AS state, collect(s.referent) AS referents",
            claim_id=claim_id,
        )
        rec = await result.single()
    return (rec["state"], rec["referents"]) if rec else (None, [])


@pytest.mark.asyncio
async def test_backfill_end_to_end(driver, captures_dir, prefix) -> None:
    def n(suffix: str) -> str:
        return f"{prefix}{suffix}"

    # ---- AC-2 fixture: >=10 legacy entities whose minting captures demonstrably
    # contain their names. 2 of the 10 are pre-stamped 'none' (simulating the
    # incidental create_entity/create_conversation touch — codex finding 1) to prove
    # the widened predicate, not just the IS NULL case.
    reconstructable_names = [n(f"Recon{i}") for i in range(10)]
    for i, name in enumerate(reconstructable_names):
        trace_id = f"{prefix}-trace-recon-{i}"
        _write_fixture_capture(
            trace_id, fetches=[(f"https://example.com/{name}", f"{name} is a real organization.")]
        )
        pre_state = "none" if i < 2 else "__unset__"
        await _seed_entity(driver, name, trace_id=trace_id, state=pre_state)

    # ---- No false attribution: a capture that does NOT mention the entity.
    no_match_name = n("NoMatch")
    no_match_trace = f"{prefix}-trace-nomatch"
    _write_fixture_capture(
        no_match_trace, fetches=[("https://example.com/other", "Nothing relevant.")]
    )
    await _seed_entity(driver, no_match_name, trace_id=no_match_trace)

    # ---- Missing capture: trace_id set but no capture file exists.
    missing_name = n("MissingCapture")
    await _seed_entity(driver, missing_name, trace_id=f"{prefix}-trace-does-not-exist")

    # ---- Claim reconstruction.
    claim_id = f"{prefix}-claim-1"
    claim_trace = f"{prefix}-trace-claim"
    claim_content = f"{prefix} lease ends in June."
    _write_fixture_capture(claim_trace, fetches=[("https://example.com/lease", claim_content)])
    await _seed_claim(driver, claim_id, claim_content, trace_id=claim_trace)

    # ---- Legacy relationship: never attempted for reconstruction, always -> 'none'.
    await _seed_relationship(driver, n("RelSource"), n("RelTarget"), "FOUNDED")

    capture_index = build_capture_index()
    graph = _Neo4jGraph(driver)

    # --dry-run's relationship preview must agree with what the real run then marks —
    # same predicate, read-only vs write (code-reviewer finding: a hard-coded 0 under
    # dry-run would silently break that contract).
    preview_count = await graph.count_relationship_candidates()

    report = await run_backfill(
        graph, capture_index, _REGISTRY, None, run_id=_RUN_ID, now=_NOW, dry_run=False
    )
    assert report.relationships_marked_none == preview_count

    # ---- AC-2: all 10 (including the 2 pre-stamped 'none') are provenanced with a
    # :Source carrying the fixture's referent.
    for i, name in enumerate(reconstructable_names):
        state, referents = await _entity_state(driver, name)
        assert state == "provenanced", f"{name} (pre_state idx={i}) did not reconstruct"
        assert referents == [f"https://example.com/{name}"]

    # ---- AC-3: independently re-run containment against the fixture's own content for a
    # sample of the reconstructed entities (:Source never stores content — D3).
    for name in reconstructable_names[:5]:
        outcome = check_containment(name, f"{name} is a real organization.")
        assert outcome.outcome is ContainmentOutcome.CONTAINED

    # No false attribution.
    no_match_state, no_match_referents = await _entity_state(driver, no_match_name)
    assert no_match_state == "none"
    assert no_match_referents == []

    # Missing capture -> none, never provenanced.
    missing_state, _ = await _entity_state(driver, missing_name)
    assert missing_state == "none"

    # Claim reconstructed.
    claim_state, claim_referents = await _claim_state(driver, claim_id)
    assert claim_state == "provenanced"
    assert claim_referents == ["https://example.com/lease"]

    # Legacy relationship: marked none, never attempted for reconstruction (no :Source edge).
    async with driver.session() as session:
        result = await session.run(
            "MATCH (:Entity {name: $s})-[r:FOUNDED]->(:Entity {name: $t}) "
            "RETURN r.provenance_state AS state",
            s=n("RelSource"),
            t=n("RelTarget"),
        )
        rec = await result.single()
    assert rec["state"] == "none"

    # ---- AC-4: counts reported, buckets reconcile.
    assert report.entities_reconstructed >= 10
    assert report.entities_none_missing_capture >= 1
    assert (
        report.entities_reconstructed
        + report.entities_none_no_match
        + report.entities_none_missing_capture
        + report.entities_errors
        == report.entities_total
    )
    assert (
        report.claims_reconstructed
        + report.claims_none_no_match
        + report.claims_none_missing_capture
        + report.claims_errors
        == report.claims_total
    )

    # ---- AC-1: no silent third state, over nodes and relationships, scoped to this
    # fixture's prefix so pre-existing graph data cannot mask (or fail) the assertion.
    async with driver.session() as session:
        node_result = await session.run(
            "MATCH (x) WHERE (x:Entity OR x:Claim) "
            "AND (x.name STARTS WITH $prefix OR x.claim_id STARTS WITH $prefix) "
            "AND (x.provenance_state IS NULL OR NOT x.provenance_state IN $valid_states) "
            "RETURN count(x) AS n",
            prefix=prefix,
            valid_states=list(_VALID_STATES),
        )
        node_rec = await node_result.single()

        rel_result = await session.run(
            "MATCH (a)-[r]->(b) WHERE (a.name STARTS WITH $prefix OR b.name STARTS WITH $prefix) "
            "AND r.weight IS NOT NULL AND NOT type(r) IN $structural_types "
            "AND (r.provenance_state IS NULL OR NOT r.provenance_state IN $valid_states) "
            "RETURN count(r) AS n",
            prefix=prefix,
            structural_types=list(_STRUCTURAL_RELATIONSHIP_TYPES),
            valid_states=list(_VALID_STATES),
        )
        rel_rec = await rel_result.single()

    assert node_rec["n"] == 0
    assert rel_rec["n"] == 0

    # Cleanup happens in the `prefix` fixture's teardown, even if an assertion above
    # this point fails.
