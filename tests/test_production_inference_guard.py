"""The root conftest refuses a real request to the production inference port."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import _refuse_production_inference

PRODUCTION_URL = "http://127.0.0.1:8600/v1/chat/completions"


def test_a_real_sync_request_to_the_production_port_fails_the_test() -> None:
    with httpx.Client() as client, pytest.raises(pytest.fail.Exception, match="production"):
        client.post(PRODUCTION_URL, json={})


@pytest.mark.asyncio
async def test_a_real_async_request_to_the_production_port_fails_the_test() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(pytest.fail.Exception, match="production"):
            await client.post(PRODUCTION_URL, json={})


def test_another_port_is_not_refused() -> None:
    _refuse_production_inference(httpx.Request("POST", "http://localhost:9099/v1/chat/completions"))


def test_a_mock_transport_is_not_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.post(PRODUCTION_URL, json={}).json() == {"ok": True}
