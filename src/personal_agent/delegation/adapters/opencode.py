"""The OpenCode adapter: drives ``opencode serve`` in a job's agent container (ADR-0159 D2.2, D2.3).

The gateway is not on ``dev-agent``, so every request goes through the broker's control relay
(``<control_url>/relay/<job_id>/...``, ADR-0159 D4.3). The relay takes its target from the job id
alone and forwards the ``Authorization`` header, which carries the job's OpenCode server password.

The adapter follows the OpenCode 2.2.5 HTTP API, observed on 2026-11-07 in a no-network container:

- ``GET /api/event`` is a server-sent event stream. Each ``data:`` line is ``{id, type, data}``.
  Events of a session carry ``data.sessionID``.
- ``POST /api/session`` with ``{title, model: {id, providerID}}`` creates the session.
- ``POST /api/session/{id}/prompt`` with ``{text}`` starts the work.
- A run ends with ``session.execution.succeeded``, ``.failed`` or ``.interrupted``.
- ``POST /api/session/{id}/interrupt`` stops a run. ``GET /api/session/active`` lists running
  sessions.

The adapter subscribes to the stream before it creates the session, so it misses no event.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import UUID

import httpx
import structlog

from personal_agent.delegation.protocols import DelegationOutcome, DelegationPackage
from personal_agent.dev_broker.relay import SECRET_HEADER
from personal_agent.security import DomainGuard, GuardMode, create_guarded_http_client
from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)

#: The provider id of the broker route in the agent image's ``opencode.json``.
PROVIDER_ID = "seshat"
#: The end events of a run, and whether each one is a success.
END_EVENTS: dict[str, bool] = {
    "session.execution.succeeded": True,
    "session.execution.failed": False,
    "session.execution.interrupted": False,
}

ProgressCallback = Callable[[str], Awaitable[None]]


def opencode_client(
    *, control_url: str, job_id: UUID, control_secret: str, server_password: str
) -> httpx.AsyncClient:
    """Build the client that reaches one job's OpenCode server through the relay (ADR-0132 D2).

    Its egress guard allows the broker's control host only. It ignores proxy variables and never
    follows a redirect. The read timeout is unlimited because the event stream is long-lived: the
    adapter bounds every call with its own deadline.

    Args:
        control_url: The broker's control API, for example ``http://dev-broker:8701``.
        job_id: The job. The relay forwards to this job's container only.
        control_secret: The relay's secret.
        server_password: The job's OpenCode server password (Basic auth, user ``opencode``).

    Returns:
        The client. The caller closes it.
    """
    host = urlparse(control_url).hostname or ""
    basic = base64.b64encode(f"opencode:{server_password}".encode()).decode()
    return create_guarded_http_client(
        guard=DomainGuard(mode=GuardMode.ALLOWLIST, allowlist=frozenset({host})),
        base_url=f"{control_url.rstrip('/')}/relay/{job_id}",
        headers={SECRET_HEADER: control_secret, "Authorization": f"Basic {basic}"},
        timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
        trust_env=False,
        follow_redirects=False,
    )


@dataclass
class _Run:
    """What the adapter saw of one run."""

    session_id: str | None = None
    steps: int = 0
    last_text: str = ""
    ended: bool | None = None


class _StreamEnded(Exception):
    """The event stream closed before the run ended."""


class _NotReady(Exception):
    """The OpenCode server did not answer within the readiness time."""


class OpenCodeAdapter:
    """Implements ``DelegationExecutorProtocol`` for OpenCode 2.x (ADR-0159 D2.2)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        model_id: str,
        on_progress: ProgressCallback | None = None,
        ready_timeout_seconds: float = 90.0,
        poll_seconds: float = 1.0,
        reconnects: int = 3,
    ) -> None:
        """Create the adapter for one job.

        Args:
            client: The job's client (:func:`opencode_client`).
            model_id: The catalog key of the job's model (a model of ``opencode.json``).
            on_progress: Called with a short step description, for example ``tool shell``.
            ready_timeout_seconds: How long to wait for the server after the container start.
            poll_seconds: The interval of the readiness and activity polls.
            reconnects: How often a dropped event stream is reopened.
        """
        self._client = client
        self._model_id = model_id
        self._on_progress = on_progress
        self._ready_timeout = ready_timeout_seconds
        self._poll = poll_seconds
        self._reconnects = reconnects

    def available(self) -> bool:
        """Return whether the client carries the relay secret. No I/O.

        Returns:
            ``True`` when the relay secret header is set.
        """
        return bool(self._client.headers.get(SECRET_HEADER))

    async def delegate(
        self, package: DelegationPackage, timeout: float, trace_ctx: TraceContext
    ) -> DelegationOutcome:
        """Run the package's task as one OpenCode session, until it ends or ``timeout`` passes.

        Args:
            package: The task. ``task_description`` is the prompt.
            timeout: The most seconds to wait. At the deadline the run is interrupted.
            trace_ctx: The job's trace.

        Returns:
            The outcome. ``what_was_missing`` names why a run did not succeed: ``timed_out``,
            ``failed``, ``interrupted``, ``end_event_missed``, ``server_not_ready`` or an error type. Never raises.
        """
        started = time.monotonic()
        run = _Run()
        reason = ""
        try:
            async with asyncio.timeout(timeout):
                await self._wait_ready()
                reason = await self._run(package, run, trace_ctx)
        except TimeoutError:
            reason = "timed_out"
            await self._interrupt(run, trace_ctx)
        except _NotReady:
            reason = "server_not_ready"
        except Exception as exc:  # noqa: BLE001 — the protocol never raises; the outcome says why
            reason = f"error: {type(exc).__name__}: {exc}"
            log.warning(
                "opencode_run_error",
                task_id=package.task_id,
                trace_id=trace_ctx.trace_id,
                error_type=type(exc).__name__,
            )
        success = run.ended is True
        log.info(
            "opencode_run_ended",
            task_id=package.task_id,
            trace_id=trace_ctx.trace_id,
            session_id=run.session_id,
            success=success,
            reason=reason,
            steps=run.steps,
        )
        return DelegationOutcome(
            task_id=package.task_id,
            success=success,
            rounds_needed=run.steps,
            what_worked=run.last_text,
            what_was_missing="" if success else reason,
            duration_minutes=(time.monotonic() - started) / 60.0,
        )

    async def _wait_ready(self) -> None:
        deadline = time.monotonic() + self._ready_timeout
        while True:
            try:
                answer = await self._client.get("/api/session/active")
                if answer.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise _NotReady("the OpenCode server did not answer")
            await asyncio.sleep(self._poll)

    async def _run(self, package: DelegationPackage, run: _Run, trace_ctx: TraceContext) -> str:
        for attempt in range(self._reconnects + 1):
            async with self._client.stream("GET", "/api/event") as stream:
                stream.raise_for_status()
                if run.session_id is None:
                    run.session_id = await self._start_session(package)
                    log.info(
                        "opencode_session_started",
                        task_id=package.task_id,
                        trace_id=trace_ctx.trace_id,
                        session_id=run.session_id,
                    )
                elif not await self._is_active(run.session_id):
                    # The run ended while the stream was down. Its end event is lost.
                    return "end_event_missed"
                try:
                    return await self._follow(_events(stream), run)
                except _StreamEnded, httpx.TransportError:
                    log.warning(
                        "opencode_event_stream_dropped",
                        task_id=package.task_id,
                        trace_id=trace_ctx.trace_id,
                        attempt=attempt,
                    )
        raise _StreamEnded("the event stream dropped too often")

    async def _start_session(self, package: DelegationPackage) -> str:
        created = await self._client.post(
            "/api/session",
            json={
                "title": f"job {package.task_id}",
                "model": {"id": self._model_id, "providerID": PROVIDER_ID},
            },
        )
        created.raise_for_status()
        session_id = str(created.json()["data"]["id"])
        prompted = await self._client.post(
            f"/api/session/{session_id}/prompt", json={"text": package.task_description}
        )
        prompted.raise_for_status()
        return session_id

    async def _is_active(self, session_id: str) -> bool:
        answer = await self._client.get("/api/session/active")
        answer.raise_for_status()
        return session_id in answer.json().get("data", {})

    async def _follow(self, events: AsyncIterator[dict[str, object]], run: _Run) -> str:
        async for event in events:
            kind = str(event.get("type", ""))
            data = event.get("data") or {}
            if not isinstance(data, dict) or data.get("sessionID") != run.session_id:
                continue
            if kind == "session.step.started":
                run.steps += 1
                await self._progress(f"step {run.steps}")
            elif kind == "session.tool.input.started":
                await self._progress(f"tool {data.get('name', '?')}")
            elif kind == "session.text.ended":
                run.last_text = str(data.get("text", ""))
            elif kind in END_EVENTS:
                run.ended = END_EVENTS[kind]
                return "" if run.ended else kind.rsplit(".", 1)[1]
        raise _StreamEnded("the event stream closed")

    async def _progress(self, step: str) -> None:
        if self._on_progress is not None:
            await self._on_progress(step)

    async def _interrupt(self, run: _Run, trace_ctx: TraceContext) -> None:
        if run.session_id is None:
            return
        try:
            await asyncio.wait_for(
                self._client.post(f"/api/session/{run.session_id}/interrupt"), timeout=10
            )
        except (TimeoutError, httpx.HTTPError) as exc:
            log.warning(
                "opencode_interrupt_failed",
                session_id=run.session_id,
                trace_id=trace_ctx.trace_id,
                error_type=type(exc).__name__,
            )


async def _events(stream: httpx.Response) -> AsyncIterator[dict[str, object]]:
    """Yield the JSON events of a server-sent event stream. Comments and other fields are skipped."""
    async for line in stream.aiter_lines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:])
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event
