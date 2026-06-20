"""Persist :class:`ResultDoc` instances to Elasticsearch.

Index name: ``<prefix>-YYYY.MM.DD`` (rolls daily). The :func:`write_result`
function is a thin wrapper around the async ES client so the walk module
stays substrate-agnostic and can be unit-tested without an ES dependency.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from personal_agent.observability.joinability.result import (
    ResultDoc,
    SubstrateResultDoc,
)
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


def substrate_index_name_for(doc: SubstrateResultDoc, *, prefix: str) -> str:
    """Compute the daily index name for a flat per-substrate doc.

    Args:
        doc: Flat per-substrate result document.
        prefix: Base joinability prefix (e.g. ``agent-monitors-joinability``);
            the ``-substrate-`` suffix is appended here so the run-doc and
            substrate-doc indices share a single settings key.

    Returns:
        ``{prefix}-substrate-YYYY.MM.DD`` (UTC date from ``started_at``).
    """
    return f"{prefix}-substrate-{doc.started_at.strftime('%Y.%m.%d')}"


async def write_substrate_results(
    es: "AsyncElasticsearch",
    docs: Sequence[SubstrateResultDoc],
    *,
    prefix: str,
    trace_id: str,
) -> None:
    """Write flat per-substrate docs to ES — one per ``(run, substrate)``.

    Document id is ``{run_id}::{substrate}`` (deterministic and idempotent, so
    a re-run of the same probe overwrites rather than duplicates). Substrate
    uniqueness within a run is enforced upstream by
    :func:`substrate_docs_from_result`, so the id never collides.

    Args:
        es: Connected AsyncElasticsearch client.
        docs: Flat substrate docs from :func:`substrate_docs_from_result`.
        prefix: Base index prefix (e.g. ``agent-monitors-joinability``).
        trace_id: The probe run's ``SystemTraceContext`` trace id, for the
            structured completion log (the docs themselves carry ``run_id``).

    Raises:
        elasticsearch.ApiError: When an index operation fails. The caller logs
            and swallows — a substrate-doc write failure must not abort the
            brainstem scheduler loop (mirrors the :func:`write_result` contract).
    """
    for doc in docs:
        await es.index(
            index=substrate_index_name_for(doc, prefix=prefix),
            id=f"{doc.run_id}::{doc.substrate}",
            document=doc.model_dump(mode="json"),
        )
    log.info(
        "joinability_substrate_docs_indexed",
        count=len(docs),
        run_id=docs[0].run_id if docs else None,
        trace_id=trace_id,
    )
