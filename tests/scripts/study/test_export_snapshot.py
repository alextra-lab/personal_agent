"""Tests for the ADR-0114 D1 frozen-snapshot export script (FRE-838).

Unit-level: mocked Neo4j/Postgres, no real infra. Exercises the pure
transformation/gating logic — the live read/write path is exercised
manually against `make study-infra-up` (see the implementation plan).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from scripts.study.export_snapshot import (
    ExportedNode,
    ExportedRelationship,
    SnapshotCorpus,
    _discover_node_labels,
    _discover_relationship_types,
    _validate_cypher_identifier,
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
    assert manifest["skipped_relationships"] == 0


# ---------------------------------------------------------------------------
# build_node_batch_create_cypher / build_relationship_batch_create_cypher —
# UNWIND-batched Cypher shape + guardrails
# ---------------------------------------------------------------------------


def test_build_node_batch_create_cypher_accepts_any_safe_label_not_just_a_known_list() -> None:
    """FRE-838 fix-forward: labels are discovered dynamically from prod, not
    checked against a hardcoded list — a label never seen before must still
    be accepted as long as it's a safe Cypher identifier (this is exactly
    the defect class that silently dropped 12,480 relationships: a closed
    allowlist masquerading as a completeness check).
    """
    node = ExportedNode("n1", ("SomeLabelNeverSeenBefore",), {})
    query, params = build_node_batch_create_cypher(("SomeLabelNeverSeenBefore",), [node])
    assert "SomeLabelNeverSeenBefore" in query


def test_build_node_batch_create_cypher_rejects_unsafe_label() -> None:
    node = ExportedNode("n1", ("Entity) DETACH DELETE (n",), {})
    with pytest.raises(ValueError, match="unsafe node label"):
        build_node_batch_create_cypher(("Entity) DETACH DELETE (n",), [node])


def test_build_relationship_batch_create_cypher_accepts_any_safe_type_not_just_a_known_list() -> (
    None
):
    rel = ExportedRelationship("RELATED_TO", "n1", "n2", {})
    query, params = build_relationship_batch_create_cypher(
        "RELATED_TO", [rel], {"n1": "id1", "n2": "id2"}
    )
    assert "RELATED_TO" in query


def test_build_relationship_batch_create_cypher_rejects_unsafe_type() -> None:
    rel = ExportedRelationship("DISCUSSES]->(n) DETACH DELETE (n", "n1", "n2", {})
    with pytest.raises(ValueError, match="unsafe relationship type"):
        build_relationship_batch_create_cypher(
            "DISCUSSES]->(n) DETACH DELETE (n", [rel], {"n1": "id1", "n2": "id2"}
        )


def test_build_node_batch_create_cypher_carries_source_element_ids_for_endpoint_mapping() -> None:
    nodes = [
        ExportedNode("n1", ("Entity",), {"name": "Hypertension"}),
        ExportedNode("n2", ("Entity",), {"name": "Arterial calcification"}),
    ]
    query, params = build_node_batch_create_cypher(("Entity",), nodes)
    assert "UNWIND $rows AS row" in query
    assert "_export_source_element_id" in query
    assert "RETURN row.source_element_id AS old_id, elementId(n) AS new_id" in query
    assert {row["source_element_id"] for row in params["rows"]} == {"n1", "n2"}


def test_build_relationship_batch_create_cypher_references_correct_endpoints() -> None:
    rels = [ExportedRelationship("DISCUSSES", "n1", "n2", {"confidence": 0.9})]
    id_map = {"n1": "sandbox-id-1", "n2": "sandbox-id-2"}

    query, params = build_relationship_batch_create_cypher("DISCUSSES", rels, id_map)

    assert "UNWIND $rows AS row" in query
    assert "elementId(a) = row.start_new_id" in query
    assert params["rows"][0]["start_new_id"] == "sandbox-id-1"
    assert params["rows"][0]["end_new_id"] == "sandbox-id-2"
    assert "DISCUSSES" in query


def test_build_relationship_batch_create_cypher_raises_on_missing_mapping() -> None:
    rels = [ExportedRelationship("DISCUSSES", "n1", "unmapped", {})]
    with pytest.raises(ValueError, match="Missing sandbox node mapping"):
        build_relationship_batch_create_cypher("DISCUSSES", rels, {"n1": "sandbox-id-1"})


# ---------------------------------------------------------------------------
# _validate_cypher_identifier / _discover_node_labels / _discover_relationship_types
# — the FRE-838 fix-forward: discovery replaces a hardcoded enumeration,
# the regex check is an injection guard only.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Entity", "RELATED_TO", "SomeCamelCaseLabel", "_leading_underscore", "USEs"],
)
def test_validate_cypher_identifier_accepts_safe_names(name: str) -> None:
    assert _validate_cypher_identifier(name, "node label") == name


@pytest.mark.parametrize(
    "name",
    [
        "Entity) DETACH DELETE (n",
        "Entity`; DROP",
        "has space",
        "trailing-dash",
        "",
    ],
)
def test_validate_cypher_identifier_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        _validate_cypher_identifier(name, "node label")


class _FakeResult:
    def __init__(self, records: list[dict]) -> None:
        self._records = records

    def __aiter__(self) -> AsyncIterator[dict]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[dict]:
        for record in self._records:
            yield record


class _FakeDiscoverySession:
    """A session whose `run` returns canned db.labels()/db.relationshipTypes() rows."""

    def __init__(self, labels: list[str], rel_types: list[str]) -> None:
        self._labels = labels
        self._rel_types = rel_types

    async def run(self, query: str, parameters: dict | None = None) -> _FakeResult:
        if "db.labels()" in query:
            return _FakeResult([{"label": label} for label in self._labels])
        if "db.relationshipTypes()" in query:
            return _FakeResult([{"relationshipType": t} for t in self._rel_types])
        raise AssertionError(f"unexpected query: {query}")


@pytest.mark.asyncio
async def test_discover_node_labels_returns_every_label_not_a_fixed_set() -> None:
    # Deliberately includes labels that never appeared in the old hardcoded
    # PROD_NODE_LABELS list this fix-forward removed.
    session = _FakeDiscoverySession(labels=["Entity", "SomeFutureLabel"], rel_types=[])
    assert await _discover_node_labels(session) == ["Entity", "SomeFutureLabel"]


@pytest.mark.asyncio
async def test_discover_relationship_types_returns_every_type_not_a_fixed_set() -> None:
    # The exact defect this fix-forward closes: RELATED_TO/USES/etc. were
    # never in the old hardcoded PROD_RELATIONSHIP_TYPES list, so they were
    # silently dropped (master's finding, FRE-838, 2026-07-10: 12,480 of
    # 34,301 real prod relationships lost this way).
    session = _FakeDiscoverySession(
        labels=[],
        rel_types=["DISCUSSES", "RELATED_TO", "USES", "PART_OF", "SIMILAR_TO"],
    )
    assert await _discover_relationship_types(session) == [
        "DISCUSSES",
        "RELATED_TO",
        "USES",
        "PART_OF",
        "SIMILAR_TO",
    ]


@pytest.mark.asyncio
async def test_discover_relationship_types_rejects_unsafe_discovered_name() -> None:
    session = _FakeDiscoverySession(labels=[], rel_types=["FINE", "bad; DROP"])
    with pytest.raises(ValueError, match="unsafe relationship type"):
        await _discover_relationship_types(session)


# ---------------------------------------------------------------------------
# write_sandbox_corpus — relationship endpoint mapping over a small fixture,
# using a fake driver/session that records every Cypher call instead of a
# real Neo4j instance.
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def run(self, query: str, params: dict | None = None) -> _FakeResult:
        params = params or {}
        self.calls.append((query, params))
        if "RETURN row.source_element_id AS old_id" in query:
            # Simulate Neo4j assigning a fresh sandbox element id per node,
            # so the relationship-write phase exercises real id-map lookup.
            return _FakeResult(
                [
                    {
                        "old_id": row["source_element_id"],
                        "new_id": f"sandbox-{row['source_element_id']}",
                    }
                    for row in params.get("rows", [])
                ]
            )
        return _FakeResult([])

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

    skipped = await write_sandbox_corpus(driver, corpus)

    node_calls = [c for c in driver.fake_session.calls if "CREATE (n:" in c[0]]
    rel_calls = [c for c in driver.fake_session.calls if "elementId(a) = row.start_new_id" in c[0]]

    # One node all share the same label-set ("Entity",) -> one batched call,
    # not three (code-review finding: no N+1 round-trips).
    assert len(node_calls) == 1
    assert {row["source_element_id"] for row in node_calls[0][1]["rows"]} == {"a", "b", "c"}

    # Both relationships share type DISCUSSES -> one batched call, not two.
    assert len(rel_calls) == 1
    endpoint_pairs = {(row["start_new_id"], row["end_new_id"]) for row in rel_calls[0][1]["rows"]}
    # Endpoints resolved via the id_map captured from node creation
    # (sandbox-a/sandbox-b/sandbox-c per the fake driver above), not the
    # original prod ids directly — proves the elementId indirection works.
    assert endpoint_pairs == {("sandbox-a", "sandbox-b"), ("sandbox-b", "sandbox-c")}
    assert skipped == 0


@pytest.mark.asyncio
async def test_write_sandbox_corpus_skips_unresolvable_relationship_endpoints() -> None:
    """Self-review follow-up (FRE-838): a relationship whose endpoint node
    wasn't exported (e.g. prod wasn't perfectly quiesced between the
    node-read and relationship-read passes) must be skipped and counted,
    not crash mid-write after earlier batches already committed.
    """
    corpus = SnapshotCorpus(
        nodes=(
            ExportedNode("a", ("Entity",), {"name": "A"}),
            ExportedNode("b", ("Entity",), {"name": "B"}),
        ),
        relationships=(
            ExportedRelationship("DISCUSSES", "a", "b", {}),
            ExportedRelationship("DISCUSSES", "a", "never-exported", {}),
        ),
        sessions=(),
    )
    driver = _FakeDriver()

    skipped = await write_sandbox_corpus(driver, corpus)

    assert skipped == 1
    rel_calls = [c for c in driver.fake_session.calls if "elementId(a) = row.start_new_id" in c[0]]
    assert len(rel_calls) == 1
    assert len(rel_calls[0][1]["rows"]) == 1  # only the resolvable relationship was written


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


# ---------------------------------------------------------------------------
# Direct-path invocation regression (FRE-838 fix-forward, 2026-07-10): the
# documented README command `python scripts/study/export_snapshot.py`
# crashed with ModuleNotFoundError because direct-path execution doesn't put
# the repo root on sys.path — only `-m` invocation or pytest's own rootdir
# insertion do, which is exactly why in-process tests never caught it. This
# runs the real command as a subprocess to exercise the actual sys.path a
# user gets.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_direct_path_invocation_dry_run_does_not_crash() -> None:
    env = os.environ.copy()
    for var in (
        "STUDY_NEO4J_PASSWORD",
        "STUDY_NEO4J_URI",
        "AGENT_NEO4J_URI",
        "AGENT_NEO4J_PASSWORD",
    ):
        env.pop(var, None)

    result = subprocess.run(
        [sys.executable, "scripts/study/export_snapshot.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "Dry run" in result.stdout


def test_direct_path_invocation_execute_path_imports_cleanly() -> None:
    """The actually-broken path (master's finding, 2026-07-10): dry run never
    imports ``scripts.study.config`` (it returns before that point), so a
    dry-run-only check does not exercise the bug at all — this is exactly
    the gap that let the original ModuleNotFoundError regression through.
    ``--execute`` forces the import; STUDY_NEO4J_URI is deliberately a
    non-study port so this exercises the import and the allowlist-refusal
    path without needing a real Neo4j connection.
    """
    env = os.environ.copy()
    env["STUDY_NEO4J_PASSWORD"] = "study_dev_password"
    env["STUDY_NEO4J_URI"] = "bolt://localhost:9999"  # deliberately not the study port

    result = subprocess.run(
        [sys.executable, "scripts/study/export_snapshot.py", "--execute"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "Refusing to run" in result.stderr
