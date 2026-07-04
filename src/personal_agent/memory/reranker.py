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
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
import structlog

from personal_agent.config import get_settings
from personal_agent.service.cf_service_token import cf_access_service_token_headers

log = structlog.get_logger(__name__)

# Hostname of the Access-gated Mac SLM gateway (mirrors llm_client/client.py:58).
# Requests to it must carry the CF-Access service token; the internal Docker
# reranker (reranker:8504) must not. (FRE-656)
_SLM_TUNNEL_HOSTNAME = "slm.frenchforet.com"


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
    """Load reranker model id and endpoint via the role matrix.

    Returns:
        Tuple of (model_id, endpoint_url).

    Raises:
        ModelRoleError: If the 'reranker' role cannot be resolved (missing
            matrix, or resolved key absent from the active model config).
    """
    from personal_agent.config import load_model_config, resolve_role_model_key  # noqa: PLC0415

    config = load_model_config()
    model_def = config.models[resolve_role_model_key("reranker")]
    endpoint = model_def.endpoint or "http://localhost:8504/v1"
    return model_def.id, endpoint


async def rerank(
    query: str,
    documents: Sequence[str],
    top_k: int | None = None,
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> list[RerankResult]:
    """Re-score documents using cross-attention reranker.

    Calls slm_server's /v1/rerank endpoint. If the reranker is
    disabled or unreachable, returns documents in original order
    with default scores (graceful degradation).

    Stamps the ``reranker_applied`` / ``reranker_failed`` telemetry with the
    request identity tuple and a per-call ``span_id`` so two reranks in one
    turn are distinguishable and the events join to the turn (ADR-0074). When a
    ``trace_id`` is supplied the same keys are forwarded to the SLM server as
    ``X-Trace-Id`` / ``X-Session-Id`` / ``X-Span-Id`` headers so its rerank log
    can join too. Callers should thread ``trace_id`` and ``session_id`` together
    (both or neither) — a lone ``session_id`` produces an unjoinable event.

    Args:
        query: The search query to rank against.
        documents: Candidate documents to re-score.
        top_k: Max results to return. None uses settings.reranker_top_k.
        trace_id: Request trace id for event correlation (ADR-0074).
        session_id: Session id for event correlation (ADR-0074).
        task_id: Sub-agent task id when the rerank runs inside a delegated task
            (FRE-513); ``None`` on the ordinary recall path.

    Returns:
        List of RerankResult sorted by relevance score descending.
    """
    settings = get_settings()
    span_id = str(uuid.uuid4())
    input_cap = settings.reranker_input_cap

    if not settings.reranker_enabled:
        return _passthrough(documents)

    if not documents:
        return []

    if top_k is None:
        top_k = settings.reranker_top_k

    try:
        model_id, endpoint = _get_reranker_config()
    except KeyError:
        log.warning(
            "reranker_config_missing",
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            span_id=span_id,
        )
        return _passthrough(documents)

    headers = cf_access_service_token_headers() if _SLM_TUNNEL_HOSTNAME in endpoint else {}
    # Forward join keys to the SLM server only when a real trace exists — a
    # standalone span on the SLM side would be unjoinable (ADR-0074).
    if trace_id is not None:
        headers = {**headers, "X-Trace-Id": trace_id, "X-Span-Id": span_id}
        if session_id is not None:
            headers["X-Session-Id"] = session_id
        if task_id is not None:
            headers["X-Task-Id"] = task_id

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
                headers=headers,
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

        log.info(
            "reranker_applied",
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            span_id=span_id,
            model_id=model_id,
            candidate_count=len(documents),
            input_cap=input_cap,
            top_k=top_k,
            result_count=len(results),
            top_score=results[0].score if results else None,
            duration_ms=round(duration_ms, 1),
        )

        return results

    except Exception as exc:
        duration_ms = (time.monotonic() - start) * 1000
        log.warning(
            "reranker_failed",
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            span_id=span_id,
            model_id=model_id,
            error=str(exc),
            duration_ms=round(duration_ms, 1),
            candidate_count=len(documents),
            input_cap=input_cap,
        )
        return _passthrough(documents)


def _passthrough(documents: Sequence[str]) -> list[RerankResult]:
    """Return documents in original order with default scores."""
    return [
        RerankResult(index=i, score=1.0 / (i + 1), document=doc) for i, doc in enumerate(documents)
    ]
