"""Unit tests for the FRE-632 owner-identity unify orchestration.

These exercise the migration ALGORITHM (:func:`run_unify`) against an in-memory fake graph — no
Neo4j — so they run in ``make test`` as the CI-gating control-flow proof. The real Cypher
(:class:`_Neo4jGraph`: the two-step ``SET += apoc.map.removeKeys`` + ``apoc.refactor.mergeNodes``)
is exercised against a live :7688 graph by
``test_migrate_fre632_unify_owner_identity_integration.py``.
"""

from __future__ import annotations

import pytest
from scripts.migrate_fre632_unify_owner_identity import (
    NodeSnapshot,
    _is_unified,
    _print_summary,
    run_unify,
)


class FakeNode:
    """A mutable in-memory node the FakeGraph merges."""

    def __init__(
        self,
        eid: str,
        labels: list[str],
        name: str | None,
        *,
        is_owner: bool | None = None,
        user_id: str | None = None,
        has_embedding: bool = False,
        rel_counts: dict[str, int] | None = None,
    ) -> None:
        self.eid = eid
        self.labels = labels
        self.name = name
        self.is_owner = is_owner
        self.user_id = user_id
        self.has_embedding = has_embedding
        self.rel_counts = rel_counts or {}


class FakeGraph:
    """In-memory graph seam; simulates the merge's structural effect (union labels, move rels,
    inherit embedding, delete drop). Cypher-level de-dup fidelity is the integration test's job.
    """

    def __init__(self, nodes: list[FakeNode]) -> None:
        self.nodes = {n.eid: n for n in nodes}
        self.merge_calls = 0

    async def find_owner_ids(self) -> list[tuple[str, str | None]]:
        return [(n.eid, n.name) for n in self.nodes.values() if n.is_owner is True]

    async def find_split_entity_ids(self, owner_name: str, owner_eid: str) -> list[str]:
        return [
            n.eid
            for n in self.nodes.values()
            if "Entity" in n.labels
            and n.name == owner_name
            and n.user_id is None
            and n.eid != owner_eid
        ]

    async def snapshot(self, element_id: str) -> NodeSnapshot | None:
        n = self.nodes.get(element_id)
        if n is None:
            return None
        return NodeSnapshot(
            element_id=n.eid,
            labels=list(n.labels),
            name=n.name,
            is_owner=n.is_owner,
            user_id=n.user_id,
            has_embedding=n.has_embedding,
            rel_counts=dict(n.rel_counts),
        )

    async def count_named(self, name: str) -> int:
        return sum(1 for n in self.nodes.values() if n.name == name)

    async def merge_one(self, keep_eid: str, drop_eid: str) -> str:
        self.merge_calls += 1
        keep = self.nodes[keep_eid]
        drop = self.nodes.pop(drop_eid)
        for label in drop.labels:
            if label not in keep.labels:
                keep.labels.append(label)
        keep.has_embedding = keep.has_embedding or drop.has_embedding
        for rt, c in drop.rel_counts.items():
            keep.rel_counts[rt] = keep.rel_counts.get(rt, 0) + c
        return keep_eid


def _owner(eid: str = "owner", name: str = "Alex", labels: list[str] | None = None) -> FakeNode:
    return FakeNode(
        eid,
        labels if labels is not None else ["Person", "Entity"],
        name,
        is_owner=True,
        user_id="uid-1",
        rel_counts={"OUT:HAS_FACT": 26, "IN:OPERATED_BY": 1},
    )


def _split_entity(eid: str = "ent", name: str = "Alex") -> FakeNode:
    return FakeNode(
        eid,
        ["Entity"],
        name,
        is_owner=None,
        user_id=None,
        has_embedding=True,
        rel_counts={"OUT:USES": 15, "IN:DISCUSSES": 77},
    )


@pytest.mark.asyncio
async def test_merges_split_entity_into_owner() -> None:
    graph = FakeGraph([_owner(labels=["Person"]), _split_entity()])
    report = await run_unify(graph, dry_run=False)

    assert report.success is True
    assert graph.merge_calls == 1
    assert report.named_count_before == 2
    assert report.named_count_after == 1
    assert report.unified is not None
    assert set(report.unified.labels) == {"Person", "Entity"}
    assert report.unified.is_owner is True
    assert report.unified.has_embedding is True  # inherited from the split :Entity
    # relationships from both nodes are present on the unified node
    assert report.unified.rel_counts["IN:DISCUSSES"] == 77
    assert report.unified.rel_counts["OUT:HAS_FACT"] == 26


@pytest.mark.asyncio
async def test_dry_run_writes_nothing() -> None:
    graph = FakeGraph([_owner(labels=["Person"]), _split_entity()])
    report = await run_unify(graph, dry_run=True)

    assert report.success is True
    assert report.dry_run is True
    assert graph.merge_calls == 0
    assert await graph.count_named("Alex") == 2  # untouched
    assert "DRY-RUN" in report.note


@pytest.mark.asyncio
async def test_idempotent_noop_when_already_unified() -> None:
    # Owner already :Person:Entity, no split node — a re-run must be a clean no-op.
    graph = FakeGraph([_owner()])
    report = await run_unify(graph, dry_run=False)

    assert report.success is True
    assert graph.merge_calls == 0
    assert report.named_count_after == 1
    assert "already unified" in report.note


@pytest.mark.asyncio
async def test_merges_multiple_split_entities() -> None:
    graph = FakeGraph([_owner(labels=["Person"]), _split_entity("ent1"), _split_entity("ent2")])
    report = await run_unify(graph, dry_run=False)

    assert report.success is True
    assert graph.merge_calls == 2
    assert report.named_count_after == 1


@pytest.mark.asyncio
async def test_fails_when_no_owner() -> None:
    graph = FakeGraph([_split_entity()])
    report = await run_unify(graph, dry_run=False)

    assert report.success is False
    assert "exactly one is_owner" in report.note


@pytest.mark.asyncio
async def test_fails_when_multiple_owners() -> None:
    graph = FakeGraph([_owner("o1"), _owner("o2")])
    report = await run_unify(graph, dry_run=False)

    assert report.success is False
    assert "found 2" in report.note


@pytest.mark.asyncio
async def test_noop_flags_bare_owner_missing_entity_label() -> None:
    # No split node, but the owner is bare :Person (bootstrap fix not applied) — surfaced, not
    # silently reported unified.
    graph = FakeGraph([_owner(labels=["Person"])])
    report = await run_unify(graph, dry_run=False)

    assert report.success is False
    assert "not :Person:Entity" in report.note


def test_is_unified_predicate() -> None:
    ok = NodeSnapshot("x", ["Person", "Entity"], "Alex", True, "uid", True, {})
    assert _is_unified(ok) is True
    bare = NodeSnapshot("x", ["Person"], "Alex", True, "uid", False, {})
    assert _is_unified(bare) is False  # missing :Entity label
    not_owner = NodeSnapshot("x", ["Person", "Entity"], "Alex", None, "uid", True, {})
    assert _is_unified(not_owner) is False  # not flagged is_owner
    assert _is_unified(None) is False


@pytest.mark.asyncio
async def test_unrelated_same_named_node_does_not_flip_success() -> None:
    """F3 regression: a legitimately-distinct node sharing the owner's name (a contact :Person,
    not a foldable split) must NOT make a correct merge report failure.
    """
    contact = FakeNode("contact", ["Person"], "Alex", is_owner=None, user_id=None)
    graph = FakeGraph([_owner(labels=["Person"]), _split_entity(), contact])
    report = await run_unify(graph, dry_run=False)

    assert report.success is True  # merged despite the same-named contact still existing
    assert report.named_count_after == 2  # owner + contact — count is informational, not gating
    # the contact was never touched (not a user_id-NULL :Entity match... it lacks :Entity)
    assert await graph.count_named("Alex") == 2


def test_print_summary_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    report_node = NodeSnapshot(
        "x", ["Person", "Entity"], "Alex", True, "uid", True, {"OUT:USES": 15}
    )
    from scripts.migrate_fre632_unify_owner_identity import UnifyReport

    r = UnifyReport(
        dry_run=False, unified=report_node, named_count_after=1, success=True, note="ok"
    )
    _print_summary(r)
    out = capsys.readouterr().out
    assert "FRE-632" in out
    assert "success: True" in out
