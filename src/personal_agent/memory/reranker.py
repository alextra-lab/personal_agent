# src/personal_agent/memory/reranker.py
"""Cross-attention reranker for Seshat memory retrieval.

Re-scores top-K candidates from the hybrid search pipeline. PRIMARY target is
Voyage rerank-2.5 (managed API); on error/timeout, falls back to the Mac-tunnel
Qwen3-Reranker-4B (MLX, via the SLM endpoint — settings.slm_base_url);
on total failure, degrades to passthrough (FRE-851).

Model identity and endpoints are configured in config/models.yaml (ADR-0031),
resolved via the "reranker" (primary) and "reranker_fallback" roles
(config/model_roles.yaml, ADR-0099). Runtime toggle and top_k are configured
in settings.py / .env.

See: ADR-0035 (seshat-backend-decision)
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from personal_agent.config import get_settings
from personal_agent.config.settings import AppConfig
from personal_agent.llm_client.cost_tracker import record_vendor_cost
from personal_agent.security import create_guarded_http_client

log = structlog.get_logger(__name__)

# Voyage AI's managed rerank API host (FRE-851). Requests to it must carry a
# Bearer API key, not the CF-Access service token.
_VOYAGE_HOSTNAME = "api.voyageai.com"

# Per-attempt httpx timeouts (FRE-851). Voyage's expected latency is ~250ms
# (FRE-695 measurement), so a generous-but-short cap fails fast on a genuine
# outage instead of waiting the full legacy ceiling before falling back;
# the fallback keeps that legacy 30s ceiling unchanged. Worst case for one
# rerank() call is now the sum of both (~40s) rather than an unbounded chain.
_VOYAGE_CALL_TIMEOUT_SECONDS = 10.0
_FALLBACK_CALL_TIMEOUT_SECONDS = 30.0


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


def _resolve_reranker_role_config(role: str, default_endpoint: str) -> tuple[str, str]:
    """Load a reranker role's model id and endpoint via the role matrix (FRE-851).

    Shared by the primary ("reranker") and fallback ("reranker_fallback")
    roles — identical lookup mechanism, different role name and endpoint
    default.

    Returns:
        Tuple of (model_id, endpoint_url).

    Raises:
        ModelRoleError: If the role cannot be resolved (missing matrix, or
            resolved key absent from the active model config).
    """
    from personal_agent.config.model_loader import (  # noqa: PLC0415
        ModelRoleError,
        resolve_role_definition,
    )

    # Effective definition, not the raw deployment — see embeddings.py.
    model_def = resolve_role_definition(role)
    if model_def is None:
        raise ModelRoleError(f"role {role!r} resolves to no deployment")
    endpoint = model_def.endpoint or default_endpoint
    return model_def.id, endpoint


def _get_reranker_config() -> tuple[str, str]:
    """Load the PRIMARY reranker model id and endpoint via the role matrix.

    Returns:
        Tuple of (model_id, endpoint_url).

    Raises:
        ModelRoleError: If the 'reranker' role cannot be resolved (missing
            matrix, or resolved key absent from the active model config).
    """
    return _resolve_reranker_role_config("reranker", "https://api.voyageai.com/v1")


def _get_reranker_fallback_config() -> tuple[str, str]:
    """Load the FALLBACK reranker model id and endpoint via the role matrix (FRE-851).

    Tried when the primary ("reranker" role, Voyage) errors or times out.

    Returns:
        Tuple of (model_id, endpoint_url).

    Raises:
        ModelRoleError: If the 'reranker_fallback' role cannot be resolved
            (missing matrix, or resolved key absent from the active model
            config).
    """
    slm_base = get_settings().resolved_slm_base_url
    return _resolve_reranker_role_config("reranker_fallback", f"{slm_base.rstrip('/')}/v1")


def _get_reranker_pricing() -> float | None:
    """Resolve the "reranker" role's USD-per-token rate (FRE-974).

    Only the Voyage primary is billed; the Mac-tunnel fallback is free local
    infra, so this is only ever consulted from the Voyage branch of
    :func:`_attempt_rerank`.

    Returns:
        USD cost per token, or ``None`` if unset (skips cost recording rather
        than emitting a wrong number).
    """
    from personal_agent.config.model_loader import (  # noqa: PLC0415
        load_model_config,
        resolve_role_definition,
    )

    model_def = resolve_role_definition("reranker", config=load_model_config())
    return model_def.input_cost_per_token if model_def else None


async def _attempt_rerank(
    model_id: str,
    endpoint: str,
    query: str,
    documents: Sequence[str],
    top_k: int,
    *,
    timeout: float,
    trace_id: str | None,
    session_id: str | None,
    task_id: str | None,
    span_id: str,
    settings: AppConfig,
) -> tuple[list[RerankResult], str, float | None]:
    """Call one reranker endpoint's /rerank and return sorted results (FRE-851).

    Handles the Voyage vs. legacy contract difference by hostname: Voyage
    takes ``top_k`` in the request and returns items under ``data``; every
    other target (the Mac-tunnel 4B, the local Docker reranker) takes
    ``top_n`` and returns items under ``results``. Auth is likewise
    hostname-gated: Voyage gets a Bearer API key, the Mac SLM tunnel gets the
    CF-Access service token, anything else gets no auth header.

    Internal join-key headers (``X-Trace-Id``/``X-Session-Id``/``X-Span-Id``/
    ``X-Task-Id``) are forwarded to every target except Voyage — their whole
    purpose is letting the SLM server's own rerank log join the trace
    (ADR-0074), which does not apply to a third-party vendor, and there is no
    reason to leak internal correlation ids off our infra.

    On a successful **Voyage** call whose response carries ``usage.total_tokens``,
    records this call's real vendor cost (FRE-974) — the Mac-tunnel/local
    fallback is never billed, so cost recording never fires for it even if its
    response happens to carry a ``usage`` field.

    Returns:
        A tuple of ``(results, provider, cost_usd)`` — ``provider`` is
        ``"voyage"`` or ``"slm_local"`` (hostname-derived, same signal already
        used for auth/request-shape above); ``cost_usd`` is ``None`` for any
        non-Voyage target or a Voyage response with no ``usage``.

    Raises:
        RuntimeError: If the target is Voyage and no API key is configured —
            raised immediately, before any network call, so the caller falls
            back at once instead of sending a doomed "Bearer None" request
            and waiting out a timeout for Voyage to reject it.
        Exception: Any HTTP or parsing failure. The caller decides whether to
            fall back to another target or degrade to passthrough.
    """
    is_voyage = _VOYAGE_HOSTNAME in endpoint
    provider = "voyage" if is_voyage else "slm_local"

    if is_voyage:
        if not settings.voyage_api_key:
            raise RuntimeError("voyage_api_key not configured")
        headers: dict[str, str] = {"Authorization": f"Bearer {settings.voyage_api_key}"}
    else:
        # No CF service token (ADR-0132 D1): a CF-gated SLM endpoint resolves to
        # the internal Caddy egress block, which injects it.
        headers = {}

    # Forward join keys only when a real trace exists (a standalone span
    # would be unjoinable, ADR-0074) and only to our own infra — Voyage never
    # gets them.
    if trace_id is not None and not is_voyage:
        headers = {**headers, "X-Trace-Id": trace_id, "X-Span-Id": span_id}
        if session_id is not None:
            headers["X-Session-Id"] = session_id
        if task_id is not None:
            headers["X-Task-Id"] = task_id

    request_key = "top_k" if is_voyage else "top_n"
    response_key = "data" if is_voyage else "results"

    call_start = time.monotonic()
    async with create_guarded_http_client(timeout=timeout) as client:
        resp = await client.post(
            f"{endpoint}/rerank",
            json={
                "model": model_id,
                "query": query,
                "documents": list(documents),
                request_key: top_k,
            },
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    call_latency_ms = int((time.monotonic() - call_start) * 1000)

    results = []
    for item in data.get(response_key, []):
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

    cost_usd: float | None = None
    if is_voyage:
        total_tokens = data.get("usage", {}).get("total_tokens")
        # bool is an int subclass; excluded explicitly since a boolean token
        # count is never sane. A malformed/adversarial Voyage response
        # (non-numeric, negative) must degrade to "skip cost recording",
        # never raise into this already-successful rerank (security review,
        # FRE-974).
        if (
            isinstance(total_tokens, int)
            and not isinstance(total_tokens, bool)
            and total_tokens >= 0
        ):
            pricing = _get_reranker_pricing()
            if pricing is not None:
                cost_usd = total_tokens * pricing
                await record_vendor_cost(
                    provider=provider,
                    model=model_id,
                    tokens=total_tokens,
                    cost_usd=cost_usd,
                    trace_id=trace_id,
                    session_id=session_id,
                    purpose="reranker",
                    latency_ms=call_latency_ms,
                )
        else:
            log.debug(
                "reranker_cost_usage_missing",
                provider=provider,
                model_id=model_id,
                trace_id=trace_id,
                session_id=session_id,
                total_tokens=total_tokens,
            )

    return results, provider, cost_usd


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

    PRIMARY target is Voyage rerank-2.5 (managed API). On error or timeout,
    falls back to the Mac-tunnel Qwen3-Reranker-4B; if that also fails (or
    the reranker is disabled/misconfigured), returns documents in original
    order with default scores (graceful degradation, FRE-851).

    Stamps the ``reranker_applied`` / ``reranker_failed`` telemetry with the
    request identity tuple and a per-call ``span_id`` so two reranks in one
    turn are distinguishable and the events join to the turn (ADR-0074). When a
    ``trace_id`` is supplied the same keys are forwarded to the fallback target
    (never Voyage) as ``X-Trace-Id`` / ``X-Session-Id`` / ``X-Span-Id`` headers
    so its rerank log can join too. Callers should thread ``trace_id`` and
    ``session_id`` together (both or neither) — a lone ``session_id`` produces
    an unjoinable event.

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
    from personal_agent.config.model_loader import ModelRoleError  # noqa: PLC0415

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
    except (KeyError, ModelRoleError):
        log.warning(
            "reranker_config_missing",
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            span_id=span_id,
        )
        return _passthrough(documents)

    start = time.monotonic()
    try:
        results, provider, cost_usd = await _attempt_rerank(
            model_id,
            endpoint,
            query,
            documents,
            top_k,
            timeout=_VOYAGE_CALL_TIMEOUT_SECONDS,
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            span_id=span_id,
            settings=settings,
        )
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
        return await _rerank_fallback(
            query,
            documents,
            top_k,
            overall_start=start,
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            span_id=span_id,
            input_cap=input_cap,
            settings=settings,
        )

    duration_ms = (time.monotonic() - start) * 1000
    _log_reranker_applied(
        trace_id=trace_id,
        session_id=session_id,
        task_id=task_id,
        span_id=span_id,
        model_id=model_id,
        candidate_count=len(documents),
        input_cap=input_cap,
        top_k=top_k,
        results=results,
        duration_ms=duration_ms,
        fallback=False,
        provider=provider,
        cost_usd=cost_usd,
    )
    return results


async def _rerank_fallback(
    query: str,
    documents: Sequence[str],
    top_k: int,
    *,
    overall_start: float,
    trace_id: str | None,
    session_id: str | None,
    task_id: str | None,
    span_id: str,
    input_cap: int,
    settings: AppConfig,
) -> list[RerankResult]:
    """Try the "reranker_fallback" target after the primary has failed (FRE-851).

    ``overall_start`` is the monotonic timestamp the ORIGINAL (primary)
    attempt began — every duration logged here covers the full time the
    caller actually waited (the failed primary attempt plus this one), not
    just this attempt's own time, so telemetry reflects the true cost of a
    degraded turn instead of hiding the primary's latency.

    Degrades to passthrough if the fallback role can't be resolved or its
    call also fails — the same graceful-degradation contract as the primary.
    """
    from personal_agent.config.model_loader import ModelRoleError  # noqa: PLC0415

    try:
        model_id, endpoint = _get_reranker_fallback_config()
    except (KeyError, ModelRoleError):
        log.warning(
            "reranker_fallback_config_missing",
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            span_id=span_id,
        )
        return _passthrough(documents)

    try:
        results, provider, cost_usd = await _attempt_rerank(
            model_id,
            endpoint,
            query,
            documents,
            top_k,
            timeout=_FALLBACK_CALL_TIMEOUT_SECONDS,
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            span_id=span_id,
            settings=settings,
        )
    except Exception as exc:
        duration_ms = (time.monotonic() - overall_start) * 1000
        log.warning(
            "reranker_fallback_failed",
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

    duration_ms = (time.monotonic() - overall_start) * 1000
    _log_reranker_applied(
        trace_id=trace_id,
        session_id=session_id,
        task_id=task_id,
        span_id=span_id,
        model_id=model_id,
        candidate_count=len(documents),
        input_cap=input_cap,
        top_k=top_k,
        results=results,
        duration_ms=duration_ms,
        fallback=True,
        provider=provider,
        cost_usd=cost_usd,
    )
    return results


def _log_reranker_applied(
    *,
    trace_id: str | None,
    session_id: str | None,
    task_id: str | None,
    span_id: str,
    model_id: str,
    candidate_count: int,
    input_cap: int,
    top_k: int,
    results: list[RerankResult],
    duration_ms: float,
    fallback: bool,
    provider: str,
    cost_usd: float | None,
) -> None:
    """Emit the shared "reranker_applied" telemetry event (FRE-851 primary + fallback paths).

    ``provider``/``cost_usd`` (FRE-974): ``provider`` is ``"voyage"`` or
    ``"slm_local"``; ``cost_usd`` is ``None`` for the free local fallback and
    for a Voyage response with no reported ``usage``.
    """
    log.info(
        "reranker_applied",
        trace_id=trace_id,
        session_id=session_id,
        task_id=task_id,
        span_id=span_id,
        model_id=model_id,
        candidate_count=candidate_count,
        input_cap=input_cap,
        top_k=top_k,
        result_count=len(results),
        top_score=results[0].score if results else None,
        duration_ms=round(duration_ms, 1),
        fallback=fallback,
        provider=provider,
        cost_usd=cost_usd,
    )


def _passthrough(documents: Sequence[str]) -> list[RerankResult]:
    """Return documents in original order with default scores."""
    return [
        RerankResult(index=i, score=1.0 / (i + 1), document=doc) for i, doc in enumerate(documents)
    ]
