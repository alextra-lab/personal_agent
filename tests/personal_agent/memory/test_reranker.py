# tests/personal_agent/memory/test_reranker.py
"""Tests for the cross-attention reranker module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.memory.reranker import RerankResult, _passthrough, rerank


def _mock_reranker_config() -> MagicMock:
    """Create a mock ModelConfig with a reranker entry."""
    model_def = MagicMock()
    model_def.id = "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF"
    model_def.endpoint = "http://localhost:8504/v1"
    config = MagicMock()
    config.models = {"reranker": model_def}
    return config


class TestRerank:
    @pytest.mark.asyncio
    async def test_successful_rerank(self) -> None:
        """Should call /v1/rerank and return sorted results."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.3},
                {"index": 1, "relevance_score": 0.9},
                {"index": 2, "relevance_score": 0.5},
            ]
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF", "http://localhost:8504/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=mock_client),
        ):
            results = await rerank("what database?", ["doc0", "doc1", "doc2"])

        assert len(results) == 3
        # Should be sorted by score descending
        assert results[0].score == 0.9
        assert results[0].index == 1
        assert results[0].document == "doc1"
        assert results[1].score == 0.5
        assert results[2].score == 0.3

    @pytest.mark.asyncio
    async def test_disabled_returns_passthrough(self) -> None:
        """When reranker_enabled=False, return documents in original order."""
        with patch(
            "personal_agent.memory.reranker.get_settings",
        ) as mock_settings:
            mock_settings.return_value.reranker_enabled = False
            results = await rerank("query", ["a", "b", "c"])

        assert len(results) == 3
        assert results[0].index == 0
        assert results[0].document == "a"

    @pytest.mark.asyncio
    async def test_server_down_returns_passthrough(self) -> None:
        """When reranker server is unreachable, return documents in original order."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF", "http://localhost:8504/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=mock_client),
        ):
            results = await rerank("query", ["a", "b"])

        assert len(results) == 2
        assert results[0].document == "a"

    @pytest.mark.asyncio
    async def test_empty_documents(self) -> None:
        """Empty document list should return empty list."""
        results = await rerank("query", [])
        assert results == []

    @pytest.mark.asyncio
    async def test_missing_config_returns_passthrough(self) -> None:
        """Missing reranker config in models.yaml should degrade gracefully."""
        with patch(
            "personal_agent.memory.reranker._get_reranker_config",
            side_effect=KeyError("reranker"),
        ):
            results = await rerank("query", ["a", "b"])

        assert len(results) == 2
        assert results[0].document == "a"


class TestPassthrough:
    def test_preserves_order(self) -> None:
        results = _passthrough(["first", "second", "third"])
        assert len(results) == 3
        assert results[0].document == "first"
        assert results[0].index == 0
        assert results[1].index == 1

    def test_decreasing_scores(self) -> None:
        results = _passthrough(["a", "b", "c"])
        assert results[0].score > results[1].score > results[2].score

    def test_frozen_dataclass(self) -> None:
        result = RerankResult(index=0, score=0.5, document="test")
        with pytest.raises(AttributeError):
            result.score = 0.9  # type: ignore[misc]
