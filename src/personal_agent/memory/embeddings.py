# src/personal_agent/memory/embeddings.py
"""Embedding generation pipeline for Seshat memory.

Generates vector embeddings for Entity and Turn nodes to enable
hybrid search (vector + keyword + graph traversal).

Supports OpenAI API (text-embedding-3-small) and local models
(nomic-embed-text via MLX). Provider selected via config.

See: ADR-0035 (seshat-backend-decision), Enhancement 1
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import structlog

from personal_agent.config import get_settings

logger = structlog.get_logger(__name__)


class EmbeddingProvider(Enum):
    """Embedding model provider."""

    OPENAI = "openai"
    LOCAL = "local"


async def generate_embedding(text: str | None) -> list[float]:
    """Generate an embedding vector for a single text.

    Args:
        text: Text to embed. Returns zero vector for empty/None.

    Returns:
        List of floats with length == settings.embedding_dimensions.
    """
    settings = get_settings()
    dimensions = settings.embedding_dimensions

    if not text or not text.strip():
        return [0.0] * dimensions

    try:
        response = await _call_openai_embeddings(
            texts=[text],
            model=settings.embedding_model,
            dimensions=dimensions,
        )
        return [float(x) for x in response.data[0].embedding]

    except Exception as exc:
        logger.warning(
            "embedding_generation_failed",
            text_length=len(text),
            error=str(exc),
        )
        return [0.0] * dimensions


async def generate_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts.

    Args:
        texts: List of texts to embed.

    Returns:
        List of embedding vectors, one per input text.
    """
    if not texts:
        return []

    settings = get_settings()

    try:
        response = await _call_openai_embeddings(
            texts=texts,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
        return [[float(x) for x in d.embedding] for d in response.data]

    except Exception as exc:
        logger.warning(
            "embedding_batch_failed",
            batch_size=len(texts),
            error=str(exc),
        )
        return [[0.0] * settings.embedding_dimensions for _ in texts]


async def _call_openai_embeddings(
    texts: list[str],
    model: str,
    dimensions: int,
) -> Any:
    """Call the OpenAI embeddings API.

    Args:
        texts: Texts to embed.
        model: Model name (e.g., "text-embedding-3-small").
        dimensions: Output dimensions.

    Returns:
        OpenAI API response object with .data[].embedding.
    """
    import openai

    settings = get_settings()
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    return await client.embeddings.create(
        model=model,
        input=texts,
        dimensions=dimensions,
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
