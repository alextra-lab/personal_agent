"""Persist :class:`ResultDoc` instances to Elasticsearch.

Index name: ``<prefix>-YYYY.MM.DD`` (rolls daily). The :func:`write_result`
function is a thin wrapper around the async ES client so the walk module
stays substrate-agnostic and can be unit-tested without an ES dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from personal_agent.observability.joinability.result import ResultDoc
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = get_logger(__name__)


def index_name_for(doc: ResultDoc, *, prefix: str) -> str:
    """Compute the daily index name for a result doc.

    Args:
        doc: Result document.
        prefix: Index prefix (e.g. ``agent-monitors-joinability``).

    Returns:
        Index name suffixed by the UTC date in ``YYYY.MM.DD`` form to align
        with the project's other ``agent-*`` daily indices.
    """
    return f"{prefix}-{doc.started_at.strftime('%Y.%m.%d')}"


async def write_result(
    es: "AsyncElasticsearch",
    doc: ResultDoc,
    *,
    prefix: str,
) -> None:
    """Write one result doc to ES, using ``run_id`` as the document id.

    Args:
        es: Connected AsyncElasticsearch client.
        doc: Result document.
        prefix: Index prefix (e.g. from
            ``settings.joinability_probe_index_prefix``).

    Raises:
        elasticsearch.ApiError: When the index operation fails. The caller
            is expected to log and swallow — a probe whose result couldn't
            be persisted should not abort the brainstem scheduler loop.
    """
    index = index_name_for(doc, prefix=prefix)
    await es.index(
        index=index,
        id=doc.run_id,
        document=doc.model_dump(mode="json"),
    )
    log.info(
        "joinability_probe_result_indexed",
        index=index,
        run_id=doc.run_id,
        outcome=doc.outcome,
        sampled_session_id=doc.sampled_session_id,
        trace_id=doc.trace_id,
    )
