# ruff: noqa: D101, D102
"""The gateway wiring of the job supervisor (FRE-1684, FRE-1685)."""

from __future__ import annotations

import asyncio
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


class _RecordingEvents:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, str | None]] = []

    async def notify(self, job_id: object, event: str, step: str | None = None) -> None:
        self.calls.append((job_id, event, step))


class TestJobCardWiring:
    """FRE-1688 (ADR-0159 D10.6): the production supervisor sends the card events."""

    async def test_an_adapter_step_reaches_the_event_sink(self) -> None:
        events = _RecordingEvents()
        factory = gateway.relay_adapter_factory("http://dev-broker:8701", "s3cret", events)  # type: ignore[arg-type]
        job = _job()
        async with factory(job, "pw") as adapter:
            await adapter._on_progress("tool shell")  # type: ignore[attr-defined,misc]
        assert events.calls == [(job.id, "step", "tool shell")]

    async def test_start_supervisor_gives_the_supervisor_and_the_factory_the_same_sink(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from personal_agent.dev_agent.job_card import PoolJobEventSink
        from personal_agent.transport.agui.transport import emit_dev_job

        seen: dict[str, object] = {}

        class CapturingSupervisor:
            owner = "test"

            def __init__(self, **kwargs: object) -> None:
                seen.update(kwargs)

            async def run_forever(self) -> None:
                await asyncio.Event().wait()

        def capture_factory(url: str, secret: str, events: object = None) -> object:
            seen["factory_events"] = events
            return object()

        monkeypatch.setattr(gateway.settings, "dev_broker_control_secret", "s3cret")
        monkeypatch.setattr(gateway, "github_config", lambda: MagicMock())
        monkeypatch.setattr(gateway, "load_profiles", lambda: MagicMock())
        monkeypatch.setattr(gateway.asyncpg, "create_pool", AsyncMock(return_value=MagicMock()))
        monkeypatch.setattr(gateway, "create_guarded_http_client", lambda: MagicMock())
        monkeypatch.setattr(gateway, "DockerLauncher", MagicMock())
        monkeypatch.setattr(gateway, "load_toolkit_assets", lambda: MagicMock())
        monkeypatch.setattr(gateway, "GitHubAppHost", MagicMock())
        monkeypatch.setattr(gateway, "GitHubRepoPipeline", MagicMock())
        monkeypatch.setattr(gateway, "get_artifact_store", lambda: MagicMock())
        monkeypatch.setattr(gateway, "JobSupervisor", CapturingSupervisor)
        monkeypatch.setattr(gateway, "relay_adapter_factory", capture_factory)

        running = await gateway.start_supervisor()
        assert running is not None
        running.task.cancel()

        sink = seen["events"]
        assert isinstance(sink, PoolJobEventSink)
        assert seen["factory_events"] is sink
        assert sink._emit is emit_dev_job
