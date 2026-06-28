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

import structlog

from personal_agent.config import get_settings
from personal_agent.service.cf_service_token import cf_access_service_token_headers

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

# Instruction prefix for query-mode embeddings (Qwen3-Embedding format).
# Documents are embedded without a prefix.
_QUERY_PREFIX = "Instruct: Given a query, retrieve relevant entities and passages\nQuery: "

# Hostname of the Access-gated Mac SLM gateway (mirrors llm_client/client.py:58).
# Requests to it must carry the CF-Access service token; internal Docker
# endpoints (embeddings:8503) must not. (FRE-656)
_SLM_TUNNEL_HOSTNAME = "slm.frenchforet.com"

# The OpenAI SDK's default ``User-Agent: OpenAI/Python <ver>`` trips a Cloudflare
# WAF managed rule on the gateway (a 403 "request blocked", which would degrade
# silently to a zero vector). The raw-httpx LLM client is unaffected; only this
# SDK path needs a benign UA. Applied only for the gated host. (FRE-656)
_EMBEDDING_USER_AGENT = "seshat-memory/1.0"


def _get_embedding_config() -> tuple[str, str]:
    """Load embedding model id and endpoint from config/models.yaml.

    Returns:
        Tuple of (model_id, endpoint_url).

    Raises:
        KeyError: If 'embedding' entry is missing from models.yaml.
    """
    from personal_agent.config import load_model_config  # noqa: PLC0415

    config = load_model_config()
    model_def = config.models["embedding"]
    endpoint = model_def.endpoint or "http://localhost:8503/v1"
    return model_def.id, endpoint


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
        model_id, endpoint = _get_embedding_config()
        response = await _call_embeddings_api(
            texts=[embed_text],
            model=model_id,
            endpoint=endpoint,
        )
        return [float(x) for x in response.data[0].embedding]

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
        model_id, endpoint = _get_embedding_config()
        response = await _call_embeddings_api(
            texts=embed_texts,
            model=model_id,
            endpoint=endpoint,
        )
        return [[float(x) for x in d.embedding] for d in response.data]

    except Exception as exc:
        logger.warning(
            "embedding_batch_failed",
            batch_size=len(texts),
            error=str(exc),
        )
        return [[0.0] * settings.embedding_dimensions for _ in texts]


# One client per endpoint. The previous single global bound to the first
# endpoint it saw and ignored later ones — a correctness trap when an A/B run
# switches embedders (e.g. the Docker 0.6B vs the Access-gated 4B). (FRE-656)
_openai_clients: dict[str, Any] = {}


async def _call_embeddings_api(
    texts: list[str],
    model: str,
    endpoint: str,
) -> Any:
    """Call the embedding API via OpenAI-compatible client.

    Args:
        texts: Texts to embed.
        model: Model identifier from models.yaml (e.g., "Qwen/Qwen3-Embedding-0.6B").
        endpoint: Base URL of the embedding server (e.g., "http://localhost:8503/v1").

    Returns:
        OpenAI API response object with .data[].embedding.
    """
    client = _openai_clients.get(endpoint)
    if client is None:
        import openai  # noqa: PLC0415

        # Access-gated Mac SLM gateway needs the CF service token and a benign
        # User-Agent (the SDK default is WAF-blocked); internal Docker endpoints
        # need neither (gated by hostname).
        headers: dict[str, str] = {}
        if _SLM_TUNNEL_HOSTNAME in endpoint:
            headers = {
                **cf_access_service_token_headers(),
                "User-Agent": _EMBEDDING_USER_AGENT,
            }
        # Local server does not require an API key, but the OpenAI client
        # requires a non-empty string.
        client = openai.AsyncOpenAI(
            api_key="unused",
            base_url=endpoint,
            default_headers=headers,
        )
        _openai_clients[endpoint] = client

    settings = get_settings()
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
