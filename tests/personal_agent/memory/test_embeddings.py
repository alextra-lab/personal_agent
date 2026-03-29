# tests/personal_agent/memory/test_embeddings.py
"""Tests for the embedding generation pipeline.

Verifies Qwen3-Embedding-0.6B integration via slm_server (768d vectors,
instruction prefix for query mode, graceful degradation on failure).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.memory.embeddings import (
    _QUERY_PREFIX,
    cosine_similarity,
    generate_embedding,
    generate_embeddings_batch,
)

# Dimensions match Qwen3-Embedding-0.6B native output
_DIMS = 1024


def _mock_response(vectors: list[list[float]]) -> MagicMock:
    """Create a mock OpenAI embeddings response."""
    data = [MagicMock(embedding=v) for v in vectors]
    resp = MagicMock()
    resp.data = data
    return resp


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_generates_768d_vector(self) -> None:
        """Should return a list of floats with 768 dimensions."""
        mock_resp = _mock_response([[0.1] * _DIMS])

        with (
            patch(
                "personal_agent.memory.embeddings._get_embedding_config",
                return_value=("Qwen/Qwen3-Embedding-0.6B", "http://localhost:8503/v1"),
            ),
            patch(
                "personal_agent.memory.embeddings._call_embeddings_api",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ) as mock_api,
        ):
            embedding = await generate_embedding("Hello world")
            assert len(embedding) == _DIMS
            assert all(isinstance(x, float) for x in embedding)
            # Document mode: no instruction prefix
            call_args = mock_api.call_args
            texts_arg = call_args.kwargs.get("texts", call_args.args[0] if call_args.args else [])
            assert not any(t.startswith("Instruct:") for t in texts_arg)

    @pytest.mark.asyncio
    async def test_query_mode_adds_prefix(self) -> None:
        """Query mode should prepend instruction prefix."""
        mock_resp = _mock_response([[0.1] * _DIMS])

        with (
            patch(
                "personal_agent.memory.embeddings._get_embedding_config",
                return_value=("Qwen/Qwen3-Embedding-0.6B", "http://localhost:8503/v1"),
            ),
            patch(
                "personal_agent.memory.embeddings._call_embeddings_api",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ) as mock_api,
        ):
            await generate_embedding("what database?", mode="query")
            call_args = mock_api.call_args
            texts_arg = call_args.kwargs.get("texts", call_args.args[0] if call_args.args else [])
            assert texts_arg[0].startswith(_QUERY_PREFIX)

    @pytest.mark.asyncio
    async def test_empty_text_returns_zeros(self) -> None:
        """Empty text should return zero vector without API call."""
        embedding = await generate_embedding("")
        assert len(embedding) == _DIMS
        assert all(x == 0.0 for x in embedding)

    @pytest.mark.asyncio
    async def test_none_text_returns_zeros(self) -> None:
        """None text should return zero vector without API call."""
        embedding = await generate_embedding(None)
        assert len(embedding) == _DIMS
        assert all(x == 0.0 for x in embedding)

    @pytest.mark.asyncio
    async def test_api_failure_returns_zeros(self) -> None:
        """API failure should return zero vector (graceful degradation)."""
        with (
            patch(
                "personal_agent.memory.embeddings._get_embedding_config",
                return_value=("Qwen/Qwen3-Embedding-0.6B", "http://localhost:8503/v1"),
            ),
            patch(
                "personal_agent.memory.embeddings._call_embeddings_api",
                new_callable=AsyncMock,
                side_effect=ConnectionError("server down"),
            ),
        ):
            embedding = await generate_embedding("test")
            assert len(embedding) == _DIMS
            assert all(x == 0.0 for x in embedding)


class TestGenerateEmbeddingsBatch:
    @pytest.mark.asyncio
    async def test_batch_generation(self) -> None:
        """Batch should return one 768d embedding per input text."""
        mock_resp = _mock_response([[0.1] * _DIMS for _ in range(3)])

        with (
            patch(
                "personal_agent.memory.embeddings._get_embedding_config",
                return_value=("Qwen/Qwen3-Embedding-0.6B", "http://localhost:8503/v1"),
            ),
            patch(
                "personal_agent.memory.embeddings._call_embeddings_api",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
        ):
            embeddings = await generate_embeddings_batch(["a", "b", "c"])
            assert len(embeddings) == 3
            assert all(len(e) == _DIMS for e in embeddings)

    @pytest.mark.asyncio
    async def test_empty_batch(self) -> None:
        """Empty batch should return empty list."""
        embeddings = await generate_embeddings_batch([])
        assert embeddings == []

    @pytest.mark.asyncio
    async def test_batch_failure_returns_zeros(self) -> None:
        """Batch failure should return zero vectors for all inputs."""
        with (
            patch(
                "personal_agent.memory.embeddings._get_embedding_config",
                return_value=("Qwen/Qwen3-Embedding-0.6B", "http://localhost:8503/v1"),
            ),
            patch(
                "personal_agent.memory.embeddings._call_embeddings_api",
                new_callable=AsyncMock,
                side_effect=ConnectionError("server down"),
            ),
        ):
            embeddings = await generate_embeddings_batch(["a", "b"])
            assert len(embeddings) == 2
            assert all(len(e) == _DIMS for e in embeddings)
            assert all(x == 0.0 for e in embeddings for x in e)


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_empty_vectors(self) -> None:
        assert cosine_similarity([], []) == 0.0

    def test_different_lengths(self) -> None:
        assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0
