"""Every ask reaches the user and runs only on the user's yes (FRE-1574, ADR-0156 D8).

The ADR-0156 AC-5 tests, for the primary and for a worker (FRE-1574 AC-3), and failing
closed (FRE-1574 AC-4):

- a call that matches no rule shows the card with the exact arguments (and the worker), runs
  on "allow once", and is refused on deny;
- with no approval surface (UI flag off, no session id, no transport, no PWA client) the call
  is denied with an error that names the refused call, never ``approval_connection_lost``;
- a rules file that fails to load, or an evaluation that raises, denies;
- "allow for this session" on an unmatched call lets the same call run without a card;
- a call that matches an explicit ask rule offers no session option, and a session rule that
  equals the call does not stop the card.

Seeded negative for AC-4: restoring ADR-0063 Amendment A row A3 (a call that needs approval
runs when ``approval_ui_enabled`` is false) makes ``test_ui_disabled_denies`` fail, because the
recorder would hold the call.

The injected ``approve`` callable (ADR-0153 D7, FRE-1535) keeps its own tests below.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance import permissions
from personal_agent.governance.models import Mode, ToolPolicy
from personal_agent.governance.session_rules import add_session_rule, clear_session_rules
from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call
from personal_agent.telemetry import TraceContext
from personal_agent.tools.executor import ToolExecutionLayer
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolDefinition, ToolParameter
from personal_agent.transport.agui.ws_endpoint import ApprovalDecision
from tests._helpers.permissions import use_rules

SESSION = "sess-1574"
_ALL_MODES = ["NORMAL", "ALERT", "DEGRADED"]
_WORKERS = [None, "general"]


# ── helpers ──────────────────────────────────────────────────────────────────


def _settings(*, ui_enabled: bool) -> MagicMock:
    settings = MagicMock()
    settings.approval_ui_enabled = ui_enabled
    settings.approval_timeout_seconds = 60.0
    return settings


def _transport(*decisions: str) -> MagicMock:
    transport = MagicMock()
    transport.request_tool_approval = AsyncMock(
        side_effect=[ApprovalDecision(decision=d) for d in (decisions or ("approve",))]  # type: ignore[arg-type]
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


def _governance(name: str = "probe") -> Any:
    config = copy.deepcopy(load_governance_config())
    config.tools[name] = ToolPolicy(allowed_in_modes=_ALL_MODES)
    return config


def _layer(
    executor: Callable[..., Any],
    *,
    transport: MagicMock | None = None,
    mode: Mode = Mode.NORMAL,
    params: tuple[str, ...] = ("x",),
) -> ToolExecutionLayer:
    registry = ToolRegistry()
    registry.register(_tool_def(params=params), executor)
    mode_manager = MagicMock()
    mode_manager.get_current_mode.return_value = mode
    return ToolExecutionLayer(
        registry=registry,
        governance_config=_governance(),
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


@pytest.fixture(autouse=True)
def _fresh_session_rules() -> Iterator[None]:
    clear_session_rules()
    yield
    clear_session_rules()


async def _call(
    layer: ToolExecutionLayer,
    worker: str | None,
    arguments: dict[str, Any] | None = None,
    *,
    session_id: str | None = SESSION,
) -> dict[str, Any]:
    """One call through the shared dispatch boundary, as the primary or as a worker."""
    return await dispatch_tool_call(
        tool_call_id="c1",
        tool_name="probe",
        arguments=arguments if arguments is not None else {"x": "1"},
        tool_layer=layer,
        trace_ctx=TraceContext.new_trace(),
        trace_id="t",
        session_id=session_id,
        loaded_skills=set(),
        principal="sub_agent" if worker else "primary",
        worker=worker,
    )


def _model_text(result: dict[str, Any]) -> str:
    return str(result["content"])


# ── AC-3: the card and its answers, for the primary and a worker ──────────────


@pytest.mark.usefixtures("no_phase_events")
@pytest.mark.parametrize("worker", _WORKERS)
class TestTheCard:
    @pytest.mark.asyncio
    async def test_unmatched_call_shows_the_card_and_runs_on_allow_once(
        self, worker: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch)
        recorder, transport = _Recorder(), _transport("approve")
        layer = _layer(recorder.run, transport=transport)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await _call(layer, worker, {"x": "exact arg"})

        assert result["success"] is True
        assert recorder.calls == [{"x": "exact arg"}]
        kwargs = transport.request_tool_approval.await_args.kwargs
        assert kwargs["tool"] == "probe"
        assert kwargs["args"] == {"x": "exact arg"}
        assert kwargs["worker"] == worker
        assert kwargs["session_option"] is True
        assert kwargs["session_id"] == SESSION

    @pytest.mark.asyncio
    async def test_deny_refuses_and_names_the_call(
        self, worker: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch)
        recorder = _Recorder()
        layer = _layer(recorder.run, transport=_transport("deny"))
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await _call(layer, worker, {"x": "exact arg"})

        assert result["success"] is False
        assert recorder.calls == []
        error = json.loads(result["content"])["error"]
        assert error["code"] == "denied_by_user"
        assert "exact arg" in error["message"]

    @pytest.mark.asyncio
    async def test_each_call_asks_again(
        self, worker: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T8 AC-4: two calls that match the same ask rule each show a card."""
        use_rules(monkeypatch, ask=["probe"])
        recorder, transport = _Recorder(), _transport("approve", "approve")
        layer = _layer(recorder.run, transport=transport)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            await _call(layer, worker, {"x": "1"})
            await _call(layer, worker, {"x": "2"})

        assert transport.request_tool_approval.await_count == 2
        assert recorder.calls == [{"x": "1"}, {"x": "2"}]

    @pytest.mark.asyncio
    async def test_allow_for_session_lets_the_same_call_run_without_a_card(
        self, worker: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch)
        recorder, transport = _Recorder(), _transport("approve_session", "approve")
        layer = _layer(recorder.run, transport=transport)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            first = await _call(layer, worker, {"x": "1"})
            again = await _call(layer, worker, {"x": "1"})
            other = await _call(layer, worker, {"x": "2"})

        assert (first["success"], again["success"], other["success"]) == (True, True, True)
        # The repeat ran without a card; the different call asked again.
        assert transport.request_tool_approval.await_count == 2
        assert transport.request_tool_approval.await_args.kwargs["args"] == {"x": "2"}

    @pytest.mark.asyncio
    async def test_explicit_ask_offers_no_session_option_and_is_never_overridden(
        self, worker: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch, ask=['probe(x="1")'])
        recorder, transport = _Recorder(), _transport("approve_session", "approve")
        layer = _layer(recorder.run, transport=transport)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            await _call(layer, worker, {"x": "1"})
            assert transport.request_tool_approval.await_args.kwargs["session_option"] is False
            # A session rule that equals the call does not stand in front of the ask rule.
            add_session_rule(SESSION, "probe", {"x": "1"})
            await _call(layer, worker, {"x": "1"})

        assert transport.request_tool_approval.await_count == 2


# ── AC-4: no approval surface denies; the rules fail closed ─────────────────────


@pytest.mark.usefixtures("no_phase_events")
@pytest.mark.parametrize("worker", _WORKERS)
class TestNoSurfaceDenies:
    @pytest.mark.asyncio
    async def test_ui_disabled_denies(
        self, worker: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch)
        recorder, transport = _Recorder(), _transport()
        layer = _layer(recorder.run, transport=transport)
        with (
            patch("personal_agent.tools.executor.settings", _settings(ui_enabled=False)),
            patch("personal_agent.tools.executor.log") as log,
        ):
            result = await _call(layer, worker, {"x": "the refused call"})

        assert recorder.calls == []
        transport.request_tool_approval.assert_not_awaited()
        assert "approval_ui_disabled_denied" in _event_names(log)
        text = _model_text(result)
        assert "the refused call" in text
        assert "approval_connection_lost" not in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("session_id", "transport", "event"),
        [
            (None, "transport", "approval_denied_no_session_id"),
            (SESSION, None, "approval_denied_no_transport"),
        ],
    )
    async def test_no_session_or_no_transport_denies(
        self,
        worker: str | None,
        session_id: str | None,
        transport: str | None,
        event: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        use_rules(monkeypatch)
        recorder = _Recorder()
        layer = _layer(recorder.run, transport=_transport() if transport else None)
        with (
            patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)),
            patch("personal_agent.tools.executor.log") as log,
        ):
            result = await _call(layer, worker, {"x": "q"}, session_id=session_id)

        assert recorder.calls == []
        assert event in _event_names(log)
        assert json.loads(result["content"])["error"]["code"] == "approval_unavailable"

    @pytest.mark.asyncio
    async def test_no_pwa_client_denies_without_connection_lost_text(
        self, worker: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch)
        recorder = _Recorder()
        layer = _layer(recorder.run, transport=_transport("connection_lost"))
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await _call(layer, worker, {"x": "q"})

        assert recorder.calls == []
        text = _model_text(result)
        assert "no PWA client" in text
        assert "approval_connection_lost" not in text

    @pytest.mark.asyncio
    async def test_rules_that_fail_to_load_deny(
        self, worker: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken = tmp_path / "permissions.yaml"
        broken.write_text("allow:\n  - rule: bash(env *)\n")
        monkeypatch.setattr(permissions, "rules_file_paths", lambda: [broken])
        recorder, transport = _Recorder(), _transport()
        layer = _layer(recorder.run, transport=transport)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await _call(layer, worker, {"x": "the refused call"})

        assert recorder.calls == []
        transport.request_tool_approval.assert_not_awaited()
        error = json.loads(result["content"])["error"]
        assert error["code"] == "permission_rules_unavailable"
        assert "the refused call" in error["message"]

    @pytest.mark.asyncio
    async def test_an_evaluation_error_denies(
        self, worker: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        use_rules(monkeypatch, allow=["probe"])
        monkeypatch.setattr(permissions, "evaluate_permission", boom)
        recorder = _Recorder()
        layer = _layer(recorder.run, transport=_transport())
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await _call(layer, worker)

        assert result["success"] is False
        assert recorder.calls == []

    @pytest.mark.asyncio
    async def test_a_deny_rule_refuses_even_with_a_card_available(
        self, worker: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch, deny=["probe(x=bad)"], allow=["probe"])
        recorder, transport = _Recorder(), _transport()
        layer = _layer(recorder.run, transport=transport)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await _call(layer, worker, {"x": "bad"})

        assert recorder.calls == []
        transport.request_tool_approval.assert_not_awaited()
        error = json.loads(result["content"])["error"]
        assert error["code"] == "permission_denied"
        assert "probe(x=bad)" in error["message"]


# ── wiring ───────────────────────────────────────────────────────────────────


class TestTransportWiring:
    def test_the_primary_layer_has_a_transport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import personal_agent.orchestrator.executor as ex
        from personal_agent.transport.agui.transport import AGUITransport

        monkeypatch.setattr(ex, "_tool_execution_layer", None)

        assert isinstance(ex._get_tool_execution_layer().transport, AGUITransport)

    def test_the_shared_worker_layer_has_one_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ADR-0156 D8.3: a worker's ask reaches the same card."""
        import personal_agent.orchestrator.tool_dispatch as td
        from personal_agent.transport.agui.transport import AGUITransport

        monkeypatch.setattr(td, "_tool_execution_layer", None)

        assert isinstance(td.get_shared_tool_execution_layer().transport, AGUITransport)


# ── AC-3: the injected ``approve`` callable ──────────────────────────────────


@pytest.mark.usefixtures("no_phase_events")
class TestApproveCallable:
    @pytest.fixture(autouse=True)
    def _probe_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_rules(monkeypatch, allow=["probe"])

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

        layer = _layer(executor, transport=transport)
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

        layer = _layer(executor, transport=mock_transport)
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
        layer = _layer(recorder.run, transport=_transport())
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
        layer = _layer(executor, transport=_transport(), params=("x", "approve"))
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
        layer = _layer(recorder.run, transport=_transport(), params=("x", "approve"))
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

        layer = _layer(executor, transport=_transport())
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            result = await layer.execute_tool(
                "probe", {"x": "1"}, TraceContext.new_trace(), session_id=SESSION
            )

        assert result.success is False
        assert ran == []


# ── AC-1: the real transport, the real bash tool ─────────────────────────────


class TestRealTransportBlocksBash:
    """``bash`` outside the allow rules pushes one request and waits for the decision.

    Runs on the production rules: ``touch`` has no allow rule, so it asks.
    """

    @staticmethod
    async def _run(
        tmp_path: Path, decision: str, worker: str | None = None
    ) -> tuple[bool, bool, list[Any]]:
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
                        worker=worker,
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
    @pytest.mark.parametrize("worker", _WORKERS)
    async def test_deny_leaves_the_command_unrun(self, tmp_path: Path, worker: str | None) -> None:
        ran_before, ran_after, (request, result) = await self._run(tmp_path, "deny", worker)

        assert request.tool == "bash"
        assert request.worker == worker
        assert ran_before is False
        assert ran_after is False
        assert result.success is False
        assert "approval_denied_by_user" in (result.error or "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("worker", _WORKERS)
    async def test_approve_runs_the_command_only_after_the_decision(
        self, tmp_path: Path, worker: str | None
    ) -> None:
        ran_before, ran_after, (request, result) = await self._run(tmp_path, "approve", worker)

        assert request.tool == "bash"
        assert request.args == {"command": f"touch {tmp_path / 'marker'}"}
        assert request.worker == worker
        assert request.session_option is True
        assert ran_before is False
        assert ran_after is True
        assert result.success is True


@pytest.mark.usefixtures("no_phase_events")
class TestOneCardAtATime:
    @pytest.mark.asyncio
    async def test_concurrent_asks_in_one_session_reach_the_card_in_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The PWA shows one card; a second concurrent ask waits for the first answer."""
        use_rules(monkeypatch)
        open_cards = 0
        most_open = 0

        async def answer(**_kw: Any) -> ApprovalDecision:
            nonlocal open_cards, most_open
            open_cards += 1
            most_open = max(most_open, open_cards)
            await asyncio.sleep(0.05)
            open_cards -= 1
            return ApprovalDecision(decision="approve")

        transport = MagicMock()
        transport.request_tool_approval = AsyncMock(side_effect=answer)
        recorder = _Recorder()
        layer = _layer(recorder.run, transport=transport)
        with patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)):
            await asyncio.gather(
                _call(layer, None, {"x": "1"}), _call(layer, "general", {"x": "2"})
            )

        assert transport.request_tool_approval.await_count == 2
        assert most_open == 1
        assert sorted(c["x"] for c in recorder.calls) == ["1", "2"]


# ── FRE-1676 AC-3: a refused call logs its tool name and argument length ──────


@pytest.mark.usefixtures("no_phase_events")
class TestRefusedCallLogsItsSize:
    @pytest.mark.asyncio
    async def test_a_refused_call_logs_tool_name_and_argument_length_but_not_the_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch)
        recorder = _Recorder()
        layer = _layer(recorder.run, transport=None)
        arguments = {"x": "SECRET-BODY-" + "y" * 9000}
        with (
            patch("personal_agent.tools.executor.settings", _settings(ui_enabled=True)),
            patch("personal_agent.tools.executor.log") as log,
        ):
            result = await _call(layer, None, arguments)

        assert result["success"] is False
        assert recorder.calls == []
        violations = [
            c.kwargs
            for c in log.warning.call_args_list
            if c.args and c.args[0] == "policy_violation"
        ]
        assert len(violations) == 1
        assert violations[0]["tool_name"] == "probe"
        assert violations[0]["argument_length"] == len(json.dumps(arguments))
        assert all("SECRET-BODY" not in repr(v) for v in violations[0].values())
