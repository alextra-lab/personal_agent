#!/usr/bin/env python3
"""One-time, idempotent Neo4j migration: re-type entity nodes V1 (7-type) → V2 (10-type) (FRE-772).

Backing: ADR-0109 (Accepted) + Amendment 1, Implementation Notes step 5. Re-types every ``:Entity``
node's scalar ``entity_type`` property from the inherited V1 vocabulary to the V2 vocabulary:

  * deterministic remap  — ``Technology`` → ``TechnicalArtifact``, ``Topic`` → ``DomainOrTopic``
    (``Person``/``Organization``/``Location``/``Event`` are valid verbatim in both → untouched);
  * model re-classification — each ``Concept`` node → exactly one of the five conceptual V2 types
    (``MethodOrConcept``/``DomainOrTopic``/``Phenomenon``/``QuantityMeasure``/``KnowledgeArtifact``),
    **fail-closed**: an out-of-set / empty / errored classification leaves the node ``Concept`` and
    records it as ``unclassified`` (a re-run retries it — the migration never guesses a type).

Idempotent: every write is guarded by ``WHERE entity_type = <V1>`` (already-migrated nodes are skipped)
and stamped with an update-provenance marker (``entity_type_migration`` = the run id, ADR-0074's
update-time analog — the original ``originating_*`` provenance is left intact). Batched in Python so no
single transaction exceeds ``--batch-size`` nodes; the ``Concept`` path pages by an ``elementId`` cursor
so a fail-closed node is not re-fetched within a run.

Snapshot + rollback: a reversible ``{name, entity_type}`` snapshot is written before mutating; ``--rollback``
restores ``entity_type`` from a snapshot by name (the MERGE key). Take a full Neo4j dump too before a prod run.

Usage:
    uv run python scripts/migrate_fre772_entity_type_v2.py --dry-run --confirm-prod   # preview, no writes
    uv run python scripts/migrate_fre772_entity_type_v2.py --confirm-prod             # migrate
    uv run python scripts/migrate_fre772_entity_type_v2.py --rollback \
        --snapshot-path <file> --confirm-prod                                         # restore

Preflight: refuses to run unless the in-process recall keyword map already speaks V2 (proof the FRE-793
consumer remap is in the deployed code) — override with ``--skip-consumer-check`` for the isolated test
substrate. Never run this before the V2 extractor prompt + FRE-793 remap are deployed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

import orjson

from personal_agent.config import resolve_role_model_key, settings
from personal_agent.second_brain.taxonomy import (
    V1_CONCEPT_TARGET_TYPES,
    V1_RETIRED_TYPES,
    V1_TO_V2_DETERMINISTIC,
)
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.cost_gate import CostGate

log = get_logger(__name__)

# Bumped when the classifier prompt/logic changes so a report can be tied to the instrument that produced it.
PROMPT_VERSION = "fre772-v1"

DEFAULT_BATCH_SIZE = 500
DEFAULT_CONCURRENCY = 4


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConceptNode:
    """A ``Concept`` entity awaiting model re-classification."""

    element_id: str
    name: str
    description: str


@dataclass(frozen=True)
class ClassifyResult:
    """Outcome of classifying one ``Concept`` node.

    Attributes:
        entity_type: A member of :data:`V1_CONCEPT_TARGET_TYPES`, or ``None`` when the model
            returned an out-of-set / empty answer or errored (fail-closed).
        cost_usd: Cost of the classification call (0.0 for the test fake).
        reason: Short machine tag describing a ``None`` outcome (e.g. ``"out_of_set"``, ``"error"``).
    """

    entity_type: str | None
    cost_usd: float = 0.0
    reason: str = ""


Classifier = Callable[[str, str], Awaitable[ClassifyResult]]


@dataclass
class MigrationReport:
    """Structured, serialisable record of a migration run (AC-4 evidence)."""

    run_id: str
    dry_run: bool
    prompt_version: str
    classifier_model: str
    started_at: str
    finished_at: str = ""
    before_histogram: dict[str, int] = field(default_factory=dict)
    after_histogram: dict[str, int] = field(default_factory=dict)
    deterministic: dict[str, int] = field(default_factory=dict)
    concept_classified: dict[str, int] = field(default_factory=dict)
    concept_unclassified: list[dict[str, str]] = field(default_factory=list)
    concept_total: int = 0
    cost_usd: float = 0.0
    v1_remnants_after: dict[str, int] = field(default_factory=dict)
    success: bool = False


# ---------------------------------------------------------------------------
# Graph seam — all Cypher lives behind this Protocol so the orchestration is unit-testable
# ---------------------------------------------------------------------------


class GraphProtocol(Protocol):
    """The minimal graph operations the migration needs (real impl: :class:`_Neo4jGraph`)."""

    async def count_by_type(self) -> dict[str, int]: ...

    async def snapshot(self) -> list[dict[str, str]]: ...

    async def remap_deterministic(
        self, v1: str, v2: str, *, run_id: str, now: str, batch: int
    ) -> int: ...

    async def fetch_concepts(self, cursor: str | None, limit: int) -> list[ConceptNode]: ...

    async def set_entity_type(self, element_id: str, v2: str, *, run_id: str, now: str) -> None: ...

    async def mark_error(self, element_id: str, reason: str, *, now: str) -> None: ...

    async def restore_types(self, rows: Sequence[dict[str, str]], *, batch: int) -> int: ...


class _Neo4jGraph:
    """Real :class:`GraphProtocol` over an async Neo4j driver."""

    def __init__(self, driver: object) -> None:
        self._driver = driver

    async def count_by_type(self) -> dict[str, int]:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (e:Entity) RETURN coalesce(e.entity_type, '') AS t, count(*) AS n"
            )
            rows = await result.data()
        return {row["t"]: row["n"] for row in rows}

    async def snapshot(self) -> list[dict[str, str]]:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (e:Entity) WHERE e.name IS NOT NULL "
                "RETURN e.name AS name, coalesce(e.entity_type, '') AS entity_type"
            )
            rows = await result.data()
        return [{"name": r["name"], "entity_type": r["entity_type"]} for r in rows]

    async def remap_deterministic(
        self, v1: str, v2: str, *, run_id: str, now: str, batch: int
    ) -> int:
        total = 0
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            while True:
                result = await session.run(
                    "MATCH (e:Entity) WHERE e.entity_type = $v1 "
                    "WITH e LIMIT $batch "
                    "SET e.entity_type = $v2, "
                    "    e.entity_type_migration = $run_id, "
                    "    e.entity_type_migrated_at = $now "
                    "RETURN count(e) AS n",
                    v1=v1,
                    v2=v2,
                    run_id=run_id,
                    now=now,
                    batch=batch,
                )
                rec = await result.single()
                n = rec["n"] if rec else 0
                total += n
                if n == 0:
                    break
        return total

    async def fetch_concepts(self, cursor: str | None, limit: int) -> list[ConceptNode]:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (e:Entity) WHERE e.entity_type = 'Concept' "
                "AND ($cursor IS NULL OR elementId(e) > $cursor) "
                "RETURN elementId(e) AS eid, e.name AS name, "
                "       coalesce(e.description, '') AS description "
                "ORDER BY elementId(e) LIMIT $limit",
                cursor=cursor,
                limit=limit,
            )
            rows = await result.data()
        return [
            ConceptNode(element_id=r["eid"], name=r["name"] or "", description=r["description"])
            for r in rows
        ]

    async def set_entity_type(self, element_id: str, v2: str, *, run_id: str, now: str) -> None:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            await session.run(
                "MATCH (e:Entity) WHERE elementId(e) = $eid "
                "SET e.entity_type = $v2, "
                "    e.entity_type_migration = $run_id, "
                "    e.entity_type_migrated_at = $now "
                "REMOVE e.entity_type_migration_error",
                eid=element_id,
                v2=v2,
                run_id=run_id,
                now=now,
            )

    async def mark_error(self, element_id: str, reason: str, *, now: str) -> None:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            await session.run(
                "MATCH (e:Entity) WHERE elementId(e) = $eid "
                "SET e.entity_type_migration_error = $reason, "
                "    e.entity_type_migration_error_at = $now",
                eid=element_id,
                reason=reason,
                now=now,
            )

    async def restore_types(self, rows: Sequence[dict[str, str]], *, batch: int) -> int:
        total = 0
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            for start in range(0, len(rows), batch):
                chunk = list(rows[start : start + batch])
                result = await session.run(
                    "UNWIND $rows AS row "
                    "MATCH (e:Entity {name: row.name}) "
                    "SET e.entity_type = row.entity_type "
                    "REMOVE e.entity_type_migration, e.entity_type_migrated_at, "
                    "       e.entity_type_migration_error, e.entity_type_migration_error_at "
                    "RETURN count(e) AS n",
                    rows=chunk,
                )
                rec = await result.single()
                total += rec["n"] if rec else 0
        return total


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


_CLASSIFIER_SYSTEM = (
    "You are a precise knowledge-graph entity-type classifier. "
    "Output only one type name from the allowed set, nothing else."
)

_CLASSIFIER_TEMPLATE = """\
Classify the following knowledge-graph entity into EXACTLY ONE of these five types.
Output only the type name, nothing else.

MethodOrConcept — a specific human-invented abstract idea, method, technique, algorithm, data
  structure, pattern, or principle. e.g. GraphRAG, trie, Nash equilibrium.
DomainOrTopic — a broad field, domain, discipline, or subject area as a whole. e.g. behavioral
  economics, cosmology, cybersecurity, game theory.
Phenomenon — a naturally-occurring physical/natural phenomenon, process, effect, force, limit, or
  observable that exists independently of human design. e.g. gravity, photosynthesis, the greenhouse
  effect, the diffraction limit.
QuantityMeasure — a named physical quantity, property, dimension, or unit of measure. e.g. wavelength,
  mass, temperature, frequency, luminosity.
KnowledgeArtifact — a concrete, named human-authored work whose purpose is to convey understanding to a
  reader — a document, ADR, report, paper, article, chapter, specification, or plan.

Entity name: {name}
Entity description: {description}

Type:"""


def _parse_classification(content: str) -> str | None:
    """Return the single conceptual V2 type named in ``content``, else ``None`` (fail-closed).

    Requires an unambiguous answer: exactly one of the five target types may appear.

    Args:
        content: Raw model output.

    Returns:
        The matched type, or ``None`` when zero or more than one target type is present.
    """
    hits = [t for t in V1_CONCEPT_TARGET_TYPES if t in content]
    if len(hits) == 1:
        return hits[0]
    return None


def _build_llm_classifier() -> tuple[Classifier, str]:
    """Build the production LiteLLM-backed classifier and return it with its model id.

    The classifier resolves the ``entity_extraction`` role so its cost lands in that budget lane and
    carries a ``SystemTraceContext`` so its cost/log rows join (ADR-0074). Any exception is caught and
    surfaced as a fail-closed :class:`ClassifyResult` (``entity_type=None``) so one bad node never aborts
    the run.

    Returns:
        A ``(classifier, model_id)`` pair.
    """
    from personal_agent.llm_client import ModelRole
    from personal_agent.llm_client.factory import get_llm_client
    from personal_agent.telemetry.trace import SystemTraceContext

    role = resolve_role_model_key("entity_extraction")
    client = get_llm_client(role_name=role)

    async def classify(name: str, description: str) -> ClassifyResult:
        prompt = _CLASSIFIER_TEMPLATE.format(name=name, description=description or "(none)")
        try:
            response = await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": prompt}],
                system_prompt=_CLASSIFIER_SYSTEM,
                trace_ctx=SystemTraceContext.new("entity_type_migration"),
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed: never abort the run on one node
            log.warning(
                "fre772_classify_error", entity=name, error=str(exc), error_type=type(exc).__name__
            )
            return ClassifyResult(entity_type=None, cost_usd=0.0, reason="error")
        entity_type = _parse_classification(response.get("content") or "")
        cost = float(response.get("cost_usd") or 0.0)
        if entity_type is None:
            return ClassifyResult(entity_type=None, cost_usd=cost, reason="out_of_set")
        return ClassifyResult(entity_type=entity_type, cost_usd=cost)

    return classify, role


# ---------------------------------------------------------------------------
# Orchestration (pure — unit-tested with a fake graph + fake classifier)
# ---------------------------------------------------------------------------


async def run_migration(
    graph: GraphProtocol,
    classifier: Classifier,
    *,
    run_id: str,
    now: str,
    classifier_model: str,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
    snapshot_path: Path | None = None,
) -> MigrationReport:
    """Re-type every V1 entity node to V2, idempotently. Returns the run report.

    Args:
        graph: The graph seam (real Neo4j or an in-memory fake).
        classifier: Async callable classifying one ``Concept`` node into a conceptual V2 type.
        run_id: Stable id stamped on every migrated node as update provenance.
        now: ISO-8601 timestamp stamped alongside ``run_id``.
        classifier_model: Model id recorded in the report.
        dry_run: When True, issue **zero** writes — still counts and previews Concept classifications.
        batch_size: Max nodes mutated per transaction / read per Concept page.
        concurrency: Max concurrent classifier calls.
        snapshot_path: When set and not ``dry_run``, write a reversible ``{name, entity_type}`` snapshot
            before mutating.

    Returns:
        A populated :class:`MigrationReport` (also its ``success`` flag: no V1-retired remnants left).
    """
    report = MigrationReport(
        run_id=run_id,
        dry_run=dry_run,
        prompt_version=PROMPT_VERSION,
        classifier_model=classifier_model,
        started_at=now,
    )
    report.before_histogram = await graph.count_by_type()

    if not dry_run and snapshot_path is not None:
        snap = await graph.snapshot()
        snapshot_path.write_bytes(orjson.dumps(snap))
        log.info("fre772_snapshot_written", path=str(snapshot_path), nodes=len(snap))

    # 1) Deterministic remaps (string changes only).
    for v1, v2 in V1_TO_V2_DETERMINISTIC.items():
        if dry_run:
            count = report.before_histogram.get(v1, 0)
        else:
            count = await graph.remap_deterministic(
                v1, v2, run_id=run_id, now=now, batch=batch_size
            )
        report.deterministic[f"{v1}->{v2}"] = count

    # 2) Concept re-classification, paged by an elementId cursor (fail-closed nodes are cursored past).
    classified: Counter[str] = Counter()
    semaphore = asyncio.Semaphore(concurrency)
    cursor: str | None = None
    while True:
        batch = await graph.fetch_concepts(cursor, batch_size)
        if not batch:
            break
        cursor = batch[-1].element_id

        async def _classify(node: ConceptNode) -> tuple[ConceptNode, ClassifyResult]:
            async with semaphore:
                return node, await classifier(node.name, node.description)

        results = await asyncio.gather(*(_classify(n) for n in batch))
        for node, res in results:
            report.concept_total += 1
            report.cost_usd += res.cost_usd
            target = res.entity_type
            if target is not None and target in V1_CONCEPT_TARGET_TYPES:
                classified[target] += 1
                if not dry_run:
                    await graph.set_entity_type(node.element_id, target, run_id=run_id, now=now)
            else:
                reason = res.reason or "out_of_set"
                report.concept_unclassified.append(
                    {"name": node.name, "element_id": node.element_id, "reason": reason}
                )
                if not dry_run:
                    await graph.mark_error(node.element_id, reason, now=now)
    report.concept_classified = dict(classified)

    report.after_histogram = await graph.count_by_type()
    report.v1_remnants_after = {
        t: report.after_histogram.get(t, 0)
        for t in V1_RETIRED_TYPES
        if report.after_histogram.get(t, 0) > 0
    }
    report.success = not dry_run and not report.v1_remnants_after
    report.finished_at = now
    return report


async def run_rollback(graph: GraphProtocol, snapshot_path: Path, *, batch_size: int) -> int:
    """Restore ``entity_type`` from a snapshot file, stripping the migration markers.

    Args:
        graph: The graph seam.
        snapshot_path: Path to a snapshot written by a prior run.
        batch_size: UNWIND chunk size.

    Returns:
        Number of nodes restored (nodes created after the snapshot are not matched and left as-is).
    """
    rows = orjson.loads(snapshot_path.read_bytes())
    restored = await graph.restore_types(rows, batch=batch_size)
    log.info("fre772_rollback_done", restored=restored, snapshot=str(snapshot_path))
    return restored


# ---------------------------------------------------------------------------
# Preflight + CLI
# ---------------------------------------------------------------------------


def _map_speaks_v2(keyword_map: object) -> bool:
    """Return True iff a recall keyword map emits no retired V1 type strings.

    Pure over its argument so the gate logic is unit-testable without the live module state (which flips
    when FRE-793 lands). Values may be a single string (V1) or a tuple/list of strings (post-FRE-793);
    both are flattened.

    Args:
        keyword_map: A mapping of query keyword → entity-type string(s).

    Returns:
        True when no value is a V1-only (retired) type string.
    """
    values: set[str] = set()
    for value in keyword_map.values():  # type: ignore[attr-defined]
        if isinstance(value, str):
            values.add(value)
        else:
            values.update(value)
    return values.isdisjoint(V1_RETIRED_TYPES)


def _consumer_speaks_v2() -> bool:
    """Return True iff the in-process recall keyword map emits no retired V1 type strings.

    Proves the FRE-793 consumer remap is present in the deployed code (the migration must not run while
    recall consumers still filter on V1 strings — see the plan's runbook).

    Returns:
        True when the live keyword map is V2-clean.
    """
    from personal_agent.orchestrator.executor import _ENTITY_TYPE_KEYWORDS

    return _map_speaks_v2(_ENTITY_TYPE_KEYWORDS)


def _print_summary(report: MigrationReport) -> None:
    """Print a human-readable run summary (structlog carries the machine record)."""
    mode = "DRY-RUN (no writes)" if report.dry_run else "APPLIED"
    print(f"\n=== FRE-772 entity-type migration [{mode}] run_id={report.run_id} ===")
    print(f"before: {dict(sorted(report.before_histogram.items()))}")
    print(f"after:  {dict(sorted(report.after_histogram.items()))}")
    print(f"deterministic: {report.deterministic}")
    print(
        f"concept: {report.concept_total} total, "
        f"classified={report.concept_classified}, "
        f"unclassified={len(report.concept_unclassified)}"
    )
    if report.concept_unclassified:
        for row in report.concept_unclassified:
            print(f"  UNCLASSIFIED  {row['name']!r} ({row['reason']})")
    print(f"cost_usd: {report.cost_usd:.4f}")
    print(f"v1_remnants_after: {report.v1_remnants_after or 'none'}")
    print(f"success: {report.success}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FRE-772: re-type entity nodes V1 (7-type) → V2 (10-type). Idempotent."
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
        "--rollback",
        action="store_true",
        default=False,
        help="Restore entity_type from --snapshot-path instead of migrating.",
    )
    parser.add_argument(
        "--snapshot-path", type=Path, default=None, help="Snapshot file to write/restore."
    )
    parser.add_argument(
        "--report-path", type=Path, default=None, help="Where to write the JSON report."
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--skip-consumer-check",
        action="store_true",
        default=False,
        help="Skip the FRE-793 preflight (isolated test substrate only).",
    )
    return parser.parse_args()


async def _setup_cost_gate() -> CostGate:
    """Construct, connect, and register a CostGate mirroring the gateway app's startup wiring.

    ``LiteLLMClient.respond`` calls ``get_default_gate()`` (ADR-0065), which raises ``RuntimeError``
    when no gate is registered — the standalone script never did this, so every Concept
    classification failed fast and the dry-run reported 3888 UNCLASSIFIED at cost 0.0000 (FRE-800).
    Must be called, and the gate registered, before any ``respond()`` call.

    Returns:
        The connected, registered ``CostGate``. The caller owns disconnecting it.
    """
    from personal_agent.cost_gate import CostGate, load_budget_config, set_default_gate

    budget_config = load_budget_config()
    gate = CostGate(config=budget_config, db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    return gate


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
    cost_gate: CostGate | None = None
    try:
        if args.rollback:
            if not args.snapshot_path or not args.snapshot_path.exists():
                print("--rollback requires an existing --snapshot-path.", file=sys.stderr)
                return 2
            restored = await run_rollback(graph, args.snapshot_path, batch_size=args.batch_size)
            print(f"Rollback complete — {restored} node(s) restored from {args.snapshot_path}.")
            return 0

        if not args.skip_consumer_check and not _consumer_speaks_v2():
            print(
                "ERROR: the in-process recall keyword map still emits V1 type strings.\n"
                "The FRE-793 consumer remap must be deployed before this migration runs "
                "(see the plan runbook). Use --skip-consumer-check only on the isolated test substrate.",
                file=sys.stderr,
            )
            return 3

        # Every Concept classification is a paid LLM call gated by the Cost Check Gate
        # (ADR-0065) — register it before building the classifier (FRE-800).
        cost_gate = await _setup_cost_gate()

        classifier, model = _build_llm_classifier()
        report = await run_migration(
            graph,
            classifier,
            run_id=f"fre772-{uuid4()}",
            now=datetime.now(timezone.utc).isoformat(),
            classifier_model=model,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            snapshot_path=args.snapshot_path,
        )
        _print_summary(report)
        if args.report_path:
            args.report_path.write_bytes(orjson.dumps(asdict(report)))
            print(f"report written: {args.report_path}")
        return 0 if (report.success or args.dry_run) else 4
    finally:
        if cost_gate is not None:
            from personal_agent.cost_gate import set_default_gate

            set_default_gate(None)
            # This one-shot script has no reaper task running (unlike the gateway app's
            # 30s-cadence background sweep) — reap once here so a mid-run crash doesn't
            # leave a reservation occupying budget headroom for its full 180s TTL.
            await cost_gate.reap_stale()
            await cost_gate.disconnect()
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
