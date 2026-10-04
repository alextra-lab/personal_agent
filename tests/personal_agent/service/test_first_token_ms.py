"""FRE-1512 AC-2 (ADR-0154 D6) — ``first_token_ms`` is the first user-visible push.

The seam writes the route-trace row before the reply reaches the user, so the service
records ``first_token_ms`` afterwards with one conditional update, then projects the
turn-level ES document once. These tests drive both chat entry points with a stub
orchestrator that delays its reply by a known time and assert the recorded value within
50 ms.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from personal_agent.observability.route_trace.types import RouteTraceRow
from personal_agent.service.app import _process_chat_stream_background, chat
from personal_agent.service.auth import RequestUser

_DELAY_S = 0.2
_TOLERANCE_MS = 50.0
_USER_ID = uuid4()
_REQUEST_USER = RequestUser(user_id=_USER_ID, email="test@example.com")


@pytest.fixture(autouse=True)
def _no_turn_registry_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRE-1543: ``open_turn`` reads Postgres; a unit test of the chat task must not."""
    monkeypatch.setattr(
        "personal_agent.transport.agui.transport.open_turn", AsyncMock(return_value=None)
    )


def _turn_row(trace_id: str, **overrides: object) -> RouteTraceRow:
    base: dict[str, object] = dict(
        trace_id=UUID(trace_id),
        session_id=uuid4(),
        decomposition_strategy="hybrid",
        decomposition_reason="planner_asked",
        orchestration_event="primary_handled",
        gateway_label="conversational/hybrid",
    )
    base.update(overrides)
    return RouteTraceRow(**base)  # type: ignore[arg-type]


class _Recorder:
    """Records the order of push, update and projection calls."""

    def __init__(self, trace_id: str, *, push_result: bool = True, update_raises: bool = False):
        self.calls: list[str] = []
        self.t_push: float | None = None
        self.t_update: float | None = None
        self.trace_id = trace_id
        self.projected: list[tuple[RouteTraceRow, str]] = []
        self.first_token_ms: float | None = None
        row = _turn_row(trace_id)
        ledger = MagicMock()

        async def _set(tid: UUID, ms: float) -> RouteTraceRow | None:
            self.calls.append("update")
            self.t_update = time.monotonic()
            if update_raises:
                raise RuntimeError("db down")
            self.first_token_ms = ms
            return replace(row, first_token_ms=ms)

        async def _get(tid: UUID) -> list[RouteTraceRow]:
            self.calls.append("fetch")
            return [row]

        ledger.set_first_token_ms = AsyncMock(side_effect=_set)
        ledger.get_by_trace_id = AsyncMock(side_effect=_get)
        self.ledger = ledger

        async def _push(*_a: object, **_k: object) -> bool:
            self.calls.append("push")
            self.t_push = time.monotonic()
            return push_result

        self.push = AsyncMock(side_effect=_push)

    def project(self, row: RouteTraceRow, *, topology: str) -> None:
        self.calls.append("project")
        self.projected.append((row, topology))


@asynccontextmanager
async def _fake_db_session(_mock_db: MagicMock):  # type: ignore[no-untyped-def]
    yield _mock_db


def _orchestrator(delay_s: float = _DELAY_S) -> MagicMock:
    session_manager = MagicMock()
    session_manager.get_session.return_value = None
    orchestrator = MagicMock()
    orchestrator.session_manager = session_manager

    async def _handle(**_kwargs: object) -> dict[str, str]:
        await asyncio.sleep(delay_s)  # the stub "stream" delays its first chunk
        return {"reply": "hi there", "trace_id": "t"}

    orchestrator.handle_user_request = AsyncMock(side_effect=_handle)
    return orchestrator


async def _run_stream(rec: _Recorder, *, received: float | None) -> MagicMock:
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    repo = MagicMock()
    repo.get = AsyncMock(return_value=session)
    repo.append_message = AsyncMock(return_value=None)
    orchestrator = _orchestrator()
    emit_done = AsyncMock()
    with (
        patch("personal_agent.service.app._validate_attachments", new=AsyncMock(return_value=[])),
        patch("personal_agent.transport.agui.transport.emit_done", new=emit_done),
        patch("personal_agent.transport.agui.transport._push_event", new=rec.push),
        patch("personal_agent.orchestrator.Orchestrator", return_value=orchestrator),
        patch("personal_agent.service.app.SessionRepository", return_value=repo),
        patch(
            "personal_agent.service.app.AsyncSessionLocal",
            side_effect=lambda: _fake_db_session(MagicMock()),
        ),
        patch("personal_agent.service.app.get_route_trace_ledger", return_value=rec.ledger),
        patch("personal_agent.service.app.project_route_trace_to_es", new=rec.project),
        patch("personal_agent.service.app.run_gateway_pipeline", new=AsyncMock(return_value=None)),
    ):
        kwargs: dict[str, object] = {}
        rec.received = time.monotonic() if received is None else received  # type: ignore[attr-defined]
        kwargs["received_monotonic"] = rec.received  # type: ignore[attr-defined]
        await _process_chat_stream_background(
            session_id=str(session_id),
            message="hello",
            user_id=_USER_ID,
            trace_id=rec.trace_id,
            **kwargs,  # type: ignore[arg-type]
        )
    rec.emit_done = emit_done  # type: ignore[attr-defined]
    rec.orchestrator = orchestrator  # type: ignore[attr-defined]
    return orchestrator


@pytest.mark.asyncio
async def test_stream_records_time_from_receipt_to_the_push() -> None:
    rec = _Recorder(str(uuid4()))

    await _run_stream(rec, received=None)

    assert rec.first_token_ms is not None and rec.t_push is not None
    # At least the stub's delay, and equal to receipt-to-push within the 50 ms tolerance.
    assert rec.first_token_ms >= _DELAY_S * 1000
    assert abs(rec.first_token_ms - (rec.t_push - rec.received) * 1000) <= _TOLERANCE_MS  # type: ignore[attr-defined]
    # push, then the conditional update, then the single projection
    assert rec.calls == ["push", "update", "project"]
    ((row, _topology),) = rec.projected
    assert row.first_token_ms == rec.first_token_ms


@pytest.mark.asyncio
async def test_stream_counts_from_receipt_not_from_the_background_task_start() -> None:
    """Time spent before the task started (auth, selection, session lookup) is in the value."""
    rec = _Recorder(str(uuid4()))
    received = time.monotonic() - 1.0  # the request arrived 1 s before the task ran

    await _run_stream(rec, received=received)

    assert rec.first_token_ms is not None
    assert rec.first_token_ms >= 1000 + _DELAY_S * 1000


@pytest.mark.asyncio
async def test_failed_push_leaves_first_token_unset_and_still_projects_once() -> None:
    rec = _Recorder(str(uuid4()), push_result=False)

    await _run_stream(rec, received=time.monotonic())

    assert rec.first_token_ms is None
    assert rec.calls == ["push", "fetch", "project"]
    ((row, _),) = rec.projected
    assert row.first_token_ms is None


@pytest.mark.asyncio
async def test_ledger_failure_does_not_fail_the_turn_and_the_document_is_still_projected() -> None:
    rec = _Recorder(str(uuid4()), update_raises=True)

    await _run_stream(rec, received=time.monotonic())

    rec.emit_done.assert_awaited_once()  # type: ignore[attr-defined]
    assert rec.calls.count("project") == 1


@pytest.mark.asyncio
async def test_projection_label_is_the_terminal_label_of_a_declined_turn() -> None:
    rec = _Recorder(str(uuid4()))
    rec.ledger.set_first_token_ms = AsyncMock(
        return_value=_turn_row(rec.trace_id, planner_decision="declined", first_token_ms=1.0)
    )

    await _run_stream(rec, received=time.monotonic())

    ((_, topology),) = rec.projected
    assert topology == "primary"  # hybrid + planner_asked + declined


@pytest.mark.asyncio
async def test_orchestrator_gets_the_budget_even_when_the_gateway_pipeline_fails() -> None:
    rec = _Recorder(str(uuid4()))
    with (
        patch("personal_agent.brainstem.sensors.poll_system_metrics", return_value={}),
        patch("personal_agent.brainstem.expansion.compute_expansion_budget", return_value=2),
    ):
        orchestrator = await _run_stream_with_failing_gateway(rec)

    assert orchestrator.handle_user_request.await_args.kwargs["gateway_output"] is None
    assert orchestrator.handle_user_request.await_args.kwargs["expansion_budget"] == 2


async def _run_stream_with_failing_gateway(rec: _Recorder) -> MagicMock:
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    repo = MagicMock()
    repo.get = AsyncMock(return_value=session)
    repo.append_message = AsyncMock(return_value=None)
    orchestrator = _orchestrator(0.0)
    with (
        patch("personal_agent.service.app._validate_attachments", new=AsyncMock(return_value=[])),
        patch("personal_agent.transport.agui.transport.emit_done", new=AsyncMock()),
        patch("personal_agent.transport.agui.transport._push_event", new=rec.push),
        patch("personal_agent.orchestrator.Orchestrator", return_value=orchestrator),
        patch("personal_agent.service.app.SessionRepository", return_value=repo),
        patch(
            "personal_agent.service.app.AsyncSessionLocal",
            side_effect=lambda: _fake_db_session(MagicMock()),
        ),
        patch("personal_agent.service.app.get_route_trace_ledger", return_value=rec.ledger),
        patch("personal_agent.service.app.project_route_trace_to_es", new=rec.project),
        patch(
            "personal_agent.service.app.run_gateway_pipeline",
            new=AsyncMock(side_effect=RuntimeError("pipeline down")),
        ),
    ):
        await _process_chat_stream_background(
            session_id=str(session_id), message="hello", user_id=_USER_ID, trace_id=rec.trace_id
        )
    return orchestrator


@pytest.mark.asyncio
async def test_chat_records_time_from_receipt_to_the_response() -> None:
    trace_id = str(uuid4())
    rec = _Recorder(trace_id)
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    repo = MagicMock()
    repo.get = AsyncMock(return_value=session)
    repo.append_message = AsyncMock(return_value=None)

    from personal_agent.service import app as app_mod

    receipts: list[float] = []
    real_start = app_mod.start_first_token_clock

    def _spy_start(received: float | None = None) -> float:
        value = real_start(received)
        receipts.append(value)
        return value

    with (
        patch("personal_agent.service.app.start_first_token_clock", new=_spy_start),
        patch("personal_agent.service.app.read_or_mint_trace_id", return_value=trace_id),
        patch("personal_agent.service.app.run_gateway_pipeline", new=AsyncMock(return_value=None)),
        patch("personal_agent.orchestrator.Orchestrator", return_value=_orchestrator()),
        patch("personal_agent.service.app.SessionRepository", return_value=repo),
        patch("personal_agent.service.app.get_route_trace_ledger", return_value=rec.ledger),
        patch("personal_agent.service.app.project_route_trace_to_es", new=rec.project),
    ):
        response = await chat(
            message="hello",
            session_id=str(session_id),
            request_user=_REQUEST_USER,
            db=AsyncMock(),
        )

    assert response["response"] == "hi there"
    assert rec.first_token_ms is not None and rec.t_update is not None
    assert rec.first_token_ms >= _DELAY_S * 1000
    assert abs(rec.first_token_ms - (rec.t_update - receipts[0]) * 1000) <= _TOLERANCE_MS
    assert rec.calls == ["update", "project"]
