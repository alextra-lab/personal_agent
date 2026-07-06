"""FRE-817 -- unit tests for the OVH embeddings HTTP helper (fake transport, no live network)."""

from __future__ import annotations

import json

import httpx
import pytest
from scripts.eval.fre817_corpus_ab_embedder.corpus_ab import _embed_ovh


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


@pytest.mark.asyncio
async def test_embed_ovh_reorders_out_of_order_rows() -> None:
    """Response rows out of index order are re-sorted before extraction."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        n = len(body["input"])
        # Return rows in REVERSED index order -- the helper must still return
        # vectors in the original input order.
        rows = [{"index": i, "embedding": [float(i), float(i)]} for i in range(n)]
        return httpx.Response(200, json={"data": list(reversed(rows))})

    async with _client(httpx.MockTransport(handler)) as client:
        vectors = await _embed_ovh(
            ["a", "b", "c"], "document", "https://example.test", "tok", "m", client=client
        )
    assert vectors == [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]


@pytest.mark.asyncio
async def test_embed_ovh_raises_on_truncated_response() -> None:
    """Fewer rows than inputs is a truncated response -- refuse to score it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(SystemExit, match="truncated/expanded"):
            await _embed_ovh(
                ["a", "b", "c"], "document", "https://example.test", "tok", "m", client=client
            )


@pytest.mark.asyncio
async def test_embed_ovh_prefixes_query_mode_only() -> None:
    """Only query mode gets the Qwen instruction prefix; document mode sends raw text."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["input"] = body["input"]
        n = len(body["input"])
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [1.0]} for i in range(n)]}
        )

    async with _client(httpx.MockTransport(handler)) as client:
        await _embed_ovh(["hello"], "query", "https://example.test", "tok", "m", client=client)
        assert captured["input"] == [
            "Instruct: Given a query, retrieve relevant entities and passages\nQuery: hello"
        ]

        await _embed_ovh(["hello"], "document", "https://example.test", "tok", "m", client=client)
        assert captured["input"] == ["hello"]


@pytest.mark.asyncio
async def test_embed_ovh_raises_on_http_error() -> None:
    """A non-2xx response raises rather than silently returning empty/garbage vectors."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _embed_ovh(["a"], "document", "https://example.test", "tok", "m", client=client)
