#!/usr/bin/env python3
"""One-time, idempotent Neo4j backfill: reconstruct provenance for legacy knowledge items
that predate ADR-0098 Amendment A (FRE-1348, ADR-0098 Amendment A · A5).

Every ``:Entity``/``:Claim`` node and extracted relationship written before FRE-1346
shipped carries no ``provenance_state`` at all — the silent third state A5 forbids. A
blanket ``'none'`` would discard recoverable provenance, so this script replays T2/T3's
already-shipped containment rule (``memory/provenance.py``) offline against each item's
*minting capture* — the ``TaskCapture`` whose ``tool_results`` originally produced it —
and reconstructs real ``SOURCED_FROM`` provenance wherever the address is still
recoverable. Only where it genuinely is not does an item take ``'none'``.

**Entities and Claims are reconstructable**: both carry a durable back-pointer to their
minting capture, set ON CREATE by every write path — ``Entity.originating_trace_id``
(``memory/service.py:2296``, ``:1236``/``:1307`` for the ``create_conversation`` bare-MERGE
paths) and ``Claim.trace_id`` (``memory/service.py:2148``). **Relationships are not**:
``create_relationship`` never persists a trace/session key (ADR-0098 A4b), so a legacy
relationship gets a flat ``'none'`` stamp, reported separately, never a reconstruction
attempt.

**Why the entity/claim candidate query is ``provenance_state IS NULL OR provenance_state
<> 'provenanced'``, not ``IS NULL`` alone.** FRE-1346's own self-review fold-in stamps
``provenance_state = COALESCE(e.provenance_state, 'none')`` on *every* mention of *every*
entity that goes through ``create_entity``'s sourceless-write path (``service.py:2211``)
or ``create_conversation``'s inline bare ``:Entity`` MERGE (``service.py:1320``) —
including a legacy entity that has never been through reconstruction. Since FRE-1346
already shipped, by the time this migration runs some legacy entities will already read
``'none'`` from that incidental touch, not from a genuine reconstruction failure.
Filtering on ``IS NULL`` alone — or on ``NOT IN ['provenanced','none']``, which still
excludes ``'none'`` from re-examination — would silently skip exactly the subset of the
corpus AC-2 exists to prove gets reconstructed. ``<> 'provenanced'`` also catches any
historical out-of-enum value (never observed to be written by current code, but AC-1
checks for it, so the migration handles it rather than assuming it cannot exist) —
``'provenanced'`` is the only state a candidate query must ever exclude.

Idempotent and safely re-runnable, but not a no-op on a second pass: every candidate whose
predicate still matches is re-examined every run — a candidate correctly left ``'none'``
today may become reconstructable later (a purged capture restored, an ES-only capture
indexed). The cost is extra reads, never an incorrect write: setting ``'provenanced'`` is
unconditional (safe from *any* prior state — it is the lattice's terminal value, A5), while
setting ``'none'`` is guarded by a ``CASE`` that preserves an already-``'provenanced'``
value untouched, closing the race window a separate ``WHERE ... SET`` would leave against a
concurrent live write.

Follows ``scripts/migrate_fre865_entity_class_backfill.py``'s established shape
(``GraphProtocol`` seam, ``--dry-run``, ``--confirm-prod``, a printed + optionally
persisted ``BackfillReport``). No ``--rollback``: unlike FRE-865 this migration only ever
moves ``none -> provenanced``, which is not something there is a legitimate reason to
undo.

Usage:
    uv run python scripts/migrate_fre1348_provenance_backfill.py --dry-run --confirm-prod
    uv run python scripts/migrate_fre1348_provenance_backfill.py --confirm-prod
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import orjson

from personal_agent.captains_log.capture import (
    TaskCapture,
    build_capture_index,
    read_captures_by_trace_ids,
)
from personal_agent.config import settings
from personal_agent.memory.provenance import SourceRecord, associate, sources_from_tool_results
from personal_agent.memory.service import MemoryService
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.tools.registry import ToolRegistry

log = get_logger(__name__)

DEFAULT_BATCH_SIZE = 500

#: Provenance write sites emit only these two values (ADR-0098 A5) — anything else,
#: including NULL, is a candidate for this migration.
_VALID_STATES = ("provenanced", "none")

# ``create_relationship`` (memory/service.py:3978) is the ONLY write site that sets
# `weight` on a relationship — every fixed-label structural edge in the codebase is
# written via a literal `MERGE (a)-[:TYPE]->(b)` Cypher block that never touches it, so
# `r.weight IS NOT NULL` cleanly separates the dynamic extracted-relationship population.
# `weight` is not an API-enforced invariant of that function, though (nothing stops a
# future caller from passing relationship_type="DISCUSSES"), so this frozenset of every
# fixed structural type in the codebase today is a defensive second discriminator.
_STRUCTURAL_RELATIONSHIP_TYPES = frozenset(
    {
        "DISCUSSES",
        "PARTICIPATED_IN",
        "NEXT",
        "CONTAINS",
        "SOURCED_FROM",
        "HAD_DESCRIPTION",
        "HAS_FACT",
        "HAS_STANCE",
        "OPERATED_BY",
        "CURRENTLY_AT",
    }
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityCandidate:
    """An ``:Entity`` node whose provenance is missing or invalid, awaiting backfill."""

    element_id: str
    name: str
    originating_trace_id: str | None


@dataclass(frozen=True)
class ClaimCandidate:
    """A ``:Claim`` node whose provenance is missing or invalid, awaiting backfill."""

    element_id: str
    claim_id: str
    content: str
    trace_id: str | None


@dataclass(frozen=True)
class ReconstructOutcome:
    """The result of replaying A4 containment against one candidate's minting capture."""

    matched_sources: list[SourceRecord]
    missing_capture: bool


@dataclass
class BackfillReport:
    """Structured, serialisable record of a backfill run (AC-4: reported, not asserted)."""

    run_id: str
    dry_run: bool
    started_at: str
    finished_at: str = ""
    entities_total: int = 0
    entities_reconstructed: int = 0
    entities_none_no_match: int = 0
    entities_none_missing_capture: int = 0
    entities_errors: int = 0
    claims_total: int = 0
    claims_reconstructed: int = 0
    claims_none_no_match: int = 0
    claims_none_missing_capture: int = 0
    claims_errors: int = 0
    relationships_marked_none: int = 0
    success: bool = True


# ---------------------------------------------------------------------------
# Graph seam — all Cypher lives behind this Protocol so the orchestration is unit-testable
# ---------------------------------------------------------------------------


class GraphProtocol(Protocol):
    """The minimal graph operations the backfill needs (real impl: :class:`_Neo4jGraph`)."""

    async def fetch_entity_candidates(
        self, cursor: str | None, limit: int
    ) -> list[EntityCandidate]: ...

    async def write_entity_provenanced(
        self, name: str, sources: Sequence[SourceRecord]
    ) -> None: ...

    async def write_entity_none(self, name: str) -> None: ...

    async def fetch_claim_candidates(
        self, cursor: str | None, limit: int
    ) -> list[ClaimCandidate]: ...

    async def write_claim_provenanced(
        self, claim_id: str, sources: Sequence[SourceRecord]
    ) -> None: ...

    async def write_claim_none(self, claim_id: str) -> None: ...

    async def mark_relationships_none(self) -> int: ...


#: Candidate selection: anything NOT YET 'provenanced' — NULL, 'none' (a legacy item may
#: already read 'none' from an incidental post-FRE-1346 touch, see module docstring), or
#: any out-of-enum value. Deliberately NOT "NOT IN ['provenanced','none']", which would
#: exclude 'none' from re-examination and defeat the whole point of the widened filter.
_CANDIDATE_PREDICATE = "n.provenance_state IS NULL OR n.provenance_state <> 'provenanced'"


class _Neo4jGraph:
    """Real :class:`GraphProtocol` over an async Neo4j driver."""

    def __init__(self, driver: object) -> None:
        self._driver = driver

    async def fetch_entity_candidates(
        self, cursor: str | None, limit: int
    ) -> list[EntityCandidate]:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (n:Entity) "
                f"WHERE ({_CANDIDATE_PREDICATE}) "
                "AND ($cursor IS NULL OR elementId(n) > $cursor) "
                "RETURN elementId(n) AS eid, n.name AS name, "
                "       n.originating_trace_id AS trace_id "
                "ORDER BY elementId(n) LIMIT $limit",
                cursor=cursor,
                limit=limit,
            )
            rows = await result.data()
        return [
            EntityCandidate(
                element_id=r["eid"], name=r["name"] or "", originating_trace_id=r["trace_id"]
            )
            for r in rows
        ]

    async def write_entity_provenanced(self, name: str, sources: Sequence[SourceRecord]) -> None:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            # Unconditional: 'provenanced' is the lattice's terminal value (A5), safe from
            # any prior state. The :Source merge runs unconditionally too — even an
            # already-provenanced item legitimately gains a newly-discovered corroborating
            # edge (A4b: multiple sources are recorded, not treated as ambiguity).
            await session.run(
                "MATCH (e:Entity {name: $name})\n"
                "SET e.provenance_state = 'provenanced'\n"
                "WITH e\n" + MemoryService._source_merge_clause("e") + "RETURN e.name",
                name=name,
                source_records=[record.to_cypher_map() for record in sources],
            )

    async def write_entity_none(self, name: str) -> None:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            # CASE, not a separate WHERE guard: reads and writes atomically within the one
            # SET, so a concurrent live write racing this statement cannot be clobbered —
            # mirrors create_entity's own existing provenance_state CASE (service.py:2217).
            await session.run(
                "MATCH (e:Entity {name: $name}) "
                "SET e.provenance_state = "
                "CASE WHEN e.provenance_state = 'provenanced' THEN e.provenance_state ELSE 'none' END",
                name=name,
            )

    async def fetch_claim_candidates(self, cursor: str | None, limit: int) -> list[ClaimCandidate]:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (n:Claim) "
                f"WHERE ({_CANDIDATE_PREDICATE}) "
                "AND ($cursor IS NULL OR elementId(n) > $cursor) "
                "RETURN elementId(n) AS eid, n.claim_id AS claim_id, "
                "       n.content AS content, n.trace_id AS trace_id "
                "ORDER BY elementId(n) LIMIT $limit",
                cursor=cursor,
                limit=limit,
            )
            rows = await result.data()
        return [
            ClaimCandidate(
                element_id=r["eid"],
                claim_id=r["claim_id"],
                content=r["content"] or "",
                trace_id=r["trace_id"],
            )
            for r in rows
        ]

    async def write_claim_provenanced(self, claim_id: str, sources: Sequence[SourceRecord]) -> None:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            await session.run(
                "MATCH (cl:Claim {claim_id: $claim_id})\n"
                "SET cl.provenance_state = 'provenanced'\n"
                "WITH cl\n" + MemoryService._source_merge_clause("cl") + "RETURN cl.claim_id",
                claim_id=claim_id,
                source_records=[record.to_cypher_map() for record in sources],
            )

    async def write_claim_none(self, claim_id: str) -> None:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            await session.run(
                "MATCH (cl:Claim {claim_id: $claim_id}) "
                "SET cl.provenance_state = "
                "CASE WHEN cl.provenance_state = 'provenanced' THEN cl.provenance_state ELSE 'none' END",
                claim_id=claim_id,
            )

    async def mark_relationships_none(self) -> int:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH ()-[r]->() "
                "WHERE r.weight IS NOT NULL AND NOT type(r) IN $structural_types "
                f"AND ({_CANDIDATE_PREDICATE.replace('n.', 'r.')}) "
                "SET r.provenance_state = "
                "CASE WHEN r.provenance_state = 'provenanced' THEN r.provenance_state ELSE 'none' END "
                "RETURN count(r) AS n",
                structural_types=list(_STRUCTURAL_RELATIONSHIP_TYPES),
            )
            rec = await result.single()
        return int(rec["n"]) if rec else 0


# ---------------------------------------------------------------------------
# Reconstruction (pure — unit-tested directly)
# ---------------------------------------------------------------------------


def _reconstruct(
    capture: TaskCapture | None,
    tool_registry: "ToolRegistry",
    *,
    attribution: str,
) -> ReconstructOutcome:
    """Replay A4 containment for one candidate against its (already-resolved) capture.

    Args:
        capture: The candidate's minting capture, or None if it could not be resolved
            from either store.
        tool_registry: Registry to read each tool's ``referent_parameter`` declaration
            from (A2).
        attribution: The candidate's attribution string — an entity's name or a claim's
            content (ADR-0098 A4).

    Returns:
        The sources whose retrieved content contains ``attribution`` (possibly empty),
        or ``missing_capture=True`` when there was nothing to replay against.
    """
    if capture is None:
        return ReconstructOutcome(matched_sources=[], missing_capture=True)
    sources = sources_from_tool_results(
        capture.tool_results,
        retrieved_at=capture.timestamp,
        capture_trace_id=capture.trace_id,
        tool_registry=tool_registry,
    )
    matched_ids = set(associate(attribution, sources))
    matched = [source for source in sources if source.source_id in matched_ids]
    return ReconstructOutcome(matched_sources=matched, missing_capture=False)


# ---------------------------------------------------------------------------
# Orchestration (pure — unit-tested with a fake graph)
# ---------------------------------------------------------------------------


async def run_backfill(
    graph: GraphProtocol,
    capture_index: Mapping[str, Path],
    tool_registry: "ToolRegistry",
    es_client: Any | None,
    *,
    run_id: str,
    now: str,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BackfillReport:
    """Reconstruct provenance for every candidate entity/claim, then bulk-mark relationships.

    Args:
        graph: The graph seam (real Neo4j or an in-memory fake).
        capture_index: A ``build_capture_index()`` result.
        tool_registry: Registry to read each tool's referent declaration from.
        es_client: Open Elasticsearch client for the capture-resolution fallback, or None
            to resolve from disk only.
        run_id: Recorded on the report for correlation; not stamped on any node.
        now: ISO-8601 timestamp recorded on the report.
        dry_run: When True, issue zero writes — still counts and previews outcomes.
        batch_size: Max candidates read per DB page.

    Returns:
        A populated :class:`BackfillReport`.
    """
    report = BackfillReport(run_id=run_id, dry_run=dry_run, started_at=now)

    # Entities
    cursor: str | None = None
    while True:
        page = await graph.fetch_entity_candidates(cursor, batch_size)
        if not page:
            break
        cursor = page[-1].element_id
        captures = await read_captures_by_trace_ids(
            [c.originating_trace_id for c in page if c.originating_trace_id],
            disk_index=capture_index,
            es_client=es_client,
        )
        for cand in page:
            report.entities_total += 1
            try:
                capture = (
                    captures.get(cand.originating_trace_id) if cand.originating_trace_id else None
                )
                outcome = _reconstruct(capture, tool_registry, attribution=cand.name)
            except Exception as exc:  # noqa: BLE001 — one bad candidate never aborts the run
                report.entities_errors += 1
                log.warning(
                    "fre1348_entity_reconstruct_error",
                    name=cand.name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            if outcome.missing_capture:
                report.entities_none_missing_capture += 1
                if not dry_run:
                    await graph.write_entity_none(cand.name)
            elif outcome.matched_sources:
                report.entities_reconstructed += 1
                if not dry_run:
                    await graph.write_entity_provenanced(cand.name, outcome.matched_sources)
            else:
                report.entities_none_no_match += 1
                if not dry_run:
                    await graph.write_entity_none(cand.name)

    # Claims
    claim_cursor: str | None = None
    while True:
        claim_page = await graph.fetch_claim_candidates(claim_cursor, batch_size)
        if not claim_page:
            break
        claim_cursor = claim_page[-1].element_id
        claim_captures = await read_captures_by_trace_ids(
            [c.trace_id for c in claim_page if c.trace_id],
            disk_index=capture_index,
            es_client=es_client,
        )
        for claim_cand in claim_page:
            report.claims_total += 1
            try:
                capture = claim_captures.get(claim_cand.trace_id) if claim_cand.trace_id else None
                outcome = _reconstruct(capture, tool_registry, attribution=claim_cand.content)
            except Exception as exc:  # noqa: BLE001 — one bad candidate never aborts the run
                report.claims_errors += 1
                log.warning(
                    "fre1348_claim_reconstruct_error",
                    claim_id=claim_cand.claim_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            if outcome.missing_capture:
                report.claims_none_missing_capture += 1
                if not dry_run:
                    await graph.write_claim_none(claim_cand.claim_id)
            elif outcome.matched_sources:
                report.claims_reconstructed += 1
                if not dry_run:
                    await graph.write_claim_provenanced(
                        claim_cand.claim_id, outcome.matched_sources
                    )
            else:
                report.claims_none_no_match += 1
                if not dry_run:
                    await graph.write_claim_none(claim_cand.claim_id)

    # Relationships — never reconstructed (A4b: no trace/session key persisted), one
    # bulk pass covers the whole population.
    report.relationships_marked_none = 0 if dry_run else await graph.mark_relationships_none()

    report.success = report.entities_errors == 0 and report.claims_errors == 0
    report.finished_at = now
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(report: BackfillReport) -> None:
    """Print a human-readable run summary (structlog carries the machine record)."""
    mode = "DRY-RUN (no writes)" if report.dry_run else "APPLIED"
    print(f"\n=== FRE-1348 provenance backfill [{mode}] run_id={report.run_id} ===")
    print(
        f"entities: total={report.entities_total} reconstructed={report.entities_reconstructed} "
        f"none_no_match={report.entities_none_no_match} "
        f"none_missing_capture={report.entities_none_missing_capture} "
        f"errors={report.entities_errors}"
    )
    print(
        f"claims:   total={report.claims_total} reconstructed={report.claims_reconstructed} "
        f"none_no_match={report.claims_none_no_match} "
        f"none_missing_capture={report.claims_none_missing_capture} "
        f"errors={report.claims_errors}"
    )
    print(f"relationships marked none: {report.relationships_marked_none}")
    print(f"success: {report.success}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FRE-1348: reconstruct provenance for legacy knowledge items. Idempotent."
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

    from uuid import uuid4

    from personal_agent.tools import get_default_registry

    es_client: Any | None = None
    try:
        from elasticsearch import AsyncElasticsearch  # noqa: PLC0415

        es_client = AsyncElasticsearch(settings.elasticsearch_url)
    except Exception as exc:  # noqa: BLE001 — capture resolution degrades to disk-only
        print(
            f"Elasticsearch unavailable, resolving captures from disk only: {exc}", file=sys.stderr
        )

    graph = _Neo4jGraph(driver)
    try:
        capture_index = build_capture_index()
        tool_registry = get_default_registry()
        report = await run_backfill(
            graph,
            capture_index,
            tool_registry,
            es_client,
            run_id=f"fre1348-{uuid4()}",
            now=datetime.now(timezone.utc).isoformat(),
            dry_run=args.dry_run,
            batch_size=args.batch_size,
        )
        _print_summary(report)
        if args.report_path:
            args.report_path.write_bytes(orjson.dumps(asdict(report)))
            print(f"report written: {args.report_path}")
        return 0 if report.success else 1
    finally:
        if es_client is not None:
            await es_client.close()
        await driver.close()


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
