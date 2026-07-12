#!/usr/bin/env python3
"""One-time sweep: evict System-natured ``:Entity`` nodes from Neo4j Core (FRE-868, ADR-0115 D3).

FRE-865's backfill marked existing System-natured ``:Entity`` nodes (``class IS NULL``, output_kind
``ephemeral``/``finding``) with ``e.class_backfill_output_kind`` but never removed them — it only
identifies them. FRE-728 gates *new* writes before they reach Core but has no sweep over
already-existing nodes. This script is the missing piece: it consumes the FRE-865 marker and
**deletes** the marked nodes, completing ADR-0115 D3's "absent from Core" invariant for the
pre-existing corpus:

  * ``ephemeral`` -> deleted directly. No sysgraph write (the original capture was already durably
    observed in Elasticsearch at extraction time, independent of this sweep — mirrors FRE-728).
  * ``finding`` -> routed to ``sysgraph.stat`` via ``SysgraphRepository.record_finding()`` (the same
    sink FRE-728 uses for new ``finding`` items), then deleted — only once the sysgraph write
    succeeds; a failed dispatch leaves the node untouched (marker stays, retried next run).

Deletion is destructive, so rollback needs a real snapshot — not FRE-865's in-place property
restore, which only works because that script never removes the node. Before any mutation for a
candidate, this script captures a :class:`NodeSnapshot` (full properties + every relationship
touching it) and writes it **durably** (flush+fsync) to a JSONL file at ``--snapshot-path`` — only
then does it proceed to the sysgraph write and/or the delete. A crash before the snapshot line is
durable leaves the node untouched (nothing lost, no rollback needed); a crash after leaves an undo
record on disk.

Rollback (``--rollback --run-id <id> --snapshot-path <path>``) is two-pass and idempotent: pass 1
recreates every node via an identity-keyed ``MERGE`` (``fre868_restored_from_element_id``), so a
rerun after a partial-rollback crash matches the already-recreated node instead of duplicating it;
pass 2 reconnects relationships, resolved via the same-run old-to-new element-id map first (for
pairs where *both* ends were evicted together), else via a per-label **stable key**
(``Entity.name``, ``Turn.turn_id`` — never raw ``elementId``, which is not a durable identity across
a rollback file read back later), deduped via a stamped ``fre868_restored_rel_id`` so an
Entity<->Entity edge captured in *both* endpoints' snapshots is restored only once. Rollback never
touches ``sysgraph.stat`` (an append-only observation log, ADR-0105) — a ``finding`` row this sweep
wrote stays after a Core-side rollback of the entity it described.

**Explicitly test-substrate-scoped**: this script never runs against prod in this ticket. The prod
run is a separate, later, master-gated ops action behind ``--confirm-prod``, same posture as
FRE-865/FRE-772.

Usage:
    uv run python scripts/sweep_fre868_evict_system_entities.py --dry-run --confirm-prod
    uv run python scripts/sweep_fre868_evict_system_entities.py --snapshot-path run.jsonl --confirm-prod
    uv run python scripts/sweep_fre868_evict_system_entities.py --rollback --run-id <id> --snapshot-path run.jsonl --confirm-prod
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

import orjson
from neo4j.time import Date as Neo4jDate
from neo4j.time import DateTime as Neo4jDateTime
from neo4j.time import Time as Neo4jTime

from personal_agent.config import settings
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.sysgraph import SysgraphRepository

log = get_logger(__name__)

DEFAULT_BATCH_SIZE = 500

_VALID_MARKER_KINDS = frozenset({"ephemeral", "finding"})
_STABLE_KEY_FIELD = {"Entity": "name", "Turn": "turn_id"}

_DT_TAG = "__fre868_dt__"
_UNSUPPORTED_TAG = "__fre868_unsupported__"

# Reconstructors for each temporal shape this codebase's Entity/Turn schema actually produces
# (Neo4j's own driver-native types, plus the stdlib equivalents a caller might pass directly —
# e.g. a unit test). Tagged by exact type name so a bare ``Date`` round-trips back to a ``Date``,
# not a ``DateTime`` (a code review caught the prior version collapsing every temporal shape into
# a full ``datetime.fromisoformat()`` call, silently upcasting a Neo4j ``Date`` property into a
# ``DateTime`` on restore).
_TEMPORAL_RECONSTRUCTORS: dict[str, Any] = {
    "Neo4jDate": Neo4jDate.from_iso_format,
    "Neo4jDateTime": Neo4jDateTime.from_iso_format,
    "Neo4jTime": Neo4jTime.from_iso_format,
    "PyDateTime": datetime.fromisoformat,
}


# ---------------------------------------------------------------------------
# Property type-tagging (decision 7) — bounded JSON round-trip for Neo4j-native values
# ---------------------------------------------------------------------------


def _encode_value(value: Any) -> Any:
    """Type-tag a Neo4j-native temporal value for JSON round-trip; pass everything else through.

    Node/relationship properties in Neo4j are always flat (primitives or homogeneous arrays of
    primitives) — no recursion is needed. Any value that is JSON-serializable as-is (str, int,
    float, bool, None, list of primitives) passes through unchanged. A ``neo4j.time`` native
    temporal value (``Date``/``DateTime``/``Time``) or a plain :class:`datetime` is tagged with its
    *exact* type name so :func:`_decode_value` reconstructs the same shape it captured — a bare
    ``Date`` restores as a ``Date``, not a ``DateTime`` (both driver-native types independently
    expose ``iso_format()``, so a duck-typed capture-time check alone cannot tell them apart;
    dispatching on ``type(value).__name__`` first, then falling back to ``iso_format()`` only for
    an unrecognized-but-temporal-shaped value, keeps the common case exact). Anything else
    non-serializable (e.g. a Point) is captured as an unrestorable diagnostic marker rather than
    crashing the snapshot — spatial round-trip is out of scope (rare on System-natured entities);
    the property is simply dropped on restore rather than silently mis-typed.
    """
    type_name = type(value).__name__
    reconstructor_key = "Neo4j" + type_name if type_name in ("Date", "DateTime", "Time") else None
    if reconstructor_key in _TEMPORAL_RECONSTRUCTORS:
        return {_DT_TAG: reconstructor_key, "value": value.iso_format()}
    if isinstance(value, datetime):
        return {_DT_TAG: "PyDateTime", "value": value.isoformat()}
    if hasattr(value, "iso_format"):
        # An unrecognized temporal-shaped value (not one of the three types above) — best-effort:
        # capture it, but only DateTime reconstruction is attempted on restore (see decode).
        return {_DT_TAG: "PyDateTime", "value": value.iso_format()}
    try:
        orjson.dumps(value)
    except TypeError:
        return {_UNSUPPORTED_TAG: True, "repr": repr(value)}
    return value


def _decode_value(value: Any) -> Any:
    """Inverse of :func:`_encode_value`. An unsupported-tagged value decodes to ``None`` (dropped)."""
    if isinstance(value, dict):
        tag = value.get(_DT_TAG)
        if tag in _TEMPORAL_RECONSTRUCTORS:
            return _TEMPORAL_RECONSTRUCTORS[tag](value["value"])
        if value.get(_UNSUPPORTED_TAG):
            return None
    return value


def _stable_key_for(
    labels: list[str], props: dict[str, Any]
) -> tuple[tuple[str, str] | None, bool]:
    """Return ``((label, key_value), restorable)`` for a neighbor node, per decision 9.

    Only ``Entity`` (keyed on ``name``) and ``Turn`` (keyed on ``turn_id``) are supported — the
    only neighbor labels this corpus's actual write paths produce
    (``consolidator.py``'s ``create_conversation``/``create_entity``/``create_relationship``). Any
    other label is flagged non-restorable rather than silently reconnected by raw ``elementId``.
    """
    for label, key_field in _STABLE_KEY_FIELD.items():
        if label in labels and props.get(key_field):
            return (label, props[key_field]), True
    return None, False


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvictionCandidate:
    """An ``:Entity`` node carrying ``class_backfill_output_kind IS NOT NULL``, awaiting eviction."""

    element_id: str
    name: str
    entity_type: str
    description: str
    output_kind: str


@dataclass(frozen=True)
class RelSnapshot:
    """One relationship touching an evicted node, captured before deletion."""

    rel_type: str
    outgoing: bool
    old_rel_element_id: str
    other_element_id: str
    other_labels: list[str]
    other_stable_key: tuple[str, str] | None
    restorable: bool
    other_properties: dict[str, Any]
    rel_properties: dict[str, Any]


@dataclass(frozen=True)
class NodeSnapshot:
    """Full pre-deletion record of one evicted node — the rollback unit."""

    run_id: str
    element_id: str
    labels: list[str]
    properties: dict[str, Any]
    relationships: list[RelSnapshot] = field(default_factory=list)


@dataclass
class SweepReport:
    """Structured, serialisable record of a sweep run."""

    run_id: str
    dry_run: bool
    started_at: str
    finished_at: str = ""
    before_marked_count: int = 0
    after_marked_count: int = 0
    before_total_entities: int = 0
    after_total_entities: int = 0
    evicted_ephemeral: int = 0
    evicted_finding: int = 0
    dispatch_finding_failed: int = 0
    unrecognized_marker_count: int = 0
    total_candidates_this_run: int = 0
    success: bool = True


# ---------------------------------------------------------------------------
# Snapshot durability — flush+fsync per candidate, BEFORE that candidate's mutation
# ---------------------------------------------------------------------------


class SnapshotWriter:
    """Appends one JSON line per :class:`NodeSnapshot`, flushed+fsynced on every write.

    Constructed once per run and passed into :func:`run_sweep`. Durability here is the load-bearing
    half of decision 1: a snapshot line must be on disk before its node's sysgraph write/delete
    proceeds, so a crash never destroys the only undo record for a node that was actually mutated.
    """

    def __init__(self, path: Path) -> None:
        self._file = path.open("ab")

    def write(self, snapshot: NodeSnapshot) -> None:
        line = orjson.dumps(asdict(snapshot)) + b"\n"
        self._file.write(line)
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        self._file.close()


def _load_snapshots(path: Path, run_id: str) -> list[NodeSnapshot]:
    """Read every snapshot line matching ``run_id`` from a JSONL file written by :class:`SnapshotWriter`."""
    snapshots: list[NodeSnapshot] = []
    for line in path.read_bytes().splitlines():
        if not line:
            continue
        row = orjson.loads(line)
        if row["run_id"] != run_id:
            continue
        rels = [
            RelSnapshot(
                rel_type=r["rel_type"],
                outgoing=r["outgoing"],
                old_rel_element_id=r["old_rel_element_id"],
                other_element_id=r["other_element_id"],
                other_labels=r["other_labels"],
                other_stable_key=(
                    tuple(r["other_stable_key"]) if r["other_stable_key"] is not None else None
                ),
                restorable=r["restorable"],
                other_properties=r["other_properties"],
                rel_properties=r["rel_properties"],
            )
            for r in row["relationships"]
        ]
        snapshots.append(
            NodeSnapshot(
                run_id=row["run_id"],
                element_id=row["element_id"],
                labels=row["labels"],
                properties=row["properties"],
                relationships=rels,
            )
        )
    return snapshots


# ---------------------------------------------------------------------------
# Graph seam — all Cypher lives behind this Protocol so the orchestration is unit-testable
# ---------------------------------------------------------------------------


class GraphProtocol(Protocol):
    """The minimal graph operations the sweep needs (real impl: :class:`_Neo4jGraph`)."""

    async def count_marked(self) -> int: ...

    async def count_total_entities(self) -> int: ...

    async def fetch_candidates(self, cursor: str | None, limit: int) -> list[EvictionCandidate]: ...

    async def snapshot_node(self, element_id: str) -> NodeSnapshot: ...

    async def delete_node(self, element_id: str) -> None: ...

    async def restore_node(self, snapshot: NodeSnapshot) -> str: ...

    async def find_element_id_by_stable_key(self, label: str, key_value: str) -> str | None: ...

    async def restore_relationship(
        self,
        *,
        rel_type: str,
        old_rel_element_id: str,
        start_element_id: str,
        end_element_id: str,
        rel_properties: dict[str, Any],
    ) -> bool: ...


class _Neo4jGraph:
    """Real :class:`GraphProtocol` over an async Neo4j driver."""

    def __init__(self, driver: object) -> None:
        self._driver = driver

    async def count_marked(self) -> int:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (e:Entity) WHERE e.class_backfill_output_kind IS NOT NULL "
                "RETURN count(e) AS n"
            )
            rec = await result.single()
        return int(rec["n"]) if rec else 0

    async def count_total_entities(self) -> int:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run("MATCH (e:Entity) RETURN count(e) AS n")
            rec = await result.single()
        return int(rec["n"]) if rec else 0

    async def fetch_candidates(self, cursor: str | None, limit: int) -> list[EvictionCandidate]:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (e:Entity) WHERE e.class_backfill_output_kind IS NOT NULL "
                "AND ($cursor IS NULL OR elementId(e) > $cursor) "
                "RETURN elementId(e) AS eid, e.name AS name, "
                "       coalesce(e.entity_type, '') AS entity_type, "
                "       coalesce(e.description, '') AS description, "
                "       e.class_backfill_output_kind AS output_kind "
                "ORDER BY elementId(e) LIMIT $limit",
                cursor=cursor,
                limit=limit,
            )
            rows = await result.data()
        return [
            EvictionCandidate(
                element_id=r["eid"],
                name=r["name"] or "",
                entity_type=r["entity_type"],
                description=r["description"],
                output_kind=r["output_kind"],
            )
            for r in rows
        ]

    async def snapshot_node(self, element_id: str) -> NodeSnapshot:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (e) WHERE elementId(e) = $eid "
                "OPTIONAL MATCH (e)-[r]-(other) "
                "RETURN labels(e) AS labels, properties(e) AS props, "
                "  collect(CASE WHEN r IS NULL THEN NULL ELSE { "
                "    rel_eid: elementId(r), rel_type: type(r), "
                "    outgoing: elementId(startNode(r)) = elementId(e), "
                "    rel_props: properties(r), other_eid: elementId(other), "
                "    other_labels: labels(other), other_props: properties(other) "
                "  } END) AS rels",
                eid=element_id,
            )
            rec = await result.single()
        if rec is None:
            raise ValueError(f"node {element_id} not found for snapshot")

        relationships: list[RelSnapshot] = []
        for r in rec["rels"]:
            if r is None:
                continue
            other_props = dict(r["other_props"] or {})
            stable_key, restorable = _stable_key_for(r["other_labels"] or [], other_props)
            relationships.append(
                RelSnapshot(
                    rel_type=r["rel_type"],
                    outgoing=r["outgoing"],
                    old_rel_element_id=r["rel_eid"],
                    other_element_id=r["other_eid"],
                    other_labels=r["other_labels"] or [],
                    other_stable_key=stable_key,
                    restorable=restorable,
                    other_properties={k: _encode_value(v) for k, v in other_props.items()},
                    rel_properties={
                        k: _encode_value(v) for k, v in dict(r["rel_props"] or {}).items()
                    },
                )
            )
        return NodeSnapshot(
            run_id="",  # stamped by the caller before writing
            element_id=element_id,
            labels=rec["labels"] or [],
            properties={k: _encode_value(v) for k, v in dict(rec["props"] or {}).items()},
            relationships=relationships,
        )

    async def delete_node(self, element_id: str) -> None:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            await session.run("MATCH (e) WHERE elementId(e) = $eid DETACH DELETE e", eid=element_id)

    async def restore_node(self, snapshot: NodeSnapshot) -> str:
        decoded_props = {k: _decode_value(v) for k, v in snapshot.properties.items()}
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "CALL apoc.merge.node($labels, "
                "  {fre868_restored_from_element_id: $old_eid}, $on_create_props, {}) "
                "YIELD node RETURN elementId(node) AS new_eid",
                labels=snapshot.labels,
                old_eid=snapshot.element_id,
                on_create_props=decoded_props,
            )
            rec = await result.single()
        return str(rec["new_eid"])

    async def find_element_id_by_stable_key(self, label: str, key_value: str) -> str | None:
        key_field = _STABLE_KEY_FIELD.get(label)
        if key_field is None:
            return None
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                f"MATCH (n:{label}) WHERE n.{key_field} = $key_value "
                "RETURN elementId(n) AS eid LIMIT 1",
                key_value=key_value,
            )
            rec = await result.single()
        return rec["eid"] if rec else None

    async def restore_relationship(
        self,
        *,
        rel_type: str,
        old_rel_element_id: str,
        start_element_id: str,
        end_element_id: str,
        rel_properties: dict[str, Any],
    ) -> bool:
        decoded_props = {k: _decode_value(v) for k, v in rel_properties.items()}
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (a) WHERE elementId(a) = $start_eid "
                "MATCH (b) WHERE elementId(b) = $end_eid "
                "CALL apoc.merge.relationship(a, $rel_type, "
                "  {fre868_restored_rel_id: $old_rel_id}, $rel_props, b, {}) "
                "YIELD rel RETURN elementId(rel) AS new_rel_eid",
                start_eid=start_element_id,
                end_eid=end_element_id,
                rel_type=rel_type,
                old_rel_id=old_rel_element_id,
                rel_props=decoded_props,
            )
            rec = await result.single()
        return rec is not None


# ---------------------------------------------------------------------------
# Sysgraph seam
# ---------------------------------------------------------------------------


class SysgraphProtocol(Protocol):
    """The minimal sysgraph operation the sweep needs (real impl: :class:`_SysgraphSink`)."""

    async def record_finding(
        self, *, entity_name: str, entity_type: str, description: str | None
    ) -> None: ...


class _SysgraphSink:
    """Real :class:`SysgraphProtocol` wrapping a :class:`SysgraphRepository`.

    This script is not the running gateway app, so no process-level
    ``get_default_sysgraph_repo()`` singleton is set — the CLI owns an explicit
    ``connect()``/``disconnect()`` lifecycle around the sweep, mirroring the Neo4j driver's.
    """

    def __init__(self, repo: SysgraphRepository) -> None:
        self._repo = repo

    async def connect(self) -> None:
        await self._repo.connect()

    async def disconnect(self) -> None:
        await self._repo.disconnect()

    async def record_finding(
        self, *, entity_name: str, entity_type: str, description: str | None
    ) -> None:
        await self._repo.record_finding(
            entity_name=entity_name,
            entity_type=entity_type,
            description=description,
            trace_id=None,
            session_id=None,
        )


# ---------------------------------------------------------------------------
# Orchestration (pure — unit-tested with a fake graph + fake sysgraph sink)
# ---------------------------------------------------------------------------


async def run_sweep(
    graph: GraphProtocol,
    sysgraph: SysgraphProtocol,
    snapshot_writer: SnapshotWriter,
    *,
    run_id: str,
    now: str,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> SweepReport:
    """Evict every ``class_backfill_output_kind``-marked ``:Entity`` node. Returns the run report.

    Per candidate, the order is fixed (decisions 1/2): snapshot (read-only) -> durable write to
    ``snapshot_writer`` -> sysgraph dispatch (``finding`` only) -> delete. A dry run stops after
    counting — it issues zero graph writes, zero sysgraph calls, and zero snapshot-file writes.

    Args:
        graph: The graph seam (real Neo4j or an in-memory fake).
        sysgraph: The sysgraph seam (real repository or an in-memory fake).
        snapshot_writer: Durable per-candidate snapshot sink. Unused when ``dry_run``.
        run_id: Stable id stamped on every snapshot as rollback provenance.
        now: ISO-8601 timestamp for the report.
        dry_run: When True, issue zero writes anywhere — still counts and previews outcomes.
        batch_size: Max candidates read per DB page.

    Returns:
        A populated :class:`SweepReport`.
    """
    report = SweepReport(run_id=run_id, dry_run=dry_run, started_at=now)
    report.before_marked_count = await graph.count_marked()
    report.before_total_entities = await graph.count_total_entities()

    cursor: str | None = None
    while True:
        page = await graph.fetch_candidates(cursor, batch_size)
        if not page:
            break
        cursor = page[-1].element_id

        for candidate in page:
            report.total_candidates_this_run += 1

            if candidate.output_kind not in _VALID_MARKER_KINDS:
                report.unrecognized_marker_count += 1
                log.warning(
                    "fre868_unrecognized_marker",
                    element_id=candidate.element_id,
                    output_kind=candidate.output_kind,
                )
                continue

            if dry_run:
                if candidate.output_kind == "ephemeral":
                    report.evicted_ephemeral += 1
                else:
                    report.evicted_finding += 1
                continue

            snapshot = await graph.snapshot_node(candidate.element_id)
            snapshot = NodeSnapshot(
                run_id=run_id,
                element_id=snapshot.element_id,
                labels=snapshot.labels,
                properties=snapshot.properties,
                relationships=snapshot.relationships,
            )
            snapshot_writer.write(snapshot)

            if candidate.output_kind == "finding":
                try:
                    await sysgraph.record_finding(
                        entity_name=candidate.name,
                        entity_type=candidate.entity_type,
                        description=candidate.description or None,
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort dispatch, never abort the run
                    report.dispatch_finding_failed += 1
                    log.warning(
                        "fre868_dispatch_finding_failed",
                        element_id=candidate.element_id,
                        error=str(exc),
                    )
                    continue  # node left untouched — marker stays, retried next run
                await graph.delete_node(candidate.element_id)
                report.evicted_finding += 1
            else:  # ephemeral
                await graph.delete_node(candidate.element_id)
                report.evicted_ephemeral += 1

    report.after_marked_count = (
        report.before_marked_count if dry_run else await graph.count_marked()
    )
    report.after_total_entities = (
        report.before_total_entities if dry_run else await graph.count_total_entities()
    )
    # A code review caught `success` left at its True default with nothing in this function ever
    # setting it False — the documented `0 if report.success else 4` exit-code contract (CLI, per
    # _amain) was dead code, so a run with failed sysgraph dispatches or unrecognized marker values
    # (both cases that already print a WARNING) still reported a clean exit. Tie it to the two
    # failure signals this run actually tracks.
    report.success = report.dispatch_finding_failed == 0 and report.unrecognized_marker_count == 0
    report.finished_at = now
    return report


async def run_rollback(
    graph: GraphProtocol, snapshot_path: Path, run_id: str, *, dry_run: bool = False
) -> tuple[int, int, list[str]]:
    """Restore every node + relationship captured under ``run_id`` in ``snapshot_path``.

    Idempotent: recreating nodes/relationships uses identity-keyed ``MERGE`` (decision 1), so
    calling this twice for the same ``run_id`` produces the same counts both times.

    Args:
        dry_run: When True, issue zero graph writes — preview only. Node recreation is
            simulated with an identity old->new mapping (sufficient to detect same-run
            both-evicted relationship pairs, which resolve via that map regardless of
            whether the mapped value is a placeholder or a real new element id); resolving a
            relationship against a neighbor NOT evicted this run still runs its normal
            read-only ``find_element_id_by_stable_key`` lookup, since that call never
            mutates anything either way. A code review caught this flag being silently
            ignored by the CLI (accepted but never threaded through) — a `--rollback
            --dry-run` invocation was performing real writes.

    Returns:
        ``(restored_node_count, restored_relationship_count, skipped_descriptions)``.
    """
    snapshots = _load_snapshots(snapshot_path, run_id)

    old_to_new: dict[str, str] = {}
    for snap in snapshots:
        old_to_new[snap.element_id] = snap.element_id if dry_run else await graph.restore_node(snap)

    restored_rel_count = 0
    skipped: list[str] = []
    seen_rel_ids: set[str] = set()
    for snap in snapshots:
        owning_new_eid = old_to_new[snap.element_id]
        for rel in snap.relationships:
            # Dedup on sight, regardless of which branch below eventually runs — an
            # Entity<->Entity relationship where both ends were evicted is captured in
            # BOTH endpoints' snapshots, restorable or not (closes the double-count-in-
            # `skipped` gap a code review caught: marking `seen` only on the restorable
            # path let a non-restorable relationship's second sighting slip past this
            # guard and get reported twice).
            if rel.old_rel_element_id in seen_rel_ids:
                continue
            seen_rel_ids.add(rel.old_rel_element_id)

            if not rel.restorable:
                skipped.append(
                    f"{rel.rel_type} -> {rel.other_element_id} "
                    "(unsupported neighbor label, not restorable)"
                )
                continue

            other_new_eid = old_to_new.get(rel.other_element_id)
            if other_new_eid is None:
                assert rel.other_stable_key is not None  # restorable implies a stable key
                label, key_value = rel.other_stable_key
                other_new_eid = await graph.find_element_id_by_stable_key(label, key_value)
            if other_new_eid is None:
                skipped.append(f"{rel.rel_type} -> {rel.other_element_id} (neighbor not found)")
                continue

            if dry_run:
                # Both endpoints resolve — this relationship WOULD be restored. Never call
                # the mutating restore_relationship() under a preview.
                restored_rel_count += 1
                continue

            start_eid = owning_new_eid if rel.outgoing else other_new_eid
            end_eid = other_new_eid if rel.outgoing else owning_new_eid
            connected = await graph.restore_relationship(
                rel_type=rel.rel_type,
                old_rel_element_id=rel.old_rel_element_id,
                start_element_id=start_eid,
                end_element_id=end_eid,
                rel_properties=rel.rel_properties,
            )
            if connected:
                restored_rel_count += 1
            else:
                # A code review caught this branch silently dropping the relationship —
                # neither counted nor reported — when the MATCH inside restore_relationship
                # found no start/end node (e.g. a neighbor deleted between the stable-key
                # lookup above and the MATCH itself). Report it like any other unresolved
                # relationship rather than pretending nothing happened.
                skipped.append(
                    f"{rel.rel_type} -> {rel.other_element_id} "
                    "(restore_relationship found no matching start/end node)"
                )

    log.info(
        "fre868_rollback_done",
        restored_nodes=len(snapshots),
        restored_relationships=restored_rel_count,
        skipped=len(skipped),
        run_id=run_id,
    )
    return len(snapshots), restored_rel_count, skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _require_snapshot_path_for_apply(
    *, snapshot_path: Path | None, dry_run: bool, rollback: bool
) -> None:
    """Refuse to run a destructive sweep with nowhere to durably write the undo record."""
    if dry_run or rollback:
        return
    if snapshot_path is None:
        print(
            "ERROR: --snapshot-path is required for an applying (non-dry-run) sweep — "
            "this script deletes nodes and needs somewhere to write the undo record first.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _print_summary(report: SweepReport) -> None:
    """Print a human-readable run summary (structlog carries the machine record)."""
    mode = "DRY-RUN (no writes)" if report.dry_run else "APPLIED"
    print(f"\n=== FRE-868 System-entity eviction sweep [{mode}] run_id={report.run_id} ===")
    print(
        f"before: marked={report.before_marked_count} total_entities={report.before_total_entities}"
    )
    print(
        f"after:  marked={report.after_marked_count} total_entities={report.after_total_entities}"
    )
    print(
        f"candidates this run: {report.total_candidates_this_run} "
        f"(evicted_ephemeral={report.evicted_ephemeral}, evicted_finding={report.evicted_finding}, "
        f"dispatch_finding_failed={report.dispatch_finding_failed}, "
        f"unrecognized_marker={report.unrecognized_marker_count})"
    )
    if report.dispatch_finding_failed:
        print(
            "WARNING: one or more sysgraph dispatch calls failed — those nodes were left "
            "untouched (marker intact) and will be retried on the next run."
        )
    print(f"success: {report.success}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FRE-868: evict class_backfill_output_kind-marked :Entity nodes from Core."
    )
    parser.add_argument(
        "--confirm-prod",
        action="store_true",
        default=False,
        help="Required when AGENT_ENVIRONMENT is not 'test'. Confirms intent to write production data.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False, help="Preview; write nothing."
    )
    parser.add_argument(
        "--snapshot-path",
        type=Path,
        default=None,
        help="JSONL file to durably record pre-deletion snapshots (required unless --dry-run).",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        default=False,
        help="Restore from --snapshot-path for --run-id instead of sweeping.",
    )
    parser.add_argument("--run-id", type=str, default=None, help="Run id to roll back.")
    parser.add_argument(
        "--report-path", type=Path, default=None, help="Where to write the JSON report."
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
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
        if args.rollback:
            if not args.run_id or not args.snapshot_path:
                print("--rollback requires --run-id and --snapshot-path.", file=sys.stderr)
                return 2
            restored_nodes, restored_rels, skipped = await run_rollback(
                graph, args.snapshot_path, args.run_id, dry_run=args.dry_run
            )
            mode = "DRY-RUN (no writes) — would restore" if args.dry_run else "Rollback complete —"
            print(
                f"{mode} {restored_nodes} node(s), {restored_rels} relationship(s) "
                f"restored for run_id={args.run_id}."
            )
            if skipped:
                print(f"Skipped {len(skipped)} relationship(s): {skipped}")
            return 0

        from personal_agent.sysgraph import SysgraphRepository

        sysgraph = _SysgraphSink(SysgraphRepository(dsn=settings.sysgraph_database_url))
        if not args.dry_run:
            await sysgraph.connect()
        try:
            snapshot_writer: SnapshotWriter | None = None
            if not args.dry_run:
                snapshot_writer = SnapshotWriter(args.snapshot_path)
            report = await run_sweep(
                graph,
                sysgraph,
                snapshot_writer,  # type: ignore[arg-type]
                run_id=f"fre868-{uuid4()}",
                now=datetime.now().isoformat(),
                dry_run=args.dry_run,
                batch_size=args.batch_size,
            )
        finally:
            if not args.dry_run:
                await sysgraph.disconnect()
                if snapshot_writer is not None:
                    snapshot_writer.close()

        _print_summary(report)
        if args.report_path:
            args.report_path.write_bytes(orjson.dumps(asdict(report)))
            print(f"report written: {args.report_path}")
        return 0 if report.success else 4
    finally:
        await driver.close()


def main() -> int:
    """CLI entrypoint with the house prod-write env guard."""
    args = _parse_args()
    _require_snapshot_path_for_apply(
        snapshot_path=args.snapshot_path, dry_run=args.dry_run, rollback=args.rollback
    )
    from personal_agent.config.env_loader import Environment

    if settings.environment != Environment.TEST and not args.confirm_prod:
        print(
            "ERROR: Running against non-TEST environment without --confirm-prod.\n"
            "This script deletes production substrate data.\n"
            "Re-run with --confirm-prod if you intend to modify production data.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
