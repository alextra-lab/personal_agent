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
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

import structlog

log = structlog.get_logger(__name__)

# Node labels / relationship types that exist in the prod schema today
# (src/personal_agent/memory/service.py). A closed allowlist — both for
# scoping the export and as a defensive check before interpolating a label
# or relationship type into Cypher (neither can be parameterized).
PROD_NODE_LABELS: tuple[str, ...] = (
    "Turn",
    "Session",
    "Entity",
    "EntityDescriptionVersion",
    "Person",
    "Agent",
    "Claim",
    "Location",
)

PROD_RELATIONSHIP_TYPES: tuple[str, ...] = (
    "PARTICIPATED_IN",
    "DISCUSSES",
    "CONTAINS",
    "NEXT",
    "HAD_DESCRIPTION",
    "HAS_STANCE",
    "HAS_FACT",
    "OPERATED_BY",
    "CURRENTLY_AT",
    "VISITED",
)

_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1"})


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

    Imports ``scripts.study.config`` locally (not at module level) so this
    module stays runnable via a direct ``python scripts/study/export_snapshot.py``
    invocation — no other file under ``scripts/`` imports cross-package at
    module level, and doing so here broke that (code-review follow-up,
    FRE-838): direct execution doesn't put the repo root on ``sys.path``,
    only ``python -m scripts...`` or pytest's rootdir insertion do.
    """
    from scripts.study.config import STUDY_NEO4J_BOLT_PORT

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
    corpus: SnapshotCorpus, snapshot_date: datetime, content_hash: str
) -> dict[str, Any]:
    """Build the ``snapshot_manifest.json`` payload for this corpus."""
    return {
        "snapshot_date": snapshot_date.astimezone(timezone.utc).isoformat(),
        "content_hash": content_hash,
        "node_counts_by_label": corpus.node_counts_by_label(),
        "relationship_counts_by_type": corpus.relationship_counts_by_type(),
        "prod_node_total": len(corpus.nodes),
        "prod_relationship_total": len(corpus.relationships),
        "prod_session_count": len(corpus.sessions),
    }


def build_node_batch_create_cypher(
    labels: tuple[str, ...], nodes: list[ExportedNode]
) -> tuple[str, dict[str, Any]]:
    """Build one UNWIND-batched CREATE statement for all nodes sharing *labels*.

    Validates labels against the closed prod allowlist before
    interpolation (labels cannot be parameterized in Cypher). Batching by
    label-set avoids an N+1 round-trip per node on a real prod-sized corpus
    (code-review finding, FRE-838) — one query per distinct label
    combination instead of one per node.
    """
    for label in labels:
        if label not in PROD_NODE_LABELS:
            raise ValueError(f"Refusing to export unrecognized node label: {label!r}")
    label_clause = ":".join(labels)
    query = (
        "UNWIND $rows AS row "
        f"CREATE (n:{label_clause}) "
        "SET n = row.properties, n._export_source_element_id = row.source_element_id"
    )
    rows = [
        {"properties": node.properties, "source_element_id": node.source_element_id}
        for node in nodes
    ]
    return query, {"rows": rows}


def build_relationship_batch_create_cypher(
    rel_type: str, relationships: list[ExportedRelationship]
) -> tuple[str, dict[str, Any]]:
    """Build one UNWIND-batched MATCH..CREATE statement for all relationships of *rel_type*.

    Endpoints are resolved via ``_export_source_element_id`` (set by
    ``build_node_batch_create_cypher`` above), not by re-derived Neo4j
    element ids — sandbox element ids are assigned fresh on write and are
    not equal to the prod ids being referenced here.
    """
    if rel_type not in PROD_RELATIONSHIP_TYPES:
        raise ValueError(f"Refusing to export unrecognized relationship type: {rel_type!r}")
    query = (
        "UNWIND $rows AS row "
        "MATCH (a {_export_source_element_id: row.start_source_element_id}), "
        "(b {_export_source_element_id: row.end_source_element_id}) "
        f"CREATE (a)-[r:{rel_type}]->(b) SET r = row.properties"
    )
    rows = [
        {
            "start_source_element_id": rel.start_source_element_id,
            "end_source_element_id": rel.end_source_element_id,
            "properties": rel.properties,
        }
        for rel in relationships
    ]
    return query, {"rows": rows}


async def read_prod_corpus(neo4j_driver: Neo4jDriver, pg_dsn: str) -> SnapshotCorpus:
    """Read-only export of the prod graph + conversation traces.

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

    async with neo4j_driver.session() as session:
        for label in PROD_NODE_LABELS:
            result = await session.run(
                f"MATCH (n:{label}) RETURN elementId(n) AS source_element_id, "
                "labels(n) AS labels, properties(n) AS properties"
            )
            async for record in result:
                nodes.append(
                    ExportedNode(
                        source_element_id=record["source_element_id"],
                        labels=tuple(record["labels"]),
                        properties=dict(record["properties"]),
                    )
                )

        for rel_type in PROD_RELATIONSHIP_TYPES:
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


async def write_sandbox_corpus(neo4j_driver: Neo4jDriver, corpus: SnapshotCorpus) -> None:
    """Write the corpus into the study sandbox.

    One UNWIND-batched query per distinct node label-set, one per
    relationship type, then one batched query attaching raw
    conversation-trace payloads onto matching Session nodes — not one
    round-trip per node/relationship (code-review finding, FRE-838): a
    real prod-sized corpus turning into thousands of serial Bolt
    round-trips would needlessly extend the quiesced window the AC-5(2)
    zero-delta proof depends on.
    """
    nodes_by_labels: dict[tuple[str, ...], list[ExportedNode]] = {}
    for node in corpus.nodes:
        nodes_by_labels.setdefault(node.labels, []).append(node)

    rels_by_type: dict[str, list[ExportedRelationship]] = {}
    for rel in corpus.relationships:
        rels_by_type.setdefault(rel.rel_type, []).append(rel)

    async with neo4j_driver.session() as session:
        for labels, nodes in nodes_by_labels.items():
            query, params = build_node_batch_create_cypher(labels, nodes)
            await session.run(query, params)

        for rel_type, rels in rels_by_type.items():
            query, params = build_relationship_batch_create_cypher(rel_type, rels)
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
                    "source_element_id": node.source_element_id,
                    "raw_messages_json": json.dumps(trace["messages"], sort_keys=True, default=str),
                }
            )
        if session_trace_rows:
            await session.run(
                "UNWIND $rows AS row "
                "MATCH (s:Session {_export_source_element_id: row.source_element_id}) "
                "SET s.raw_messages_json = row.raw_messages_json",
                {"rows": session_trace_rows},
            )


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

    from pathlib import Path

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
        await write_sandbox_corpus(target_driver, corpus)
    finally:
        await source_driver.close()
        await target_driver.close()

    snapshot_date = datetime.now(timezone.utc)
    content_hash = compute_content_hash(corpus)
    manifest = build_manifest(corpus, snapshot_date, content_hash)

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
