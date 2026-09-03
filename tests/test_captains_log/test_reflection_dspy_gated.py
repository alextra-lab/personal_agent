"""FRE-989 finding eight: Captain's Log reflection is gated on the cloud path.

The defect in one sentence: ``captains_log`` is bound to ``claude_sonnet``
(cloud), reflection drove it through ``dspy.ChainOfThought``, and DSPy calls the
provider directly — so the role behind the FRE-987 cost incident was spending
with no reservation and no ledger row.

These tests assert the *behaviour at the reflection call site*, not the gate
helper in isolation (that is ``test_dspy_gate.py``): a cloud role reserves, a
local role does not, and a denial does not leave the job holding headroom.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from personal_agent.captains_log.models import CaptainLogEntry, CaptainLogEntryType


def _entry() -> CaptainLogEntry:
    """A minimal VALID entry.

    It must validate: an invalid one makes ``generate_reflection_dspy`` raise,
    reflection falls through to the manual (also gated) client, and the
    assertions below then measure the fallback instead of the DSPy path.
    """
    return CaptainLogEntry(
        entry_id=str(uuid4()),
        type=CaptainLogEntryType.REFLECTION,
        title="t",
        rationale="r",
        trace_id="trace-test",
    )


def _fake_dspy(*args: Any, **kwargs: Any) -> tuple[CaptainLogEntry, list[str]]:
    return _entry(), []


async def _fake_to_thread(fn: Any, **kwargs: Any) -> Any:
    return fn(**kwargs)


def _stub_gate() -> AsyncMock:
    gate = AsyncMock()
    gate.reserve = AsyncMock(return_value=uuid4())
    return gate


@contextmanager
def _reflection_env(gate: AsyncMock, recorder: AsyncMock, *, is_cloud: bool) -> Iterator[None]:
    """Patch everything reflection touches except the behaviour under test.

    Uses the REAL catalog definition for claude_sonnet rather than a
    hand-built one, so a catalog change surfaces here instead of being
    papered over by a fixture that drifted from the config.
    """
    from personal_agent.config import load_model_config

    model_def = load_model_config().models["claude_sonnet"]
    patches = [
        patch(
            "personal_agent.captains_log.reflection._fetch_trace_events",
            AsyncMock(return_value=[]),
        ),
        patch("personal_agent.captains_log.reflection.DSPY_AVAILABLE", True),
        patch("personal_agent.captains_log.reflection.generate_reflection_dspy", new=_fake_dspy),
        patch.object(asyncio, "to_thread", new=_fake_to_thread),
        patch(
            "personal_agent.captains_log.reflection.load_mean_rating_lookup",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "personal_agent.captains_log.reflection.resolve_dspy_target",
            return_value=("claude_sonnet", model_def, is_cloud),
        ),
        patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
        patch("personal_agent.llm_client.cost_tracker.record_vendor_cost", new=recorder),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


async def _run_reflection() -> None:
    from personal_agent.captains_log.reflection import generate_reflection_entry

    await generate_reflection_entry(
        user_message="hi",
        trace_id="trace-test",
        steps_count=1,
        final_state="COMPLETED",
        reply_length=5,
        session_id=str(uuid4()),
    )


@pytest.mark.asyncio
async def test_cloud_reflection_reserves_against_captains_log() -> None:
    """A paid reflection takes a reservation on its own lane before it runs."""
    gate, recorder = _stub_gate(), AsyncMock()
    with _reflection_env(gate, recorder, is_cloud=True):
        await _run_reflection()

    gate.reserve.assert_awaited_once()
    assert gate.reserve.await_args.kwargs["role"] == "captains_log"
    gate.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cloud_reflection_writes_a_role_attributed_ledger_row() -> None:
    """AC-3 for this channel: the spend is attributable to captains_log."""
    gate, recorder = _stub_gate(), AsyncMock()
    with _reflection_env(gate, recorder, is_cloud=True):
        await _run_reflection()

    recorder.assert_awaited_once()
    assert recorder.await_args.kwargs["purpose"] == "captains_log"


@pytest.mark.asyncio
async def test_local_reflection_takes_no_reservation() -> None:
    """A free local call must not consume a paid lane's headroom."""
    gate, recorder = _stub_gate(), AsyncMock()
    with _reflection_env(gate, recorder, is_cloud=False):
        await _run_reflection()

    gate.reserve.assert_not_awaited()
    recorder.assert_not_awaited()


def test_cost_is_collected_even_when_the_job_raises() -> None:
    """The sink must be populated on the FAILURE path, not only on success.

    The most likely DSPy failure is a post-call one — the predictor returned and
    parsing its result failed — which is exactly when the provider has already
    billed. If ``collect_dspy_cost`` ran only on the success path, the sink would
    be empty on those paths and the caller would refund spend that really
    happened, so the cap could never trip on a repeatedly-failing reflection.
    """
    import dspy

    from personal_agent.llm_client.dspy_gate import DspyJobCost

    sink = DspyJobCost()
    lm = SimpleNamespace(
        model="anthropic/claude-sonnet-4-6",
        history=[{"cost": 0.07, "usage": {"prompt_tokens": 900, "completion_tokens": 120}}],
    )

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("predictor exploded after the provider billed")

    from personal_agent.captains_log import reflection_dspy

    with (
        # Patched at its source module: generate_reflection_dspy imports
        # configure_dspy_lm inside the function body.
        patch(
            "personal_agent.llm_client.dspy_adapter.configure_dspy_lm",
            return_value=lm,
        ),
        patch.object(dspy, "ChainOfThought", _boom),
        pytest.raises(RuntimeError, match="predictor exploded"),
    ):
        reflection_dspy.generate_reflection_dspy(
            user_message="hi",
            trace_id="trace-test",
            steps_count=1,
            final_state="COMPLETED",
            reply_length=5,
            telemetry_summary="",
            captains_log_role="captains_log",
            cost_sink=sink,
        )

    assert sink.actual_cost_usd == Decimal("0.07"), (
        "a job that raised after spending must still report what it spent"
    )
    assert sink.input_tokens == 900


@pytest.mark.asyncio
async def test_denial_does_not_leave_the_job_holding_headroom() -> None:
    """A denied reflection degrades to the manual path without a stuck reservation.

    ``captains_log`` is a ``nack`` role: the denial must reach the caller rather
    than be absorbed, and no reservation may be left active for the reaper.
    """
    from datetime import datetime
    from decimal import Decimal

    from personal_agent.cost_gate import BudgetDenied

    gate, recorder = _stub_gate(), AsyncMock()
    gate.reserve = AsyncMock(
        side_effect=BudgetDenied(
            role="captains_log",
            time_window="daily",
            current_spend=Decimal("5.00"),
            cap=Decimal("5.00"),
            window_resets_at=datetime.now(),
        )
    )
    with _reflection_env(gate, recorder, is_cloud=True):
        await _run_reflection()  # falls through to the manual path, does not raise

    gate.commit.assert_not_awaited()
    gate.refund.assert_not_awaited()  # nothing was reserved, so nothing to refund
    recorder.assert_not_awaited()
