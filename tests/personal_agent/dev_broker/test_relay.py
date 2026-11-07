# ruff: noqa: D102
"""The control relay forwards only to the current job's container (ADR-0159 D4.3; FRE-1683 AC-5)."""

from __future__ import annotations

from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv4Network
from uuid import UUID, uuid4

import httpx
import pytest
from personal_agent.dev_broker.relay import SECRET_HEADER, build_control_app
from personal_agent.security import DomainGuard, GuardMode, create_guarded_http_client

from personal_agent.dev_agent.container_spec import agent_container_name

from .fakes import FakeClock

pytestmark = pytest.mark.asyncio

_SECRET = "control-secret-value"
_SUBNET = IPv4Network("172.19.0.0/16")
_AGENT_IP = IPv4Address("172.19.0.7")
_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


class _Ledger:
    def __init__(self, open_jobs: set[UUID]) -> None:
        self.open_jobs = open_jobs

    async def relay_open(self, job_id: UUID, *, now: datetime) -> UUID | None:
        return uuid4() if job_id in self.open_jobs else None


class _Upstream:
    """The OpenCode server, as an httpx mock transport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=b"data: {}\n\n",
            headers={"content-type": "text/event-stream", "set-cookie": "x=1"},
        )


def _client(
    job_id: UUID,
    upstream: _Upstream,
    *,
    resolved: dict[str, list[IPv4Address]] | None = None,
    open_jobs: set[UUID] | None = None,
) -> httpx.AsyncClient:
    names = resolved if resolved is not None else {agent_container_name(job_id): [_AGENT_IP]}

    async def resolve(host: str) -> list[IPv4Address]:
        return names.get(host, [])

    app = build_control_app(
        secret=_SECRET,
        ledger=_Ledger({job_id} if open_jobs is None else open_jobs),
        resolver=resolve,
        subnet=_SUBNET,
        opencode_port=4096,
        clock=FakeClock(_NOW),
        upstream_factory=lambda target: create_guarded_http_client(
            guard=DomainGuard(mode=GuardMode.ALLOWLIST, allowlist=frozenset({str(target)})),
            transport=httpx.MockTransport(upstream.handler),
        ),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker")


async def test_forwards_to_the_jobs_container_only() -> None:
    job_id, upstream = uuid4(), _Upstream()
    async with _client(job_id, upstream) as client:
        response = await client.post(
            f"/relay/{job_id}/session/abc/message?directory=%2Fworkspace",
            content=b'{"parts": []}',
            headers={
                SECRET_HEADER: _SECRET,
                "Authorization": "Basic b3BlbmNvZGU6cHc=",
                "Content-Type": "application/json",
                "Cookie": "gateway=1",
                "X-Forwarded-For": "1.2.3.4",
            },
        )

    assert response.status_code == 200
    assert response.text == "data: {}\n\n"
    assert "set-cookie" not in response.headers
    [sent] = upstream.requests
    assert sent.url.host == "172.19.0.7"
    assert sent.url.port == 4096
    assert sent.url.raw_path == b"/session/abc/message?directory=%2Fworkspace"
    assert sent.headers["authorization"] == "Basic b3BlbmNvZGU6cHc="
    assert SECRET_HEADER.lower() not in sent.headers
    assert "cookie" not in sent.headers
    assert "x-forwarded-for" not in sent.headers
    assert sent.content == b'{"parts": []}'


@pytest.mark.parametrize("secret", [None, "wrong", _SECRET + "x"])
async def test_a_missing_or_wrong_secret_is_401(secret: str | None) -> None:
    job_id, upstream = uuid4(), _Upstream()
    headers = {} if secret is None else {SECRET_HEADER: secret}
    async with _client(job_id, upstream) as client:
        response = await client.get(f"/relay/{job_id}/event", headers=headers)
    assert response.status_code == 401
    assert upstream.requests == []


async def test_a_job_that_is_not_current_is_404() -> None:
    """A revoked, expired or unknown job (the ledger says not open) is refused."""
    job_id, upstream = uuid4(), _Upstream()
    async with _client(job_id, upstream, open_jobs=set()) as client:
        response = await client.get(f"/relay/{job_id}/event", headers={SECRET_HEADER: _SECRET})
    assert response.status_code == 404
    assert upstream.requests == []


async def test_another_jobs_container_is_never_the_target() -> None:
    """The target comes from the job id, never from the request."""
    job_id, other, upstream = uuid4(), uuid4(), _Upstream()
    resolved = {
        agent_container_name(job_id): [_AGENT_IP],
        agent_container_name(other): [IPv4Address("172.19.0.9")],
    }
    async with _client(job_id, upstream, resolved=resolved) as client:
        response = await client.get(
            f"/relay/{other}/event", headers={SECRET_HEADER: _SECRET, "Host": "172.19.0.9"}
        )
    assert response.status_code == 404
    assert upstream.requests == []


@pytest.mark.parametrize(
    "addresses",
    [
        [IPv4Address("172.25.0.3")],  # a cloud-sim address
        [_AGENT_IP, IPv4Address("172.25.0.3")],  # one inside, one outside
        [IPv4Address("127.0.0.1")],
        [],
    ],
)
async def test_a_name_that_resolves_outside_the_dev_agent_subnet_is_refused(
    addresses: list[IPv4Address],
) -> None:
    job_id, upstream = uuid4(), _Upstream()
    resolved = {agent_container_name(job_id): addresses}
    async with _client(job_id, upstream, resolved=resolved) as client:
        response = await client.get(f"/relay/{job_id}/event", headers={SECRET_HEADER: _SECRET})
    assert response.status_code == 502
    assert upstream.requests == []


@pytest.mark.parametrize(
    "path",
    ["@169.254.169.254/latest", "http://github.com/", "//caddy:8601/x", "..%2F..%2Fetc"],
)
async def test_no_path_can_change_the_host_or_port(path: str) -> None:
    job_id, upstream = uuid4(), _Upstream()
    async with _client(job_id, upstream) as client:
        await client.get(f"/relay/{job_id}/{path}", headers={SECRET_HEADER: _SECRET})
    for sent in upstream.requests:
        assert (sent.url.host, sent.url.port) == ("172.19.0.7", 4096)


async def test_the_relay_has_no_other_route() -> None:
    job_id, upstream = uuid4(), _Upstream()
    async with _client(job_id, upstream) as client:
        for path in ("/", "/docs", "/openapi.json", "/v1/chat/completions", "/proxy/x"):
            response = await client.get(path, headers={SECRET_HEADER: _SECRET})
            assert response.status_code == 404, path
    assert upstream.requests == []


async def test_the_production_client_reaches_only_its_target() -> None:
    """ADR-0132 D2: the relay's client is guarded, and its guard allows the one address."""
    from personal_agent.dev_broker.relay import guarded_upstream_client
    from personal_agent.security import EgressBlockedError

    client = guarded_upstream_client(_AGENT_IP)
    try:
        with pytest.raises(EgressBlockedError):
            await client.get("http://172.19.0.8:4096/event")
        with pytest.raises(EgressBlockedError):
            await client.get("http://github.com/")
    finally:
        await client.aclose()


async def test_an_open_stream_ends_when_the_job_is_revoked() -> None:
    """D8.8.1: revocation cuts every path, including an event stream that is already open."""
    import asyncio

    job_id = uuid4()
    ledger = _Ledger({job_id})

    class _Endless(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            while True:
                yield b"data: {}\n\n"
                await asyncio.sleep(0.01)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Endless(), headers={"content-type": "text/event-stream"})

    async def resolve(_host: str) -> list[IPv4Address]:
        return [_AGENT_IP]

    app = build_control_app(
        secret=_SECRET,
        ledger=ledger,
        resolver=resolve,
        subnet=_SUBNET,
        opencode_port=4096,
        clock=FakeClock(_NOW),
        upstream_factory=lambda target: create_guarded_http_client(
            guard=DomainGuard(mode=GuardMode.ALLOWLIST, allowlist=frozenset({str(target)})),
            transport=httpx.MockTransport(handler),
        ),
        revocation_check_seconds=0.05,
    )
    # httpx's ASGI transport buffers a whole body, so drive the app over raw ASGI instead.
    chunks = 0
    ended = asyncio.Event()

    requested = False

    async def receive() -> dict[str, object]:
        nonlocal requested
        if not requested:  # the (empty) request body first
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await ended.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        nonlocal chunks
        if message["type"] == "http.response.body":
            if message.get("body"):
                chunks += 1
                if chunks == 3:
                    ledger.open_jobs.clear()  # the gateway revokes the token
            if not message.get("more_body", False):
                ended.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/relay/{job_id}/event",
        "raw_path": f"/relay/{job_id}/event".encode(),
        "query_string": b"",
        "headers": [(SECRET_HEADER.lower().encode(), _SECRET.encode())],
        "server": ("broker", 8701),
        "client": ("172.25.0.2", 5000),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=3)
    assert ended.is_set()
    assert chunks >= 3


async def test_the_upstream_closes_when_the_answer_never_starts() -> None:
    """The gateway can leave before the first byte. The upstream connection must not stay open."""
    import asyncio

    job_id = uuid4()
    closed = asyncio.Event()

    class _Tracked(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield b"data: {}\n\n"

        async def aclose(self) -> None:
            closed.set()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Tracked())

    async def resolve(_host: str) -> list[IPv4Address]:
        return [_AGENT_IP]

    app = build_control_app(
        secret=_SECRET,
        ledger=_Ledger({job_id}),
        resolver=resolve,
        subnet=_SUBNET,
        opencode_port=4096,
        clock=FakeClock(_NOW),
        upstream_factory=lambda target: create_guarded_http_client(
            guard=DomainGuard(mode=GuardMode.ALLOWLIST, allowlist=frozenset({str(target)})),
            transport=httpx.MockTransport(handler),
        ),
    )

    sent_request = False

    async def receive() -> dict[str, object]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            raise OSError("the gateway went away")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/relay/{job_id}/event",
        "raw_path": f"/relay/{job_id}/event".encode(),
        "query_string": b"",
        "headers": [(SECRET_HEADER.lower().encode(), _SECRET.encode())],
        "server": ("broker", 8701),
        "client": ("172.25.0.2", 5000),
    }
    with pytest.raises(OSError):
        await asyncio.wait_for(app(scope, receive, send), timeout=3)
    assert closed.is_set()
