"""FRE-523: sub-agent captures carry EVAL provenance.

The per-sub-agent audit record (FRE-505) is written unconditionally. Under the
FRE-523 contract it must additionally carry ``eval_mode`` so eval-derived
sub-agent activity is identifiable, uniform with the primary path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec


def _spec() -> SubAgentSpec:
    return SubAgentSpec(
        task="test task",
        context=[{"role": "user", "content": "do the thing"}],
        output_format="text",
        max_tokens=1024,
        timeout_seconds=30.0,
    )


@pytest.mark.asyncio
async def test_sub_agent_capture_carries_eval_provenance() -> None:
    """run_sub_agent(eval_mode=True) emits a SubAgentCapture with eval_mode=True."""
    mock_client = AsyncMock()
    mock_client.respond = AsyncMock(return_value="sub result")

    with patch("personal_agent.orchestrator.sub_agent.write_sub_agent_capture") as mock_write:
        await run_sub_agent(
            spec=_spec(),
            llm_client=mock_client,
            trace_id="trace-1",
            session_id="session-1",
            eval_mode=True,
        )

    mock_write.assert_called_once()
    capture = mock_write.call_args.args[0]
    assert capture.eval_mode is True


@pytest.mark.asyncio
async def test_sub_agent_capture_defaults_non_eval() -> None:
    """Default run_sub_agent (no eval_mode) emits eval_mode=False provenance."""
    mock_client = AsyncMock()
    mock_client.respond = AsyncMock(return_value="sub result")

    with patch("personal_agent.orchestrator.sub_agent.write_sub_agent_capture") as mock_write:
        await run_sub_agent(
            spec=_spec(),
            llm_client=mock_client,
            trace_id="trace-1",
            session_id="session-1",
        )

    mock_write.assert_called_once()
    assert mock_write.call_args.args[0].eval_mode is False
