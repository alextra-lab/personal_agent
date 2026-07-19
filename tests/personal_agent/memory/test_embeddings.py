# tests/personal_agent/memory/test_embeddings.py
"""Tests for the embedding generation pipeline.

Verifies Qwen3-Embedding-0.6B integration via slm_server (768d vectors,
instruction prefix for query mode, graceful degradation on failure).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config import settings
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
                # (model, endpoint, api_key) — the credential is resolved from the
                # deployment's provider since ADR-0121; calling a managed
                # endpoint without it 401s into the fail-open zero vector.
                return_value=(
                    "Qwen/Qwen3-Embedding-0.6B",
                    "http://localhost:8503/v1",
                    "unused",
                ),
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
                # (model, endpoint, api_key) — the credential is resolved from the
                # deployment's provider since ADR-0121; calling a managed
                # endpoint without it 401s into the fail-open zero vector.
                return_value=(
                    "Qwen/Qwen3-Embedding-0.6B",
                    "http://localhost:8503/v1",
                    "unused",
                ),
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
                # (model, endpoint, api_key) — the credential is resolved from the
                # deployment's provider since ADR-0121; calling a managed
                # endpoint without it 401s into the fail-open zero vector.
                return_value=(
                    "Qwen/Qwen3-Embedding-0.6B",
                    "http://localhost:8503/v1",
                    "unused",
                ),
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
                # (model, endpoint, api_key) — the credential is resolved from the
                # deployment's provider since ADR-0121; calling a managed
                # endpoint without it 401s into the fail-open zero vector.
                return_value=(
                    "Qwen/Qwen3-Embedding-0.6B",
                    "http://localhost:8503/v1",
                    "unused",
                ),
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
                # (model, endpoint, api_key) — the credential is resolved from the
                # deployment's provider since ADR-0121; calling a managed
                # endpoint without it 401s into the fail-open zero vector.
                return_value=(
                    "Qwen/Qwen3-Embedding-0.6B",
                    "http://localhost:8503/v1",
                    "unused",
                ),
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


_CF_HEADERS = {"CF-Access-Client-Id": "id", "CF-Access-Client-Secret": "sec"}


class TestCfAccessInjection:
    """CF-Access injection + per-endpoint client for the embedding path (FRE-656).

    The embedding client must send CF-Access headers to the Access-gated Mac SLM
    gateway, and only to it, and must use a distinct client per endpoint (the old
    module-global singleton bound to the first endpoint and ignored the rest).
    """

    def _fake_ctor(self, captured: list[dict[str, object]]):
        def ctor(**kwargs: object) -> MagicMock:
            captured.append(kwargs)
            client = MagicMock()
            client.embeddings.create = AsyncMock(return_value=_mock_response([[0.1] * 4]))
            return client

        return ctor

    @pytest.mark.asyncio
    async def test_slm_endpoint_gets_cf_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.memory.embeddings import _call_embeddings_api

        monkeypatch.setattr(settings, "slm_tunnel_base_url", "https://slm.example.com")
        captured: list[dict[str, object]] = []
        with (
            patch.dict("personal_agent.memory.embeddings._openai_clients", {}, clear=True),
            patch("openai.AsyncOpenAI", side_effect=self._fake_ctor(captured)),
            patch(
                "personal_agent.memory.embeddings.cf_access_service_token_headers",
                return_value=dict(_CF_HEADERS),
            ),
        ):
            await _call_embeddings_api(["x"], "m", "https://slm.example.com/v1")

        sent = captured[0]["default_headers"]
        assert sent["CF-Access-Client-Id"] == "id"
        assert sent["CF-Access-Client-Secret"] == "sec"
        # SDK default UA is WAF-blocked on the gateway → must be overridden.
        assert sent["User-Agent"] == "seshat-memory/1.0"

    @pytest.mark.asyncio
    async def test_non_slm_endpoint_no_cf_headers(self) -> None:
        from personal_agent.memory.embeddings import _call_embeddings_api

        captured: list[dict[str, object]] = []
        with (
            patch.dict("personal_agent.memory.embeddings._openai_clients", {}, clear=True),
            patch("openai.AsyncOpenAI", side_effect=self._fake_ctor(captured)),
            patch(
                "personal_agent.memory.embeddings.cf_access_service_token_headers",
                return_value=dict(_CF_HEADERS),
            ),
        ):
            await _call_embeddings_api(["x"], "m", "http://embeddings:8503/v1")

        # Gated by hostname: even with creds available, the internal Docker
        # endpoint must not receive the service token.
        assert captured[0]["default_headers"] == {}

    @pytest.mark.asyncio
    async def test_distinct_client_per_endpoint(self) -> None:
        from personal_agent.memory.embeddings import _call_embeddings_api

        captured: list[dict[str, object]] = []
        with (
            patch.dict("personal_agent.memory.embeddings._openai_clients", {}, clear=True),
            patch("openai.AsyncOpenAI", side_effect=self._fake_ctor(captured)),
            patch(
                "personal_agent.memory.embeddings.cf_access_service_token_headers",
                return_value={},
            ),
        ):
            await _call_embeddings_api(["x"], "m", "http://embeddings:8503/v1")
            await _call_embeddings_api(["x"], "m", "https://slm.example.com/v1")

        assert [c["base_url"] for c in captured] == [
            "http://embeddings:8503/v1",
            "https://slm.example.com/v1",
        ]


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_empty_vectors(self) -> None:
        assert cosine_similarity([], []) == 0.0

    def test_different_lengths(self) -> None:
        assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0
