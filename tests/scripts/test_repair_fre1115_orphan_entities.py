"""FRE-1115: the orphan-repair planner must never conflate two distinct entities.

The script folds a description-less orphan into another node only when that node's name
*is the same name*. Anything else is left in place and reported — after FRE-1115's dedup
containment, an orphan such as ``mathematics`` is a correct distinct entity that merely
lacks a description, and folding it into its former (wrong) canonical would recreate the
conflation the containment prevents.
"""

# ruff: noqa: D103

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from scripts.repair_fre1115_orphan_entities import OrphanPlan, _apply_plan, plan_repairs

from personal_agent.memory.service import MemoryService


def _orphan(
    name: str, turn_edges: int = 1, entity_edges: int = 0, entity_type: str = "DomainOrTopic"
) -> dict[str, Any]:
    return {
        "name": name,
        "entity_type": entity_type,
        "turn_edges": turn_edges,
        "entity_edges": entity_edges,
    }


def _described(*names: str, entity_type: str = "DomainOrTopic") -> list[dict[str, Any]]:
    return [{"name": n, "entity_type": entity_type} for n in names]


def test_orphan_folds_into_its_name_equivalent_twin() -> None:
    plans = plan_repairs([_orphan("predictive processing")], _described("Predictive Processing"))
    assert len(plans) == 1
    assert plans[0].action == "fold"
    assert plans[0].canonical == "Predictive Processing"


@pytest.mark.parametrize(
    "orphan_name,described",
    [
        ("mathematics", ["computer science"]),
        ("Blueberries", ["Apricots"]),
        ("Azure", ["Bedrock"]),
        ("Walkaway", ["Little Brother"]),
        ("Cumin", ["Ground cumin"]),
    ],
)
def test_distinct_entity_is_kept_not_folded(orphan_name: str, described: list[str]) -> None:
    """These are the pairs dedup wrongly merged; folding them would re-conflate."""
    plans = plan_repairs([_orphan(orphan_name)], _described(*described))
    assert plans[0].action == "keep"
    assert plans[0].canonical is None
    assert "no name-equivalent twin" in plans[0].reason


def test_diacritic_and_punctuation_variants_are_the_same_name() -> None:
    plans = plan_repairs(
        [_orphan("Pate a bombe"), _orphan("Météo-France")],
        _described("Pâté à bombe", "Météo France"),
    )
    assert [p.action for p in plans] == ["fold", "fold"]
    assert plans[0].canonical == "Pâté à bombe"
    assert plans[1].canonical == "Météo France"


def test_ambiguous_twins_are_kept_and_named() -> None:
    """Two described nodes normalize the same — the script must not guess."""
    plans = plan_repairs([_orphan("neo4j")], _described("Neo4j", "NEO 4J"))
    assert plans[0].action == "keep"
    assert plans[0].canonical is None
    assert "ambiguous" in plans[0].reason
    assert "Neo4j" in plans[0].reason


def test_every_orphan_gets_a_reason() -> None:
    """AC-6: the run explains each disposition rather than reporting a bare count."""
    plans = plan_repairs(
        [_orphan("predictive processing"), _orphan("mathematics"), _orphan("neo4j")],
        _described("Predictive Processing", "computer science", "Neo4j", "NEO 4J"),
    )
    assert len(plans) == 3
    assert all(p.reason for p in plans)


def test_edge_counts_are_carried_into_the_plan() -> None:
    """The operator sees how much is being moved before approving a write."""
    plans = plan_repairs(
        [_orphan("predictive processing", turn_edges=4, entity_edges=7)],
        _described("Predictive Processing"),
    )
    assert plans[0].turn_edges == 4
    assert plans[0].entity_edges == 7


def test_no_described_entities_keeps_everything() -> None:
    plans = plan_repairs([_orphan("mathematics"), _orphan("Blueberries")], [])
    assert {p.action for p in plans} == {"keep"}


class TestApplyAgainstLiveNeo4j:
    """Integration proof of the destructive path (test substrate :7688 only).

    ``--apply`` deletes nodes, so the edge-redirect must be proven to move every edge
    off the orphan before it is removed — a dropped edge is silent data loss.
    """

    pytestmark = pytest.mark.integration

    @pytest_asyncio.fixture
    async def svc(self):
        """Connected service against the isolated test Neo4j, wiped either side."""
        service = MemoryService()  # fre-375-allow: integration test, skips if unavailable
        if not await service.connect():
            pytest.skip("Neo4j not available (make test-infra-up)")
        assert service.driver is not None
        await self._wipe(service)
        yield service
        await self._wipe(service)
        await service.disconnect()

    @staticmethod
    async def _wipe(service: MemoryService) -> None:
        assert service.driver is not None
        async with service.driver.session() as s:
            await s.run("MATCH (n) WHERE n.name STARTS WITH 'FRE1115R_' DETACH DELETE n")
            await s.run("MATCH (t:Turn) WHERE t.turn_id STARTS WITH 'FRE1115R_' DETACH DELETE t")

    @pytest.mark.asyncio
    async def test_apply_moves_every_edge_then_deletes_the_orphan(self, svc: MemoryService) -> None:
        """Every edge follows the fold before the orphan is removed."""
        assert svc.driver is not None
        async with svc.driver.session() as s:
            # Orphan (no entity_id, no description) + described twin differing only in case.
            await s.run(
                """
                CREATE (orphan:Entity {name: 'FRE1115R_neo4j'})
                CREATE (canon:Entity {name: 'FRE1115R_Neo4J', entity_id: 'FRE1115R_Neo4J',
                                      description: 'A graph database'})
                CREATE (other:Entity {name: 'FRE1115R_Cypher', entity_id: 'FRE1115R_Cypher',
                                      description: 'A query language'})
                CREATE (t:Turn {turn_id: 'FRE1115R_turn'})
                CREATE (t)-[:DISCUSSES]->(orphan)
                CREATE (orphan)-[:RELATED_TO]->(other)
                CREATE (other)-[:MENTIONS]->(orphan)
                """
            )

        plan = OrphanPlan(
            name="FRE1115R_neo4j",
            action="fold",
            reason="test",
            canonical="FRE1115R_Neo4J",
            turn_edges=1,
            entity_edges=2,
        )
        counts = await _apply_plan(svc, plan)

        assert counts["deleted"] == 1
        assert counts["discusses_moved"] == 1
        assert counts["entity_edges_moved"] == 2

        async with svc.driver.session() as s:
            result = await s.run("MATCH (e:Entity {name: 'FRE1115R_neo4j'}) RETURN count(e) AS n")
            assert (await result.single())["n"] == 0, "orphan must be gone"

            result = await s.run(
                """
                MATCH (canon:Entity {name: 'FRE1115R_Neo4J'})
                OPTIONAL MATCH (t:Turn)-[:DISCUSSES]->(canon)
                OPTIONAL MATCH (canon)-[out:RELATED_TO]->(:Entity)
                OPTIONAL MATCH (:Entity)-[inc:MENTIONS]->(canon)
                RETURN canon.description AS description, count(DISTINCT t) AS turns,
                       count(DISTINCT out) AS outgoing, count(DISTINCT inc) AS incoming
                """
            )
            row = await result.single()

        assert row["turns"] == 1, "the Turn must now discuss the canonical node"
        assert row["outgoing"] == 1, "the outgoing edge must have followed the fold"
        assert row["incoming"] == 1, "the incoming edge must have followed the fold"
        assert row["description"] == "A graph database", "the description is never touched"

    @pytest.mark.asyncio
    async def test_edge_between_orphan_and_canonical_is_dropped_not_self_looped(
        self, svc: MemoryService
    ) -> None:
        """Refactoring an orphan<->canonical edge would mint a self-loop (review finding)."""
        assert svc.driver is not None
        async with svc.driver.session() as s:
            await s.run(
                """
                CREATE (orphan:Entity {name: 'FRE1115R_neo4j'})
                CREATE (canon:Entity {name: 'FRE1115R_Neo4J', entity_id: 'FRE1115R_Neo4J',
                                      description: 'A graph database'})
                CREATE (orphan)-[:RELATED_TO]->(canon)
                """
            )

        plan = OrphanPlan(
            name="FRE1115R_neo4j",
            action="fold",
            reason="test",
            canonical="FRE1115R_Neo4J",
            turn_edges=0,
            entity_edges=1,
        )
        counts = await _apply_plan(svc, plan)

        assert counts["edges_to_canonical_dropped"] == 1
        assert counts["deleted"] == 1
        async with svc.driver.session() as s:
            result = await s.run(
                """
                MATCH (canon:Entity {name: 'FRE1115R_Neo4J'})
                OPTIONAL MATCH (canon)-[loop]->(canon)
                RETURN count(loop) AS self_loops
                """
            )
            assert (await result.single())["self_loops"] == 0, "no self-loop may be created"

    @pytest.mark.asyncio
    async def test_fold_aborts_untouched_when_the_node_stopped_being_an_orphan(
        self, svc: MemoryService
    ) -> None:
        """Plan/apply TOCTOU: a described node must keep every edge (review finding)."""
        assert svc.driver is not None
        async with svc.driver.session() as s:
            # Node matches the plan's name but has since been described by live traffic.
            await s.run(
                """
                CREATE (n:Entity {name: 'FRE1115R_neo4j', entity_id: 'FRE1115R_neo4j',
                                  description: 'Described between plan and apply'})
                CREATE (canon:Entity {name: 'FRE1115R_Neo4J', entity_id: 'FRE1115R_Neo4J',
                                      description: 'A graph database'})
                CREATE (t:Turn {turn_id: 'FRE1115R_turn'})
                CREATE (t)-[:DISCUSSES]->(n)
                """
            )

        plan = OrphanPlan(
            name="FRE1115R_neo4j",
            action="fold",
            reason="stale plan",
            canonical="FRE1115R_Neo4J",
            turn_edges=1,
            entity_edges=0,
        )
        counts = await _apply_plan(svc, plan)

        assert counts["aborted"] == 1
        assert counts["deleted"] == 0
        assert counts["discusses_moved"] == 0
        async with svc.driver.session() as s:
            result = await s.run(
                """
                MATCH (n:Entity {name: 'FRE1115R_neo4j'})
                OPTIONAL MATCH (t:Turn)-[:DISCUSSES]->(n)
                RETURN n.description AS description, count(t) AS turns
                """
            )
            row = await result.single()
        assert row["description"] == "Described between plan and apply"
        assert row["turns"] == 1, "the node must keep its edges — nothing was touched"
