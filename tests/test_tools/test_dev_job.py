"""The checks that ``start_dev_job`` makes before it writes a row (FRE-1687, ADR-0159 D10).

None of these reaches the database: each refusal comes first. The database path is in
``tests/personal_agent/dev_agent/test_start_dev_job_integration.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.config import settings
from personal_agent.dev_agent import gateway
from personal_agent.telemetry import TraceContext
from personal_agent.tools.dev_job import (
    DevJobError,
    start_dev_job_executor,
    start_dev_job_pre_card_check,
)
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
    """The supervisor is on and running. Its job store is a fake that records any write."""
    store = MagicMock()
    store.create_approved_job = AsyncMock()
    store.refusal_before_card = AsyncMock(return_value=None)
    store.fix_source_exists = AsyncMock(return_value=True)
    with (
        patch.object(settings, "dev_agent_supervisor_enabled", True),
        patch("personal_agent.tools.dev_job._running_job_store", return_value=store),
    ):
        yield store.create_approved_job


async def _code(**kwargs: Any) -> str:
    with pytest.raises(DevJobError) as refused:
        await start_dev_job_executor(**kwargs)
    return refused.value.code


@pytest.mark.asyncio
class TestRefusedBeforeTheDatabase:
    async def test_no_grant(self, available: MagicMock) -> None:
        assert await _code(**_ARGS, ctx=_ctx()) == "approval_required"
        available.assert_not_called()

    async def test_no_supervisor(self) -> None:
        with (
            patch.object(settings, "dev_agent_supervisor_enabled", True),
            patch("personal_agent.tools.dev_job._running_job_store", return_value=None),
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


@pytest.mark.asyncio
class TestPreCardCheck:
    async def test_it_passes_a_call_that_can_start(self, available: MagicMock) -> None:
        await start_dev_job_pre_card_check(_ARGS, _ctx())

    @pytest.mark.parametrize("reason", ["job_active", "month_cap_reached"])
    async def test_it_refuses_what_the_store_would_refuse(self, reason: str) -> None:
        store = MagicMock()
        store.refusal_before_card = AsyncMock(return_value=reason)
        store.fix_source_exists = AsyncMock(return_value=True)
        with (
            patch.object(settings, "dev_agent_supervisor_enabled", True),
            patch("personal_agent.tools.dev_job._running_job_store", return_value=store),
            pytest.raises(DevJobError) as refused,
        ):
            await start_dev_job_pre_card_check(_ARGS, _ctx())
        assert refused.value.code == reason

    async def test_it_refuses_invalid_arguments(self, available: MagicMock) -> None:
        with pytest.raises(DevJobError) as refused:
            await start_dev_job_pre_card_check({"task": "x", "mode": "fix"}, _ctx())
        assert refused.value.code == "invalid_arguments"

    async def test_a_non_uuid_identity_is_not_available(self, available: MagicMock) -> None:
        ctx = TraceContext.new_trace(user_id=uuid4(), session_id="not-a-uuid")
        with pytest.raises(DevJobError) as refused:
            await start_dev_job_pre_card_check(_ARGS, ctx)
        assert refused.value.code == "not_available"

    async def test_a_fix_of_a_missing_artifact_is_refused(self, available: MagicMock) -> None:
        store = MagicMock()
        store.fix_source_exists = AsyncMock(return_value=False)
        store.refusal_before_card = AsyncMock(return_value=None)
        args = {"task": "Fix it.", "mode": "fix", "artifact_id": str(uuid4())}
        with (
            patch("personal_agent.tools.dev_job._running_job_store", return_value=store),
            pytest.raises(DevJobError) as refused,
        ):
            await start_dev_job_pre_card_check(args, _ctx())
        assert refused.value.code == "source_not_found"

    async def test_a_broker_config_that_fails_to_load_is_not_available(
        self, available: MagicMock
    ) -> None:
        with (
            patch(
                "personal_agent.tools.dev_job._broker_config",
                AsyncMock(side_effect=FileNotFoundError("broker.yaml")),
            ),
            pytest.raises(DevJobError) as refused,
        ):
            await start_dev_job_pre_card_check(_ARGS, _ctx())
        assert refused.value.code == "not_available"

    def test_the_registry_holds_it(self) -> None:
        import personal_agent.tools as tools
        from personal_agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        with patch.object(settings, "dev_agent_supervisor_enabled", True):
            tools.register_mvp_tools(registry)
        assert registry.get_pre_card_check("start_dev_job") is start_dev_job_pre_card_check


class TestSupervisorRunning:
    def test_false_before_a_start(self) -> None:
        with patch.object(gateway, "_running", None):
            assert gateway.running_job_store() is None

    @pytest.mark.asyncio
    async def test_true_while_the_task_runs_and_false_after(self) -> None:
        task = asyncio.create_task(asyncio.sleep(10))
        running = MagicMock(task=task)
        with patch.object(gateway, "_running", running):
            assert gateway.running_job_store() is not None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert gateway.running_job_store() is None
