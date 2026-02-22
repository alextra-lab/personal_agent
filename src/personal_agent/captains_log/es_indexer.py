"""Optional Elasticsearch indexing for Captain's Log (Phase 2.3).

When the service runs with ES connected, captures and reflections can be
indexed to daily indices for analytics and Kibana. Indexing is best-effort
and non-blocking: failures are logged but never raise.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from personal_agent.telemetry import get_logger

log = get_logger(__name__)

# Type for async indexer: (index_name: str, document: dict[str, Any]) -> None
ESIndexer = Callable[[str, dict[str, Any]], Awaitable[None]]

_es_indexer: ESIndexer | None = None


def set_es_indexer(indexer: ESIndexer | None) -> None:
    """Set the optional Elasticsearch indexer (called from service lifespan).

    Args:
        indexer: Async callable(index_name, document), or None to disable.
    """
    global _es_indexer
    _es_indexer = indexer


def get_es_indexer() -> ESIndexer | None:
    """Return the current ES indexer if configured."""
    return _es_indexer


def _build_indexer_from_handler(es_handler: Any | None) -> ESIndexer | None:
    """Build an indexer from an Elasticsearch handler object.

    Args:
        es_handler: Handler with `_connected` and `es_logger.index_document(...)`.

    Returns:
        Async ES indexer callable, or None if handler is unavailable.
    """
    if not es_handler:
        return None
    if not getattr(es_handler, "_connected", False):
        return None
    es_logger = getattr(es_handler, "es_logger", None)
    if es_logger is None:
        return None

    async def _index(index_name: str, document: dict[str, Any]) -> None:
        await es_logger.index_document(index_name, document)

    return _index


def schedule_es_index(
    index_name: str,
    document: dict[str, Any],
    es_handler: Any | None = None,
) -> None:
    """Schedule a non-blocking index of a document to Elasticsearch.

    If no explicit handler/indexer is available or ES is down, this is a no-op.
    Errors are logged and never propagated.

    Args:
        index_name: Target index (e.g. agent-captains-captures-2026-02-22).
        document: JSON-serializable document to index.
        es_handler: Optional explicit Elasticsearch handler.
    """
    indexer = _build_indexer_from_handler(es_handler) if es_handler else get_es_indexer()
    if not indexer:
        return

    async def _index() -> None:
        try:
            await indexer(index_name, document)
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
