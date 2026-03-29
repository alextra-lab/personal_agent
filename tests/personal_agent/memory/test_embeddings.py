# tests/personal_agent/memory/test_embeddings.py
"""Tests for the embedding generation pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.memory.embeddings import (
    EmbeddingProvider,
    generate_embedding,
    generate_embeddings_batch,
)


class TestEmbeddingProvider:
    def test_provider_enum(self) -> None:
        assert EmbeddingProvider.OPENAI.value == "openai"
        assert EmbeddingProvider.LOCAL.value == "local"


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_generates_vector(self) -> None:
        """Should return a list of floats with correct dimensions."""
        mock_response = type("R", (), {"data": [type("D", (), {"embedding": [0.1] * 1536})()]})()

        with patch(
            "personal_agent.memory.embeddings._call_openai_embeddings",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            embedding = await generate_embedding("Hello world")
            assert len(embedding) == 1536
            assert all(isinstance(x, float) for x in embedding)

    @pytest.mark.asyncio
    async def test_empty_text_returns_zeros(self) -> None:
        """Empty text should return zero vector."""
        embedding = await generate_embedding("")
        assert len(embedding) == 1536
        assert all(x == 0.0 for x in embedding)

    @pytest.mark.asyncio
    async def test_none_text_returns_zeros(self) -> None:
        """None text should return zero vector."""
        embedding = await generate_embedding(None)
        assert len(embedding) == 1536
        assert all(x == 0.0 for x in embedding)


class TestGenerateEmbeddingsBatch:
    @pytest.mark.asyncio
    async def test_batch_generation(self) -> None:
        """Batch should return one embedding per input text."""
        mock_response = type(
            "R", (), {"data": [type("D", (), {"embedding": [0.1] * 1536})() for _ in range(3)]}
        )()

        with patch(
            "personal_agent.memory.embeddings._call_openai_embeddings",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            embeddings = await generate_embeddings_batch(["a", "b", "c"])
            assert len(embeddings) == 3
            assert all(len(e) == 1536 for e in embeddings)

    @pytest.mark.asyncio
    async def test_empty_batch(self) -> None:
        """Empty batch should return empty list."""
        embeddings = await generate_embeddings_batch([])
        assert embeddings == []
