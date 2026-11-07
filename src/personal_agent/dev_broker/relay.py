"""The control relay: the gateway's only way to the job's OpenCode server (ADR-0159 D4.3; FRE-1683).

The gateway is not on ``dev-agent``, so it talks to OpenCode through this relay. The relay:

- accepts a request only with the control secret, which only the gateway holds;
- takes its target from the job id alone: the job's container name, resolved on ``dev-agent``,
  and the OpenCode port. No header, path or query can name another host or port;
- forwards only while the job's token is ``active`` or ``stopped``, never after revocation;
- refuses a name that resolves to any address outside the ``dev-agent`` subnet;
- forwards a fixed set of request headers, follows no redirect, and streams the answer back.

It has no general proxy function.
"""

from __future__ import annotations

import asyncio
import hmac
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from ipaddress import IPv4Address, IPv4Network
from typing import Protocol
from uuid import UUID

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from personal_agent.dev_agent.container_spec import agent_container_name
from personal_agent.dev_broker.clock import Clock
from personal_agent.security import DomainGuard, GuardMode, create_guarded_http_client

log = structlog.get_logger(__name__)

#: The header that carries the control secret. It never reaches OpenCode.
SECRET_HEADER = "X-Dev-Broker-Secret"

#: Request headers that reach OpenCode. ``authorization`` carries the OpenCode server password.
_REQUEST_HEADERS = ("authorization", "content-type", "accept", "last-event-id")
#: Response headers that reach the gateway.
_RESPONSE_HEADERS = ("content-type", "cache-control")

Resolver = Callable[[str], Awaitable[list[IPv4Address]]]


class RelayLedger(Protocol):
    """The ledger call that the relay uses."""

    async def relay_open(self, job_id: UUID, *, now: datetime) -> UUID | None:
        """Return the job's trace id while the relay may forward to it, else ``None``."""
        ...


async def resolve_ipv4(host: str) -> list[IPv4Address]:
    """Resolve ``host`` to its IPv4 addresses through the container's resolver (Docker DNS).

    Args:
        host: A container name.

    Returns:
        The distinct addresses, or an empty list when the name does not resolve.
    """
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return []
    return sorted({IPv4Address(info[4][0]) for info in infos})


UpstreamFactory = Callable[[IPv4Address], httpx.AsyncClient]


def guarded_upstream_client(target: IPv4Address) -> httpx.AsyncClient:
    """Return a client that can reach ``target`` and nothing else (ADR-0132 D2).

    Its egress guard is an allowlist of the one address, so even a redirect could not leave it. It
    ignores proxy variables, and its read timeout is unlimited because the OpenCode event stream is
    long-lived.

    Args:
        target: The job container's address.

    Returns:
        The client. The caller closes it.
    """
    guard = DomainGuard(mode=GuardMode.ALLOWLIST, allowlist=frozenset({str(target)}))
    return create_guarded_http_client(
        guard=guard,
        timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
        trust_env=False,
        follow_redirects=False,
    )


def _denied(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status)


def _upstream_path(request: Request) -> bytes:
    """Return the raw path after ``/relay/<job_id>``, with the raw query, as sent by the gateway."""
    raw: bytes = request.scope.get("raw_path") or request.url.path.encode("ascii")
    parts = raw.split(b"/", 3)  # [b"", b"relay", b"<job_id>", b"rest"]
    path = b"/" + (parts[3] if len(parts) > 3 else b"")
    query: bytes = request.scope.get("query_string", b"")
    return path + (b"?" + query if query else b"")


def build_control_app(
    *,
    secret: str,
    ledger: RelayLedger,
    resolver: Resolver,
    subnet: IPv4Network,
    opencode_port: int,
    clock: Clock,
    upstream_factory: UpstreamFactory = guarded_upstream_client,
) -> FastAPI:
    """Build the ASGI app of the control listener.

    Args:
        secret: The control secret.
        ledger: Answers whether a job is current.
        resolver: Resolves a container name on ``dev-agent``.
        subnet: The ``dev-agent`` subnet. Every resolved address must be inside it.
        opencode_port: The OpenCode server port.
        clock: The time source.
        upstream_factory: Builds the client that forwards to one target address.

    Returns:
        The app.

    Raises:
        ValueError: If ``secret`` is empty.
    """
    if not secret:
        raise ValueError("the control relay needs a secret")
    expected = secret.encode("utf-8")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/relay/{job_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def relay(job_id: UUID, path: str, request: Request) -> Response:
        provided = request.headers.get(SECRET_HEADER)
        if provided is None or not hmac.compare_digest(provided.encode("utf-8"), expected):
            log.warning("dev_broker_relay_unauthenticated", job_id=str(job_id), trace_id=None)
            return _denied(401, "unauthenticated")

        trace_id = await ledger.relay_open(job_id, now=clock.now())
        if trace_id is None:
            log.warning("dev_broker_relay_job_not_current", job_id=str(job_id), trace_id=None)
            return _denied(404, "job_not_current")

        addresses = await resolver(agent_container_name(job_id))
        if not addresses or any(address not in subnet for address in addresses):
            log.error(
                "dev_broker_relay_target_refused",
                job_id=str(job_id),
                trace_id=trace_id.hex,
                addresses=[str(a) for a in addresses],
            )
            return _denied(502, "target_refused")

        url = httpx.URL(
            scheme="http",
            host=str(addresses[0]),
            port=opencode_port,
            raw_path=_upstream_path(request),
        )
        headers = {
            name: value
            for name in _REQUEST_HEADERS
            if (value := request.headers.get(name)) is not None
        }
        body = await request.body()
        upstream = upstream_factory(addresses[0])
        outbound = upstream.build_request(request.method, url, headers=headers, content=body)
        try:
            answer = await upstream.send(outbound, stream=True, follow_redirects=False)
        except httpx.HTTPError as exc:
            await upstream.aclose()
            log.warning(
                "dev_broker_relay_upstream_failed",
                job_id=str(job_id),
                trace_id=trace_id.hex,
                error_type=type(exc).__name__,
            )
            return _denied(502, "upstream_unreachable")

        async def relayed() -> AsyncIterator[bytes]:
            # Closes the answer and the client however the stream ends: done, an upstream
            # error mid-stream, or the gateway going away.
            try:
                async for chunk in answer.aiter_bytes():
                    yield chunk
            finally:
                await answer.aclose()
                await upstream.aclose()

        log.info(
            "dev_broker_relayed",
            job_id=str(job_id),
            trace_id=trace_id.hex,
            method=request.method,
            status=answer.status_code,
        )
        return StreamingResponse(
            relayed(),
            status_code=answer.status_code,
            headers={
                name: value
                for name in _RESPONSE_HEADERS
                if (value := answer.headers.get(name)) is not None
            },
        )

    return app
