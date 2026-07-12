#!/usr/bin/env python3
"""One-time, idempotent Neo4j backfill: classify existing ``:Entity`` nodes carrying
``class IS NULL`` (FRE-865, ADR-0115 D2 + Implementation Notes step 5 + Risks table).

FRE-863/864 shipped the two-axis emission contract and the Entity persistence write, so **new**
extractions carry ``class ∈ {World, Personal}``. The ~7,992 entities that existed before that write
landed carry no class at all. This script re-runs classification over them:

  * ``output_kind = knowledge`` → sets ``e.class`` to ``World`` or ``Personal``, fail-**open** to
    ``World`` on any classifier uncertainty or parse/call failure (ADR-0115 D4 — never drop a
    candidate). This is the mirror image of ``migrate_fre772_entity_type_v2.py``'s Concept
    classifier, which is fail-**closed** (leaves the node untouched, retried next run); FRE-865
    must never leave an existing entity permanently unclassified.
  * ``output_kind ∈ {ephemeral, finding}`` (System-natured) → the entity is **marked for later
    dispatch** via ``e.class_backfill_output_kind``, NOT given a ``World``/``Personal`` value.
    This does *not* satisfy ADR-0115 D3's "absent from Core" invariant — the FRE-728 dispatch
    consumer (a separate, unbuilt ticket) is what would actually move/delete these nodes. This
    script only identifies them.

**Classifier fidelity note:** unlike the emission-time extractor (which classifies from full
conversation-turn context), this backfill classifies from ``(name, entity_type, description)``
alone — the only context an already-materialized Entity node carries. This is a **meaningfully
lower-fidelity** classification than emission time, and is expected to skew ``World`` more than a
full-context classification would (the ADR's own recovery story for a bad call — "raw turn kept in
ES" — is a channel this standalone classifier does not consult). Accepted here because this ticket
proves the script on the **test substrate only**; the actual prod corpus backfill is a separate,
later, master-gated ops action that can revisit fidelity if the observed leak/miss rate warrants it.

**Fail-open safety valve:** every candidate this run still resolves to *some* outcome (D4's
"never drop a candidate" always holds), but if the fail-open ratio (or whole-batch-exception rate)
exceeds ``--fail-open-threshold`` across a run of at least ``--fail-open-min-sample`` candidates,
``report.success`` is set to False and the CLI exits non-zero with a printed warning — surfacing a
suspected classifier-path outage so the operator investigates before trusting that run's World
labels, rather than silently mass-labeling the corpus on a broken classifier.

Idempotent: candidates are ``WHERE e.class IS NULL AND e.class_backfill_output_kind IS NULL`` — a
classified or marked node is excluded from all future runs (no re-billing). Known, accepted gap: a
node classified (billed) but whose terminal write is lost to a mid-run crash still matches the
candidate predicate and is re-classified (re-billed) next run; no resumable-billing ledger is built
for this given test-substrate-only scale.

Rollback is run-id-based (simpler than FRE-772's snapshot file, since every touched node started
from ``class IS NULL``): ``run_rollback`` restores ``class`` + strips the ``class_backfill_*``
markers for every node stamped with a given ``run_id``, **skipping** (and reporting by name) any
node whose ``last_seen`` moved past the backfill's own write — i.e. live traffic touched it since,
so rollback declines to clobber it. This guard is scoped to test-substrate use; a prod rollback
story is designed when a prod run is actually planned.

Usage:
    uv run python scripts/migrate_fre865_entity_class_backfill.py --dry-run --confirm-prod
    uv run python scripts/migrate_fre865_entity_class_backfill.py --confirm-prod
    uv run python scripts/migrate_fre865_entity_class_backfill.py --rollback --run-id <id> --confirm-prod
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

import orjson

from personal_agent.config import resolve_role_model_key, settings
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.cost_gate import CostGate

log = get_logger(__name__)

PROMPT_VERSION = "fre865-v1"

DEFAULT_BATCH_SIZE = 500
DEFAULT_CLASSIFY_BATCH_SIZE = 40
DEFAULT_CONCURRENCY = 4
DEFAULT_FAIL_OPEN_THRESHOLD = 0.5
DEFAULT_FAIL_OPEN_MIN_SAMPLE = 20

_VALID_OUTPUT_KINDS = frozenset({"knowledge", "ephemeral", "finding"})
_VALID_ENTITY_CLASSES = frozenset({"World", "Personal"})


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityCandidate:
    """An ``:Entity`` node with ``class IS NULL`` awaiting backfill classification."""

    element_id: str
    name: str
    entity_type: str
    description: str


@dataclass(frozen=True)
class ClassifyResult:
    """Outcome of classifying one entity candidate.

    Attributes:
        output_kind: One of ``_VALID_OUTPUT_KINDS``.
        knowledge_class: ``World``/``Personal`` when ``output_kind == "knowledge"``, else None.
        fail_open: True when the D4 default was applied (classifier uncertainty, a parse anomaly,
            or a whole-batch call exception) rather than a confident model answer.
        reason: Short machine tag for a fail-open outcome (e.g. ``"out_of_set"``, ``"missing"``,
            ``"ambiguous_index"``, ``"error"``). Empty for a confident classification.
    """

    output_kind: str
    knowledge_class: str | None
    fail_open: bool
    reason: str = ""


@dataclass(frozen=True)
class BatchClassifyResult:
    """Outcome of classifying one batch of entity candidates.

    Attributes:
        results: Exactly one :class:`ClassifyResult` per input candidate, in the same order.
        cost_usd: Cost of the single batch call (0.0 for the test fake).
        input_tokens: Prompt tokens billed for the call.
        output_tokens: Completion tokens for the call.
        cached_tokens: Prompt tokens served from the provider cache (proof the cache-stable prefix
            worked).
    """

    results: list[ClassifyResult]
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class BackfillReport:
    """Structured, serialisable record of a backfill run."""

    run_id: str
    dry_run: bool
    prompt_version: str
    classifier_model: str
    started_at: str
    finished_at: str = ""
    before_class_histogram: dict[str, int] = field(default_factory=dict)
    after_class_histogram: dict[str, int] = field(default_factory=dict)
    classified_world: int = 0
    classified_personal: int = 0
    marked_for_dispatch: dict[str, int] = field(default_factory=dict)
    fail_open_count: int = 0
    total_candidates_this_run: int = 0
    remaining_unclassified: int = 0
    model_calls: int = 0
    batch_count: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    success: bool = False


# A batch classifier maps N entity candidates → one :class:`BatchClassifyResult`.
BatchClassifier = Callable[[Sequence[EntityCandidate]], Awaitable[BatchClassifyResult]]


# ---------------------------------------------------------------------------
# Graph seam — all Cypher lives behind this Protocol so the orchestration is unit-testable
# ---------------------------------------------------------------------------


class GraphProtocol(Protocol):
    """The minimal graph operations the backfill needs (real impl: :class:`_Neo4jGraph`)."""

    async def count_by_class(self) -> dict[str, int]: ...

    async def count_unclassified(self) -> int: ...

    async def fetch_candidates(self, cursor: str | None, limit: int) -> list[EntityCandidate]: ...

    async def set_class(
        self, element_id: str, class_value: str, *, fail_open: bool, run_id: str, now: str
    ) -> None: ...

    async def mark_for_dispatch(
        self, element_id: str, output_kind: str, *, run_id: str, now: str
    ) -> None: ...

    async def restore_by_run_id(self, run_id: str) -> tuple[int, list[str]]: ...


class _Neo4jGraph:
    """Real :class:`GraphProtocol` over an async Neo4j driver."""

    def __init__(self, driver: object) -> None:
        self._driver = driver

    async def count_by_class(self) -> dict[str, int]:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (e:Entity) RETURN coalesce(e.class, '(unset)') AS c, count(*) AS n"
            )
            rows = await result.data()
        return {row["c"]: row["n"] for row in rows}

    async def count_unclassified(self) -> int:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (e:Entity) "
                "WHERE e.class IS NULL AND e.class_backfill_output_kind IS NULL "
                "RETURN count(e) AS n"
            )
            rec = await result.single()
        return int(rec["n"]) if rec else 0

    async def fetch_candidates(self, cursor: str | None, limit: int) -> list[EntityCandidate]:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            result = await session.run(
                "MATCH (e:Entity) "
                "WHERE e.class IS NULL AND e.class_backfill_output_kind IS NULL "
                "AND ($cursor IS NULL OR elementId(e) > $cursor) "
                "RETURN elementId(e) AS eid, e.name AS name, "
                "       coalesce(e.entity_type, '') AS entity_type, "
                "       coalesce(e.description, '') AS description "
                "ORDER BY elementId(e) LIMIT $limit",
                cursor=cursor,
                limit=limit,
            )
            rows = await result.data()
        return [
            EntityCandidate(
                element_id=r["eid"],
                name=r["name"] or "",
                entity_type=r["entity_type"],
                description=r["description"],
            )
            for r in rows
        ]

    async def set_class(
        self, element_id: str, class_value: str, *, fail_open: bool, run_id: str, now: str
    ) -> None:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            await session.run(
                "MATCH (e:Entity) WHERE elementId(e) = $eid "
                "SET e.class = $class_value, "
                "    e.class_backfill_run_id = $run_id, "
                # datetime($now), not the raw string: e.last_seen is a native Neo4j datetime
                # (set via Cypher datetime() in create_entity), and restore_by_run_id compares
                # the two directly — mismatched types (datetime vs string) would break that guard.
                "    e.class_backfill_at = datetime($now), "
                "    e.class_backfill_fail_open = $fail_open",
                eid=element_id,
                class_value=class_value,
                run_id=run_id,
                now=now,
                fail_open=fail_open,
            )

    async def mark_for_dispatch(
        self, element_id: str, output_kind: str, *, run_id: str, now: str
    ) -> None:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            await session.run(
                "MATCH (e:Entity) WHERE elementId(e) = $eid "
                "SET e.class_backfill_output_kind = $output_kind, "
                "    e.class_backfill_run_id = $run_id, "
                "    e.class_backfill_at = datetime($now)",
                eid=element_id,
                output_kind=output_kind,
                run_id=run_id,
                now=now,
            )

    async def restore_by_run_id(self, run_id: str) -> tuple[int, list[str]]:
        async with self._driver.session() as session:  # type: ignore[attr-defined]
            # Restore only nodes whose last_seen has not moved past this backfill's own write —
            # a later last_seen means live traffic touched the node since, so rollback declines to
            # clobber it (reported, not silently skipped). last_seen is HETEROGENEOUS across the
            # substrate (a plain ISO string on the Turn-DISCUSSES-Entity mention path,
            # memory/service.py:1060, vs. a native Neo4j datetime() on the create/access path,
            # service.py:1341 — see service.py:278's own comment on this). class_backfill_at is
            # always a native datetime (written via datetime($now)). Comparing the two directly
            # (datetime <= string) evaluates to null in Cypher, silently matching neither branch —
            # wrap both in toString(), mirroring the codebase's established coercion
            # (service.py:285/300/308), so the comparison is well-defined for either representation.
            result = await session.run(
                "MATCH (e:Entity) WHERE e.class_backfill_run_id = $run_id "
                "AND (e.last_seen IS NULL OR toString(e.last_seen) <= toString(e.class_backfill_at)) "
                "REMOVE e.class, e.class_backfill_run_id, e.class_backfill_at, "
                "       e.class_backfill_output_kind, e.class_backfill_fail_open "
                "RETURN count(e) AS n",
                run_id=run_id,
            )
            rec = await result.single()
            restored = int(rec["n"]) if rec else 0

            skipped_result = await session.run(
                "MATCH (e:Entity) WHERE e.class_backfill_run_id = $run_id "
                "AND e.last_seen IS NOT NULL AND toString(e.last_seen) > toString(e.class_backfill_at) "
                "RETURN e.name AS name",
                run_id=run_id,
            )
            skipped_rows = await skipped_result.data()
        return restored, [r["name"] for r in skipped_rows]


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

_CLASSIFIER_SYSTEM = (
    "You are a precise knowledge-graph entity classifier. "
    "Output only the requested lines, nothing else."
)

# Cache-stable prefix (ADR-0115's two-axis contract, condensed from entity_extraction.py's prompt).
# Must be a byte-identical prefix on every batch call — only the entity batch is appended after it —
# so the provider caches it after the first call.
_CLASSIFIER_PREFIX = """\
Classify each knowledge-graph entity below on two axes.

OUTPUT KIND — exactly one of:
  knowledge — durable, subject-matter knowledge worth keeping in a personal memory graph.
  ephemeral — transient noise: test scaffolding, throwaway artifacts, nothing worth keeping.
  finding — the agent's own infrastructure/tooling/telemetry/healthcheck self-observation.

KNOWLEDGE CLASS — only when output_kind is "knowledge", exactly one of:
  World — impersonal, general-world knowledge (a technology, a field, a public fact).
  Personal — knowledge about the user's own life, relationships, situation, or belongings.

For EACH numbered entity below, output one line of the form
  <number>. <output_kind>[ | <class>]
including "| <class>" only when output_kind is "knowledge". Use the entity's own number, one line
per entity, in the same order as the entities, nothing else on the line.

Entities:
"""

# A batch output line: leading entity number, a separator, then output_kind[ | class].
_BATCH_LINE_RE = re.compile(r"^\s*(\d+)\s*[.):\-]\s*(.*)$")


def _render_batch(nodes: Sequence[EntityCandidate]) -> str:
    """Render the variable tail of the batch prompt (numbered entities)."""
    return "\n".join(
        f"{i}. name: {node.name} | type: {node.entity_type} | "
        f"description: {node.description or '(none)'}"
        for i, node in enumerate(nodes, start=1)
    )


def _build_batch_prompt(nodes: Sequence[EntityCandidate]) -> str:
    """Return the full user prompt: the byte-identical prefix + the numbered entity batch."""
    return _CLASSIFIER_PREFIX + _render_batch(nodes)


_FAIL_OPEN_DEFAULT = ("knowledge", "World")


def _parse_one_line(content: str) -> tuple[str, str | None, str]:
    """Parse a single answer line into ``(output_kind, class|None, reason)``, fail-open on anomaly.

    Returns:
        ``reason`` is empty for a confident classification, else a machine tag identifying why the
        D4 fail-open default (``knowledge``, ``World``) was applied.
    """
    parts = [p.strip() for p in content.split("|")]
    kind = parts[0].strip().lower()
    if kind not in _VALID_OUTPUT_KINDS:
        return (*_FAIL_OPEN_DEFAULT, "out_of_set")
    if kind != "knowledge":
        return kind, None, ""
    if len(parts) < 2:
        return (*_FAIL_OPEN_DEFAULT, "out_of_set")
    cls = parts[1].strip()
    if cls not in _VALID_ENTITY_CLASSES:
        return (*_FAIL_OPEN_DEFAULT, "out_of_set")
    return "knowledge", cls, ""


def _parse_batch_classification(content: str, count: int) -> list[ClassifyResult]:
    """Map a batch response to one :class:`ClassifyResult` per entity, fail-OPEN per anomaly.

    Unlike FRE-772's fail-closed Concept parser, every anomaly here (missing line, duplicated
    index, ambiguous/off-vocabulary answer, or a whole-batch unnumbered response) resolves to the
    ADR-0115 D4 default (``knowledge``/``World``) rather than leaving the entity unresolved — this
    backfill must never drop a candidate.

    Args:
        content: Raw model output for the batch.
        count: Number of entities in the batch (indices ``1..count``).

    Returns:
        A list of length ``count``; entry ``i`` is the outcome for the ``(i+1)``-th entity.
    """
    seen: dict[int, str] = {}
    duplicated: set[int] = set()
    for line in content.splitlines():
        match = _BATCH_LINE_RE.match(line)
        if not match:
            continue
        idx = int(match.group(1))
        if idx in seen:
            duplicated.add(idx)
        seen[idx] = match.group(2)

    out: list[ClassifyResult] = []
    for i in range(1, count + 1):
        kind: str
        cls: str | None
        if i in duplicated:
            kind, cls = _FAIL_OPEN_DEFAULT
            out.append(ClassifyResult(kind, cls, fail_open=True, reason="ambiguous_index"))
        elif i not in seen:
            kind, cls = _FAIL_OPEN_DEFAULT
            out.append(ClassifyResult(kind, cls, fail_open=True, reason="missing"))
        else:
            kind, cls, reason = _parse_one_line(seen[i])
            out.append(ClassifyResult(kind, cls, fail_open=bool(reason), reason=reason))
    return out


def _build_llm_batch_classifier() -> tuple[BatchClassifier, str]:
    """Build the production LiteLLM-backed batch classifier and return it with its model id.

    One ``respond()`` call classifies a whole batch: the byte-identical :data:`_CLASSIFIER_PREFIX`
    is sent once, with only the numbered entity batch appended, so the definition block is billed
    at the provider cached rate after the first call. The classifier resolves the
    ``entity_extraction`` role so its cost lands in that budget lane and carries a
    ``SystemTraceContext`` so its cost/log rows join (ADR-0074). This function does not itself
    catch call exceptions — :func:`run_backfill`'s own per-batch wrapper does, converting any
    raised exception (from this or any other :data:`BatchClassifier`) into a fail-open batch result,
    so the fail-open contract holds regardless of which classifier implementation is plugged in.

    Deliberately bypasses ``get_llm_client(role_name=...)`` for the cloud path: that factory's
    ``budget_role_for(role_name)`` (cost_gate/__init__.py) expects the ORIGINAL role name
    (``"entity_extraction"``) to map to the ``entity_extraction`` budget lane, but
    ``resolve_role_model_key("entity_extraction")`` returns a resolved MODEL KEY (e.g.
    ``"gpt-5.4-mini"``, per ``config/model_roles.yaml``), which misses that lookup and silently
    falls back to ``"main_inference"`` — the same latent mis-billing already present in
    ``entity_extraction.py``'s ``get_llm_client(role_name=entity_extraction_role)`` call and
    ``migrate_fre772_entity_type_v2.py``'s identical pattern (pre-existing, out of this ticket's
    scope to fix repo-wide). Here we mirror ``entity_extraction.py``'s OWN eval-override branch,
    which already works around this by constructing ``LiteLLMClient`` directly with an explicit
    ``budget_role="entity_extraction"`` — so this backfill's spend lands in the correct budget
    lane regardless of the shared factory's latent bug.

    Returns:
        A ``(batch_classifier, model_id)`` pair.
    """
    from personal_agent.config import load_model_config
    from personal_agent.llm_client import ModelRole
    from personal_agent.telemetry.trace import SystemTraceContext

    role = resolve_role_model_key("entity_extraction")
    model_def = load_model_config().models.get(role)
    # Typed Any to match get_llm_client's own return annotation (factory.py) — LocalLLMClient's
    # concrete respond() signature (extra named kwargs) doesn't structurally satisfy the LLMClient
    # Protocol's **kwargs catch-all, the same pre-existing mismatch the factory function sidesteps.
    client: Any
    if model_def is not None and model_def.provider_type != "local":
        from personal_agent.llm_client.litellm_client import LiteLLMClient

        client = LiteLLMClient(
            model_id=model_def.id,
            provider=model_def.provider or "anthropic",
            max_tokens=model_def.max_tokens or 8192,
            budget_role="entity_extraction",
        )
    else:
        from personal_agent.llm_client.client import LocalLLMClient

        client = LocalLLMClient()

    async def classify(nodes: Sequence[EntityCandidate]) -> BatchClassifyResult:
        prompt = _build_batch_prompt(nodes)
        response = await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": prompt}],
            system_prompt=_CLASSIFIER_SYSTEM,
            trace_ctx=SystemTraceContext.new("entity_class_backfill"),
        )
        results = _parse_batch_classification(response.get("content") or "", len(nodes))
        usage = response.get("usage") or {}
        return BatchClassifyResult(
            results=results,
            cost_usd=float(response.get("cost_usd") or 0.0),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
        )

    return classify, role


# ---------------------------------------------------------------------------
# Orchestration (pure — unit-tested with a fake graph + fake classifier)
# ---------------------------------------------------------------------------


async def run_backfill(
    graph: GraphProtocol,
    classifier: BatchClassifier,
    *,
    run_id: str,
    now: str,
    prompt_version: str,
    classifier_model: str,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    classify_batch_size: int = DEFAULT_CLASSIFY_BATCH_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
    fail_open_threshold: float = DEFAULT_FAIL_OPEN_THRESHOLD,
    fail_open_min_sample: int = DEFAULT_FAIL_OPEN_MIN_SAMPLE,
) -> BackfillReport:
    """Classify every candidate ``:Entity`` (``class IS NULL``), idempotently. Returns the run report.

    Args:
        graph: The graph seam (real Neo4j or an in-memory fake).
        classifier: Async callable classifying a batch of :class:`EntityCandidate`.
        run_id: Stable id stamped on every touched node as backfill provenance.
        now: ISO-8601 timestamp stamped alongside ``run_id``.
        prompt_version: Classifier prompt version recorded in the report.
        classifier_model: Model id recorded in the report.
        dry_run: When True, issue **zero** writes — still counts and previews outcomes.
        batch_size: Max candidates read per DB page.
        classify_batch_size: Candidates classified per model call.
        concurrency: Max concurrent classifier (batch) calls.
        fail_open_threshold: Fail-open ratio above which the run is flagged unsuccessful (a
            suspected classifier-path outage) — see module docstring. Every candidate still
            resolves to an outcome regardless; this only affects ``report.success``.
        fail_open_min_sample: Minimum candidates this run before the fail-open ratio is
            evaluated — a small sample's fail-open ratio is not a reliable outage signal.

    Returns:
        A populated :class:`BackfillReport`.
    """
    report = BackfillReport(
        run_id=run_id,
        dry_run=dry_run,
        prompt_version=prompt_version,
        classifier_model=classifier_model,
        started_at=now,
    )
    report.before_class_histogram = await graph.count_by_class()

    marked: dict[str, int] = {}
    semaphore = asyncio.Semaphore(concurrency)
    cursor: str | None = None
    while True:
        page = await graph.fetch_candidates(cursor, batch_size)
        if not page:
            break
        cursor = page[-1].element_id

        sub_batches = [
            page[i : i + classify_batch_size] for i in range(0, len(page), classify_batch_size)
        ]

        async def _classify(
            group: Sequence[EntityCandidate],
        ) -> tuple[Sequence[EntityCandidate], BatchClassifyResult]:
            async with semaphore:
                try:
                    return group, await classifier(group)
                except Exception as exc:  # noqa: BLE001 — fail-open: one bad batch never aborts
                    log.warning(
                        "fre865_batch_classify_error",
                        batch_size=len(group),
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    kind, cls = _FAIL_OPEN_DEFAULT
                    return group, BatchClassifyResult(
                        results=[
                            ClassifyResult(kind, cls, fail_open=True, reason="error") for _ in group
                        ]
                    )

        for group, res in await asyncio.gather(*(_classify(g) for g in sub_batches)):
            report.model_calls += 1
            report.batch_count += 1
            report.cost_usd += res.cost_usd
            report.input_tokens += res.input_tokens
            report.output_tokens += res.output_tokens
            report.cached_tokens += res.cached_tokens
            # strict=False: a short/oversized result list from a misbehaving classifier must not
            # abort the run — unmatched candidates are simply not resolved this call (retried next
            # run via the same candidate predicate, since nothing was written for them).
            for node, res_item in zip(group, res.results, strict=False):
                report.total_candidates_this_run += 1
                # A BatchClassifier is a pluggable Protocol (the LLM-backed one validates via
                # _parse_one_line, but nothing enforces that on an arbitrary implementation) — an
                # off-enum output_kind/class from ANY classifier falls open to D4's default here,
                # never written verbatim, so a misbehaving classifier can't put an out-of-enum
                # value on e.class (violating the World/Personal invariant) or an arbitrary
                # e.class_backfill_output_kind value. fail_open increments at most once per
                # candidate regardless of how many validation issues stack, so the ratio used by
                # the safety valve stays a true per-candidate rate.
                output_kind = res_item.output_kind
                cls = res_item.knowledge_class
                fail_open = res_item.fail_open
                if output_kind not in _VALID_OUTPUT_KINDS or (
                    output_kind == "knowledge" and cls not in _VALID_ENTITY_CLASSES
                ):
                    output_kind, cls = _FAIL_OPEN_DEFAULT
                    fail_open = True
                if fail_open:
                    report.fail_open_count += 1
                if output_kind == "knowledge":
                    # cls is guaranteed non-None here: either it was already a valid member of
                    # _VALID_ENTITY_CLASSES, or the override above replaced it with the fail-open
                    # default ("World") — both branches of the "or" above cover every path that
                    # reaches this line with output_kind == "knowledge".
                    assert cls is not None
                    if cls == "World":
                        report.classified_world += 1
                    else:
                        report.classified_personal += 1
                    if not dry_run:
                        await graph.set_class(
                            node.element_id,
                            cls,
                            fail_open=fail_open,
                            run_id=run_id,
                            now=now,
                        )
                else:
                    marked[output_kind] = marked.get(output_kind, 0) + 1
                    if not dry_run:
                        await graph.mark_for_dispatch(
                            node.element_id, output_kind, run_id=run_id, now=now
                        )
    report.marked_for_dispatch = marked

    report.after_class_histogram = (
        report.before_class_histogram if dry_run else await graph.count_by_class()
    )
    report.remaining_unclassified = await graph.count_unclassified()

    fail_open_ratio = (
        report.fail_open_count / report.total_candidates_this_run
        if report.total_candidates_this_run
        else 0.0
    )
    valve_tripped = (
        report.total_candidates_this_run >= fail_open_min_sample
        and fail_open_ratio > fail_open_threshold
    )
    # Deliberately NOT `not dry_run and ...` (unlike FRE-772's "a dry run is never a completed
    # migration" success semantics): the whole point of --dry-run here is letting an operator
    # preview classifier health (the fail-open ratio) BEFORE spending money on the real run. If
    # dry runs always reported success=False, a healthy preview and an outage-flagged preview
    # would print identically, defeating that purpose.
    report.success = not valve_tripped
    report.finished_at = now
    return report


async def run_rollback(graph: GraphProtocol, run_id: str) -> tuple[int, list[str]]:
    """Restore ``class`` + strip the ``class_backfill_*`` markers for every node in ``run_id``.

    Skips (rather than clobbers) any node whose ``last_seen`` moved past this backfill's own write
    — live traffic touched it since the backfill ran.

    Args:
        graph: The graph seam.
        run_id: The backfill run to restore.

    Returns:
        ``(restored_count, skipped_names)``.
    """
    restored, skipped = await graph.restore_by_run_id(run_id)
    log.info("fre865_rollback_done", restored=restored, skipped=len(skipped), run_id=run_id)
    return restored, skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(report: BackfillReport) -> None:
    """Print a human-readable run summary (structlog carries the machine record)."""
    mode = "DRY-RUN (no writes)" if report.dry_run else "APPLIED"
    print(f"\n=== FRE-865 entity-class backfill [{mode}] run_id={report.run_id} ===")
    print(f"prompt_version={report.prompt_version} classifier_model={report.classifier_model}")
    print(f"before: {dict(sorted(report.before_class_histogram.items()))}")
    print(f"after:  {dict(sorted(report.after_class_histogram.items()))}")
    print(
        f"candidates this run: {report.total_candidates_this_run} "
        f"(World={report.classified_world}, Personal={report.classified_personal}, "
        f"marked_for_dispatch={report.marked_for_dispatch}, fail_open={report.fail_open_count})"
    )
    print(f"remaining unclassified: {report.remaining_unclassified}")
    print(
        f"model calls: {report.model_calls} (batches: {report.batch_count}); "
        f"tokens in={report.input_tokens} out={report.output_tokens} cached={report.cached_tokens}"
    )
    print(f"cost_usd: {report.cost_usd:.4f}")
    if not report.success:
        preview_note = " (this was a preview — nothing was written)" if report.dry_run else ""
        print(
            "WARNING: fail-open ratio exceeded threshold — this run's classifications look "
            "suspect (possible classifier-path outage)"
            f"{preview_note}. Investigate before trusting the World labels this run applied. "
            "Every candidate still received an outcome (D4 preserved)."
        )
    print(f"success: {report.success}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FRE-865: backfill class on existing class=None :Entity nodes. Idempotent."
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
        help="Restore class + markers for --run-id instead of backfilling.",
    )
    parser.add_argument("--run-id", type=str, default=None, help="Run id to roll back.")
    parser.add_argument(
        "--report-path", type=Path, default=None, help="Where to write the JSON report."
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--classify-batch-size", type=int, default=DEFAULT_CLASSIFY_BATCH_SIZE)
    parser.add_argument(
        "--fail-open-threshold",
        type=float,
        default=DEFAULT_FAIL_OPEN_THRESHOLD,
        help="Fail-open ratio above which the run is flagged unsuccessful (default 0.5).",
    )
    parser.add_argument(
        "--fail-open-min-sample",
        type=int,
        default=DEFAULT_FAIL_OPEN_MIN_SAMPLE,
        help="Minimum candidates before the fail-open ratio is evaluated (default 20).",
    )
    return parser.parse_args()


async def _setup_cost_gate() -> CostGate:
    """Construct, connect, and register a CostGate mirroring the gateway app's startup wiring.

    ``LiteLLMClient.respond`` calls ``get_default_gate()`` (ADR-0065), which raises ``RuntimeError``
    when no gate is registered — must be called, and the gate registered, before any ``respond()``
    call (FRE-800 regression class).

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
            if not args.run_id:
                print("--rollback requires --run-id.", file=sys.stderr)
                return 2
            restored, skipped = await run_rollback(graph, args.run_id)
            print(f"Rollback complete — {restored} node(s) restored for run_id={args.run_id}.")
            if skipped:
                print(
                    f"Skipped {len(skipped)} node(s) mutated by live traffic since the backfill: "
                    f"{skipped}"
                )
            return 0

        # Every classification is a paid LLM call gated by the Cost Check Gate (ADR-0065) —
        # register it before building the classifier (FRE-800).
        cost_gate = await _setup_cost_gate()

        classifier, model = _build_llm_batch_classifier()
        report = await run_backfill(
            graph,
            classifier,
            run_id=f"fre865-{uuid4()}",
            now=datetime.now(timezone.utc).isoformat(),
            prompt_version=PROMPT_VERSION,
            classifier_model=model,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            classify_batch_size=args.classify_batch_size,
            fail_open_threshold=args.fail_open_threshold,
            fail_open_min_sample=args.fail_open_min_sample,
        )
        _print_summary(report)
        if args.report_path:
            args.report_path.write_bytes(orjson.dumps(asdict(report)))
            print(f"report written: {args.report_path}")
        # A dry run's own success now genuinely reflects the fail-open valve (see run_backfill),
        # so it is no longer unconditionally forced to 0 — a --dry-run whose preview looks like a
        # classifier outage should exit non-zero too, exactly the signal the preview exists for.
        return 0 if report.success else 4
    finally:
        if cost_gate is not None:
            from personal_agent.cost_gate import set_default_gate

            set_default_gate(None)
            # No reaper task runs in this one-shot script — reap once so a mid-run crash doesn't
            # leave a reservation occupying budget headroom for its full TTL.
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
