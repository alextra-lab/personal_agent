"""Tests for the managed-embedder path (ADR-0112 D3/D4, AC-5/AC-6, FRE-821).

Covers the `managed_embedder` substrate profile's OVH-managed call (Bearer auth,
25-row batch cap, order preservation) and the same-model local-fallback failover.
The default `private`/`dev`/`test` profiles must be byte-identical to their
pre-FRE-821 behavior (regression guard).
"""

from __future__ import annotations

import json

import httpx
import pytest

from personal_agent.config.settings import AppConfig
from personal_agent.memory.embeddings import (
    EmbeddingResponseError,
    _embed_managed,
    generate_embedding,
    generate_embeddings_batch,
)

_MANAGED_SETTINGS = AppConfig(
    substrate_profile="managed_embedder",
    managed_embedding_endpoint="https://oai.endpoints.ovh.net/v1",
    managed_embedding_token="tok",
    managed_embedding_model="Qwen3-Embedding-8B",
)


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


class TestEmbedManaged:
    """Unit coverage for the OVH-managed batch helper (mirrors FRE-817's tested
    `_embed_ovh`/`_embed_ovh_batch` -- same endpoint, same 25-row cap).
    """

    @pytest.mark.asyncio
    async def test_sends_bearer_auth(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

        async with _client(httpx.MockTransport(handler)) as client:
            await _embed_managed(
                ["hello"], "https://example.test", "tok", "m", client=client, dimensions=2
            )
        assert captured["auth"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_request_includes_dimensions_param(self) -> None:
        """ADR-0112/FRE-826 AC: the OVH request must carry the configured width
        (server-side MRL truncation) -- the pre-fix payload was just {model, input}.
        """
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

        async with _client(httpx.MockTransport(handler)) as client:
            await _embed_managed(
                ["hello"], "https://example.test", "tok", "m", client=client, dimensions=1
            )
        assert captured["body"]["dimensions"] == 1

    @pytest.mark.asyncio
    async def test_chunks_at_25(self) -> None:
        seen_batch_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            n = len(body["input"])
            seen_batch_sizes.append(n)
            return httpx.Response(
                200, json={"data": [{"index": i, "embedding": [1.0]} for i in range(n)]}
            )

        async with _client(httpx.MockTransport(handler)) as client:
            vectors = await _embed_managed(
                [f"t{i}" for i in range(30)],
                "https://example.test",
                "tok",
                "m",
                client=client,
                dimensions=1,
            )
        assert seen_batch_sizes == [25, 5]
        assert len(vectors) == 30

    @pytest.mark.asyncio
    async def test_reorders_out_of_order_rows(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            n = len(body["input"])
            # Two components so renormalization (unconditional -- see
            # test_renormalizes_to_unit_length) doesn't collapse each row to
            # a bare sign; the ratio between components is renorm-invariant
            # and still encodes each row's original index.
            rows = [{"index": i, "embedding": [1.0, float(i)]} for i in range(n)]
            return httpx.Response(200, json={"data": list(reversed(rows))})

        async with _client(httpx.MockTransport(handler)) as client:
            vectors = await _embed_managed(
                ["a", "b", "c"],
                "https://example.test",
                "tok",
                "m",
                client=client,
                dimensions=2,
            )
        ratios = [v[1] / v[0] for v in vectors]
        assert ratios == pytest.approx([0.0, 1.0, 2.0])

    @pytest.mark.asyncio
    async def test_renormalizes_to_unit_length(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]})

        async with _client(httpx.MockTransport(handler)) as client:
            vectors = await _embed_managed(
                ["hello"], "https://example.test", "tok", "m", client=client, dimensions=2
            )
        assert vectors == [pytest.approx([0.6, 0.8])]

    @pytest.mark.asyncio
    async def test_raises_on_truncated_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

        async with _client(httpx.MockTransport(handler)) as client:
            with pytest.raises(EmbeddingResponseError):
                await _embed_managed(
                    ["a", "b", "c"],
                    "https://example.test",
                    "tok",
                    "m",
                    client=client,
                    dimensions=1,
                )

    @pytest.mark.asyncio
    async def test_raises_on_wrong_dimension(self) -> None:
        """The endpoint ignoring/mis-honoring the requested width must fail loud,
        not silently return a native-width (e.g. 4096) vector (FRE-826 AC).
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 4096}]})

        async with _client(httpx.MockTransport(handler)) as client:
            with pytest.raises(EmbeddingResponseError):
                await _embed_managed(
                    ["hello"], "https://example.test", "tok", "m", client=client, dimensions=1024
                )

    @pytest.mark.asyncio
    async def test_raises_on_http_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        async with _client(httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await _embed_managed(
                    ["a"], "https://example.test", "tok", "m", client=client, dimensions=1
                )


class TestManagedProfileIntegration:
    """`generate_embedding`/`generate_embeddings_batch` under the managed_embedder profile."""

    @pytest.mark.asyncio
    async def test_managed_success_returns_managed_vectors(self) -> None:
        settings = _MANAGED_SETTINGS.model_copy(update={"embedding_dimensions": 1})

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["dimensions"] == 1
            n = len(body["input"])
            return httpx.Response(
                200, json={"data": [{"index": i, "embedding": [9.0]} for i in range(n)]}
            )

        with (
            _patched_get_settings(settings),
            _patched_httpx_client(handler),
        ):
            embedding = await generate_embedding("hello")
        # Renormalized: a lone positive scalar always renormalizes to unit sign.
        assert embedding == [1.0]

    @pytest.mark.asyncio
    async def test_managed_path_returns_1024_unit_vectors(self) -> None:
        """FRE-826 AC: with AGENT_EMBEDDING_DIMENSIONS=1024 (the default), the
        managed path returns 1024-dim unit vectors, not OVH's native 4096.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["dimensions"] == 1024
            n = len(body["input"])
            return httpx.Response(
                200,
                json={"data": [{"index": i, "embedding": [0.1] * 1024} for i in range(n)]},
            )

        with (
            _patched_get_settings(_MANAGED_SETTINGS),
            _patched_httpx_client(handler),
        ):
            embedding = await generate_embedding("hello")
        assert len(embedding) == 1024
        norm = sum(x * x for x in embedding) ** 0.5
        assert norm == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_managed_failure_falls_back_to_local(self) -> None:
        settings = _MANAGED_SETTINGS.model_copy(
            update={
                "local_fallback_embedding_endpoint": "http://local-8b:8503/v1",
                "local_fallback_embedding_model": "Qwen/Qwen3-Embedding-8B",
            }
        )

        async def _boom(*args: object, **kwargs: object) -> list[list[float]]:
            raise httpx.ConnectError("unreachable", request=httpx.Request("POST", "https://x"))

        from unittest.mock import AsyncMock, patch

        # Native-4096 local response -- the local llama.cpp server is not known
        # to honor the OpenAI `dimensions` request param, so the fallback path
        # must client-side truncate+renormalize to the configured 1024 (FRE-826).
        fallback_response = _fake_openai_response([[0.05] * 4096])
        with (
            patch("personal_agent.memory.embeddings.get_settings", return_value=settings),
            patch("personal_agent.memory.embeddings._embed_managed", side_effect=_boom),
            patch(
                "personal_agent.memory.embeddings._call_embeddings_api",
                new_callable=AsyncMock,
                return_value=fallback_response,
            ) as mock_fallback,
        ):
            embedding = await generate_embedding("hello")

        assert len(embedding) == 1024
        norm = sum(x * x for x in embedding) ** 0.5
        assert norm == pytest.approx(1.0)
        assert mock_fallback.call_args.kwargs["endpoint"] == "http://local-8b:8503/v1"
        assert mock_fallback.call_args.kwargs["model"] == "Qwen/Qwen3-Embedding-8B"

    @pytest.mark.asyncio
    async def test_managed_failure_no_fallback_returns_zero_vector(self) -> None:
        settings = _MANAGED_SETTINGS.model_copy(update={"local_fallback_embedding_endpoint": None})

        async def _boom(*args: object, **kwargs: object) -> list[list[float]]:
            raise httpx.ConnectError("unreachable", request=httpx.Request("POST", "https://x"))

        from unittest.mock import patch

        with (
            patch("personal_agent.memory.embeddings.get_settings", return_value=settings),
            patch("personal_agent.memory.embeddings._embed_managed", side_effect=_boom),
        ):
            embedding = await generate_embedding("hello")

        assert embedding == [0.0] * settings.embedding_dimensions

    @pytest.mark.asyncio
    async def test_managed_and_fallback_both_fail_returns_zero_vector(self) -> None:
        settings = _MANAGED_SETTINGS.model_copy(
            update={"local_fallback_embedding_endpoint": "http://local-8b:8503/v1"}
        )

        async def _boom(*args: object, **kwargs: object) -> list[list[float]]:
            raise httpx.ConnectError("unreachable", request=httpx.Request("POST", "https://x"))

        from unittest.mock import AsyncMock, patch

        with (
            patch("personal_agent.memory.embeddings.get_settings", return_value=settings),
            patch("personal_agent.memory.embeddings._embed_managed", side_effect=_boom),
            patch(
                "personal_agent.memory.embeddings._call_embeddings_api",
                new_callable=AsyncMock,
                side_effect=ConnectionError("fallback also down"),
            ),
        ):
            embedding = await generate_embedding("hello")

        assert embedding == [0.0] * settings.embedding_dimensions


def _fake_openai_response(vectors: list[list[float]]) -> object:
    from unittest.mock import MagicMock

    data = [MagicMock(embedding=v) for v in vectors]
    resp = MagicMock()
    resp.data = data
    return resp


def _patched_get_settings(settings: AppConfig):
    from unittest.mock import patch

    return patch("personal_agent.memory.embeddings.get_settings", return_value=settings)


_RealAsyncClient = httpx.AsyncClient


def _patched_httpx_client(handler):
    from unittest.mock import patch

    return patch(
        "httpx.AsyncClient",
        side_effect=lambda **kwargs: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestPrivateProfileUnaffected:
    """Regression guard: private/dev/test profiles behave exactly as before FRE-821."""

    @pytest.mark.asyncio
    async def test_private_profile_never_calls_embed_managed(self) -> None:
        from unittest.mock import AsyncMock, patch

        mock_resp = _fake_openai_response([[0.1] * 1024])
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
            patch("personal_agent.memory.embeddings._embed_managed") as mock_managed,
        ):
            embedding = await generate_embedding("hello")
        assert len(embedding) == 1024
        mock_managed.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_private_profile_never_calls_embed_managed(self) -> None:
        from unittest.mock import AsyncMock, patch

        mock_resp = _fake_openai_response([[0.1] * 1024, [0.2] * 1024])
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
            patch("personal_agent.memory.embeddings._embed_managed") as mock_managed,
        ):
            embeddings = await generate_embeddings_batch(["a", "b"])
        assert len(embeddings) == 2
        mock_managed.assert_not_called()
