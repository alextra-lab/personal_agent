# ruff: noqa: D101, D102
"""The gateway wiring of the job supervisor (FRE-1684)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from personal_agent.config.settings import AppConfig
from personal_agent.delegation.adapters.opencode import OpenCodeAdapter
from personal_agent.dev_agent import gateway
from personal_agent.dev_agent.jobs import DevJob, JobState

pytestmark = pytest.mark.asyncio


def _job() -> DevJob:
    return DevJob(
        id=uuid4(),
        user_id=uuid4(),
        session_id=uuid4(),
        trace_id=uuid4(),
        approval_request_id="a",
        mode="build",
        task_text="t",
        source_artifact_id=None,
        model_id="deepseek-v4.1-flash-scaleway",
        state=JobState.QUEUED,
        lease_epoch=1,
        started_at=None,
        result_commit=None,
        result_artifact_id=None,
    )


class TestWiring:
    async def test_the_supervisor_is_off_by_default(self) -> None:
        assert AppConfig.model_fields["dev_agent_supervisor_enabled"].default is False

    async def test_no_control_secret_starts_no_supervisor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gateway.settings, "dev_broker_control_secret", None)
        assert await gateway.start_supervisor() is None

    async def test_the_relay_factory_yields_an_adapter_and_closes_its_client(self) -> None:
        factory = gateway.relay_adapter_factory("http://dev-broker:8701", "s3cret")
        async with factory(_job(), "pw") as adapter:
            assert isinstance(adapter, OpenCodeAdapter)
            assert adapter.available()
            client = adapter._client
        assert client.is_closed
