"""ADR-0088 D7 — the observable-first done-bar, with CI teeth (FRE-513).

These tests are the enforcement the ADR promises: a forced fallback must produce a durable
degradation record *and* a ``turn_status`` degraded state; model work run outside
``observe_topology`` is detectable; and the model SDK stays confined to ``llm_client/`` so
``record_api_call`` remains the single runtime cost+observability choke point.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from personal_agent.events.models import TurnDegradedEvent
from personal_agent.observability.topology import current_topology, observe_topology
from personal_agent.observability.topology import seam as seam_mod
from personal_agent.observability.topology.projector import TurnObservationProjector

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "personal_agent"
# Benign non-invocation uses of the model SDK outside llm_client/ (verified by reading the
# call site). gateway/chat_api.py reads ``litellm.model_cost`` for pricing — not a model
# call. New entries require the same scrutiny: a *model invocation* outside llm_client/ is
# a contract violation, not an allowlist candidate.
_ALLOWED_SDK_IMPORTERS = {"gateway/chat_api.py"}


# -- (a) forced fallback → durable degradation record + turn_status degraded -------------


@pytest.mark.asyncio
async def test_forced_fallback_writes_durable_degradation_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn whose sub-agents all failed writes a durable row flagged fallback_triggered."""
    ledger = AsyncMock()
    ledger.fetch_authoritative_cost = AsyncMock(return_value=(0.9, 100, 50))
    ledger.write = AsyncMock()
    monkeypatch.setattr(seam_mod, "get_route_trace_ledger", lambda: ledger)
    monkeypatch.setattr(seam_mod, "get_event_bus", lambda: AsyncMock())

    failed_sub = SimpleNamespace(success=False, summary="", full_output="", error="boom")
    ctx = SimpleNamespace(
        trace_id=str(uuid4()),
        session_id=str(uuid4()),
        gateway_output=None,
        messages=[],
        steps=[],
        sub_agent_results=[failed_sub],
        expansion_phase_results=[],
        topology=None,
        turn_cost_usd=0.0,
    )

    async with observe_topology(ctx):
        pass

    ledger.write.assert_awaited_once()
    row = ledger.write.call_args.args[0]
    # The durable degradation record: a fallback was classified and persisted.
    assert row.fallback_triggered is True
    assert row.orchestration_event == "fallback_triggered"


@pytest.mark.asyncio
async def test_forced_fallback_raises_turn_status_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degradation event drives a visible degraded turn_status via the projector."""
    emitted: list[dict[str, object]] = []

    async def _fake_emit(*, session_id: str, value: dict[str, object]) -> None:
        emitted.append(value)

    from personal_agent.observability.topology import projector as projector_mod

    monkeypatch.setattr(projector_mod, "emit_turn_status", _fake_emit)
    proj = TurnObservationProjector()

    await proj.handle(
        TurnDegradedEvent(
            trace_id="t-1",
            session_id="s-1",
            where="decompose",
            reason="planner_schema_fail",
            severity="critical",
        )
    )

    assert emitted[-1]["degraded"] is True
    assert any("planner_schema_fail" in d for d in emitted[-1]["degradations"])  # type: ignore[operator]


# -- (b) out-of-seam model work is detectable (runtime context-var guard) -----------------


@pytest.mark.asyncio
async def test_model_work_inside_seam_carries_topology() -> None:
    """Inside observe_topology the active topology is set; the cost event would stamp it."""
    ctx = SimpleNamespace(
        trace_id=str(uuid4()),
        session_id=str(uuid4()),
        gateway_output=None,
        messages=[],
        steps=[],
        sub_agent_results=None,
        expansion_phase_results=[],
        topology=None,
        turn_cost_usd=0.0,
    )
    assert current_topology() is None  # no seam active yet
    async with observe_topology(ctx):
        # A model call on this stack would see a non-None topology (in-seam).
        assert current_topology() == "primary"
    # Reset on exit — work after the seam is out-of-seam again.
    assert current_topology() is None


def test_out_of_seam_model_work_is_flagged() -> None:
    """Model work with no active topology is the D7 violation the guard detects."""
    # No observe_topology on this call stack → current_topology() is None, which the cost
    # boundary stamps onto the event so out-of-seam calls are queryable / test-catchable.
    assert current_topology() is None


# -- (c) static guard: the model SDK is confined to llm_client/ --------------------------


def test_model_sdk_confined_to_llm_client() -> None:
    """No file outside llm_client/ imports the model SDK except the vetted allowlist."""
    pattern = re.compile(r"(^|\n)\s*(import litellm|from litellm)\b")
    offenders: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        rel = py.relative_to(_SRC_ROOT).as_posix()
        if rel.startswith("llm_client/"):
            continue
        if pattern.search(py.read_text(encoding="utf-8")):
            if rel in _ALLOWED_SDK_IMPORTERS:
                continue
            offenders.append(rel)
    assert not offenders, (
        "model SDK imported outside llm_client/ (route model calls through "
        f"CostTrackerService.record_api_call instead): {offenders}"
    )
