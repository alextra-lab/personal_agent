#!/usr/bin/env python3
"""Repair the description-less ``:Entity`` orphans left by dedup renaming (FRE-1115).

``create_conversation`` bare-MERGEd an ``:Entity`` for every raw extractor name, then
``create_entity`` ran and dedup could rewrite the write to a different canonical name —
so the description landed on the canonical node and the raw-name node was stranded with
no description. FRE-1115 stops the minting; this script cleans up what already exists.
Measured on the live graph 2026-08-02: 1,404 of 7,543 entities (18.6%), of which 1,159
carry 2,115 entity-to-entity relationship edges.

**What it will and will not do.** An orphan is only folded into another node when that
node's name *is the same name* — equal under casefold + accent-fold + punctuation
normalization, the same predicate the dedup guard uses (``memory/dedup.py``). That is the
only resolution provable from the graph alone.

Orphans with no name-equivalent twin are **left in place and reported**. This is
deliberate, not a shortfall. After FRE-1115's containment, an orphan such as
``mathematics`` (which dedup had wrongly folded into ``computer science``) is a
*correct, distinct entity that merely lacks a description* — repointing it into its
former canonical would recreate exactly the conflation the containment prevents. A
missing description is a different problem from a duplicate node, and this script only
solves the second. It never invents a description for either.

Ambiguous cases (two or more name-equivalent described twins) are also left and
reported, rather than resolved by a guess.

Usage:
    uv run python scripts/repair_fre1115_orphan_entities.py                    # dry run
    uv run python scripts/repair_fre1115_orphan_entities.py --apply --confirm-prod
    uv run python scripts/repair_fre1115_orphan_entities.py --json > plan.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import orjson
import structlog

from personal_agent.memory.dedup import _is_allcaps_identifier, _normalize_name
from personal_agent.memory.service import MemoryService

log = structlog.get_logger(__name__)

# An orphan is a node create_entity never wrote (no entity_id) that carries no
# description — the exact signature of the bare MERGE in create_conversation.
_ORPHAN_PREDICATE = "e.entity_id IS NULL AND (e.description IS NULL OR trim(e.description) = '')"

_FETCH_ORPHANS = f"""
MATCH (e:Entity)
WHERE {_ORPHAN_PREDICATE}
OPTIONAL MATCH (t:Turn)-[:DISCUSSES]->(e)
OPTIONAL MATCH (e)-[r]-(o:Entity)
RETURN e.name AS name,
       coalesce(e.entity_type, '') AS entity_type,
       count(DISTINCT t) AS turn_edges,
       count(DISTINCT r) AS entity_edges
"""

# entity_type comes back with the name: the fold predicate has to be at least as strict
# as the dedup gate, which scopes candidates to one entity_type (memory/dedup.py).
_FETCH_DESCRIBED = f"""
MATCH (e:Entity)
WHERE NOT ({_ORPHAN_PREDICATE})
RETURN e.name AS name, coalesce(e.entity_type, '') AS entity_type
"""

# Re-assert the orphan signature before touching anything. The plan is computed from a
# snapshot; if live consolidation described this node in between, the fold must abort
# BEFORE its edges are moved, not after (moving them and then declining to delete would
# leave a described node stripped of every edge).
_ASSERT_STILL_ORPHAN = f"""
MATCH (e:Entity {{name: $orphan}})
WHERE {_ORPHAN_PREDICATE}
RETURN count(e) AS still_orphan
"""

# Redirect every edge off the orphan, then remove it. DISCUSSES is MERGEd onto the
# canonical (a Turn may already discuss it), so the refactor cannot create a duplicate.
_REPOINT_DISCUSSES = """
MATCH (t:Turn)-[r:DISCUSSES]->(orphan:Entity {name: $orphan})
MATCH (canonical:Entity {name: $canonical})
MERGE (t)-[:DISCUSSES]->(canonical)
DELETE r
RETURN count(*) AS moved
"""

# Arbitrary relationship types cannot be re-created in plain Cypher, so the endpoints are
# refactored in place. An edge whose far end is the canonical itself is DELETED rather
# than refactored — refactoring it would produce a self-loop on the canonical, which is
# not information the graph had before.
_REPOINT_OUTGOING = """
MATCH (orphan:Entity {name: $orphan})-[r]->(other)
WHERE NOT other:Turn AND other.name <> $canonical
MATCH (canonical:Entity {name: $canonical})
CALL apoc.refactor.from(r, canonical) YIELD input
RETURN count(input) AS moved
"""

_REPOINT_INCOMING = """
MATCH (other)-[r]->(orphan:Entity {name: $orphan})
WHERE NOT other:Turn AND other.name <> $canonical
MATCH (canonical:Entity {name: $canonical})
CALL apoc.refactor.to(r, canonical) YIELD input
RETURN count(input) AS moved
"""

_DROP_EDGES_TO_CANONICAL = """
MATCH (orphan:Entity {name: $orphan})-[r]-(canonical:Entity {name: $canonical})
DELETE r
RETURN count(r) AS dropped
"""

_DELETE_ORPHAN = f"""
MATCH (e:Entity {{name: $orphan}})
WHERE {_ORPHAN_PREDICATE}
DETACH DELETE e
RETURN count(*) AS deleted
"""


@dataclass(frozen=True)
class OrphanPlan:
    """One orphan's disposition.

    Attributes:
        name: The orphan entity's name.
        action: ``"fold"`` to redirect and delete, ``"keep"`` to leave in place.
        reason: Why this disposition was chosen — printed for every row.
        canonical: The name-equivalent node to fold into, when action is ``"fold"``.
        turn_edges: Number of Turn DISCUSSES edges on the orphan.
        entity_edges: Number of entity-to-entity edges on the orphan.
    """

    name: str
    action: str
    reason: str
    canonical: str | None
    turn_edges: int
    entity_edges: int


def plan_repairs(
    orphans: Sequence[dict[str, Any]], described: Sequence[dict[str, Any]]
) -> list[OrphanPlan]:
    """Decide each orphan's disposition without touching the database.

    The fold predicate is deliberately **at least as strict as the live dedup gate**
    (``memory/dedup.py``): same normalized name, same ``entity_type`` (dedup only ever
    considers same-type candidates), and matching ALL_CAPS shape (FRE-412). A weaker
    predicate here would delete entities the write path would have kept distinct.

    Args:
        orphans: Rows of ``{name, entity_type, turn_edges, entity_edges}`` for
            description-less nodes.
        described: Rows of ``{name, entity_type}`` for every entity carrying a
            description.

    Returns:
        One :class:`OrphanPlan` per orphan, in input order.
    """
    by_key: dict[tuple[str, str], list[str]] = {}
    for row in described:
        key = (_normalize_name(row["name"]), row.get("entity_type", "") or "")
        by_key.setdefault(key, []).append(row["name"])

    plans: list[OrphanPlan] = []
    for row in orphans:
        name = row["name"]
        entity_type = row.get("entity_type", "") or ""
        turn_edges = int(row.get("turn_edges", 0))
        entity_edges = int(row.get("entity_edges", 0))
        twins = [
            twin
            for twin in by_key.get((_normalize_name(name), entity_type), [])
            if _is_allcaps_identifier(name) == _is_allcaps_identifier(twin)
        ]
        if len(twins) == 1:
            plans.append(
                OrphanPlan(
                    name=name,
                    action="fold",
                    reason=(f"same name and entity_type as described entity {twins[0]!r}"),
                    canonical=twins[0],
                    turn_edges=turn_edges,
                    entity_edges=entity_edges,
                )
            )
        elif len(twins) > 1:
            plans.append(
                OrphanPlan(
                    name=name,
                    action="keep",
                    reason=f"ambiguous — {len(twins)} name-equivalent twins: {sorted(twins)!r}",
                    canonical=None,
                    turn_edges=turn_edges,
                    entity_edges=entity_edges,
                )
            )
        else:
            plans.append(
                OrphanPlan(
                    name=name,
                    action="keep",
                    reason=(
                        "distinct entity with no description — no name-equivalent twin; "
                        "folding it elsewhere would conflate two different things"
                    ),
                    canonical=None,
                    turn_edges=turn_edges,
                    entity_edges=entity_edges,
                )
            )
    return plans


async def _apply_plan(service: MemoryService, plan: OrphanPlan) -> dict[str, int]:
    """Redirect one orphan's edges onto its canonical twin and delete it.

    Args:
        service: A connected memory service.
        plan: A ``fold`` plan carrying a resolved canonical name.

    Returns:
        Counts of moved discusses edges, moved entity edges, dropped orphan-to-canonical
        edges and deleted nodes. ``aborted`` is 1 when the node stopped being an orphan
        between plan and apply, in which case nothing was touched.
    """
    assert service.driver is not None
    assert plan.canonical is not None
    moved_discusses = moved_entity = dropped = deleted = 0
    async with service.driver.session() as session:
        params = {"orphan": plan.name, "canonical": plan.canonical}
        # Re-assert the signature BEFORE any write. The plan came from a snapshot; if
        # live consolidation described this node since, abort without touching it —
        # repointing first and declining to delete later would leave a described node
        # stripped of every edge.
        result = await session.run(_ASSERT_STILL_ORPHAN, orphan=plan.name)
        record = await result.single()
        if record is None or int(record["still_orphan"]) == 0:
            log.warning(
                "fre1115_orphan_fold_aborted",
                entity_name=plan.name,
                canonical_name=plan.canonical,
                trace_id=None,
                reason="node no longer matches the orphan signature; nothing touched",
            )
            return {
                "discusses_moved": 0,
                "entity_edges_moved": 0,
                "edges_to_canonical_dropped": 0,
                "deleted": 0,
                "aborted": 1,
            }
        # A direct orphan<->canonical edge carries no information once the two are one
        # node, and refactoring it would mint a self-loop. Drop it instead.
        result = await session.run(_DROP_EDGES_TO_CANONICAL, **params)
        record = await result.single()
        dropped = int(record["dropped"]) if record else 0
        for query, key in (
            (_REPOINT_DISCUSSES, "discusses"),
            (_REPOINT_OUTGOING, "outgoing"),
            (_REPOINT_INCOMING, "incoming"),
        ):
            result = await session.run(query, **params)
            record = await result.single()
            moved = int(record["moved"]) if record else 0
            if key == "discusses":
                moved_discusses = moved
            else:
                moved_entity += moved
        result = await session.run(_DELETE_ORPHAN, orphan=plan.name)
        record = await result.single()
        deleted = int(record["deleted"]) if record else 0
    return {
        "discusses_moved": moved_discusses,
        "entity_edges_moved": moved_entity,
        "edges_to_canonical_dropped": dropped,
        "deleted": deleted,
        "aborted": 0,
    }


async def run(*, apply: bool, as_json: bool, out_path: str | None) -> int:
    """Fetch orphans, plan their disposition, and optionally apply it.

    Args:
        apply: Whether to write. False performs a read-only dry run.
        as_json: Emit the plan as JSON instead of a human-readable report.
        out_path: Write the report here instead of stdout. Application logging shares
            stdout, so a machine-readable run needs a file to stay parseable.

    Returns:
        Process exit code — 0 on success, 1 when the graph is unreachable.
    """
    service = MemoryService()
    if not await service.connect():
        print("Neo4j unavailable — nothing done.", file=sys.stderr)
        return 1
    assert service.driver is not None
    try:
        async with service.driver.session() as session:
            result = await session.run(_FETCH_ORPHANS)
            orphans = [dict(record) async for record in result]
            result = await session.run(_FETCH_DESCRIBED)
            described = [dict(record) async for record in result]

        plans = plan_repairs(orphans, described)
        applied: list[dict[str, Any]] = []
        if apply:
            for plan in plans:
                if plan.action != "fold":
                    continue
                counts = await _apply_plan(service, plan)
                applied.append({"name": plan.name, "canonical": plan.canonical, **counts})
                log.info(
                    "fre1115_orphan_folded",
                    entity_name=plan.name,
                    canonical_name=plan.canonical,
                    trace_id=None,
                    **counts,
                )

        _report(
            plans,
            applied,
            apply=apply,
            as_json=as_json,
            described_count=len(described),
            out_path=out_path,
        )
        return 0
    finally:
        await service.disconnect()


def _report(
    plans: Sequence[OrphanPlan],
    applied: Sequence[dict[str, Any]],
    *,
    apply: bool,
    as_json: bool,
    described_count: int,
    out_path: str | None = None,
) -> None:
    """Print the per-orphan disposition and the totals.

    Args:
        plans: Every orphan's planned disposition.
        applied: Results of the writes actually performed.
        apply: Whether this was a write run.
        as_json: Emit machine-readable output.
        described_count: Number of described entities considered as fold targets.
        out_path: Destination file, or None for stdout.
    """
    folds = [plan for plan in plans if plan.action == "fold"]
    keeps = [plan for plan in plans if plan.action == "keep"]
    lines: list[str] = []

    if as_json:
        payload = {
            "mode": "apply" if apply else "dry-run",
            "orphans": len(plans),
            "described_entities": described_count,
            "fold": [asdict(plan) for plan in folds],
            "keep": [asdict(plan) for plan in keeps],
            "applied": list(applied),
        }
        lines.append(orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode())
    else:
        lines.append(f"FRE-1115 orphan repair — {'APPLY' if apply else 'DRY RUN'}")
        lines.append(f"  orphans found        : {len(plans)}")
        lines.append(f"  described entities   : {described_count}")
        lines.append(f"  fold into a twin     : {len(folds)}")
        lines.append(f"  keep (reported below): {len(keeps)}")
        if folds:
            lines.append("\n-- fold --")
            lines.extend(
                f"  {plan.name!r} -> {plan.canonical!r}  ({plan.reason}; "
                f"{plan.turn_edges} turn edges, {plan.entity_edges} entity edges)"
                for plan in folds
            )
        if keeps:
            lines.append("\n-- keep --")
            lines.extend(f"  {plan.name!r}: {plan.reason}" for plan in keeps)
        if applied:
            moved = sum(a["discusses_moved"] + a["entity_edges_moved"] for a in applied)
            lines.append(f"\napplied: {len(applied)} orphans folded, {moved} edges redirected")

    body = "\n".join(lines)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
    else:
        print(body)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the repair.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write changes (default is a read-only dry run)"
    )
    parser.add_argument(
        "--confirm-prod", action="store_true", help="required acknowledgement alongside --apply"
    )
    parser.add_argument("--json", action="store_true", help="emit the plan as JSON")
    parser.add_argument(
        "--out",
        default=None,
        help="write the report to this file (use with --json; stdout also carries app logs)",
    )
    args = parser.parse_args(argv)

    if args.apply and not args.confirm_prod:
        print("--apply requires --confirm-prod (this deletes nodes).", file=sys.stderr)
        return 2
    return asyncio.run(run(apply=args.apply, as_json=args.json, out_path=args.out))


if __name__ == "__main__":
    raise SystemExit(main())
