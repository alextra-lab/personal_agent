# ruff: noqa: D101, D102
"""The gateway wiring of the job supervisor (FRE-1684, FRE-1685)."""

from __future__ import annotations

import base64
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

    async def test_no_github_app_starts_no_supervisor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gateway.settings, "dev_broker_control_secret", "s3cret")
        monkeypatch.setattr(gateway.settings, "dev_agent_github_app_private_key_b64", None)
        assert await gateway.start_supervisor() is None

    async def test_github_config_decodes_the_base64_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"
        monkeypatch.setattr(gateway.settings, "dev_agent_github_app_id", 5)
        monkeypatch.setattr(gateway.settings, "dev_agent_github_installation_id", 6)
        monkeypatch.setattr(gateway.settings, "dev_agent_github_org", "example-org")
        monkeypatch.setattr(
            gateway.settings,
            "dev_agent_github_app_private_key_b64",
            base64.b64encode(pem.encode()).decode(),
        )
        config = gateway.github_config()
        assert config is not None
        assert (config.app_id, config.installation_id, config.org) == (5, 6, "example-org")
        assert config.private_key_pem == pem

    async def test_a_key_that_is_not_pem_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gateway.settings, "dev_agent_github_app_id", 5)
        monkeypatch.setattr(gateway.settings, "dev_agent_github_installation_id", 6)
        monkeypatch.setattr(gateway.settings, "dev_agent_github_org", "example-org")
        monkeypatch.setattr(
            gateway.settings, "dev_agent_github_app_private_key_b64", base64.b64encode(b"x").decode()
        )
        with pytest.raises(ValueError):
            gateway.github_config()

    async def test_the_relay_factory_yields_an_adapter_and_closes_its_client(self) -> None:
        factory = gateway.relay_adapter_factory("http://dev-broker:8701", "s3cret")
        async with factory(_job(), "pw") as adapter:
            assert isinstance(adapter, OpenCodeAdapter)
            assert adapter.available()
            client = adapter._client
        assert client.is_closed
