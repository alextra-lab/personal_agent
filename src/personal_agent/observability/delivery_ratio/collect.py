"""Live collection for the delivery-ratio probe (FRE-1051).

Separated from :mod:`probe` so the verdict logic stays pure and unit-testable while
the substrate queries live here.

**Oracle discipline.** A family is only wired to an oracle when the join has been
validated as genuinely one-to-one. A plausible-looking oracle that is not 1:1
manufactures ratios that mean nothing, and a probe that reports a meaningless ratio is
worse than one that reports ``UNVERIFIABLE`` — the first is trusted, the second
prompts work. Families the ticket names but whose join is not yet validated are
therefore **declared and reported unverifiable**, never silently omitted: omission
reads as "covered everything".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any

from personal_agent.observability.delivery_ratio.probe import (
    DEFAULT_MIN_RATIO,
    DeliveryReport,
    FamilyDelivery,
    classify_zero,
    compute_report,
)
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = get_logger(__name__)

DISCRIMINATOR_FIELD: str = "event_type"
"""The field every family query filters on.

If this is absent from the mapping every count is zero regardless of delivery, so it is
what separates ``FIELD_ABSENT`` from ``EMITTED_AND_LOST``.
"""


@dataclass(frozen=True)
class UnwiredFamily:
    """A family the contract should cover but whose oracle join is not validated.

    Attributes:
        family: Event family name.
        reason: Why it cannot yet be measured, reported verbatim in the output.
    """

    family: str
    reason: str


UNWIRED_FAMILIES: tuple[UnwiredFamily, ...] = (
    UnwiredFamily(
        family="turn.model_call_completed",
        reason=(
            "join to api_costs by trace is not validated 1:1 — one trace can carry "
            "several model calls and several cost rows"
        ),
    ),
    UnwiredFamily(
        family="session_events",
        reason="no messages table in this schema; per-session ES marker event not identified",
    ),
    UnwiredFamily(
        family="captains_log_captures",
        reason="oracle is a Docker named volume, not reachable from this probe process",
    ),
)
"""Declared-but-unmeasured families, surfaced so the report cannot imply full coverage."""


async def _count_es_family(
    es: AsyncElasticsearch,
    *,
    logs_prefix: str,
    family: str,
    since: date,
    until: date,
) -> int:
    """Count documents of one event family in the log indices for the window.

    Args:
        es: Connected Elasticsearch client.
        logs_prefix: Index prefix (e.g. ``agent-logs``), from settings — never hardcoded.
        family: ``event_type`` value to count.
        since: First UTC day of the window (inclusive).
        until: Last UTC day of the window (inclusive).

    Returns:
        Document count for the family over the window.
    """
    result = await es.count(
        index=f"{logs_prefix}-*",
        query={
            "bool": {
                "must": [
                    {"term": {"event_type": family}},
                    {
                        "range": {
                            "@timestamp": {
                                "gte": since.isoformat(),
                                "lt": (until + timedelta(days=1)).isoformat(),
                            }
                        }
                    },
                ]
            }
        },
    )
    return int(result["count"])


async def field_is_mapped(
    es: AsyncElasticsearch,
    *,
    logs_prefix: str,
    field_name: str,
) -> bool:
    """Report whether a field exists in the live mapping of the log indices.

    Separates "the query names a field that does not exist" from "the event was
    emitted and lost" — two of the three causes of a clean zero.

    Args:
        es: Connected Elasticsearch client.
        logs_prefix: Index prefix from settings.
        field_name: Field to look for.

    Returns:
        True when at least one index maps the field.
    """
    caps = await es.field_caps(index=f"{logs_prefix}-*", fields=field_name)
    return bool(caps.get("fields", {}).get(field_name))


async def _count_api_costs(pool: Any, *, since: date, until: date) -> int:
    """Count ledger rows in the window from the Postgres oracle.

    ``api_costs`` is append-only and written on a different code path from the log
    handler, so the two cannot fail together — which is what makes it an oracle.

    Bounds are passed as timezone-aware UTC datetimes rather than date strings.
    ``api_costs.timestamp`` is ``timestamptz``, so a bare literal would be read in
    the session's timezone and silently shift the window off the UTC day the
    Elasticsearch side counts.

    Args:
        pool: asyncpg pool or connection exposing ``fetchval``.
        since: First UTC day of the window (inclusive).
        until: Last UTC day of the window (inclusive).

    Returns:
        Row count for the window.
    """
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM api_costs WHERE timestamp >= $1 AND timestamp < $2",
            datetime.combine(since, time.min, tzinfo=timezone.utc),
            datetime.combine(until + timedelta(days=1), time.min, tzinfo=timezone.utc),
        )
    )


async def collect_report(
    es: AsyncElasticsearch,
    pool: Any,
    *,
    logs_prefix: str,
    since: date,
    until: date,
    min_ratio: float = DEFAULT_MIN_RATIO,
) -> DeliveryReport:
    """Measure delivery for every family and assemble the report.

    Args:
        es: Connected Elasticsearch client.
        pool: asyncpg pool or connection for the Postgres oracle.
        logs_prefix: Index prefix from settings.
        since: First UTC day of the window (inclusive).
        until: Last UTC day of the window (inclusive).
        min_ratio: Delivery floor below which a family is a breach.

    Returns:
        The assembled :class:`DeliveryReport`, worst family first.
    """
    oracle_count = await _count_api_costs(pool, since=since, until=until)
    es_count = await _count_es_family(
        es, logs_prefix=logs_prefix, family="api_cost_recorded", since=since, until=until
    )

    # Resolved once and only when it can matter. A zero must be attributed before it is
    # reported: without this the probe would blame the pipeline for a renamed field and
    # print "0% delivered", which is the very conflation it exists to end.
    field_present = True
    if es_count == 0:
        field_present = await field_is_mapped(
            es, logs_prefix=logs_prefix, field_name=DISCRIMINATOR_FIELD
        )

    families = [
        FamilyDelivery(
            family="api_cost_recorded",
            oracle="postgres:api_costs",
            oracle_count=oracle_count,
            es_count=es_count,
            min_ratio=min_ratio,
            zero_cause=classify_zero(
                oracle_count=oracle_count, es_count=es_count, field_present=field_present
            ),
        )
    ]

    for unwired in UNWIRED_FAMILIES:
        unwired_count = await _count_es_family(
            es,
            logs_prefix=logs_prefix,
            family=unwired.family,
            since=since,
            until=until,
        )
        families.append(
            FamilyDelivery(
                family=unwired.family,
                oracle=None,
                oracle_count=None,
                es_count=unwired_count,
                min_ratio=min_ratio,
            )
        )
        log.info(
            "delivery_ratio_family_unwired",
            family=unwired.family,
            reason=unwired.reason,
            es_count=unwired_count,
            component="delivery_ratio",
        )

    return compute_report(since=since, until=until, families=families)
