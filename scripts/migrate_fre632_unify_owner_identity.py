#!/usr/bin/env python3
"""One-shot, idempotent Neo4j migration: unify the split owner identity (FRE-632, ADR-0052).

ADR-0052 shipped a split: the owner exists as two disconnected nodes —

  * ``:Person {is_owner:true, user_id, name}`` — the config-bootstrap identity anchor
    (``bootstrap_owner_identity``), keyed by ``user_id``; and
  * ``:Entity {name, user_id:NULL}`` — the extraction node keyed by ``name``, carrying the
    entity substrate (``embedding``, ``class``, ``entity_type``, ``description``,
    ``mention_count``) and the ``DISCUSSES`` turn-provenance.

They never merged because bootstrap MERGEs by ``user_id`` while extraction MERGEs by ``name``,
and dedup deliberately excludes ``user_id`` nodes so a same-named third party can't collide into
the owner. This script folds the extraction ``:Entity`` INTO the owner ``:Person``, producing a
single ``:Person:Entity`` node that is both the identity anchor and the searchable entity.

Companion forward-fix (``bootstrap_owner_identity`` now labels the owner ``:Person:Entity``,
service.py) makes the owner occupy the ``MERGE (:Entity {name})`` slot so the split cannot recur.

Merge strategy (empirically verified on the :7688 test substrate, and unit/integration-tested):

  1. ``SET keep += apoc.map.removeKeys(properties(drop), keys(keep))`` — copy the entity-substrate
     properties the owner lacks (embedding/class/…) WITHOUT overwriting any owner identity property
     (user_id/is_owner/name/email/source are all already on ``keep``, so they are excluded).
  2. ``apoc.refactor.mergeNodes([keep, drop], {properties:'discard', mergeRels:true})`` — move
     ``drop``'s relationships onto ``keep``, de-dupe parallel edges (the duplicated ``USES``/
     ``RELATED_TO`` collapse to one each), union labels (``keep`` becomes ``:Person:Entity``), and
     discard ``drop``'s (already-copied) properties. No ``keep``↔``drop`` edge exists → no self-loops.

Idempotent: if no split ``:Entity`` remains (already unified), the run is a no-op and reports success.

Usage:
    uv run python scripts/migrate_fre632_unify_owner_identity.py --dry-run --confirm-prod
    uv run python scripts/migrate_fre632_unify_owner_identity.py --confirm-prod
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import orjson

from personal_agent.config import settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeSnapshot:
    """A structural snapshot of one node — labels, identity flags, and per-type/direction edges."""

    element_id: str
    labels: list[str]
    name: str | None
    is_owner: bool | None
    user_id: str | None
    has_embedding: bool
    rel_counts: dict[str, int]  # "OUT:USES" -> n, "IN:DISCUSSES" -> n


@dataclass
class UnifyReport:
    """Structured, serialisable record of a unify run."""

    dry_run: bool
    owner_before: NodeSnapshot | None = None
    drops_before: list[NodeSnapshot] = field(default_factory=list)
    merged_element_id: str | None = None
    named_count_before: int = 0
    named_count_after: int = 0
    unified: NodeSnapshot | None = None
    success: bool = False
    note: str = ""


# ---------------------------------------------------------------------------
# Graph seam — all Cypher lives behind this Protocol so orchestration is unit-testable
# ---------------------------------------------------------------------------


class GraphProtocol(Protocol):
    """The minimal graph operations the migration needs (real impl: :class:`_Neo4jGraph`)."""

    async def find_owner_ids(self) -> list[tuple[str, str | None]]: ...

    async def find_split_entity_ids(self, owner_name: str, owner_eid: str) -> list[str]: ...

    async def snapshot(self, element_id: str) -> NodeSnapshot | None: ...

    async def count_named(self, name: str) -> int: ...

    async def merge_one(self, keep_eid: str, drop_eid: str) -> str: ...


class _Neo4jGraph:
    """Real :class:`GraphProtocol` over an async Neo4j driver."""

    def __init__(self, driver: object) -> None:
        self._driver = driver

    async def find_owner_ids(self) -> list[tuple[str, str | None]]:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (o:Person {is_owner: true}) RETURN elementId(o) AS eid, o.name AS name"
            )
            rows = await result.data()
        return [(r["eid"], r["name"]) for r in rows]

    async def find_split_entity_ids(self, owner_name: str, owner_eid: str) -> list[str]:
        # The split node is the extraction :Entity carrying the owner's exact name and NO user_id
        # (dedup's invariant). Exact-name match is deliberate: a differently-cased third-party
        # entity must not be swept into the owner by this cleanup.
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (e:Entity) "
                "WHERE e.name = $name AND e.user_id IS NULL AND elementId(e) <> $owner_eid "
                "RETURN elementId(e) AS eid",
                name=owner_name,
                owner_eid=owner_eid,
            )
            rows = await result.data()
        return [r["eid"] for r in rows]

    async def snapshot(self, element_id: str) -> NodeSnapshot | None:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (n) WHERE elementId(n) = $eid "
                "OPTIONAL MATCH (n)-[r]-() "
                "RETURN labels(n) AS labels, n.name AS name, n.is_owner AS is_owner, "
                "       n.user_id AS user_id, (n.embedding IS NOT NULL) AS has_embedding, "
                "       [x IN collect(CASE WHEN r IS NULL THEN null ELSE "
                "         (CASE WHEN startNode(r) = n THEN 'OUT:' ELSE 'IN:' END) + type(r) END) "
                "        WHERE x IS NOT NULL] AS rel_tags",
                eid=element_id,
            )
            rec = await result.single()
        if rec is None:
            return None
        return NodeSnapshot(
            element_id=element_id,
            labels=list(rec["labels"]),
            name=rec["name"],
            is_owner=rec["is_owner"],
            user_id=rec["user_id"],
            has_embedding=bool(rec["has_embedding"]),
            rel_counts=dict(Counter(rec["rel_tags"])),
        )

    async def count_named(self, name: str) -> int:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run("MATCH (n {name: $name}) RETURN count(n) AS c", name=name)
            rec = await result.single()
        return int(rec["c"]) if rec else 0

    async def merge_one(self, keep_eid: str, drop_eid: str) -> str:
        # The two-step fold, empirically verified: copy drop-only props onto keep (never
        # overwriting keep's identity props), then mergeNodes to move+de-dupe relationships and
        # union labels. keep's elementId is retained (mergeNodes keeps the first node's identity).
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                """
                MATCH (keep) WHERE elementId(keep) = $keep_eid
                MATCH (drop) WHERE elementId(drop) = $drop_eid
                SET keep += apoc.map.removeKeys(properties(drop), keys(keep))
                WITH keep, drop
                CALL apoc.refactor.mergeNodes([keep, drop],
                    {properties: 'discard', mergeRels: true, produceSelfRel: false}) YIELD node
                RETURN elementId(node) AS eid
                """,
                keep_eid=keep_eid,
                drop_eid=drop_eid,
            )
            rec = await result.single()
        return str(rec["eid"]) if rec else keep_eid


# ---------------------------------------------------------------------------
# Orchestration (pure — unit-tested with a fake graph)
# ---------------------------------------------------------------------------


async def run_unify(graph: GraphProtocol, *, dry_run: bool = False) -> UnifyReport:
    """Fold the split extraction ``:Entity`` into the owner ``:Person``. Returns the run report.

    Args:
        graph: The graph seam (real Neo4j or an in-memory fake).
        dry_run: When True, issue **zero** writes — still inspects and previews the outcome.

    Returns:
        A populated :class:`UnifyReport`. ``success`` is True when the graph ends unified (or was
        already unified): the owner node is a ``:Person:Entity`` flagged ``is_owner`` and no
        foldable split ``:Entity`` remains. A legitimately-distinct node that merely shares the
        owner's name (a contact ``:Person``, a ``:Topic``) does not affect success.
    """
    report = UnifyReport(dry_run=dry_run)

    owner_ids = await graph.find_owner_ids()
    if len(owner_ids) != 1:
        report.note = f"expected exactly one is_owner Person, found {len(owner_ids)}"
        report.success = False
        return report

    owner_eid, owner_name = owner_ids[0]
    if not owner_name:
        report.note = "owner Person has no name; cannot resolve the split :Entity"
        report.success = False
        return report

    report.owner_before = await graph.snapshot(owner_eid)
    report.named_count_before = await graph.count_named(owner_name)

    drop_ids = await graph.find_split_entity_ids(owner_name, owner_eid)
    report.drops_before = [s for eid in drop_ids if (s := await graph.snapshot(eid)) is not None]

    if not drop_ids:
        # Already unified — idempotent no-op.
        report.merged_element_id = owner_eid
        report.unified = report.owner_before
        report.named_count_after = report.named_count_before
        report.success = _is_unified(report.owner_before)
        report.note = (
            "already unified (no split :Entity found)"
            if report.success
            else ("no split :Entity, but owner node is not :Person:Entity — check bootstrap")
        )
        return report

    if dry_run:
        report.note = (
            f"DRY-RUN: would merge {len(drop_ids)} split :Entity node(s) into owner "
            f"'{owner_name}' ({owner_eid})"
        )
        report.success = True
        return report

    merged_eid = owner_eid
    for drop_eid in drop_ids:
        merged_eid = await graph.merge_one(merged_eid, drop_eid)
        log.info("fre632_merged_node", keep=owner_eid, drop=drop_eid, result=merged_eid)

    report.merged_element_id = merged_eid
    report.unified = await graph.snapshot(merged_eid)
    report.named_count_after = await graph.count_named(owner_name)
    # Success = the owner is a unified :Person:Entity AND no foldable split remains. Deliberately
    # NOT keyed on named_count (a legitimately-distinct node sharing the owner's name — a contact
    # :Person, a Topic — is not a split and must not flip a correct merge to "failed").
    remaining = await graph.find_split_entity_ids(owner_name, merged_eid)
    report.success = _is_unified(report.unified) and not remaining
    report.note = (
        f"merged {len(drop_ids)} node(s) into owner '{owner_name}'"
        if report.success
        else f"merge ran but owner not unified :Person:Entity or {len(remaining)} split(s) remain"
    )
    return report


def _is_unified(node: NodeSnapshot | None) -> bool:
    """Return True when the owner node is a flagged ``:Person:Entity`` (identity + entity in one)."""
    return (
        node is not None
        and "Person" in node.labels
        and "Entity" in node.labels
        and node.is_owner is True
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(report: UnifyReport) -> None:
    """Print a human-readable run summary (structlog carries the machine record)."""
    mode = "DRY-RUN (no writes)" if report.dry_run else "APPLIED"
    print(f"\n=== FRE-632 owner-identity unify [{mode}] ===")
    if report.owner_before:
        print(
            f"owner before: labels={report.owner_before.labels} "
            f"has_embedding={report.owner_before.has_embedding} "
            f"rels={report.owner_before.rel_counts}"
        )
    for d in report.drops_before:
        print(f"split :Entity: {d.element_id} rels={d.rel_counts}")
    print(
        f"nodes named owner before: {report.named_count_before}  after: {report.named_count_after}"
    )
    if report.unified:
        print(
            f"unified node: {report.merged_element_id} labels={report.unified.labels} "
            f"has_embedding={report.unified.has_embedding} rels={report.unified.rel_counts}"
        )
    print(f"note: {report.note}")
    print(f"success: {report.success}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FRE-632: unify the split owner identity (fold extraction :Entity into the "
        "owner :Person). Idempotent."
    )
    parser.add_argument(
        "--confirm-prod",
        action="store_true",
        default=False,
        help="Required when AGENT_ENVIRONMENT is not 'test'. Confirms intent to write prod data.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False, help="Preview; write nothing."
    )
    parser.add_argument(
        "--report-path", type=Path, default=None, help="Where to write the JSON report."
    )
    return parser.parse_args()


async def _amain(args: argparse.Namespace) -> int:
    try:
        from neo4j import AsyncGraphDatabase
    except ModuleNotFoundError:
        print("neo4j package not installed — run 'uv sync' first.", file=sys.stderr)
        return 1

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        await driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot connect to Neo4j at {settings.neo4j_uri}: {exc}", file=sys.stderr)
        await driver.close()
        return 1

    graph = _Neo4jGraph(driver)
    try:
        report = await run_unify(graph, dry_run=args.dry_run)
        _print_summary(report)
        if args.report_path:
            args.report_path.write_bytes(orjson.dumps(_report_dict(report)))
            print(f"report written: {args.report_path}")
        return 0 if report.success else 4
    finally:
        await driver.close()


def _report_dict(report: UnifyReport) -> dict[str, Any]:
    """Serialise the report (dataclasses → plain dict) for the JSON artifact."""
    return asdict(report)


def main() -> int:
    """CLI entrypoint with the house prod-write env guard."""
    args = _parse_args()
    from personal_agent.config.env_loader import Environment

    if settings.environment != Environment.TEST and not args.confirm_prod:
        print(
            "ERROR: Running against non-TEST environment without --confirm-prod.\n"
            "This script writes to the production substrate.\n"
            "Re-run with --confirm-prod if you intend to modify production data.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
