# ruff: noqa: D101, D102
"""OpenCodeAdapter against a recorded OpenCode 2.2.5 event stream (ADR-0159 D2.2, D2.3; FRE-1684).

``fixtures/opencode_2_2_5_session.sse`` is the raw ``GET /api/event`` stream of one real session
in ``seshat-dev-agent`` (2026-11-07), with a fake model server inside the container: one ``shell``
tool call, one text answer, then ``session.execution.succeeded``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from personal_agent.delegation.adapters.opencode import OpenCodeAdapter, opencode_client
from personal_agent.delegation.protocols import (
    DelegationContext,
    DelegationExecutorProtocol,
    DelegationPackage,
)
from personal_agent.dev_broker.relay import SECRET_HEADER
from personal_agent.telemetry.trace import TraceContext

pytestmark = pytest.mark.asyncio

_FIXTURE = (Path(__file__).parent / "fixtures" / "opencode_2_2_5_session.sse").read_text()
_SESSION = "ses_e4a46e0f4ffeRk1LcipGdiGFoM"
_MODEL = "deepseek-v4.1-flash-scaleway"


def _package() -> DelegationPackage:
    return DelegationPackage(
        task_id=str(uuid4()),
        target_agent="opencode",
        task_description="Build a page with one working button.",
        context=DelegationContext(service_path="/workspace"),
        created_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
    )


def _trace() -> TraceContext:
    return TraceContext.new_trace()


class FakeServer:
    """Answers like OpenCode behind the relay. ``streams`` are the bodies of successive event calls."""

    def __init__(
        self, streams: list[str | None], *, active: dict[str, object] | None = None
    ) -> None:
        self.streams = streams
        self.active = active if active is not None else {}
        self.not_ready = 2
        self.requests: list[httpx.Request] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/relay/job")
        if path == "/api/session/active":
            if self.not_ready:
                self.not_ready -= 1
                return httpx.Response(502, json={"error": {"code": "upstream_unreachable"}})
            return httpx.Response(200, json={"data": self.active})
        if path == "/api/event":
            body = self.streams.pop(0)
            if body is None:
                return httpx.Response(200, stream=_Forever())
            return httpx.Response(200, content=body.encode())
        if path == "/api/session" and request.method == "POST":
            return httpx.Response(200, json={"data": {"id": _SESSION}})
        if path.startswith(f"/api/session/{_SESSION}/"):
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404)

    def posted(self, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and r.url.path.endswith(suffix)]


class _Forever(httpx.AsyncByteStream):
    """An event stream that sends one heartbeat and then nothing."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b": heartbeat\n\n"
        await asyncio.sleep(3600)


def _adapter(server: FakeServer, progress: list[str] | None = None) -> OpenCodeAdapter:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(server.handler),
        base_url="http://dev-broker:8701/relay/job",
        headers={SECRET_HEADER: "s3cret", "Authorization": "Basic x"},
    )

    async def record(step: str) -> None:
        if progress is not None:
            progress.append(step)

    return OpenCodeAdapter(client, model_id=_MODEL, on_progress=record, poll_seconds=0.01)


class TestSuccess:
    async def test_a_recorded_run_ends_with_success_and_its_last_text(self) -> None:
        server = FakeServer([_FIXTURE])
        progress: list[str] = []
        outcome = await _adapter(server, progress).delegate(_package(), 30, _trace())
        assert outcome.success is True
        assert outcome.what_worked == "All done."
        assert outcome.rounds_needed == 2
        assert progress == ["step 1", "tool bash", "step 2"]

    async def test_it_subscribes_first_then_creates_the_session_with_the_jobs_model(self) -> None:
        server = FakeServer([_FIXTURE])
        package = _package()
        await _adapter(server).delegate(package, 30, _trace())
        paths = [(r.method, r.url.path.removeprefix("/relay/job")) for r in server.requests]
        first_event = paths.index(("GET", "/api/event"))
        assert first_event < paths.index(("POST", "/api/session"))
        created = json.loads(server.posted("/api/session")[0].content)
        assert created["model"] == {"id": _MODEL, "providerID": "seshat"}
        prompt = json.loads(server.posted("/prompt")[0].content)
        assert prompt == {"text": package.task_description}

    async def test_it_satisfies_the_protocol(self) -> None:
        adapter: DelegationExecutorProtocol = _adapter(FakeServer([_FIXTURE]))
        assert adapter.available() is True


class TestEndings:
    async def test_a_failed_run_is_not_a_success(self) -> None:
        failed = _FIXTURE.replace("session.execution.succeeded", "session.execution.failed")
        outcome = await _adapter(FakeServer([failed])).delegate(_package(), 30, _trace())
        assert (outcome.success, outcome.what_was_missing) == (False, "failed")

    async def test_another_sessions_end_event_is_ignored(self) -> None:
        other = (
            'data: {"id":"evt_x","type":"session.execution.failed",'
            '"data":{"sessionID":"ses_other"}}\n\n'
        )
        outcome = await _adapter(FakeServer([other + _FIXTURE])).delegate(_package(), 30, _trace())
        assert outcome.success is True

    async def test_at_the_deadline_the_run_is_interrupted(self) -> None:
        server = FakeServer([None])
        outcome = await _adapter(server).delegate(_package(), 0.5, _trace())
        assert (outcome.success, outcome.what_was_missing) == (False, "timed_out")
        assert len(server.posted(f"/api/session/{_SESSION}/interrupt")) == 1

    async def test_a_run_that_ends_while_the_stream_is_down_is_reported(self) -> None:
        cut = _FIXTURE[: _FIXTURE.index("session.execution.succeeded")].rsplit("data:", 1)[0]
        server = FakeServer([cut, _FIXTURE], active={})
        outcome = await _adapter(server).delegate(_package(), 30, _trace())
        assert (outcome.success, outcome.what_was_missing) == (False, "end_event_missed")

    async def test_a_dropped_stream_of_a_running_session_is_followed_again(self) -> None:
        cut = _FIXTURE[: _FIXTURE.index("session.execution.succeeded")].rsplit("data:", 1)[0]
        end = (
            'data: {"id":"evt_y","type":"session.execution.succeeded",'
            f'"data":{{"sessionID":"{_SESSION}"}}}}\n\n'
        )
        server = FakeServer([cut, end], active={_SESSION: {"type": "running"}})
        outcome = await _adapter(server).delegate(_package(), 30, _trace())
        assert outcome.success is True

    async def test_a_session_id_that_could_leave_the_relay_path_is_refused(self) -> None:
        """Security review: the id comes from the untrusted container and goes into a path."""
        server = FakeServer([_FIXTURE])
        original = server.handler

        async def evil(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/api/session"):
                server.requests.append(request)
                return httpx.Response(200, json={"data": {"id": "../../other-job/api/x"}})
            return await original(request)

        server.handler = evil  # type: ignore[method-assign]
        outcome = await _adapter(server).delegate(_package(), 30, _trace())
        assert outcome.success is False
        assert "session id" in outcome.what_was_missing
        assert server.posted("/prompt") == []
        assert all("other-job" not in r.url.path for r in server.requests)

    async def test_an_unreachable_server_never_raises(self) -> None:
        async def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(down), base_url="http://dev-broker:8701"
        )
        adapter = OpenCodeAdapter(
            client, model_id=_MODEL, ready_timeout_seconds=0.05, poll_seconds=0.01
        )
        outcome = await adapter.delegate(_package(), 30, _trace())
        assert outcome.success is False
        assert outcome.what_was_missing == "server_not_ready"


class TestClient:
    async def test_the_client_targets_the_jobs_relay_with_both_credentials(self) -> None:
        job = uuid4()
        client = opencode_client(
            control_url="http://dev-broker:8701",
            job_id=job,
            control_secret="s3cret",
            server_password="pw",
        )
        try:
            assert str(client.base_url) == f"http://dev-broker:8701/relay/{job}/"
            assert client.headers[SECRET_HEADER] == "s3cret"
            assert (
                client.headers["Authorization"]
                == "Basic " + base64.b64encode(b"opencode:pw").decode()
            )
            with pytest.raises(Exception, match="(?i)blocked|denied|not allowed|refus"):
                await client.get("http://caddy:8601/x")
        finally:
            await client.aclose()
