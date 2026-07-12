"""Unit tests for the FRE-868 System-entity eviction sweep orchestration.

These exercise the sweep ALGORITHM against an in-memory fake graph + a fake sysgraph sink — no
Neo4j, no Postgres, so they run in ``make test`` as the CI-gating AC proof. The real Cypher
(:class:`_Neo4jGraph`/:class:`_SysgraphSink`) is exercised by
``test_sweep_fre868_evict_system_entities_integration.py``.

Unlike FRE-865's in-place property backfill, this sweep DELETES nodes, so rollback needs a real
snapshot written durably *before* any mutation (ADR-0115 D3; see the plan doc's design decisions
1/2/7/8/9 for the codex-hardened rationale this suite locks in).
"""

from __future__ import annotations

import json

import pytest
from scripts.sweep_fre868_evict_system_entities import (
    EvictionCandidate,
    NodeSnapshot,
    RelSnapshot,
    SnapshotWriter,
    _decode_value,
    _encode_value,
    run_rollback,
    run_sweep,
)

_RUN_ID = "fre868-test"
_NOW = "2026-07-12T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGraph:
    """In-memory graph seam: a dict of element_id -> node dict, plus relationship dicts.

    A node dict carries: ``name``, ``entity_type``, ``description``, ``class_backfill_output_kind``,
    plus any other property key. Deleting a node actually removes it from ``self.nodes`` so
    candidate-predicate re-fetch reflects the deletion (idempotency proof).
    """

    def __init__(
        self,
        nodes: dict[str, dict[str, object]],
        relationships: list[dict[str, object]] | None = None,
        *,
        fail_restore_rel_ids: set[str] | None = None,
    ) -> None:
        self.nodes = nodes
        self.relationships = list(relationships or [])
        self.deletes = 0
        self.restored_nodes: dict[str, str] = {}  # old_eid -> new_eid
        self.restored_rel_ids: set[str] = set()
        self._next_new_eid = 1000
        self._fail_restore_rel_ids = fail_restore_rel_ids or set()
        self.restore_node_calls = 0
        self.restore_relationship_calls = 0

    async def count_marked(self) -> int:
        return sum(1 for n in self.nodes.values() if n.get("class_backfill_output_kind"))

    async def count_total_entities(self) -> int:
        return len(self.nodes)

    async def fetch_candidates(self, cursor: str | None, limit: int) -> list[EvictionCandidate]:
        candidates = sorted(
            ((eid, n) for eid, n in self.nodes.items() if n.get("class_backfill_output_kind")),
            key=lambda pair: pair[0],
        )
        if cursor is not None:
            candidates = [(eid, n) for eid, n in candidates if eid > cursor]
        return [
            EvictionCandidate(
                element_id=eid,
                name=n["name"],
                entity_type=n.get("entity_type", ""),
                description=n.get("description", ""),
                output_kind=n["class_backfill_output_kind"],
            )
            for eid, n in candidates[:limit]
        ]

    async def snapshot_node(self, element_id: str) -> NodeSnapshot:
        node = self.nodes[element_id]
        rels = []
        for rel in self.relationships:
            if rel["source_eid"] == element_id:
                other_eid = rel["target_eid"]
                outgoing = True
            elif rel["target_eid"] == element_id:
                other_eid = rel["source_eid"]
                outgoing = False
            else:
                continue
            other = self.nodes.get(other_eid, rel.get("other_snapshot_props", {}))
            other_labels = rel.get("other_labels", ["Entity"])
            stable_key, restorable = _stable_key_for(other_labels, other)
            rels.append(
                RelSnapshot(
                    rel_type=rel["type"],
                    outgoing=outgoing,
                    old_rel_element_id=rel["rel_eid"],
                    other_element_id=other_eid,
                    other_labels=other_labels,
                    other_stable_key=stable_key,
                    restorable=restorable,
                    other_properties={k: _encode_value(v) for k, v in dict(other).items()},
                    rel_properties={k: _encode_value(v) for k, v in rel.get("props", {}).items()},
                )
            )
        return NodeSnapshot(
            run_id=_RUN_ID,
            element_id=element_id,
            labels=["Entity"],
            properties={k: _encode_value(v) for k, v in node.items()},
            relationships=rels,
        )

    async def delete_node(self, element_id: str) -> None:
        self.nodes.pop(element_id, None)
        self.deletes += 1

    async def restore_node(self, snapshot: NodeSnapshot) -> str:
        self.restore_node_calls += 1
        if snapshot.element_id in self.restored_nodes:
            return self.restored_nodes[snapshot.element_id]
        new_eid = f"new-{self._next_new_eid}"
        self._next_new_eid += 1
        decoded = {k: _decode_value(v) for k, v in snapshot.properties.items()}
        self.nodes[new_eid] = decoded
        self.restored_nodes[snapshot.element_id] = new_eid
        return new_eid

    async def find_element_id_by_stable_key(self, label: str, key_value: str) -> str | None:
        key_field = "name" if label == "Entity" else "turn_id"
        for eid, n in self.nodes.items():
            if n.get(key_field) == key_value:
                return eid
        return None

    async def restore_relationship(
        self,
        *,
        rel_type: str,
        old_rel_element_id: str,
        start_element_id: str,
        end_element_id: str,
        rel_properties: dict[str, object],
    ) -> bool:
        self.restore_relationship_calls += 1
        if old_rel_element_id in self._fail_restore_rel_ids:
            return False
        if old_rel_element_id in self.restored_rel_ids:
            return True
        self.restored_rel_ids.add(old_rel_element_id)
        return True


def _stable_key_for(
    labels: list[str], props: dict[str, object]
) -> tuple[tuple[str, str] | None, bool]:
    if "Entity" in labels and props.get("name"):
        return ("Entity", props["name"]), True
    if "Turn" in labels and props.get("turn_id"):
        return ("Turn", props["turn_id"]), True
    return None, False


class FakeSysgraph:
    def __init__(self, *, raise_for: set[str] | None = None) -> None:
        self.raise_for = raise_for or set()
        self.recorded: list[dict[str, object]] = []

    async def record_finding(
        self, *, entity_name: str, entity_type: str, description: str | None
    ) -> None:
        if entity_name in self.raise_for:
            raise RuntimeError("sysgraph unavailable")
        self.recorded.append(
            {"entity_name": entity_name, "entity_type": entity_type, "description": description}
        )


class FakeSnapshotWriter:
    """Records write() calls in order, mirroring :class:`SnapshotWriter`'s interface."""

    def __init__(self) -> None:
        self.written: list[NodeSnapshot] = []
        self.flush_before_next_graph_call = False

    def write(self, snapshot: NodeSnapshot) -> None:
        self.written.append(snapshot)


# ---------------------------------------------------------------------------
# Ordering-aware fake — proves snapshot-before-mutation (decisions 1/2)
# ---------------------------------------------------------------------------


class OrderTrackingGraph(FakeGraph):
    """Records the global call order of snapshot vs delete for one candidate."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.call_order: list[str] = []

    async def snapshot_node(self, element_id: str) -> NodeSnapshot:
        self.call_order.append(f"snapshot:{element_id}")
        return await super().snapshot_node(element_id)

    async def delete_node(self, element_id: str) -> None:
        self.call_order.append(f"delete:{element_id}")
        await super().delete_node(element_id)


class OrderTrackingWriter(FakeSnapshotWriter):
    def __init__(self, order_log: list[str]) -> None:
        super().__init__()
        self._order_log = order_log

    def write(self, snapshot: NodeSnapshot) -> None:
        self._order_log.append(f"snapshot_written:{snapshot.element_id}")
        super().write(snapshot)


# ---------------------------------------------------------------------------
# run_sweep tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ephemeral_candidate_is_deleted_with_no_sysgraph_call() -> None:
    graph = FakeGraph({"e1": {"name": "test-scaffold", "class_backfill_output_kind": "ephemeral"}})
    sysgraph = FakeSysgraph()
    writer = FakeSnapshotWriter()

    report = await run_sweep(
        graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=False, batch_size=10
    )

    assert report.evicted_ephemeral == 1
    assert report.evicted_finding == 0
    assert sysgraph.recorded == []
    assert "e1" not in graph.nodes
    assert graph.deletes == 1


@pytest.mark.asyncio
async def test_finding_candidate_dispatches_then_deletes() -> None:
    graph = FakeGraph(
        {
            "e1": {
                "name": "healthcheck-probe",
                "entity_type": "Concept",
                "description": "infra self-check",
                "class_backfill_output_kind": "finding",
            }
        }
    )
    sysgraph = FakeSysgraph()
    writer = FakeSnapshotWriter()

    report = await run_sweep(
        graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=False, batch_size=10
    )

    assert report.evicted_finding == 1
    assert sysgraph.recorded == [
        {
            "entity_name": "healthcheck-probe",
            "entity_type": "Concept",
            "description": "infra self-check",
        }
    ]
    assert "e1" not in graph.nodes


@pytest.mark.asyncio
async def test_finding_dispatch_failure_leaves_node_untouched() -> None:
    graph = FakeGraph({"e1": {"name": "flaky-finding", "class_backfill_output_kind": "finding"}})
    sysgraph = FakeSysgraph(raise_for={"flaky-finding"})
    writer = FakeSnapshotWriter()

    report = await run_sweep(
        graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=False, batch_size=10
    )

    assert report.dispatch_finding_failed == 1
    assert report.evicted_finding == 0
    assert "e1" in graph.nodes  # untouched — marker still present, retried next run
    assert graph.deletes == 0


@pytest.mark.asyncio
async def test_unmarked_entities_are_never_fetched_as_candidates() -> None:
    graph = FakeGraph(
        {
            "e1": {"name": "knowledge-fact", "class": "World"},
            "e2": {"name": "still-null-class"},
        }
    )
    sysgraph = FakeSysgraph()
    writer = FakeSnapshotWriter()

    report = await run_sweep(
        graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=False, batch_size=10
    )

    assert report.total_candidates_this_run == 0
    assert len(graph.nodes) == 2


@pytest.mark.asyncio
async def test_idempotent_rerun_finds_zero_candidates_after_eviction() -> None:
    graph = FakeGraph(
        {
            "e1": {"name": "a", "class_backfill_output_kind": "ephemeral"},
            "e2": {"name": "b", "class_backfill_output_kind": "finding"},
        }
    )
    sysgraph = FakeSysgraph()
    writer = FakeSnapshotWriter()

    first = await run_sweep(
        graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=False, batch_size=10
    )
    assert first.total_candidates_this_run == 2

    second = await run_sweep(
        graph, sysgraph, writer, run_id="fre868-test-2", now=_NOW, dry_run=False, batch_size=10
    )
    assert second.total_candidates_this_run == 0
    assert second.evicted_ephemeral == 0
    assert second.evicted_finding == 0


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_previews_counts() -> None:
    graph = FakeGraph(
        {
            "e1": {"name": "a", "class_backfill_output_kind": "ephemeral"},
            "e2": {"name": "b", "class_backfill_output_kind": "finding"},
        }
    )
    sysgraph = FakeSysgraph()
    writer = FakeSnapshotWriter()

    report = await run_sweep(
        graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=True, batch_size=10
    )

    assert report.evicted_ephemeral == 1
    assert report.evicted_finding == 1
    assert graph.deletes == 0
    assert sysgraph.recorded == []
    assert writer.written == []
    assert len(graph.nodes) == 2  # nothing actually removed


@pytest.mark.asyncio
async def test_unrecognized_marker_value_is_skipped_not_deleted() -> None:
    graph = FakeGraph({"e1": {"name": "weird", "class_backfill_output_kind": "knowledge"}})
    sysgraph = FakeSysgraph()
    writer = FakeSnapshotWriter()

    report = await run_sweep(
        graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=False, batch_size=10
    )

    assert report.unrecognized_marker_count == 1
    assert report.evicted_ephemeral == 0
    assert report.evicted_finding == 0
    assert "e1" in graph.nodes
    assert graph.deletes == 0


@pytest.mark.asyncio
async def test_snapshot_captures_relationships_with_rel_id_and_stable_key() -> None:
    graph = FakeGraph(
        nodes={
            "e1": {"name": "marked-a", "class_backfill_output_kind": "ephemeral"},
            "e2": {"name": "marked-b", "class_backfill_output_kind": "finding"},
            "t1": {"turn_id": "turn-abc"},
        },
        relationships=[
            {
                "rel_eid": "r1",
                "type": "RELATED_TO",
                "source_eid": "e1",
                "target_eid": "e2",
                "props": {"weight": 0.5},
                "other_labels": ["Entity"],
            },
            {
                "rel_eid": "r2",
                "type": "DISCUSSES",
                "source_eid": "t1",
                "target_eid": "e1",
                "props": {},
                "other_labels": ["Turn"],
            },
        ],
    )
    snapshot = await graph.snapshot_node("e1")

    by_type = {r.rel_type: r for r in snapshot.relationships}
    assert by_type["RELATED_TO"].old_rel_element_id == "r1"
    assert by_type["RELATED_TO"].other_stable_key == ("Entity", "marked-b")
    assert by_type["RELATED_TO"].restorable is True
    assert by_type["DISCUSSES"].other_stable_key == ("Turn", "turn-abc")
    assert by_type["DISCUSSES"].restorable is True


@pytest.mark.asyncio
async def test_snapshot_flags_unsupported_neighbor_label_as_not_restorable() -> None:
    graph = FakeGraph(
        nodes={"e1": {"name": "marked-a", "class_backfill_output_kind": "ephemeral"}},
        relationships=[
            {
                "rel_eid": "r1",
                "type": "OWNS",
                "source_eid": "e1",
                "target_eid": "p1",
                "props": {},
                "other_labels": ["Person"],
                "other_snapshot_props": {"user_id": "u1"},
            },
        ],
    )
    snapshot = await graph.snapshot_node("e1")

    assert snapshot.relationships[0].restorable is False
    assert snapshot.relationships[0].other_stable_key is None


def test_datetime_property_round_trips_through_type_tagging() -> None:
    from datetime import datetime, timezone

    original = datetime(2026, 7, 12, 3, 4, 5, tzinfo=timezone.utc)
    encoded = _encode_value(original)
    assert encoded != original  # tagged, not passed through raw
    decoded = _decode_value(encoded)
    assert decoded == original


def test_plain_scalar_and_list_values_pass_through_unchanged() -> None:
    for value in ["hello", 42, 3.14, True, None, [1.0, 2.0, 3.0]]:
        assert _decode_value(_encode_value(value)) == value


@pytest.mark.asyncio
async def test_snapshot_is_written_durably_before_mutation() -> None:
    graph = OrderTrackingGraph({"e1": {"name": "a", "class_backfill_output_kind": "ephemeral"}})
    sysgraph = FakeSysgraph()
    order_log: list[str] = []
    writer = OrderTrackingWriter(order_log)

    await run_sweep(graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=False, batch_size=10)

    combined_order = order_log + [c for c in graph.call_order if c.startswith("delete")]
    # snapshot_node happens first (graph.call_order), then the write, then delete.
    assert graph.call_order[0] == "snapshot:e1"
    assert order_log[0] == "snapshot_written:e1"
    assert graph.call_order[-1] == "delete:e1"


@pytest.mark.asyncio
async def test_applying_run_without_snapshot_path_refuses(tmp_path) -> None:
    from scripts.sweep_fre868_evict_system_entities import _require_snapshot_path_for_apply

    with pytest.raises(SystemExit):
        _require_snapshot_path_for_apply(snapshot_path=None, dry_run=False, rollback=False)

    # dry-run is exempt
    _require_snapshot_path_for_apply(snapshot_path=None, dry_run=True, rollback=False)
    # an explicit path is fine
    _require_snapshot_path_for_apply(
        snapshot_path=tmp_path / "snap.jsonl", dry_run=False, rollback=False
    )


# ---------------------------------------------------------------------------
# SnapshotWriter (real file I/O) — flush/fsync contract
# ---------------------------------------------------------------------------


def test_snapshot_writer_appends_one_json_line_per_snapshot(tmp_path) -> None:
    path = tmp_path / "snap.jsonl"
    writer = SnapshotWriter(path)
    snap = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e1",
        labels=["Entity"],
        properties={"name": "x"},
        relationships=[],
    )
    writer.write(snap)
    writer.write(snap)
    writer.close()

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        row = json.loads(line)
        assert row["run_id"] == _RUN_ID
        assert row["element_id"] == "e1"


# ---------------------------------------------------------------------------
# run_rollback tests
# ---------------------------------------------------------------------------


def _write_snapshots(path, snapshots: list[NodeSnapshot]) -> None:
    writer = SnapshotWriter(path)
    for snap in snapshots:
        writer.write(snap)
    writer.close()


@pytest.mark.asyncio
async def test_rollback_recreates_node_and_reconnects_existing_neighbor(tmp_path) -> None:
    path = tmp_path / "snap.jsonl"
    snap = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e1",
        labels=["Entity"],
        properties={"name": _encode_value("marked-a")},
        relationships=[
            RelSnapshot(
                rel_type="DISCUSSES",
                outgoing=False,
                old_rel_element_id="r1",
                other_element_id="t1",
                other_labels=["Turn"],
                other_stable_key=("Turn", "turn-abc"),
                restorable=True,
                other_properties={},
                rel_properties={},
            )
        ],
    )
    _write_snapshots(path, [snap])

    graph = FakeGraph({"t1": {"turn_id": "turn-abc"}})
    restored_nodes, restored_rels, skipped = await run_rollback(graph, path, _RUN_ID)

    assert restored_nodes == 1
    assert restored_rels == 1
    assert skipped == []
    assert graph.restored_rel_ids == {"r1"}


@pytest.mark.asyncio
async def test_rollback_is_idempotent_across_two_calls(tmp_path) -> None:
    path = tmp_path / "snap.jsonl"
    snap = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e1",
        labels=["Entity"],
        properties={"name": _encode_value("marked-a")},
        relationships=[],
    )
    _write_snapshots(path, [snap])

    graph = FakeGraph({})
    first = await run_rollback(graph, path, _RUN_ID)
    second = await run_rollback(graph, path, _RUN_ID)

    assert first == second
    assert len(graph.nodes) == 1  # no duplicate node created on the second call


@pytest.mark.asyncio
async def test_rollback_dedupes_both_ends_evicted_relationship(tmp_path) -> None:
    path = tmp_path / "snap.jsonl"
    rel_from_a = RelSnapshot(
        rel_type="RELATED_TO",
        outgoing=True,
        old_rel_element_id="r1",
        other_element_id="e2",
        other_labels=["Entity"],
        other_stable_key=("Entity", "marked-b"),
        restorable=True,
        other_properties={},
        rel_properties={},
    )
    rel_from_b = RelSnapshot(
        rel_type="RELATED_TO",
        outgoing=False,
        old_rel_element_id="r1",
        other_element_id="e1",
        other_labels=["Entity"],
        other_stable_key=("Entity", "marked-a"),
        restorable=True,
        other_properties={},
        rel_properties={},
    )
    snap_a = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e1",
        labels=["Entity"],
        properties={"name": _encode_value("marked-a")},
        relationships=[rel_from_a],
    )
    snap_b = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e2",
        labels=["Entity"],
        properties={"name": _encode_value("marked-b")},
        relationships=[rel_from_b],
    )
    _write_snapshots(path, [snap_a, snap_b])

    graph = FakeGraph({})
    restored_nodes, restored_rels, skipped = await run_rollback(graph, path, _RUN_ID)

    assert restored_nodes == 2
    assert restored_rels == 1  # not 2 — deduped via old_rel_element_id
    assert skipped == []


@pytest.mark.asyncio
async def test_rollback_reports_unresolvable_relationship_as_skipped(tmp_path) -> None:
    path = tmp_path / "snap.jsonl"
    snap = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e1",
        labels=["Entity"],
        properties={"name": _encode_value("marked-a")},
        relationships=[
            RelSnapshot(
                rel_type="RELATED_TO",
                outgoing=True,
                old_rel_element_id="r1",
                other_element_id="gone",
                other_labels=["Entity"],
                other_stable_key=("Entity", "no-longer-exists"),
                restorable=True,
                other_properties={},
                rel_properties={},
            )
        ],
    )
    _write_snapshots(path, [snap])

    graph = FakeGraph({})  # the neighbor was independently deleted — no match by stable key
    restored_nodes, restored_rels, skipped = await run_rollback(graph, path, _RUN_ID)

    assert restored_nodes == 1
    assert restored_rels == 0
    assert len(skipped) == 1
    assert "RELATED_TO" in skipped[0]


@pytest.mark.asyncio
async def test_rollback_reports_non_restorable_relationship_without_attempting(tmp_path) -> None:
    path = tmp_path / "snap.jsonl"
    snap = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e1",
        labels=["Entity"],
        properties={"name": _encode_value("marked-a")},
        relationships=[
            RelSnapshot(
                rel_type="OWNS",
                outgoing=True,
                old_rel_element_id="r1",
                other_element_id="p1",
                other_labels=["Person"],
                other_stable_key=None,
                restorable=False,
                other_properties={},
                rel_properties={},
            )
        ],
    )
    _write_snapshots(path, [snap])

    graph = FakeGraph({})
    restored_nodes, restored_rels, skipped = await run_rollback(graph, path, _RUN_ID)

    assert restored_nodes == 1
    assert restored_rels == 0
    assert len(skipped) == 1
    assert "unsupported" in skipped[0].lower() or "restorable" in skipped[0].lower()


# ---------------------------------------------------------------------------
# Regression tests — code-review-confirmed findings (fixed on this branch before PR)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_dry_run_issues_zero_graph_writes(tmp_path) -> None:
    """A code review caught `--rollback --dry-run` performing real writes — dry_run was accepted
    by run_rollback's signature but never threaded past it. Locks in the fix: dry_run=True must
    never call restore_node/restore_relationship, while still previewing accurate counts.
    """
    path = tmp_path / "snap.jsonl"
    snap = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e1",
        labels=["Entity"],
        properties={"name": _encode_value("marked-a")},
        relationships=[
            RelSnapshot(
                rel_type="DISCUSSES",
                outgoing=False,
                old_rel_element_id="r1",
                other_element_id="t1",
                other_labels=["Turn"],
                other_stable_key=("Turn", "turn-abc"),
                restorable=True,
                other_properties={},
                rel_properties={},
            )
        ],
    )
    _write_snapshots(path, [snap])

    graph = FakeGraph({"t1": {"turn_id": "turn-abc"}})
    restored_nodes, restored_rels, skipped = await run_rollback(graph, path, _RUN_ID, dry_run=True)

    assert restored_nodes == 1
    assert restored_rels == 1
    assert skipped == []
    assert graph.restore_node_calls == 0
    assert graph.restore_relationship_calls == 0
    assert "e1" not in graph.nodes  # nothing actually created
    assert graph.restored_rel_ids == set()


@pytest.mark.asyncio
async def test_rollback_dry_run_still_reports_unresolvable_relationships(tmp_path) -> None:
    path = tmp_path / "snap.jsonl"
    snap = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e1",
        labels=["Entity"],
        properties={"name": _encode_value("marked-a")},
        relationships=[
            RelSnapshot(
                rel_type="RELATED_TO",
                outgoing=True,
                old_rel_element_id="r1",
                other_element_id="gone",
                other_labels=["Entity"],
                other_stable_key=("Entity", "no-longer-exists"),
                restorable=True,
                other_properties={},
                rel_properties={},
            )
        ],
    )
    _write_snapshots(path, [snap])

    graph = FakeGraph({})
    restored_nodes, restored_rels, skipped = await run_rollback(graph, path, _RUN_ID, dry_run=True)

    assert restored_nodes == 1
    assert restored_rels == 0
    assert len(skipped) == 1


@pytest.mark.asyncio
async def test_rollback_counts_and_reports_a_failed_relationship_restore(tmp_path) -> None:
    """A code review caught restore_relationship() returning False being silently dropped —
    neither counted as restored nor added to `skipped`. Locks in the fix: a failed restore must
    show up in `skipped`, and must not inflate `restored_rel_count`.
    """
    path = tmp_path / "snap.jsonl"
    snap = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e1",
        labels=["Entity"],
        properties={"name": _encode_value("marked-a")},
        relationships=[
            RelSnapshot(
                rel_type="DISCUSSES",
                outgoing=False,
                old_rel_element_id="r1",
                other_element_id="t1",
                other_labels=["Turn"],
                other_stable_key=("Turn", "turn-abc"),
                restorable=True,
                other_properties={},
                rel_properties={},
            )
        ],
    )
    _write_snapshots(path, [snap])

    graph = FakeGraph({"t1": {"turn_id": "turn-abc"}}, fail_restore_rel_ids={"r1"})
    restored_nodes, restored_rels, skipped = await run_rollback(graph, path, _RUN_ID)

    assert restored_nodes == 1
    assert restored_rels == 0  # not counted as restored
    assert len(skipped) == 1  # but reported, not silently dropped
    assert "DISCUSSES" in skipped[0]


@pytest.mark.asyncio
async def test_rollback_dedupes_non_restorable_relationship_from_both_evicted_ends(
    tmp_path,
) -> None:
    """A code review caught seen_rel_ids only being marked on the restorable path — a
    non-restorable relationship captured from BOTH evicted endpoints' snapshots (same
    old_rel_element_id) was reported in `skipped` twice instead of once.
    """
    path = tmp_path / "snap.jsonl"
    rel_from_a = RelSnapshot(
        rel_type="OWNS",
        outgoing=True,
        old_rel_element_id="r1",
        other_element_id="e2",
        other_labels=["Person"],
        other_stable_key=None,
        restorable=False,
        other_properties={},
        rel_properties={},
    )
    rel_from_b = RelSnapshot(
        rel_type="OWNS",
        outgoing=False,
        old_rel_element_id="r1",
        other_element_id="e1",
        other_labels=["Person"],
        other_stable_key=None,
        restorable=False,
        other_properties={},
        rel_properties={},
    )
    snap_a = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e1",
        labels=["Entity"],
        properties={"name": _encode_value("marked-a")},
        relationships=[rel_from_a],
    )
    snap_b = NodeSnapshot(
        run_id=_RUN_ID,
        element_id="e2",
        labels=["Person"],
        properties={},
        relationships=[rel_from_b],
    )
    _write_snapshots(path, [snap_a, snap_b])

    graph = FakeGraph({})
    restored_nodes, restored_rels, skipped = await run_rollback(graph, path, _RUN_ID)

    assert restored_nodes == 2
    assert restored_rels == 0
    assert len(skipped) == 1  # not 2 — deduped via old_rel_element_id


def test_date_property_round_trips_as_date_not_datetime() -> None:
    """A code review caught _decode_value collapsing every temporal tag into a full
    datetime.fromisoformat() call — a bare neo4j.time.Date property was silently upcast to a
    DateTime on restore. Locks in the fix: a Date stays a Date.
    """
    from neo4j.time import Date, DateTime

    original_date = Date(2026, 7, 12)
    decoded_date = _decode_value(_encode_value(original_date))
    assert isinstance(decoded_date, Date)
    assert not isinstance(decoded_date, DateTime)
    assert decoded_date == original_date

    original_dt = DateTime(2026, 7, 12, 3, 4, 5)
    decoded_dt = _decode_value(_encode_value(original_dt))
    assert isinstance(decoded_dt, DateTime)
    assert decoded_dt == original_dt


@pytest.mark.asyncio
async def test_success_is_false_when_a_finding_dispatch_fails() -> None:
    """A code review caught SweepReport.success never being set False anywhere in run_sweep —
    the documented `0 if report.success else 4` CLI exit-code contract was dead code.
    """
    graph = FakeGraph({"e1": {"name": "flaky", "class_backfill_output_kind": "finding"}})
    sysgraph = FakeSysgraph(raise_for={"flaky"})
    writer = FakeSnapshotWriter()

    report = await run_sweep(
        graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=False, batch_size=10
    )

    assert report.dispatch_finding_failed == 1
    assert report.success is False


@pytest.mark.asyncio
async def test_success_is_false_when_an_unrecognized_marker_is_seen() -> None:
    graph = FakeGraph({"e1": {"name": "weird", "class_backfill_output_kind": "knowledge"}})
    sysgraph = FakeSysgraph()
    writer = FakeSnapshotWriter()

    report = await run_sweep(
        graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=False, batch_size=10
    )

    assert report.unrecognized_marker_count == 1
    assert report.success is False


@pytest.mark.asyncio
async def test_success_is_true_for_a_clean_run() -> None:
    graph = FakeGraph({"e1": {"name": "a", "class_backfill_output_kind": "ephemeral"}})
    sysgraph = FakeSysgraph()
    writer = FakeSnapshotWriter()

    report = await run_sweep(
        graph, sysgraph, writer, run_id=_RUN_ID, now=_NOW, dry_run=False, batch_size=10
    )

    assert report.success is True
