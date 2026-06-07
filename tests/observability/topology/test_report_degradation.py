"""Tests for the report_degradation seam primitive + its expansion migration (FRE-513).

ADR-0088 D5: every topology that does less than intended routes through the single
``report_degradation`` call, which publishes ``turn.degraded`` to ``stream:turn.observed``.
The expansion controller's ``ExpansionResult.degraded`` sites are migrated to flow through
it (the planner-fallback case being the 87cbd720 silent-degradation it makes loud).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.events.models import TurnDegradedEvent
from personal_agent.observability.topology import report_degradation
from personal_agent.observability.topology import seam as seam_mod

pytestmark = pytest.mark.asyncio


async def test_report_degradation_publishes_event(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = AsyncMock()
    monkeypatch.setattr(seam_mod, "get_event_bus", lambda: bus)

    await report_degradation(
        trace_id="t-1",
        session_id="s-1",
        where="expansion:decompose",
        reason="No valid plan produced",
        severity="critical",
        expected="a plan",
        actual="no plan",
    )

    bus.publish.assert_awaited_once()
    stream, event = bus.publish.await_args.args[0], bus.publish.await_args.args[1]
    assert stream == "stream:turn.observed"
    assert isinstance(event, TurnDegradedEvent)
    assert event.where == "expansion:decompose"
    assert event.reason == "No valid plan produced"
    assert event.severity == "critical"


async def test_report_degradation_normalizes_unknown_severity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = AsyncMock()
    monkeypatch.setattr(seam_mod, "get_event_bus", lambda: bus)

    await report_degradation(
        trace_id="t-1",
        session_id="s-1",
        where="x",
        reason="y",
        severity="catastrophic",  # not a valid level → normalized to warning
    )

    event = bus.publish.await_args.args[1]
    assert event.severity == "warning"


async def test_report_degradation_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = AsyncMock()
    bus.publish = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr(seam_mod, "get_event_bus", lambda: bus)

    # Must not raise despite the bus failing.
    await report_degradation(trace_id="t-1", session_id="s-1", where="x", reason="y")


async def test_expansion_no_plan_reports_degradation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A planner that produces no plan routes through report_degradation (ADR-0088 D5)."""
    from personal_agent.orchestrator import expansion_controller as ec_mod

    reported: list[dict[str, str]] = []

    async def _fake_report(**kwargs: str) -> None:
        reported.append(kwargs)

    monkeypatch.setattr(ec_mod, "report_degradation", _fake_report)

    controller = ec_mod.ExpansionController()

    async def _no_plan(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(controller, "_run_planner", _no_plan)

    result = await controller.execute(
        query="q",
        strategy="DECOMPOSE",
        llm_client=AsyncMock(),
        trace_id="t-1",
        messages=[],
        session_id="s-1",
    )

    assert result.degraded is True
    assert any(r["reason"] == "No valid plan produced" for r in reported)
    assert reported[0]["severity"] == "critical"
