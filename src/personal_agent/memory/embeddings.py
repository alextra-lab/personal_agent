# src/personal_agent/memory/embeddings.py
"""Embedding generation pipeline for Seshat memory.

Generates vector embeddings for Entity and Turn nodes to enable
hybrid search (vector + keyword + graph traversal).

Uses Qwen3-Embedding-0.6B served by slm_server (llama.cpp GGUF)
via an OpenAI-compatible /v1/embeddings endpoint. Model identity
and endpoint are configured in config/models.yaml (ADR-0031).

For queries, applies the Qwen3-Embedding instruction prefix format
(Instruct: <task> + newline + Query: <text>).
Documents are embedded as-is (no prefix).

See: ADR-0035 (seshat-backend-decision), Enhancement 1
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import httpx
import structlog

from personal_agent.config import get_settings
from personal_agent.service.cf_service_token import cf_access_service_token_headers

if TYPE_CHECKING:
    from personal_agent.config.settings import AppConfig

logger = structlog.get_logger(__name__)


class EmbeddingResponseError(Exception):
    """Raised when an embeddings response is malformed.

    Covers a row-count mismatch, a per-vector length that doesn't match the
    configured dimension, or a degenerate (zero) vector that can't be
    renormalized.

    A dedicated exception (not ``SystemExit`` — the eval-harness pattern this is
    adapted from uses ``SystemExit`` to hard-abort a one-off script, which would
    escape ``generate_embedding``'s ``except Exception`` and could kill the
    serving process; production code must degrade, not crash).
    """


#: OVH AI Endpoints rejects a batch bigger than this with HTTP 400 (confirmed
#: live against the endpoint, FRE-817). The managed embedder call chunks to this
#: size; the local-fallback call reuses the existing unchunked OpenAI-client path
#: (the same-model local server has no such cap).
_MANAGED_MAX_BATCH = 25

# Instruction prefix for query-mode embeddings (Qwen3-Embedding format).
# Documents are embedded without a prefix.
_QUERY_PREFIX = "Instruct: Given a query, retrieve relevant entities and passages\nQuery: "

# The OpenAI SDK's default ``User-Agent: OpenAI/Python <ver>`` trips a Cloudflare
# WAF managed rule on the gateway (a 403 "request blocked", which would degrade
# silently to a zero vector). The raw-httpx LLM client is unaffected; only this
# SDK path needs a benign UA. Applied only for the gated host. (FRE-656)
_EMBEDDING_USER_AGENT = "seshat-memory/1.0"


def _get_embedding_config() -> tuple[str, str, str]:
    """Load embedding model id, endpoint, and credential via the role binding.

    The credential comes from the deployment's PROVIDER (``auth_env`` names an
    ``AppConfig`` field). Before ADR-0121 this path always pointed at an
    unauthenticated local server, so no key was needed; the deployment now
    resolves to a managed provider, and calling it unauthenticated returns 401 —
    which the caller's fail-open swallows into an all-zero vector, silently
    corrupting the Neo4j index.

    Returns:
        Tuple of (model_id, endpoint_url, api_key). ``api_key`` is ``"unused"``
        for providers that declare no ``auth_env``.

    Raises:
        ModelRoleError: If the 'embedding' role cannot be resolved (missing
            matrix, or resolved key absent from the active model config).
    """
    # Effective definition, not the raw deployment: config.models[key] bypasses
    # the Layer-3 binding, so any per-use override on this role would be
    # silently dropped here while tests of the resolver still pass (ADR-0121).
    from personal_agent.config import settings as _settings  # noqa: PLC0415
    from personal_agent.config.model_loader import (  # noqa: PLC0415
        ModelRoleError,
        load_model_config,  # noqa: PLC0415
        resolve_role_definition,
    )

    catalog = load_model_config()
    model_def = resolve_role_definition("embedding", config=catalog)
    if model_def is None:
        raise ModelRoleError("role 'embedding' resolves to no deployment")
    endpoint = model_def.endpoint or "http://localhost:8503/v1"

    api_key = "unused"
    provider = catalog.providers.get(model_def.provider or "")
    if provider is not None and provider.auth_env:
        resolved = getattr(_settings, provider.auth_env, None)
        if not resolved:
            raise ModelRoleError(
                f"embedding provider {model_def.provider!r} requires the "
                f"{provider.auth_env!r} credential, which is unset. Refusing to "
                "call it unauthenticated: the 401 would be swallowed by the "
                "fail-open path and persist zero vectors."
            )
        api_key = str(resolved)
    return model_def.id, endpoint, api_key


def _resolve_embedder_kind(settings: AppConfig) -> str:
    """Resolve the active substrate profile's embedder backend kind (ADR-0112 D3)."""
    from personal_agent.config.substrate import resolve_substrate  # noqa: PLC0415

    return resolve_substrate(settings.substrate_profile).backends["embedder"].kind


async def _generate_vectors(texts: list[str], settings: AppConfig) -> list[list[float]]:
    """Embed already mode-prefixed *texts* through the profile-resolved embedder.

    Under a ``local`` substrate profile (``private``/``dev``/``test``, the
    default) this is exactly the pre-FRE-821 path: the configured "embedding"
    role's endpoint, no auth. Under a ``managed`` profile (e.g.
    ``managed_embedder``) this calls the managed endpoint first and, if that
    raises and a same-model local fallback endpoint is configured, retries once
    via the fallback (ADR-0112 D4's "seamless local fallback"). Any final
    failure propagates to the caller — the existing zero-vector fail-open path
    in :func:`generate_embedding` / :func:`generate_embeddings_batch`.
    """
    if _resolve_embedder_kind(settings) != "managed":
        model_id, endpoint, api_key = _get_embedding_config()
        response = await _call_embeddings_api(
            texts=texts, model=model_id, endpoint=endpoint, api_key=api_key
        )
        return [[float(x) for x in d.embedding] for d in response.data]

    dimensions = settings.embedding_dimensions
    try:
        return await _embed_managed(
            texts,
            settings.managed_embedding_endpoint or "",
            settings.managed_embedding_token or "",
            settings.managed_embedding_model,
            dimensions=dimensions,
        )
    except Exception as exc:
        if not settings.local_fallback_embedding_endpoint:
            raise
        logger.warning(
            "embedding_managed_failover",
            error=str(exc),
            fallback_endpoint=settings.local_fallback_embedding_endpoint,
        )
        response = await _call_embeddings_api(
            texts=texts,
            model=settings.local_fallback_embedding_model,
            endpoint=settings.local_fallback_embedding_endpoint,
        )
        # The same-model local server is not known to honor the OpenAI
        # `dimensions` request param the way OVH does, so it answers at its
        # native width (e.g. 4096 for the 8B model) -- truncate+renormalize
        # client-side so the fallback lands in the same space as the managed
        # path (ADR-0112 AC-6, FRE-826).
        return [
            _to_target_dimension([float(x) for x in d.embedding], dimensions) for d in response.data
        ]


async def generate_embedding(
    text: str | None,
    *,
    mode: Literal["document", "query"] = "document",
) -> list[float]:
    """Generate an embedding vector for a single text.

    Args:
        text: Text to embed. Returns zero vector for empty/None.
        mode: "document" embeds text as-is; "query" prepends the
            Qwen3-Embedding instruction prefix for asymmetric search.

    Returns:
        List of floats with length == settings.embedding_dimensions.
    """
    settings = get_settings()
    dimensions = settings.embedding_dimensions

    if not text or not text.strip():
        return [0.0] * dimensions

    embed_text = f"{_QUERY_PREFIX}{text}" if mode == "query" else text

    try:
        vectors = await _generate_vectors([embed_text], settings)
        return [float(x) for x in vectors[0]]

    except Exception as exc:
        logger.warning(
            "embedding_generation_failed",
            text_length=len(text),
            error=str(exc),
        )
        return [0.0] * dimensions


async def generate_embeddings_batch(
    texts: list[str],
    *,
    mode: Literal["document", "query"] = "document",
) -> list[list[float]]:
    """Generate embeddings for a batch of texts.

    Args:
        texts: List of texts to embed.
        mode: "document" embeds texts as-is; "query" prepends instruction prefix.

    Returns:
        List of embedding vectors, one per input text.
    """
    if not texts:
        return []

    settings = get_settings()
    embed_texts = [f"{_QUERY_PREFIX}{t}" for t in texts] if mode == "query" else texts

    try:
        return await _generate_vectors(embed_texts, settings)

    except Exception as exc:
        logger.warning(
            "embedding_batch_failed",
            batch_size=len(texts),
            error=str(exc),
        )
        return [[0.0] * settings.embedding_dimensions for _ in texts]


def _renormalize(vec: list[float]) -> list[float]:
    """L2-renormalize *vec* to unit length.

    MRL (Matryoshka) truncation — server- or client-side — yields a coherent
    lower-dimensional embedding but does not preserve unit norm; every vector
    this module returns must be unit length for cosine similarity to behave
    as callers expect.

    Raises:
        EmbeddingResponseError: If vec is the zero vector (degenerate embedding).
    """
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0.0:
        raise EmbeddingResponseError("cannot renormalize a zero vector (degenerate embedding)")
    return [x / norm for x in vec]


def _to_target_dimension(vec: list[float], dimensions: int) -> list[float]:
    """Truncate a native-width embedding to *dimensions* and renormalize.

    Matryoshka-trained embedders (Qwen3-Embedding) produce vectors whose
    leading N components are themselves a coherent lower-dimensional
    embedding, so a too-long vector (e.g. a local-fallback server that
    doesn't honor the OpenAI ``dimensions`` request param) is truncated then
    renormalized — mirrors
    ``scripts/eval/fre435_memory_recall/separation_report.truncate_renormalize``.
    Fails loud on a too-short vector rather than silently zero-padding.

    Raises:
        EmbeddingResponseError: If vec is shorter than dimensions.
    """
    if len(vec) < dimensions:
        raise EmbeddingResponseError(
            f"embedding vector has {len(vec)} components, shorter than the configured "
            f"{dimensions} -- refusing to zero-pad a degenerate response"
        )
    return _renormalize(vec[:dimensions])


async def _embed_managed_batch(
    texts: list[str],
    base_url: str,
    token: str,
    model: str,
    dimensions: int,
    client: httpx.AsyncClient,
) -> list[list[float]]:
    """One managed-embedder request for a batch within the endpoint's size limit.

    Fail-loud on cardinality mismatch and re-sorts by each row's ``index`` field
    before extraction — the response is never trusted to preserve input order
    (adapted from the FRE-817 corpus-A/B harness's already-tested OVH helper,
    ``scripts/eval/fre817_corpus_ab_embedder/corpus_ab.py::_embed_ovh_batch``).

    Requests server-side MRL truncation to *dimensions* (OVH honors the OpenAI
    ``dimensions`` param, verified live — FRE-826) and enforces the response
    actually came back at that exact width: a mismatch means the endpoint
    didn't honor the request, which is a server-side anomaly worth failing
    loud on rather than silently re-truncating. Server-side MRL truncation
    doesn't renormalize, so the result is L2-renormalized before returning.
    """
    payload = {"model": model, "input": texts, "dimensions": dimensions}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"{base_url.rstrip('/')}/embeddings"
    response = await client.post(url, json=payload, headers=headers)
    response.raise_for_status()
    rows = response.json()["data"]
    if len(rows) != len(texts):
        raise EmbeddingResponseError(
            f"managed embeddings response returned {len(rows)} rows for {len(texts)} "
            "inputs -- truncated/expanded response, refusing to score"
        )
    ordered = sorted(rows, key=lambda row: row["index"])
    vectors = [[float(x) for x in row["embedding"]] for row in ordered]
    for vec in vectors:
        if len(vec) != dimensions:
            raise EmbeddingResponseError(
                f"managed embeddings response returned a {len(vec)}-dim vector, expected "
                f"{dimensions} (settings.embedding_dimensions) -- the endpoint did not "
                "honor the requested width"
            )
    return [_renormalize(vec) for vec in vectors]


async def _embed_managed(
    texts: list[str],
    base_url: str,
    token: str,
    model: str,
    *,
    client: httpx.AsyncClient | None = None,
    dimensions: int | None = None,
) -> list[list[float]]:
    """Embed via the managed embedder endpoint (OVH AI Endpoints Qwen3-Embedding-8B).

    Chunks ``texts`` to :data:`_MANAGED_MAX_BATCH` per request (the endpoint
    rejects larger batches, confirmed live — FRE-817) and concatenates results
    in input order.

    Args:
        texts: Already mode-prefixed input texts to embed.
        base_url: Managed endpoint base URL.
        token: Bearer token.
        model: Model id to request.
        client: Injected client for testing; a real one is opened when omitted.
        dimensions: Target embedding width requested from the endpoint (server-side
            MRL truncation, FRE-826). Defaults to ``settings.embedding_dimensions``
            when omitted (live callers, e.g. the FRE-821 failover probe, don't
            need to plumb it through explicitly).

    Returns:
        Unit-length embedding vectors, each exactly ``dimensions`` components,
        in input order.

    Raises:
        httpx.HTTPStatusError: On a non-2xx response.
        EmbeddingResponseError: On a response whose row count or per-vector
            length doesn't match the request, or a degenerate (zero) vector.
    """
    if dimensions is None:
        dimensions = get_settings().embedding_dimensions
    chunks = [
        texts[start : start + _MANAGED_MAX_BATCH]
        for start in range(0, len(texts), _MANAGED_MAX_BATCH)
    ]
    if client is not None:
        results = [
            await _embed_managed_batch(chunk, base_url, token, model, dimensions, client)
            for chunk in chunks
        ]
    else:
        async with httpx.AsyncClient(timeout=120.0) as owned_client:
            results = [
                await _embed_managed_batch(chunk, base_url, token, model, dimensions, owned_client)
                for chunk in chunks
            ]
    return [vec for batch in results for vec in batch]


# One client per endpoint. The previous single global bound to the first
# endpoint it saw and ignored later ones — a correctness trap when an A/B run
# switches embedders (e.g. the Docker 0.6B vs the Access-gated 4B). (FRE-656)
_openai_clients: dict[tuple[str, str], Any] = {}


async def _call_embeddings_api(
    texts: list[str],
    model: str,
    endpoint: str,
    api_key: str = "unused",
) -> Any:
    """Call the embedding API via OpenAI-compatible client.

    Args:
        texts: Texts to embed.
        model: Model identifier from models.yaml (e.g., "Qwen/Qwen3-Embedding-0.6B").
        endpoint: Base URL of the embedding server (e.g., "http://localhost:8503/v1").
        api_key: Credential for the endpoint. "unused" for unauthenticated local
            servers; a real token for managed providers, which return 401
            without it.

    Returns:
        OpenAI API response object with .data[].embedding.
    """
    settings = get_settings()
    client = _openai_clients.get((endpoint, api_key))
    if client is None:
        import openai  # noqa: PLC0415

        # Access-gated Mac SLM gateway needs the CF service token and a benign
        # User-Agent (the SDK default is WAF-blocked); internal Docker endpoints
        # need neither (gated by hostname).
        headers: dict[str, str] = {}
        if settings.slm_tunnel_base_url and settings.slm_tunnel_base_url in endpoint:
            headers = {
                **cf_access_service_token_headers(),
                "User-Agent": _EMBEDDING_USER_AGENT,
            }
        # Local server does not require an API key, but the OpenAI client
        # requires a non-empty string.
        client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=endpoint,
            default_headers=headers,
        )
        _openai_clients[(endpoint, api_key)] = client

    return await client.embeddings.create(
        model=model,
        input=texts,
        dimensions=settings.embedding_dimensions,
    )


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Cosine similarity score (0.0 to 1.0).
    """
    if len(a) != len(b) or not a:
        return 0.0

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return float(dot / (norm_a * norm_b))
