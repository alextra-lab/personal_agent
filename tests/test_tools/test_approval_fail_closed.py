"""Tests for the fail-closed tool-approval gate (FRE-1535, ADR-0063 Amendment A).

Before FRE-1535 an approval-required tool ran without a prompt whenever the session id
was empty, the UI flag was off, or no transport was attached. No ``ToolExecutionLayer``
was built with a transport, so on the live gateway every call ran unprompted.

AC-1: a real approval request is sent and the command does not run before the decision.
AC-2: each path with no transport has a recorded outcome and a log event with the real cause.
AC-3: the injected ``approve`` callable works with a transport and returns ``deny`` without.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode, ToolPolicy
from personal_agent.telemetry import TraceContext
from personal_agent.tools.executor import ToolExecutionLayer, _check_permissions
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolDefinition, ToolParameter
from personal_agent.transport.agui.ws_endpoint import ApprovalDecision
from tests._helpers.trace import make_test_ctx

SESSION = "sess-1535"
_ALL_MODES = ["NORMAL", "ALERT", "DEGRADED"]


# ── helpers ──────────────────────────────────────────────────────────────────


def _settings(*, ui_enabled: bool) -> MagicMock:
    settings = MagicMock()
    settings.approval_ui_enabled = ui_enabled
    settings.approval_timeout_seconds = 60.0
    return settings


def _transport(decision: str = "approve") -> MagicMock:
    transport = MagicMock()
    transport.request_tool_approval = AsyncMock(
        return_value=ApprovalDecision(decision=decision)  # type: ignore[arg-type]
    )
    return transport


def _event_names(mock_log: MagicMock) -> list[str]:
    calls = [*mock_log.warning.call_args_list, *mock_log.info.call_args_list]
    return [c.args[0] for c in calls if c.args]


def _tool_def(name: str = "probe", *, params: tuple[str, ...] = ("x",)) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="probe tool",
        category="system_dangerous",
        parameters=[
            ToolParameter(name=p, type="string", description=p, required=False) for p in params
        ],
        risk_level="high",
        allowed_modes=_ALL_MODES,
    )


def _governance(
    *, requires_approval: bool = True, in_modes: list[str] | None = None, name: str = "probe"
) -> Any:
    config = copy.deepcopy(load_governance_config())
    config.tools[name] = ToolPolicy(
        category="system_dangerous",
        allowed_in_modes=_ALL_MODES,
        requires_approval=requires_approval,
        requires_approval_in_modes=in_modes or [],
    )
    return config


def _layer(
    executor: Callable[..., Any],
    *,
    transport: MagicMock | None = None,
    mode: Mode = Mode.NORMAL,
    requires_approval: bool = True,
    in_modes: list[str] | None = None,
    params: tuple[str, ...] = ("x",),
) -> ToolExecutionLayer:
    registry = ToolRegistry()
    registry.register(_tool_def(params=params), executor)
    mode_manager = MagicMock()
    mode_manager.get_current_mode.return_value = mode
    return ToolExecutionLayer(
        registry=registry,
        governance_config=_governance(requires_approval=requires_approval, in_modes=in_modes),
        mode_manager=mode_manager,
        transport=transport,  # type: ignore[arg-type]
    )


class _Recorder:
    """Executor stand-in that records whether and how it ran."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, x: str = "") -> dict[str, Any]:
        self.calls.append({"x": x})
        return {"ran": True}


@pytest.fixture
def no_phase_events() -> Iterator[None]:
    """Replace ``phase_span`` with a no-op so unit tests do not touch Postgres."""

    @asynccontextmanager
    async def _noop(**_kw: Any) -> AsyncIterator[None]:
        yield None

    with patch("personal_agent.transport.agui.transport.phase_span", _noop):
        yield


async def _gate(
    *,
    ui_enabled: bool,
    session_id: str | None,
    transport: MagicMock | None,
    mode: Mode = Mode.NORMAL,
) -> tuple[Any, MagicMock]:
    config = _governance()
    with (
        patch("personal_agent.tools.executor.settings", _settings(ui_enabled=ui_enabled)),
        patch("personal_agent.tools.executor.log") as mock_log,
    ):
        result = await _check_permissions(
            "probe",
            _tool_def(),
            {"x": "1"},
            mode,
            config,
            transport=transport,
            session_id=session_id,
            trace_ctx=make_test_ctx("fre1535"),
        )
    return result, mock_log


# ── AC-2: the policy table ───────────────────────────────────────────────────


@pytest.mark.usefixtures("no_phase_events")
class TestPolicyTable:
    @pytest.mark.asyncio
    async def test_flag_on_no_session_id_is_denied(self) -> None:
        result, log = await _gate(ui_enabled=True, session_id=None, transport=_transport())

        assert result.allowed is False
        assert "approval_denied_no_session_id" in _event_names(log)
        assert "approval_ui_disabled_proceeding" not in _event_names(log)

    @pytest.mark.asyncio
    async def test_flag_on_no_transport_is_denied(self) -> None:
        """A CLI turn, a harness or a probe has a session but no transport."""
        result, log = await _gate(ui_enabled=True, session_id=SESSION, transport=None)

        assert result.allowed is False
        assert "approval_denied_no_transport" in _event_names(log)
        assert "approval_ui_disabled_proceeding" not in _event_names(log)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("session_id", [SESSION, None])
    async def test_flag_off_is_an_explicit_opt_out(self, session_id: str | None) -> None:
        result, log = await _gate(ui_enabled=False, session_id=session_id, transport=None)

        assert result.allowed is True
        assert "approval_ui_disabled_proceeding" in _event_names(log)

    @pytest.mark.asyncio
    async def test_approve_allows(self) -> None:
        transport = _transport("approve")
        result, _ = await _gate(ui_enabled=True, session_id=SESSION, transport=transport)

        assert result.allowed is True
        transport.request_tool_approval.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("decision", ["deny", "timeout", "connection_lost"])
    async def test_every_other_decision_denies_with_its_real_cause(self, decision: str) -> None:
        """``connection_lost`` is the no-PWA-client case: the session has no WebSocket."""
        result, log = await _gate(
            ui_enabled=True, session_id=SESSION, transport=_transport(decision)
        )

        assert result.allowed is False
        assert result.reason == f"approval_{decision}"
        assert "approval_denied" in _event_names(log)
        assert "approval_ui_disabled_proceeding" not in _event_names(log)

    @pytest.mark.asyncio
    async def test_request_carries_this_invocations_ids(self) -> None:
        transport = _transport("approve")
        ctx = make_test_ctx("fre1535-ids")
        with (
            patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)),
            patch("personal_agent.tools.executor.log"),
        ):
            await _check_permissions(
                "probe",
                _tool_def(),
                {"x": "1"},
                Mode.NORMAL,
                _governance(),
                transport=transport,
                session_id=SESSION,
                trace_ctx=ctx,
            )

        kwargs = transport.request_tool_approval.await_args.kwargs
        assert kwargs["session_id"] == SESSION
        assert kwargs["trace_id"] == ctx.trace_id
        assert kwargs["tool"] == "probe"


# ── AC-2: sub-agent path ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("no_phase_events")
class TestSubAgentUpstreamApproval:
    @pytest.mark.asyncio
    async def test_a_transportless_layer_denies_by_default(self) -> None:
        recorder = _Recorder()
        layer = _layer(recorder.run, transport=None)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await layer.execute_tool(
                "probe", {"x": "1"}, TraceContext.new_trace(), session_id=SESSION
            )

        assert result.success is False
        assert "approval_no_transport" in (result.error or "")
        assert recorder.calls == []

    @pytest.mark.asyncio
    async def test_a_broker_approved_call_runs_without_a_second_prompt(self) -> None:
        recorder = _Recorder()
        layer = _layer(recorder.run, transport=None)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await layer.execute_tool(
                "probe",
                {"x": "1"},
                TraceContext.new_trace(),
                session_id=SESSION,
                approved_upstream=True,
            )

        assert result.success is True
        assert recorder.calls == [{"x": "1"}]

    @pytest.mark.asyncio
    async def test_mode_drift_after_spawn_reaches_the_generic_gate(self) -> None:
        """The broker set is frozen at spawn; the generic gate reads the mode per call.

        ``probe`` needs no approval in NORMAL, so the broker never gated it. After a
        NORMAL→ALERT change it does. That call arrives with ``approved_upstream=False``
        and must be denied, not waved through.
        """
        recorder = _Recorder()
        in_alert = _layer(
            recorder.run, mode=Mode.ALERT, requires_approval=False, in_modes=["ALERT"], transport=None
        )
        in_normal = _layer(
            _Recorder().run, mode=Mode.NORMAL, requires_approval=False, in_modes=["ALERT"]
        )
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            drifted = await in_alert.execute_tool(
                "probe", {"x": "1"}, TraceContext.new_trace(), session_id=SESSION
            )
            control = await in_normal.execute_tool(
                "probe", {"x": "1"}, TraceContext.new_trace(), session_id=SESSION
            )

        assert drifted.success is False
        assert recorder.calls == []
        assert control.success is True

    @pytest.mark.asyncio
    async def test_dispatch_forwards_the_flag(self) -> None:
        from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call
        from personal_agent.tools.types import ToolResult

        layer = MagicMock()
        layer.registry.get_tool.return_value = None
        layer.execute_tool = AsyncMock(
            return_value=ToolResult(
                tool_name="probe", success=True, output={"ok": 1}, error=None, latency_ms=1.0
            )
        )
        common: dict[str, Any] = {
            "tool_call_id": "c0",
            "tool_name": "probe",
            "arguments": {"x": "1"},
            "tool_layer": layer,
            "trace_ctx": TraceContext.new_trace(),
            "trace_id": "t",
            "session_id": SESSION,
            "loaded_skills": set(),
        }

        await dispatch_tool_call(**common)
        assert layer.execute_tool.await_args.kwargs["approved_upstream"] is False

        await dispatch_tool_call(**common, approved_upstream=True)
        assert layer.execute_tool.await_args.kwargs["approved_upstream"] is True


# ── wiring ───────────────────────────────────────────────────────────────────


class TestTransportWiring:
    def test_the_primary_layer_has_a_transport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import personal_agent.orchestrator.executor as ex
        from personal_agent.transport.agui.transport import AGUITransport

        monkeypatch.setattr(ex, "_tool_execution_layer", None)

        assert isinstance(ex._get_tool_execution_layer().transport, AGUITransport)

    def test_the_shared_sub_agent_layer_has_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import personal_agent.orchestrator.tool_dispatch as td

        monkeypatch.setattr(td, "_tool_execution_layer", None)

        assert td.get_shared_tool_execution_layer().transport is None


# ── AC-3: the injected ``approve`` callable ──────────────────────────────────


@pytest.mark.usefixtures("no_phase_events")
class TestApproveCallable:
    @staticmethod
    def _capture() -> tuple[list[Any], Callable[..., Any]]:
        seen: list[Any] = []

        async def executor(x: str = "", approve: Any = None) -> dict[str, Any]:
            seen.append(approve)
            return {"ran": True}

        return seen, executor

    @pytest.mark.asyncio
    async def test_with_a_transport_it_sends_one_request_and_returns_the_decision(self) -> None:
        transport = _transport("approve")
        decisions: list[ApprovalDecision] = []

        async def executor(x: str = "", approve: Any = None) -> dict[str, Any]:
            decisions.append(await approve(args={"title": "T", "email": "a@b.c"}, reason="share"))
            return {}

        layer = _layer(executor, transport=transport, requires_approval=False)
        ctx = TraceContext.new_trace()
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await layer.execute_tool("probe", {"x": "1"}, ctx, session_id=SESSION)

        assert result.success is True
        assert decisions[0].decision == "approve"
        transport.request_tool_approval.assert_awaited_once()
        kwargs = transport.request_tool_approval.await_args.kwargs
        assert kwargs["session_id"] == SESSION
        assert kwargs["trace_id"] == ctx.trace_id
        assert kwargs["tool"] == "probe"
        assert kwargs["args"] == {"title": "T", "email": "a@b.c"}
        assert kwargs["reason"] == "share"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("case", "transport", "session_id", "ui_enabled"),
        [
            ("no_transport", None, SESSION, True),
            ("no_session_id", "transport", None, True),
            ("ui_disabled", "transport", SESSION, False),
        ],
    )
    async def test_without_a_channel_it_returns_deny(
        self, case: str, transport: str | None, session_id: str | None, ui_enabled: bool
    ) -> None:
        mock_transport = _transport("approve") if transport else None
        decisions: list[ApprovalDecision] = []

        async def executor(x: str = "", approve: Any = None) -> dict[str, Any]:
            decisions.append(await approve(args={}, reason="r"))
            return {}

        layer = _layer(executor, transport=mock_transport, requires_approval=False)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=ui_enabled)):
            await layer.execute_tool(
                "probe", {"x": "1"}, TraceContext.new_trace(), session_id=session_id
            )

        assert decisions[0].decision == "deny", case
        if mock_transport is not None:
            mock_transport.request_tool_approval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_executor_that_does_not_declare_it_gets_nothing(self) -> None:
        recorder = _Recorder()
        layer = _layer(recorder.run, transport=_transport(), requires_approval=False)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await layer.execute_tool(
                "probe", {"x": "1"}, TraceContext.new_trace(), session_id=SESSION
            )

        assert result.success is True
        assert recorder.calls == [{"x": "1"}]

    @pytest.mark.asyncio
    async def test_a_model_supplied_approve_never_reaches_the_executor(self) -> None:
        """``approve`` is reserved: even a tool that exposes that parameter name is overwritten."""
        seen, executor = self._capture()
        layer = _layer(
            executor, transport=_transport(), requires_approval=False, params=("x", "approve")
        )
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            await layer.execute_tool(
                "probe",
                {"x": "1", "approve": "yes-I-approve"},
                TraceContext.new_trace(),
                session_id=SESSION,
            )

        assert len(seen) == 1
        assert callable(seen[0])

    @pytest.mark.asyncio
    async def test_a_model_supplied_approve_is_dropped_when_the_executor_does_not_declare_it(
        self,
    ) -> None:
        recorder = _Recorder()
        layer = _layer(
            recorder.run, transport=_transport(), requires_approval=False, params=("x", "approve")
        )
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await layer.execute_tool(
                "probe",
                {"x": "1", "approve": "yes"},
                TraceContext.new_trace(),
                session_id=SESSION,
            )

        assert result.success is True
        assert recorder.calls == [{"x": "1"}]

    @pytest.mark.asyncio
    async def test_a_sync_executor_that_declares_it_is_refused(self) -> None:
        ran: list[bool] = []

        def executor(x: str = "", approve: Any = None) -> dict[str, Any]:
            ran.append(True)
            return {}

        layer = _layer(executor, transport=_transport(), requires_approval=False)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await layer.execute_tool(
                "probe", {"x": "1"}, TraceContext.new_trace(), session_id=SESSION
            )

        assert result.success is False
        assert ran == []


# ── AC-1: the real transport, the real bash tool ─────────────────────────────


class TestRealTransportBlocksBash:
    """``bash`` outside the allowlist pushes one request and waits for the decision."""

    @staticmethod
    async def _run(tmp_path: Path, decision: str) -> tuple[bool, bool, list[Any]]:
        from personal_agent.tools.primitives.bash import bash_executor, bash_tool
        from personal_agent.transport.agui import transport as transport_module
        from personal_agent.transport.agui import ws_endpoint
        from personal_agent.transport.agui.transport import AGUITransport
        from personal_agent.transport.events import ToolApprovalRequestEvent

        sid = f"fre1535-{uuid4()}"
        marker = tmp_path / "marker"
        pushed: list[Any] = []

        async def capture(_session_id: str, make_event: Callable[[], Any]) -> None:
            pushed.append(make_event())

        conn = ws_endpoint._ConnectionState(
            websocket=None,  # type: ignore[arg-type]
            user=MagicMock(),
            session_id=sid,
            outbound_queue=asyncio.Queue(maxsize=100),
        )
        ws_endpoint._active_connections[sid] = conn
        mode_manager = MagicMock()
        mode_manager.get_current_mode.return_value = Mode.NORMAL
        registry = ToolRegistry()
        registry.register(bash_tool, bash_executor)
        layer = ToolExecutionLayer(
            registry=registry,
            governance_config=load_governance_config(),
            mode_manager=mode_manager,
            transport=AGUITransport(),
        )

        task: asyncio.Task[Any] | None = None
        try:
            with (
                patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)),
                patch.object(transport_module, "_persist_and_enqueue", capture),
            ):
                task = asyncio.create_task(
                    layer.execute_tool(
                        "bash",
                        {"command": f"touch {marker}"},
                        TraceContext.new_trace(),
                        session_id=sid,
                    )
                )
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    if any(isinstance(e, ToolApprovalRequestEvent) for e in pushed):
                        break
                requests = [e for e in pushed if isinstance(e, ToolApprovalRequestEvent)]
                assert len(requests) == 1
                ran_before_decision = marker.exists()
                ws_endpoint._resolve_waiter(conn, requests[0].request_id, {"decision": decision})
                result = await asyncio.wait_for(task, timeout=10)
            return ran_before_decision, marker.exists(), [requests[0], result]
        finally:
            if task is not None and not task.done():
                task.cancel()
            ws_endpoint._active_connections.pop(sid, None)

    @pytest.mark.asyncio
    async def test_deny_leaves_the_command_unrun(self, tmp_path: Path) -> None:
        ran_before, ran_after, (request, result) = await self._run(tmp_path, "deny")

        assert request.tool == "bash"
        assert ran_before is False
        assert ran_after is False
        assert result.success is False
        assert "approval_deny" in (result.error or "")

    @pytest.mark.asyncio
    async def test_approve_runs_the_command_only_after_the_decision(self, tmp_path: Path) -> None:
        ran_before, ran_after, (request, result) = await self._run(tmp_path, "approve")

        assert request.tool == "bash"
        assert ran_before is False
        assert ran_after is True
        assert result.success is True
