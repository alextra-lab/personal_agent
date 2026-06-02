"""Persist :class:`SlmHealthSnapshot` instances to Elasticsearch (FRE-399 / ADR-0083).

Index name: ``<prefix>-YYYY.MM.DD`` (rolls daily), matching the project
convention for other ``agent-monitors-*`` indices. Document id = a fresh UUID
so multiple probes per day append cleanly.

Mirrors the joinability-probe sink (:mod:`personal_agent.observability.joinability.sink`)
exactly; the caller is expected to swallow any raised :class:`~elasticsearch.ApiError`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = get_logger(__name__)


def index_name_for(snapshot: SlmHealthSnapshot, *, prefix: str) -> str:
    """Compute the daily index name for a snapshot doc.

    Args:
        snapshot: The snapshot to store.
        prefix: Elasticsearch index prefix (e.g. ``"agent-monitors-slm-health"``).

    Returns:
        Index name suffixed by the UTC probe date in ``YYYY.MM.DD`` form.
    """
    return f"{prefix}-{snapshot.probed_at.strftime('%Y.%m.%d')}"


async def write_result(
    es: "AsyncElasticsearch",
    snapshot: SlmHealthSnapshot,
    *,
    prefix: str,
) -> None:
    """Write one snapshot doc to Elasticsearch.

    Uses a fresh UUID as the document id so multiple probes per day accumulate
    as distinct documents (unlike the joinability probe, which stores one per
    run and uses the run's own UUID — same intent, slightly different
    granularity).

    Args:
        es: Connected AsyncElasticsearch client.
        snapshot: Snapshot to persist.
        prefix: Index prefix from settings (e.g. ``settings.slm_health_index_prefix``).

    Raises:
        elasticsearch.ApiError: When the index operation fails. The caller
            (scheduler_runner) is expected to log and swallow — a probe whose
            result couldn't be persisted should not abort the scheduler loop.
    """
    doc_id = str(uuid.uuid4())
    index = index_name_for(snapshot, prefix=prefix)
    await es.index(
        index=index,
        id=doc_id,
        document=snapshot.model_dump(mode="json"),
    )
    log.info(
        "slm_health_result_indexed",
        index=index,
        doc_id=doc_id,
        status=snapshot.status,
        trace_id=snapshot.trace_id,
        component="slm_health",
    )
