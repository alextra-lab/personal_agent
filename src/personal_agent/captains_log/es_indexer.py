"""Optional Elasticsearch indexing for Captain's Log (Phase 2.3).

When the service runs with ES connected, captures and reflections can be
indexed to daily indices for analytics and Kibana. Indexing is best-effort
and non-blocking: failures are logged but never raise.

Deterministic document IDs (trace_id for captures, entry_id for reflections)
enable idempotent backfill replay (FRE-30).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from personal_agent.telemetry import get_logger

log = get_logger(__name__)

# Prefix for Captain's Log captures index (must match capture.CAPTURES_INDEX_PREFIX and template).
CAPTURES_INDEX_PREFIX = "agent-captains-captures"


def normalize_capture_doc_for_es(doc: dict[str, Any]) -> dict[str, Any]:
    """Ensure capture document conforms to ES captains-captures mapping.

    The index template maps tool_results[].output as ``text`` (index: false).
    Tool outputs can be dicts, lists, or other non-string types which ES
    rejects for a text field. This normalizer JSON-serializes any non-string
    output so it always arrives as a plain string.

    Args:
        doc: Capture document (e.g. from TaskCapture.model_dump(mode="json")).

    Returns:
        New dict safe to index; input is not mutated.
    """
    if "tool_results" not in doc or not isinstance(doc["tool_results"], list):
        return doc
    normalized: list[dict[str, Any]] = []
    for item in doc["tool_results"]:
        if not isinstance(item, dict):
            normalized.append({"value": item})
            continue
        output = item.get("output")
        if not isinstance(output, dict):
            item = {**item, "output": {"value": output}}
        normalized.append(item)
    return {**doc, "tool_results": normalized}


# Type for async indexer: (index_name, document, doc_id?) -> None
ESIndexer = Callable[[str, dict[str, Any], str | None], Awaitable[None]]

_es_indexer: ESIndexer | None = None


def set_es_indexer(indexer: ESIndexer | None) -> None:
    """Set the optional Elasticsearch indexer (called from service lifespan).

    Args:
        indexer: Async callable(index_name, document, doc_id=None), or None to disable.
    """
    global _es_indexer
    _es_indexer = indexer


def get_es_indexer() -> ESIndexer | None:
    """Return the current ES indexer if configured."""
    return _es_indexer


def build_es_indexer_from_handler(es_handler: Any | None) -> ESIndexer | None:
    """Build an indexer from an Elasticsearch handler object.

    Args:
        es_handler: Handler with `_connected` and `es_logger.index_document(...)`.

    Returns:
        Async ES indexer callable (index_name, document, doc_id=None), or None if unavailable.
    """
    if not es_handler:
        return None
    if not getattr(es_handler, "_connected", False):
        return None
    es_logger = getattr(es_handler, "es_logger", None)
    if es_logger is None:
        return None

    async def _index(
        index_name: str,
        document: dict[str, Any],
        doc_id: str | None = None,
    ) -> None:
        await es_logger.index_document(index_name, document, id=doc_id)

    return _index


def schedule_es_index(
    index_name: str,
    document: dict[str, Any],
    es_handler: Any | None = None,
    doc_id: str | None = None,
) -> None:
    """Schedule a non-blocking index of a document to Elasticsearch.

    If no explicit handler/indexer is available or ES is down, this is a no-op.
    Errors are logged and never propagated.

    Args:
        index_name: Target index (e.g. agent-captains-captures-2026-02-22).
        document: JSON-serializable document to index.
        es_handler: Optional explicit Elasticsearch handler.
        doc_id: Optional document ID for idempotent upsert (trace_id or entry_id).
    """
    indexer = build_es_indexer_from_handler(es_handler) if es_handler else get_es_indexer()
    if not indexer:
        return

    if index_name.startswith(CAPTURES_INDEX_PREFIX):
        document = normalize_capture_doc_for_es(document)

    async def _index() -> None:
        try:
            await indexer(index_name, document, doc_id)
        except Exception as e:
            log.warning(
                "captains_log_es_index_failed",
                index=index_name,
                error=str(e),
            )

    try:
        asyncio.get_running_loop()
        asyncio.create_task(_index())
    except RuntimeError:
        # No running loop (e.g. CLI or tests) — skip ES index
        pass
