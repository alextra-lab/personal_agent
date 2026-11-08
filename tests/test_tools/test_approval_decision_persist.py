"""The permission layer persists each card's decision and passes the grant on (ADR-0159 D10.3).

FRE-1687. The change touches the shared permission layer, so these tests also hold that no other
tool's approval changes: ``run_python(network=true)`` still asks on every call with no session
option, a failed write changes no outcome, and a tool that does not declare ``approval`` gets
nothing new.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml  # type: ignore[import-untyped]

from personal_agent.config import load_governance_config, settings
from personal_agent.config.settings import AppConfig
from personal_agent.governance.models import Mode, ToolPolicy
from personal_agent.governance.permissions import decide_permission
from personal_agent.governance.session_rules import clear_session_rules
from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call
from personal_agent.telemetry import TraceContext
from personal_agent.tools.executor import ApprovalGrant, ToolExecutionLayer, approval_args_hash
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolDefinition, ToolParameter
from personal_agent.transport.agui.ws_endpoint import ApprovalDecision
from tests._helpers.permissions import use_rules

SESSION = "sess-1687"
_ALL_MODES = ["NORMAL", "ALERT", "DEGRADED"]
_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    @asynccontextmanager
    async def _noop(**_kw: Any) -> AsyncIterator[None]:
        yield None

    clear_session_rules()
    settings_mock = MagicMock()
    settings_mock.approval_ui_enabled = True
    settings_mock.approval_timeout_seconds = 60.0
    with (
        patch("personal_agent.transport.agui.transport.phase_span", _noop),
        patch("personal_agent.tools.executor.settings", settings_mock),
    ):
        yield
    clear_session_rules()


def _transport(*answers: str, record: AsyncMock | None = None) -> MagicMock:
    transport = MagicMock()
    transport.request_tool_approval = AsyncMock(
        side_effect=[ApprovalDecision(decision=a) for a in answers]  # type: ignore[arg-type]
    )
    transport.record_approval_decision = record or AsyncMock(return_value=None)
    return transport


class _Probe:
    def __init__(self) -> None:
        self.grants: list[ApprovalGrant | None] = []

    async def run(self, x: str = "", approval: ApprovalGrant | None = None) -> dict[str, Any]:
        self.grants.append(approval)
        return {"ran": True}


def _layer(executor: Any, transport: MagicMock, *, name: str = "probe") -> ToolExecutionLayer:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=name,
            description="probe",
            category="system_dangerous",
            parameters=[ToolParameter(name="x", type="string", description="x", required=False)],
            risk_level="high",
            allowed_modes=_ALL_MODES,
        ),
        executor,
    )
    config = copy.deepcopy(load_governance_config())
    config.tools[name] = ToolPolicy(allowed_in_modes=_ALL_MODES)
    mode_manager = MagicMock()
    mode_manager.get_current_mode.return_value = Mode.NORMAL
    return ToolExecutionLayer(
        registry=registry,
        governance_config=config,
        mode_manager=mode_manager,
        transport=transport,  # type: ignore[arg-type]
    )


async def _dispatch(layer: ToolExecutionLayer, arguments: dict[str, Any]) -> dict[str, Any]:
    return await dispatch_tool_call(
        tool_call_id="call-7",
        tool_name="probe",
        arguments=arguments,
        tool_layer=layer,
        trace_ctx=TraceContext.new_trace(),
        trace_id="t",
        session_id=SESSION,
        loaded_skills=set(),
        principal="primary",
    )


class TestTheDecisionIsPersisted:
    @pytest.mark.asyncio
    async def test_an_approve_is_recorded_with_the_card_and_the_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch, ask=["probe"])
        probe, transport = _Probe(), _transport("approve")
        result = await _dispatch(_layer(probe.run, transport), {"x": "1"})

        assert result["success"] is True
        card = transport.request_tool_approval.await_args.kwargs
        record = transport.record_approval_decision.await_args.kwargs
        assert record["request_id"] == card["request_id"]
        assert record["tool_call_id"] == "call-7"
        assert record["session_id"] == SESSION
        assert record["tool"] == "probe"
        assert record["decision"] == "approve"
        assert record["args_hash"] == approval_args_hash({"x": "1"})
        # The executor gets the grant of exactly that card.
        (grant,) = probe.grants
        assert grant == ApprovalGrant(
            request_id=card["request_id"],
            tool_call_id="call-7",
            args_hash=approval_args_hash({"x": "1"}),
            args={"x": "1"},
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", ["deny", "timeout", "connection_lost"])
    async def test_a_refusal_is_recorded_and_nothing_runs(
        self, monkeypatch: pytest.MonkeyPatch, answer: str
    ) -> None:
        use_rules(monkeypatch, ask=["probe"])
        probe, transport = _Probe(), _transport(answer)
        result = await _dispatch(_layer(probe.run, transport), {"x": "1"})

        assert result["success"] is False
        assert probe.grants == []
        assert transport.record_approval_decision.await_args.kwargs["decision"] == answer

    @pytest.mark.asyncio
    async def test_a_session_answer_to_an_explicit_ask_records_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch, ask=["probe"])
        probe, transport = _Probe(), _transport("approve_session")
        await _dispatch(_layer(probe.run, transport), {"x": "1"})

        assert transport.record_approval_decision.await_args.kwargs["decision"] == "approve"
        assert probe.grants[0] is not None

    @pytest.mark.asyncio
    async def test_a_session_answer_to_an_unmatched_call_records_approve_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch)
        probe, transport = _Probe(), _transport("approve_session")
        await _dispatch(_layer(probe.run, transport), {"x": "1"})

        assert transport.record_approval_decision.await_args.kwargs["decision"] == "approve_session"


class TestTheGrant:
    @pytest.mark.asyncio
    async def test_an_allowed_call_gets_no_grant_and_writes_no_decision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch, allow=["probe"])
        probe, transport = _Probe(), _transport()
        await _dispatch(_layer(probe.run, transport), {"x": "1"})

        assert probe.grants == [None]
        transport.record_approval_decision.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_model_supplied_approval_never_reaches_the_executor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch, allow=["probe"])
        probe, transport = _Probe(), _transport()
        forged = {"request_id": "forged", "tool_call_id": None, "args_hash": "x", "args": {}}
        await _dispatch(_layer(probe.run, transport), {"x": "1", "approval": forged})

        assert probe.grants == [None]


class TestOtherToolsKeepTheirApproval:
    @pytest.mark.asyncio
    async def test_a_failed_write_changes_no_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_rules(monkeypatch, ask=["probe"])
        failing = AsyncMock(side_effect=RuntimeError("database down"))
        probe, transport = _Probe(), _transport("approve", "deny", record=failing)
        layer = _layer(probe.run, transport)

        approved = await _dispatch(layer, {"x": "1"})
        denied = await _dispatch(layer, {"x": "2"})

        assert (approved["success"], denied["success"]) == (True, False)
        assert len(probe.grants) == 1

    @pytest.mark.asyncio
    async def test_a_tool_that_does_not_declare_approval_runs_as_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_rules(monkeypatch, ask=["probe"])
        seen: list[dict[str, Any]] = []

        async def plain(x: str = "") -> dict[str, Any]:
            seen.append({"x": x})
            return {}

        await _dispatch(_layer(plain, _transport("approve")), {"x": "1"})
        assert seen == [{"x": "1"}]

    @pytest.mark.asyncio
    async def test_run_python_with_network_still_asks_every_call_with_allow_once_only(
        self,
    ) -> None:
        """Production rules: D8.6 is an explicit ask, in every mode, with no session option."""
        from personal_agent.tools.primitives.run_python import run_python_tool

        runs: list[dict[str, Any]] = []

        async def fake_run_python(**kwargs: Any) -> dict[str, Any]:
            runs.append(kwargs)
            return {"stdout": ""}

        registry = ToolRegistry()
        registry.register(run_python_tool, fake_run_python)
        mode_manager = MagicMock()
        mode_manager.get_current_mode.return_value = Mode.NORMAL
        transport = _transport("approve", "approve")
        layer = ToolExecutionLayer(
            registry=registry,
            governance_config=load_governance_config(),
            mode_manager=mode_manager,
            transport=transport,  # type: ignore[arg-type]
        )
        args = {"script": "print(1)", "network": True}
        for _ in range(2):
            result = await layer.execute_tool(
                "run_python", dict(args), TraceContext.new_trace(), session_id=SESSION
            )
            assert result.success is True

        assert transport.request_tool_approval.await_count == 2
        for call in transport.request_tool_approval.await_args_list:
            assert call.kwargs["session_option"] is False
        assert len(runs) == 2
        assert "approval" not in runs[0]


class TestTheWriteAndTheCheck:
    @pytest.mark.asyncio
    async def test_a_tool_without_approval_gets_a_background_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from personal_agent.tools import executor

        use_rules(monkeypatch, ask=["probe"])
        release = asyncio.Event()

        async def slow_record(**_kw: Any) -> None:
            await release.wait()

        ran: list[str] = []

        async def plain(x: str = "") -> dict[str, Any]:
            ran.append(x)
            return {}

        transport = _transport("approve", record=AsyncMock(side_effect=slow_record))
        result = await _dispatch(_layer(plain, transport), {"x": "1"})

        # The call ran while its decision write was still open.
        assert result["success"] is True and ran == ["1"]
        assert len(executor._BACKGROUND_RECORDS) == 1
        release.set()
        await asyncio.gather(*executor._BACKGROUND_RECORDS)
        transport.record_approval_decision.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_pre_card_refusal_shows_no_card(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.tools.contract import ToolError

        use_rules(monkeypatch, ask=["probe"])

        class _Refused(ToolError[str]):
            pass

        async def check(arguments: Any, ctx: Any) -> None:
            raise _Refused("job_active", "busy", retryable=False, next_action="wait")

        probe = _Probe()
        probe.run.__func__.pre_card_check = check  # type: ignore[attr-defined]
        try:
            transport = _transport("approve")
            result = await _dispatch(_layer(probe.run, transport), {"x": "1"})
        finally:
            del probe.run.__func__.pre_card_check  # type: ignore[attr-defined]

        assert result["success"] is False
        assert "busy" in result["content"]
        transport.request_tool_approval.assert_not_awaited()
        assert probe.grants == []


class TestTheConfig:
    @pytest.mark.parametrize("mode", ["NORMAL", "ALERT", "DEGRADED", "RECOVERY", "LOCKDOWN"])
    def test_start_dev_job_asks_in_every_mode_on_an_explicit_rule(self, mode: str) -> None:
        decision = decide_permission(
            "start_dev_job", {"task": "t", "mode": "build"}, mode, session_rules=[]
        )
        assert (decision.decision, decision.explicit, decision.rule) == (
            "ask",
            True,
            "start_dev_job",
        )

    def test_no_allow_rule_names_start_dev_job(self) -> None:
        rules = yaml.safe_load((_ROOT / "config/governance/permissions.yaml").read_text())
        for kind in ("allow",):
            assert not [r for r in rules[kind] if "start_dev_job" in str(r["rule"])]
        eval_rules = yaml.safe_load((_ROOT / "config/governance/permissions.eval.yaml").read_text())
        assert "start_dev_job" not in str(eval_rules)

    def test_no_sub_agent_holds_start_dev_job(self) -> None:
        assert "start_dev_job" not in load_governance_config().granted_sub_agent_tool_names()

    def test_delegation_enabled_stays_false(self) -> None:
        assert AppConfig.model_fields["delegation_enabled"].default is False
        env_example = (_ROOT / ".env.example").read_text()
        assert "AGENT_DELEGATION_ENABLED=true" not in env_example.replace(" ", "").lower()

    def test_the_tool_registers_only_with_the_supervisor_enabled(self) -> None:
        import personal_agent.tools as tools

        for enabled in (False, True):
            registry = ToolRegistry()
            with patch.object(settings, "dev_agent_supervisor_enabled", enabled):
                tools.register_mvp_tools(registry)
            assert ("start_dev_job" in registry.list_tool_names()) is enabled
