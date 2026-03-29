# src/personal_agent/memory/reranker.py
"""Cross-attention reranker for Seshat memory retrieval.

Re-scores top-K candidates from the hybrid search pipeline using
Qwen3-Reranker-0.6B served by slm_server (llama.cpp GGUF) via an
OpenAI-compatible /v1/rerank endpoint.

Model identity and endpoint are configured in config/models.yaml (ADR-0031).
Runtime toggle and top_k are configured in settings.py / .env.

See: ADR-0035 (seshat-backend-decision)
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
import structlog

from personal_agent.config import get_settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RerankResult:
    """A single reranked document with relevance score.

    Attributes:
        index: Original index of this document in the input list.
        score: Relevance score from the reranker (higher = more relevant).
        document: The original document text.
    """

    index: int
    score: float
    document: str


def _get_reranker_config() -> tuple[str, str]:
    """Load reranker model id and endpoint from config/models.yaml.

    Returns:
        Tuple of (model_id, endpoint_url).

    Raises:
        KeyError: If 'reranker' entry is missing from models.yaml.
    """
    from personal_agent.config import load_model_config  # noqa: PLC0415

    config = load_model_config()
    model_def = config.models["reranker"]
    endpoint = model_def.endpoint or "http://localhost:8504/v1"
    return model_def.id, endpoint


async def rerank(
    query: str,
    documents: Sequence[str],
    top_k: int | None = None,
) -> list[RerankResult]:
    """Re-score documents using cross-attention reranker.

    Calls slm_server's /v1/rerank endpoint. If the reranker is
    disabled or unreachable, returns documents in original order
    with default scores (graceful degradation).

    Args:
        query: The search query to rank against.
        documents: Candidate documents to re-score.
        top_k: Max results to return. None uses settings.reranker_top_k.

    Returns:
        List of RerankResult sorted by relevance score descending.
    """
    settings = get_settings()

    if not settings.reranker_enabled:
        return _passthrough(documents)

    if not documents:
        return []

    if top_k is None:
        top_k = settings.reranker_top_k

    try:
        model_id, endpoint = _get_reranker_config()
    except KeyError:
        logger.warning("reranker_config_missing")
        return _passthrough(documents)

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{endpoint}/rerank",
                json={
                    "model": model_id,
                    "query": query,
                    "documents": list(documents),
                    "top_n": top_k,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        duration_ms = (time.monotonic() - start) * 1000

        results = []
        for item in data.get("results", []):
            idx = item["index"]
            results.append(
                RerankResult(
                    index=idx,
                    score=float(item["relevance_score"]),
                    document=documents[idx],
                )
            )

        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)

        logger.info(
            "reranker_applied",
            candidate_count=len(documents),
            top_k=top_k,
            result_count=len(results),
            duration_ms=round(duration_ms, 1),
        )

        return results

    except Exception as exc:
        duration_ms = (time.monotonic() - start) * 1000
        logger.warning(
            "reranker_failed",
            error=str(exc),
            duration_ms=round(duration_ms, 1),
            candidate_count=len(documents),
        )
        return _passthrough(documents)


def _passthrough(documents: Sequence[str]) -> list[RerankResult]:
    """Return documents in original order with default scores."""
    return [
        RerankResult(index=i, score=1.0 / (i + 1), document=doc)
        for i, doc in enumerate(documents)
    ]
