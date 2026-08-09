"""Persist :class:`DeliveryRatioResultDoc` instances to Elasticsearch (FRE-1051 / FRE-1189).

Index name: ``<prefix>-YYYY-MM`` (monthly, FRE-543 convention). Document id = a fresh
UUID so each daily run accumulates as a distinct document.

Mirrors the SLM-health sink (:mod:`personal_agent.observability.slm_health.sink`)
exactly; the caller is expected to swallow any raised :class:`~elasticsearch.ApiError`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from personal_agent.observability.delivery_ratio.result import DeliveryRatioResultDoc
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = get_logger(__name__)


def index_name_for(doc: DeliveryRatioResultDoc, *, prefix: str) -> str:
    """Compute the monthly index name for a result doc.

    Args:
        doc: The result doc to store.
        prefix: Elasticsearch index prefix (e.g. ``"agent-monitors-delivery-ratio"``).

    Returns:
        Index name suffixed by the UTC run month in ``YYYY-MM`` form.
    """
    return f"{prefix}-{doc.run_at.strftime('%Y-%m')}"


async def write_result(
    es: "AsyncElasticsearch",
    doc: DeliveryRatioResultDoc,
    *,
    prefix: str,
) -> None:
    """Write one result doc to Elasticsearch.

    Args:
        es: Connected AsyncElasticsearch client.
        doc: Result doc to persist.
        prefix: Index prefix from settings (e.g. ``settings.delivery_ratio_probe_index_prefix``).

    Raises:
        elasticsearch.ApiError: When the index operation fails. The caller
            (scheduler_runner) is expected to log and swallow — a probe whose
            result couldn't be persisted should not abort the scheduler loop.
    """
    doc_id = str(uuid.uuid4())
    index = index_name_for(doc, prefix=prefix)
    await es.index(
        index=index,
        id=doc_id,
        document=doc.model_dump(mode="json"),
    )
    log.info(
        "delivery_ratio_result_indexed",
        index=index,
        doc_id=doc_id,
        status=doc.status,
        trace_id=doc.trace_id,
        component="delivery_ratio",
    )
