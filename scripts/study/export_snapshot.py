"""One-time frozen export of the prod KG + conversation traces (FRE-838).

Reads prod Neo4j (entities/relationships) and prod Postgres (``sessions``
table, the conversation traces) **read-only**, and writes a 1:1 copy into
the isolated study Neo4j sandbox (see ``docker-compose.study.yml`` /
``make study-infra-up``). Writes a ``snapshot_manifest.json`` recording the
snapshot date and a content hash covering the full corpus (graph data AND
conversation traces), so later parameter sweeps (FRE-839+) can verify they
are running against the exact same frozen corpus.

Safety:
  - Dry run by default — pass ``--execute`` to actually write to the sandbox.
  - Refuses to run (even with ``--execute``) unless the resolved target URI
    positively matches the study substrate (an allowlist check, not merely
    "not prod") — see ``is_study_target_uri``.
  - No raw corpus content ever touches disk; only the small manifest
    (date/hash/counts) is written to ``--snapshot-dir``.

Usage:
    uv run python scripts/study/export_snapshot.py                # dry run
    uv run python scripts/study/export_snapshot.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import structlog

if __package__ in (None, ""):
    # Direct-path invocation (`python scripts/study/export_snapshot.py`,
    # as documented in the README) doesn't put the repo root on sys.path —
    # only `-m scripts.study.export_snapshot` or pytest's rootdir insertion
    # do — so the deferred `from scripts.study.config import ...` imports
    # below would fail with ModuleNotFoundError (FRE-838 fix-forward,
    # 2026-07-10: the documented direct-path runbook command didn't
    # actually work; only the module form did).
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

log = structlog.get_logger(__name__)

# Fix-forward, FRE-838 (2026-07-10, second master verification): node
# labels and relationship types are discovered DYNAMICALLY from prod via
# `CALL db.labels()` / `CALL db.relationshipTypes()` (see
# `_discover_node_labels` / `_discover_relationship_types` below) — NOT
# enumerated from a hardcoded list. A prior version hardcoded
# PROD_RELATIONSHIP_TYPES as "every prod relationship type" from reading
# src/personal_agent/memory/service.py; that claim was false — it missed
# the entity-to-entity edges created by the entity-linking/consolidation
# path (RELATED_TO, USES, PART_OF, SIMILAR_TO, LOCATED_IN, CREATED_BY,
# CAUSES), silently dropping 12,480 of 34,301 real prod relationships
# (36%) — exactly the associative entity-to-entity structure ADR-0114
# exists to study. A closed list can go stale the moment prod's schema
# grows; discovery-from-source is complete by construction.
#
# `_SAFE_CYPHER_IDENTIFIER_RE` remains as the injection guard: label and
# relationship-type names cannot be parameterized in Cypher (interpolated
# into the query text instead), so every discovered name is validated as
# a bare identifier before use — this is unrelated to whether the name is
# "expected", only whether it's safe to interpolate.
_SAFE_CYPHER_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1"})


def _validate_cypher_identifier(name: str, kind: str) -> str:
    """Guard against Cypher injection when interpolating *name*.

    *name* is a node label or relationship type; Cypher can't parameterize
    either, so it's interpolated into the query string. Raises
    ``ValueError`` unless *name* is a safe bare identifier — this is an
    injection guard, not a completeness/allowlist check (fix-forward,
    FRE-838).
    """
    if not _SAFE_CYPHER_IDENTIFIER_RE.match(name):
        raise ValueError(f"Refusing to export unsafe {kind} name: {name!r}")
    return name


async def _discover_node_labels(session: Neo4jSession) -> list[str]:
    """Every node label present in the source graph, via ``CALL db.labels()``."""
    result = await session.run("CALL db.labels() YIELD label RETURN label")
    return [_validate_cypher_identifier(record["label"], "node label") async for record in result]


async def _discover_relationship_types(session: Neo4jSession) -> list[str]:
    """Every relationship type present in the source graph.

    Via ``CALL db.relationshipTypes()``.
    """
    result = await session.run(
        "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"
    )
    return [
        _validate_cypher_identifier(record["relationshipType"], "relationship type")
        async for record in result
    ]


class Neo4jResult(Protocol):
    """The subset of the neo4j async result API this script uses."""

    async def single(self) -> Any: ...
    def __aiter__(self) -> AsyncIterator[Any]: ...


class Neo4jSession(Protocol):
    """The subset of the neo4j async session API this script uses."""

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> Neo4jResult: ...
    async def __aenter__(self) -> "Neo4jSession": ...
    async def __aexit__(self, *exc_info: object) -> None: ...


class Neo4jDriver(Protocol):
    """The subset of the neo4j async driver API this script uses."""

    def session(self) -> Neo4jSession: ...


def is_study_target_uri(uri: str) -> bool:
    """Return True only when *uri* positively matches the study substrate.

    An allowlist, not a denylist (codex plan-review, FRE-838): the target
    must *be* the study Bolt port, rather than merely not being the prod
    fingerprint. Rejects prod (7687), internal docker DNS names
    (``bolt://neo4j:7687``), and any other host/port.
    """
    from scripts.study.config import (
        STUDY_NEO4J_BOLT_PORT,  # noqa: PLC0415 — see module-level sys.path bootstrap
    )

    parsed = urlparse(uri)
    return parsed.hostname in _LOCAL_HOSTS and parsed.port == STUDY_NEO4J_BOLT_PORT


@dataclass(frozen=True)
class ExportedNode:
    """A single node read from prod, keyed by its prod element id."""

    source_element_id: str
    labels: tuple[str, ...]
    properties: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        """Canonical dict form used by ``compute_content_hash``."""
        return {
            "source_element_id": self.source_element_id,
            "labels": sorted(self.labels),
            "properties": self.properties,
        }


@dataclass(frozen=True)
class ExportedRelationship:
    """A single relationship read from prod, endpoints keyed by prod element id."""

    rel_type: str
    start_source_element_id: str
    end_source_element_id: str
    properties: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        """Canonical dict form used by ``compute_content_hash``."""
        return {
            "rel_type": self.rel_type,
            "start_source_element_id": self.start_source_element_id,
            "end_source_element_id": self.end_source_element_id,
            "properties": self.properties,
        }


@dataclass(frozen=True)
class SnapshotCorpus:
    """The full frozen export: graph data plus conversation traces."""

    nodes: tuple[ExportedNode, ...]
    relationships: tuple[ExportedRelationship, ...]
    sessions: tuple[dict[str, Any], ...]

    def node_counts_by_label(self) -> dict[str, int]:
        """Count of exported nodes grouped by label."""
        counts: dict[str, int] = {}
        for node in self.nodes:
            for label in node.labels:
                counts[label] = counts.get(label, 0) + 1
        return counts

    def relationship_counts_by_type(self) -> dict[str, int]:
        """Count of exported relationships grouped by type."""
        counts: dict[str, int] = {}
        for rel in self.relationships:
            counts[rel.rel_type] = counts.get(rel.rel_type, 0) + 1
        return counts


def compute_content_hash(corpus: SnapshotCorpus) -> str:
    """Sha256 over the full canonically-sorted corpus.

    Covers graph data AND conversation traces (codex plan-review: D1's
    corpus is explicitly "KG entities and relationships plus conversation
    traces", so the hash must cover both, not just the graph).
    """
    canonical = {
        "nodes": sorted(
            (n.to_json_dict() for n in corpus.nodes),
            key=lambda d: d["source_element_id"],
        ),
        "relationships": sorted(
            (r.to_json_dict() for r in corpus.relationships),
            key=lambda d: (d["start_source_element_id"], d["end_source_element_id"], d["rel_type"]),
        ),
        "sessions": sorted(corpus.sessions, key=lambda s: str(s.get("session_id", ""))),
    }
    payload = json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_manifest(
    corpus: SnapshotCorpus,
    snapshot_date: datetime,
    content_hash: str,
    skipped_relationships: int = 0,
) -> dict[str, Any]:
    """Build the ``snapshot_manifest.json`` payload for this corpus.

    ``skipped_relationships`` (self-review follow-up, FRE-838): count of
    relationships whose endpoint wasn't resolvable during the write (see
    ``write_sandbox_corpus``) — non-zero here means the frozen corpus is
    missing some edges prod had; a normal, fully-quiesced run should
    always report 0.
    """
    return {
        "snapshot_date": snapshot_date.astimezone(timezone.utc).isoformat(),
        "content_hash": content_hash,
        "node_counts_by_label": corpus.node_counts_by_label(),
        "relationship_counts_by_type": corpus.relationship_counts_by_type(),
        "prod_node_total": len(corpus.nodes),
        "prod_relationship_total": len(corpus.relationships),
        "prod_session_count": len(corpus.sessions),
        "skipped_relationships": skipped_relationships,
    }


def build_node_batch_create_cypher(
    labels: tuple[str, ...], nodes: list[ExportedNode]
) -> tuple[str, dict[str, Any]]:
    """Build one UNWIND-batched CREATE statement for all nodes sharing *labels*.

    Validates each label is a safe Cypher identifier before interpolation
    (labels cannot be parameterized in Cypher) — an injection guard, not a
    completeness check (fix-forward, FRE-838: labels are discovered
    dynamically from prod, not enumerated from a hardcoded list). Batching
    by label-set avoids an N+1 round-trip per node on a real prod-sized
    corpus (code-review finding, FRE-838) — one query per distinct label
    combination instead of one per node.

    Returns ``old_id``/``new_id`` pairs (the source element id and the
    sandbox's freshly-assigned element id) so relationship endpoints can be
    resolved by a fast, native elementId lookup instead of an unindexed
    property scan (fix-forward, FRE-838 — see
    ``build_relationship_batch_create_cypher``).
    """
    for label in labels:
        _validate_cypher_identifier(label, "node label")
    label_clause = ":".join(labels)
    query = (
        "UNWIND $rows AS row "
        f"CREATE (n:{label_clause}) "
        "SET n = row.properties, n._export_source_element_id = row.source_element_id "
        "RETURN row.source_element_id AS old_id, elementId(n) AS new_id"
    )
    rows = [
        {"properties": node.properties, "source_element_id": node.source_element_id}
        for node in nodes
    ]
    return query, {"rows": rows}


def build_relationship_batch_create_cypher(
    rel_type: str, relationships: list[ExportedRelationship], id_map: dict[str, str]
) -> tuple[str, dict[str, Any]]:
    """Build one UNWIND-batched MATCH..CREATE statement for all relationships of *rel_type*.

    Endpoints are resolved by ``elementId()`` against *id_map* (prod
    ``source_element_id`` -> the sandbox node's freshly-assigned element
    id, captured from ``build_node_batch_create_cypher``'s ``RETURN``) —
    NOT by matching the ``_export_source_element_id`` *property*
    (fix-forward, FRE-838): a property-based ``MATCH`` with no index on
    that property is an unindexed full-graph scan per lookup, which is
    slow and memory-heavy at real corpus volume (34,301 relationships x 2
    endpoint lookups) and was a real contributor to the original
    corpus-load failure alongside the memory limit. ``elementId()``
    lookup is a native, O(1) storage-offset dereference — no index
    needed.

    Validates *rel_type* is a safe Cypher identifier before interpolation
    — an injection guard, not a completeness check (fix-forward, FRE-838:
    relationship types are discovered dynamically from prod, not
    enumerated from a hardcoded list).
    """
    _validate_cypher_identifier(rel_type, "relationship type")
    rows = []
    for rel in relationships:
        start_new_id = id_map.get(rel.start_source_element_id)
        end_new_id = id_map.get(rel.end_source_element_id)
        if start_new_id is None or end_new_id is None:
            raise ValueError(
                f"Missing sandbox node mapping for {rel_type} relationship endpoint "
                f"({rel.start_source_element_id!r} -> {rel.end_source_element_id!r}) — "
                "a node this relationship references was not exported."
            )
        rows.append(
            {"start_new_id": start_new_id, "end_new_id": end_new_id, "properties": rel.properties}
        )
    query = (
        "UNWIND $rows AS row "
        "MATCH (a), (b) WHERE elementId(a) = row.start_new_id AND elementId(b) = row.end_new_id "
        f"CREATE (a)-[r:{rel_type}]->(b) SET r = row.properties"
    )
    return query, {"rows": rows}


async def read_prod_corpus(neo4j_driver: Neo4jDriver, pg_dsn: str) -> SnapshotCorpus:
    """Read-only export of the prod graph + conversation traces.

    Node labels and relationship types are discovered dynamically from
    prod (``CALL db.labels()`` / ``CALL db.relationshipTypes()``), not
    enumerated from a hardcoded list (fix-forward, FRE-838) — the export
    is complete by construction regardless of what the prod schema
    contains, rather than silently dropping anything a stale hardcoded
    list didn't happen to name.

    Args:
        neo4j_driver: connected async Neo4j driver pointed at PROD (read-only
            Cypher issued below; no writes).
        pg_dsn: prod Postgres DSN (``postgresql://...``, asyncpg-compatible).

    Returns:
        The full corpus read from prod.
    """
    import asyncpg

    nodes: list[ExportedNode] = []
    relationships: list[ExportedRelationship] = []
    seen_node_ids: set[str] = set()

    async with neo4j_driver.session() as session:
        for label in await _discover_node_labels(session):
            result = await session.run(
                f"MATCH (n:{label}) RETURN elementId(n) AS source_element_id, "
                "labels(n) AS labels, properties(n) AS properties"
            )
            async for record in result:
                source_element_id = record["source_element_id"]
                if source_element_id in seen_node_ids:
                    # A node carrying more than one label is matched once per
                    # label it has — dedupe so it's exported exactly once,
                    # with its full label set (defensive, FRE-838: today's
                    # schema has no multi-labeled nodes, but per-label
                    # iteration would silently double-export one if that
                    # ever changes, corrupting id_map in write_sandbox_corpus).
                    continue
                seen_node_ids.add(source_element_id)
                nodes.append(
                    ExportedNode(
                        source_element_id=source_element_id,
                        labels=tuple(record["labels"]),
                        properties=dict(record["properties"]),
                    )
                )

        for rel_type in await _discover_relationship_types(session):
            result = await session.run(
                f"MATCH (a)-[r:{rel_type}]->(b) RETURN elementId(a) AS start_id, "
                "elementId(b) AS end_id, properties(r) AS properties"
            )
            async for record in result:
                relationships.append(
                    ExportedRelationship(
                        rel_type=rel_type,
                        start_source_element_id=record["start_id"],
                        end_source_element_id=record["end_id"],
                        properties=dict(record["properties"]),
                    )
                )

    conn = await asyncpg.connect(pg_dsn)
    try:
        rows = await conn.fetch("SELECT session_id, created_at, messages, metadata FROM sessions")
        sessions = tuple(
            {
                "session_id": str(row["session_id"]),
                "created_at": row["created_at"],
                "messages": json.loads(row["messages"])
                if isinstance(row["messages"], str)
                else row["messages"],
                "metadata": json.loads(row["metadata"])
                if isinstance(row["metadata"], str)
                else row["metadata"],
            }
            for row in rows
        )
    finally:
        await conn.close()

    return SnapshotCorpus(nodes=tuple(nodes), relationships=tuple(relationships), sessions=sessions)


async def write_sandbox_corpus(neo4j_driver: Neo4jDriver, corpus: SnapshotCorpus) -> int:
    """Write the corpus into the study sandbox.

    Returns the count of relationships skipped because an endpoint was
    unresolvable (see below).

    One UNWIND-batched query per distinct node label-set, one per
    relationship type, then one batched query attaching raw
    conversation-trace payloads onto matching Session nodes — not one
    round-trip per node/relationship (code-review finding, FRE-838): a
    real prod-sized corpus turning into thousands of serial Bolt
    round-trips would needlessly extend the quiesced window the AC-5(2)
    zero-delta proof depends on.

    Relationship and session-trace endpoints are resolved via an
    ``old_id -> new sandbox elementId`` map captured from the node-write
    results, not by re-``MATCH``ing on the ``_export_source_element_id``
    *property* (fix-forward, FRE-838: an unindexed property scan across
    the whole graph, run once per relationship endpoint, was slow and
    memory-heavy at real corpus volume — a real contributor to the
    original corpus-load failure alongside the too-small memory limit).

    A relationship whose endpoint isn't in the map (possible if prod
    wasn't perfectly quiesced between the node-read and relationship-read
    passes in ``read_prod_corpus``) is **skipped, logged, and counted**
    rather than raising (self-review follow-up, FRE-838): the prior
    property-based ``MATCH`` silently produced zero relationships for an
    unresolvable endpoint, so hard-crashing here — after earlier
    node/relationship batches are already auto-committed — would be a
    worse failure mode than what this fix-forward is closing, not a
    strictly better one. The skip is visible (logged + counted in the
    manifest) rather than silent.
    """
    nodes_by_labels: dict[tuple[str, ...], list[ExportedNode]] = {}
    for node in corpus.nodes:
        nodes_by_labels.setdefault(node.labels, []).append(node)

    rels_by_type: dict[str, list[ExportedRelationship]] = {}
    for rel in corpus.relationships:
        rels_by_type.setdefault(rel.rel_type, []).append(rel)

    id_map: dict[str, str] = {}
    skipped_relationships = 0

    async with neo4j_driver.session() as session:
        for labels, nodes in nodes_by_labels.items():
            query, params = build_node_batch_create_cypher(labels, nodes)
            result = await session.run(query, params)
            async for record in result:
                id_map[record["old_id"]] = record["new_id"]

        for rel_type, rels in rels_by_type.items():
            resolvable = [
                rel
                for rel in rels
                if rel.start_source_element_id in id_map and rel.end_source_element_id in id_map
            ]
            unresolved = len(rels) - len(resolvable)
            if unresolved:
                skipped_relationships += unresolved
                log.warning(
                    "study_export_relationship_endpoint_unresolved",
                    rel_type=rel_type,
                    unresolved_count=unresolved,
                )
            if not resolvable:
                continue
            query, params = build_relationship_batch_create_cypher(rel_type, resolvable, id_map)
            await session.run(query, params)

        sessions_by_id = {s["session_id"]: s for s in corpus.sessions}
        session_trace_rows = []
        for node in corpus.nodes:
            if "Session" not in node.labels:
                continue
            trace = sessions_by_id.get(node.properties.get("session_id"))
            if trace is None:
                continue
            session_trace_rows.append(
                {
                    "new_id": id_map[node.source_element_id],
                    "raw_messages_json": json.dumps(trace["messages"], sort_keys=True, default=str),
                }
            )
        if session_trace_rows:
            await session.run(
                "UNWIND $rows AS row "
                "MATCH (s) WHERE elementId(s) = row.new_id "
                "SET s.raw_messages_json = row.raw_messages_json",
                {"rows": session_trace_rows},
            )

    return skipped_relationships


async def count_nodes_and_relationships(neo4j_driver: Neo4jDriver) -> tuple[int, int]:
    """Total node + relationship counts.

    Used for the AC-5(2) before/after zero-delta proof against prod.
    """
    async with neo4j_driver.session() as session:
        node_result = await session.run("MATCH (n) RETURN count(n) AS c")
        node_record = await node_result.single()
        rel_result = await session.run("MATCH ()-[r]->() RETURN count(r) AS c")
        rel_record = await rel_result.single()
    return int(node_record["c"]), int(rel_record["c"])


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually write to the study sandbox. Without this, prints a dry-run notice only.",
    )
    parser.add_argument(
        "--snapshot-dir",
        default="scripts/study/snapshots",
        help="Directory to write snapshot_manifest.json into. Default: scripts/study/snapshots",
    )
    return parser.parse_args()


async def run_export(args: argparse.Namespace) -> dict[str, Any] | None:
    """Orchestrate the export.

    Returns the manifest dict when it writes, None on a dry run or a
    refused (non-allowlisted target) run.

    The ``--execute`` check runs FIRST, before any settings are
    constructed (code-review finding, FRE-838): the documented no-arg dry
    run must stay genuinely safe to run on a fresh checkout — with no
    ``STUDY_NEO4J_PASSWORD`` or prod credentials configured — rather than
    crashing on a missing-config error before it ever gets to print the
    dry-run notice.
    """
    if not args.execute:
        print("Dry run (pass --execute to write). No prod reads or sandbox writes performed.")
        return None

    from personal_agent.config import get_settings
    from scripts.study.config import STUDY_NEO4J_BOLT_PORT, StudySettings

    app_settings = get_settings()
    study_settings = StudySettings()

    if not is_study_target_uri(study_settings.neo4j_uri):
        print(
            f"ERROR: STUDY_NEO4J_URI={study_settings.neo4j_uri!r} does not resolve to the "
            f"study substrate (expected localhost/127.0.0.1:{STUDY_NEO4J_BOLT_PORT}). Refusing to run.",
            file=sys.stderr,
        )
        return None

    from neo4j import AsyncGraphDatabase

    pg_dsn = app_settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    source_driver = AsyncGraphDatabase.driver(
        app_settings.neo4j_uri, auth=(app_settings.neo4j_user, app_settings.neo4j_password)
    )
    target_driver = AsyncGraphDatabase.driver(
        study_settings.neo4j_uri, auth=(study_settings.neo4j_user, study_settings.neo4j_password)
    )
    try:
        corpus = await read_prod_corpus(source_driver, pg_dsn)
        skipped_relationships = await write_sandbox_corpus(target_driver, corpus)
    finally:
        await source_driver.close()
        await target_driver.close()

    snapshot_date = datetime.now(timezone.utc)
    content_hash = compute_content_hash(corpus)
    manifest = build_manifest(corpus, snapshot_date, content_hash, skipped_relationships)
    if skipped_relationships:
        print(
            f"WARNING: {skipped_relationships} relationship(s) skipped — an endpoint was not "
            "resolvable (see 'study_export_relationship_endpoint_unresolved' in the logs). "
            "The frozen corpus is missing those edges.",
            file=sys.stderr,
        )

    snapshot_dir = Path(args.snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "snapshot_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True)
    )

    log.info("study_snapshot_exported", **manifest)
    return manifest


def main() -> None:
    """CLI entrypoint."""
    args = _parse_args()
    manifest = asyncio.run(run_export(args))
    if manifest is not None:
        print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
