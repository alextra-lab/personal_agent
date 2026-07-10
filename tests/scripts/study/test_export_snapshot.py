"""Tests for the ADR-0114 D1 frozen-snapshot export script (FRE-838).

Unit-level: mocked Neo4j/Postgres, no real infra. Exercises the pure
transformation/gating logic — the live read/write path is exercised
manually against `make study-infra-up` (see the implementation plan).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from scripts.study.export_snapshot import (
    ExportedNode,
    ExportedRelationship,
    SnapshotCorpus,
    build_manifest,
    build_node_batch_create_cypher,
    build_relationship_batch_create_cypher,
    compute_content_hash,
    is_study_target_uri,
    run_export,
    write_sandbox_corpus,
)


def _corpus(sessions: tuple[dict, ...] = ()) -> SnapshotCorpus:
    return SnapshotCorpus(
        nodes=(
            ExportedNode("n1", ("Entity",), {"name": "Arterial calcification"}),
            ExportedNode("n2", ("Entity",), {"name": "Hypertension"}),
        ),
        relationships=(ExportedRelationship("DISCUSSES", "n1", "n2", {"confidence": 0.9}),),
        sessions=sessions,
    )


# ---------------------------------------------------------------------------
# is_study_target_uri — allowlist gate (codex plan-review: allowlist not denylist)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("bolt://localhost:7691", True),
        ("bolt://127.0.0.1:7691", True),
        ("bolt://localhost:7687", False),  # fre-375-allow: rejected
        ("bolt://neo4j:7691", False),  # internal docker DNS, not loopback
        ("bolt://cloud-sim-neo4j:7687", False),
        ("bolt://localhost:7688", False),  # test-stack port, not study
    ],
)
def test_is_study_target_uri(uri: str, expected: bool) -> None:
    assert is_study_target_uri(uri) is expected


# ---------------------------------------------------------------------------
# compute_content_hash — deterministic, covers conversation traces
# ---------------------------------------------------------------------------


def test_content_hash_is_deterministic_regardless_of_input_order() -> None:
    corpus_a = _corpus()
    corpus_b = SnapshotCorpus(
        nodes=tuple(reversed(corpus_a.nodes)),
        relationships=corpus_a.relationships,
        sessions=corpus_a.sessions,
    )
    assert compute_content_hash(corpus_a) == compute_content_hash(corpus_b)


def test_content_hash_changes_when_conversation_traces_change() -> None:
    baseline = _corpus(sessions=({"session_id": "s1", "messages": ["hello"]},))
    changed = _corpus(sessions=({"session_id": "s1", "messages": ["hello", "world"]},))
    assert compute_content_hash(baseline) != compute_content_hash(changed)


def test_content_hash_changes_when_graph_data_changes() -> None:
    baseline = _corpus()
    changed = SnapshotCorpus(
        nodes=baseline.nodes,
        relationships=(),
        sessions=baseline.sessions,
    )
    assert compute_content_hash(baseline) != compute_content_hash(changed)


# ---------------------------------------------------------------------------
# build_manifest — schema
# ---------------------------------------------------------------------------


def test_manifest_schema_has_all_required_fields() -> None:
    corpus = _corpus(sessions=({"session_id": "s1", "messages": []},))
    manifest = build_manifest(corpus, datetime(2026, 7, 10, tzinfo=timezone.utc), "deadbeef")

    assert manifest["snapshot_date"] == "2026-07-10T00:00:00+00:00"
    assert manifest["content_hash"] == "deadbeef"
    assert manifest["node_counts_by_label"] == {"Entity": 2}
    assert manifest["relationship_counts_by_type"] == {"DISCUSSES": 1}
    assert manifest["prod_node_total"] == 2
    assert manifest["prod_relationship_total"] == 1
    assert manifest["prod_session_count"] == 1


# ---------------------------------------------------------------------------
# build_node_batch_create_cypher / build_relationship_batch_create_cypher —
# UNWIND-batched Cypher shape + guardrails
# ---------------------------------------------------------------------------


def test_build_node_batch_create_cypher_rejects_unrecognized_label() -> None:
    node = ExportedNode("n1", ("SomeUnknownLabel",), {})
    with pytest.raises(ValueError, match="unrecognized node label"):
        build_node_batch_create_cypher(("SomeUnknownLabel",), [node])


def test_build_relationship_batch_create_cypher_rejects_unrecognized_type() -> None:
    rel = ExportedRelationship("SOME_UNKNOWN_REL", "n1", "n2", {})
    with pytest.raises(ValueError, match="unrecognized relationship type"):
        build_relationship_batch_create_cypher("SOME_UNKNOWN_REL", [rel])


def test_build_node_batch_create_cypher_carries_source_element_ids_for_endpoint_mapping() -> None:
    nodes = [
        ExportedNode("n1", ("Entity",), {"name": "Hypertension"}),
        ExportedNode("n2", ("Entity",), {"name": "Arterial calcification"}),
    ]
    query, params = build_node_batch_create_cypher(("Entity",), nodes)
    assert "UNWIND $rows AS row" in query
    assert "_export_source_element_id" in query
    assert {row["source_element_id"] for row in params["rows"]} == {"n1", "n2"}


def test_build_relationship_batch_create_cypher_references_correct_endpoints() -> None:
    rels = [ExportedRelationship("DISCUSSES", "n1", "n2", {"confidence": 0.9})]
    query, params = build_relationship_batch_create_cypher("DISCUSSES", rels)
    assert "UNWIND $rows AS row" in query
    assert params["rows"][0]["start_source_element_id"] == "n1"
    assert params["rows"][0]["end_source_element_id"] == "n2"
    assert "DISCUSSES" in query


# ---------------------------------------------------------------------------
# write_sandbox_corpus — relationship endpoint mapping over a small fixture,
# using a fake driver/session that records every Cypher call instead of a
# real Neo4j instance.
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def run(self, query: str, params: dict | None = None) -> None:
        self.calls.append((query, params or {}))

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeDriver:
    def __init__(self) -> None:
        self.fake_session = _FakeSession()

    def session(self) -> _FakeSession:
        return self.fake_session


@pytest.mark.asyncio
async def test_write_sandbox_corpus_batches_and_maps_relationship_endpoints() -> None:
    corpus = SnapshotCorpus(
        nodes=(
            ExportedNode("a", ("Entity",), {"name": "A"}),
            ExportedNode("b", ("Entity",), {"name": "B"}),
            ExportedNode("c", ("Entity",), {"name": "C"}),
        ),
        relationships=(
            ExportedRelationship("DISCUSSES", "a", "b", {}),
            ExportedRelationship("DISCUSSES", "b", "c", {}),
        ),
        sessions=(),
    )
    driver = _FakeDriver()

    await write_sandbox_corpus(driver, corpus)

    node_calls = [c for c in driver.fake_session.calls if "CREATE (n:" in c[0]]
    rel_calls = [c for c in driver.fake_session.calls if "MATCH (a {" in c[0]]

    # One node all share the same label-set ("Entity",) -> one batched call,
    # not three (code-review finding: no N+1 round-trips).
    assert len(node_calls) == 1
    assert {row["source_element_id"] for row in node_calls[0][1]["rows"]} == {"a", "b", "c"}

    # Both relationships share type DISCUSSES -> one batched call, not two.
    assert len(rel_calls) == 1
    endpoint_pairs = {
        (row["start_source_element_id"], row["end_source_element_id"])
        for row in rel_calls[0][1]["rows"]
    }
    assert endpoint_pairs == {("a", "b"), ("b", "c")}


# ---------------------------------------------------------------------------
# run_export — safety gates (--execute required; target must be allowlisted)
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, execute: bool, snapshot_dir: str = "unused") -> None:
        self.execute = execute
        self.snapshot_dir = snapshot_dir


@pytest.mark.asyncio
async def test_run_export_dry_run_performs_no_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDY_NEO4J_PASSWORD", "study_dev_password")
    monkeypatch.delenv("STUDY_NEO4J_URI", raising=False)

    result = await run_export(_Args(execute=False))

    assert result is None


@pytest.mark.asyncio
async def test_run_export_refuses_non_study_target_even_with_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUDY_NEO4J_PASSWORD", "study_dev_password")
    monkeypatch.setenv("STUDY_NEO4J_URI", "bolt://localhost:7687")  # fre-375-allow: rejected

    result = await run_export(_Args(execute=True))

    assert result is None
