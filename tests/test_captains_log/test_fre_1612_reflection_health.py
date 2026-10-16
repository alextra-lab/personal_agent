"""FRE-1612: a reflection document records whether its inputs were healthy.

The funnel dashboard reads ``had_errors`` and ``telemetry_available`` to show how
often a reflection ran on a failed turn, or with no telemetry at all. Each of the
three return paths of ``generate_reflection_entry`` must set both. No test calls a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log import reflection
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
)

_TRACES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parent / "fixtures" / "fre1608_traces.json").read_text()
)
_FAILED_TURN = _TRACES["FRE-1373"]
_CLEAN_TURN = _TRACES["FRE-1404"]


async def _generate(
    events: list[dict[str, Any]], *, dspy_available: bool, manual_raises: bool = False
) -> CaptainLogEntry:
    """Run ``generate_reflection_entry`` on one trace with every model call stubbed."""
    dspy_entry = CaptainLogEntry(
        entry_id="",
        type=CaptainLogEntryType.REFLECTION,
        title="t",
        rationale="r",
        status=CaptainLogStatus.AWAITING_APPROVAL,
    )
    manual_client = MagicMock()
    manual_client.respond = AsyncMock(
        side_effect=RuntimeError("model down") if manual_raises else None,
        return_value={"content": '{"rationale": "r"}'},
    )
    with (
        patch.object(reflection, "DSPY_AVAILABLE", dspy_available),
        patch.object(reflection, "_fetch_trace_events", AsyncMock(return_value=events)),
        patch("personal_agent.config.resolve_role_model_key", return_value="local"),
        patch.object(reflection, "resolve_dspy_target", return_value=("local", MagicMock(), False)),
        patch.object(reflection, "generate_reflection_dspy", return_value=(dspy_entry, [])),
        patch.object(reflection, "get_llm_client_for_key", return_value=manual_client),
        patch.object(reflection, "_apply_read_before_emit", AsyncMock()),
    ):
        return await reflection.generate_reflection_entry(
            user_message="hi",
            trace_id="trace-fre-1612",
            steps_count=1,
            final_state="COMPLETED",
            reply_length=5,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("dspy_available", [True, False], ids=["dspy", "manual"])
async def test_failed_turn_with_telemetry(dspy_available: bool) -> None:
    """A failed turn gives had_errors=True and telemetry_available=True."""
    entry = await _generate(_FAILED_TURN, dspy_available=dspy_available)
    assert entry.had_errors is True
    assert entry.telemetry_available is True


@pytest.mark.asyncio
@pytest.mark.parametrize("dspy_available", [True, False], ids=["dspy", "manual"])
async def test_clean_turn_with_telemetry(dspy_available: bool) -> None:
    """A clean turn gives had_errors=False and telemetry_available=True."""
    entry = await _generate(_CLEAN_TURN, dspy_available=dspy_available)
    assert entry.had_errors is False
    assert entry.telemetry_available is True


@pytest.mark.asyncio
@pytest.mark.parametrize("dspy_available", [True, False], ids=["dspy", "manual"])
async def test_no_telemetry_events(dspy_available: bool) -> None:
    """A trace with no events gives telemetry_available=False."""
    entry = await _generate([], dspy_available=dspy_available)
    assert entry.telemetry_available is False
    assert entry.had_errors is False


@pytest.mark.asyncio
async def test_basic_fallback_path_carries_both_fields() -> None:
    """When DSPy is off and the manual call fails, the basic entry still carries both fields."""
    entry = await _generate(_FAILED_TURN, dspy_available=False, manual_raises=True)
    assert entry.rationale.startswith("Completed task with")
    assert entry.had_errors is True
    assert entry.telemetry_available is True


@pytest.mark.asyncio
async def test_kill_switch_leaves_had_errors_unknown() -> None:
    """With failure_path_reflection_enabled false, had_errors is None, not a false negative."""
    with patch.object(reflection.settings, "failure_path_reflection_enabled", False):
        entry = await _generate(_FAILED_TURN, dspy_available=True)
    assert entry.had_errors is None
    assert entry.telemetry_available is True
