"""The checks that ``start_dev_job`` makes before it writes a row (FRE-1687, ADR-0159 D10).

None of these reaches the database: each refusal comes first. The database path is in
``tests/personal_agent/dev_agent/test_start_dev_job_integration.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.config import settings
from personal_agent.dev_agent import gateway
from personal_agent.telemetry import TraceContext
from personal_agent.tools.dev_job import DevJobError, start_dev_job_executor
from personal_agent.tools.executor import ApprovalGrant, approval_args_hash

_ARGS: dict[str, Any] = {"task": "Build a counter page.", "mode": "build"}


def _grant(args: dict[str, Any]) -> ApprovalGrant:
    return ApprovalGrant(
        request_id="r1", tool_call_id="c1", args_hash=approval_args_hash(args), args=args
    )


def _ctx() -> TraceContext:
    return TraceContext.new_trace(user_id=uuid4(), session_id=str(uuid4()))


@pytest.fixture
def available() -> Iterator[MagicMock]:
    """The supervisor is on and running. The database is never reached in these tests."""
    with (
        patch.object(settings, "dev_agent_supervisor_enabled", True),
        patch("personal_agent.tools.dev_job._supervisor_running", return_value=True),
        patch("personal_agent.tools.dev_job.asyncpg.create_pool") as pool,
    ):
        yield pool


async def _code(**kwargs: Any) -> str:
    with pytest.raises(DevJobError) as refused:
        await start_dev_job_executor(**kwargs)
    return refused.value.code


@pytest.mark.asyncio
class TestRefusedBeforeTheDatabase:
    async def test_no_grant(self, available: MagicMock) -> None:
        assert await _code(**_ARGS, ctx=_ctx()) == "approval_required"
        available.assert_not_called()

    @pytest.mark.parametrize(
        "shown",
        [
            {"task": "Build another page.", "mode": "build"},
            {"task": "Build a counter page.", "mode": "fix", "artifact_id": str(uuid4())},
        ],
    )
    async def test_a_grant_for_other_arguments(
        self, available: MagicMock, shown: dict[str, Any]
    ) -> None:
        assert await _code(**_ARGS, ctx=_ctx(), approval=_grant(shown)) == "approval_mismatch"
        available.assert_not_called()

    async def test_no_supervisor(self) -> None:
        with (
            patch.object(settings, "dev_agent_supervisor_enabled", True),
            patch("personal_agent.tools.dev_job._supervisor_running", return_value=False),
        ):
            code = await _code(**_ARGS, ctx=_ctx(), approval=_grant(_ARGS))
        assert code == "not_available"

    async def test_no_session(self, available: MagicMock) -> None:
        ctx = TraceContext.new_trace(user_id=uuid4())
        assert await _code(**_ARGS, ctx=ctx, approval=_grant(_ARGS)) == "not_available"

    @pytest.mark.parametrize(
        "args",
        [
            {"task": "x", "mode": "deploy"},
            {"task": "x", "mode": "fix"},
            {"task": "x", "mode": "fix", "artifact_id": "not-a-uuid"},
            {"task": "x", "mode": "build", "artifact_id": str(uuid4())},
            {"task": "   ", "mode": "build"},
            {"task": "x" * 8001, "mode": "build"},
        ],
    )
    async def test_invalid_arguments(self, available: MagicMock, args: dict[str, Any]) -> None:
        assert await _code(**args, ctx=_ctx(), approval=_grant(args)) == "invalid_arguments"
        available.assert_not_called()


class TestSupervisorRunning:
    def test_false_before_a_start(self) -> None:
        with patch.object(gateway, "_running", None):
            assert gateway.supervisor_running() is False

    @pytest.mark.asyncio
    async def test_true_while_the_task_runs_and_false_after(self) -> None:
        task = asyncio.create_task(asyncio.sleep(10))
        running = MagicMock(task=task)
        with patch.object(gateway, "_running", running):
            assert gateway.supervisor_running() is True
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert gateway.supervisor_running() is False
