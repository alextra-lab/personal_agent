"""Tests for CodexAdapter and GenericMCPAdapter stubs.

Verifies that both stub adapters:
- Return the correct availability status
- Return DelegationOutcome with success=False and informative error messages
- Never raise exceptions
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from personal_agent.delegation.adapters.codex import CodexAdapter
from personal_agent.delegation.adapters.generic_mcp import GenericMCPAdapter
from personal_agent.request_gateway.delegation_types import (
    DelegationContext,
    DelegationPackage,
)
from personal_agent.telemetry.trace import TraceContext


def _make_package() -> DelegationPackage:
    return DelegationPackage(
        task_id="del-stub001",
        target_agent="codex",
        task_description="Stub task",
        context=DelegationContext(service_path="src/"),
        created_at=datetime(2026, 4, 14, tzinfo=timezone.utc),
    )


def _make_trace() -> TraceContext:
    return TraceContext(trace_id="trace-stub123")


class TestCodexAdapter:
    """CodexAdapter stub behaviour."""

    def test_not_available(self) -> None:
        adapter = CodexAdapter()
        assert adapter.available() is False

    @pytest.mark.asyncio
    async def test_delegate_returns_failure(self) -> None:
        adapter = CodexAdapter()
        package = _make_package()
        trace = _make_trace()

        outcome = await adapter.delegate(package, timeout=30.0, trace_ctx=trace)

        assert outcome.success is False
        assert outcome.task_id == "del-stub001"
        assert outcome.rounds_needed == 0
        assert "not yet implemented" in outcome.what_was_missing.lower()
        assert outcome.duration_minutes == 0.0

    @pytest.mark.asyncio
    async def test_delegate_never_raises(self) -> None:
        """Stub must not raise regardless of inputs."""
        adapter = CodexAdapter()
        package = _make_package()
        # Should not raise
        outcome = await adapter.delegate(package)
        assert outcome is not None


class TestGenericMCPAdapter:
    """GenericMCPAdapter stub behaviour."""

    def test_available_with_server_url(self) -> None:
        adapter = GenericMCPAdapter(server_url="http://some-agent:9001/mcp")
        assert adapter.available() is True

    def test_unavailable_with_empty_url(self) -> None:
        adapter = GenericMCPAdapter(server_url="")
        assert adapter.available() is False

    @pytest.mark.asyncio
    async def test_delegate_returns_failure(self) -> None:
        adapter = GenericMCPAdapter(server_url="http://some-agent:9001/mcp")
        package = _make_package()
        trace = _make_trace()

        outcome = await adapter.delegate(package, timeout=30.0, trace_ctx=trace)

        assert outcome.success is False
        assert outcome.task_id == "del-stub001"
        assert outcome.rounds_needed == 0
        assert "not yet implemented" in outcome.what_was_missing.lower()
        assert outcome.duration_minutes == 0.0

    @pytest.mark.asyncio
    async def test_delegate_never_raises(self) -> None:
        """Stub must not raise regardless of inputs."""
        adapter = GenericMCPAdapter(server_url="http://localhost:9001")
        package = _make_package()
        outcome = await adapter.delegate(package)
        assert outcome is not None

    def test_stores_server_url(self) -> None:
        adapter = GenericMCPAdapter(server_url="http://example.com:8080/mcp")
        assert adapter._server_url == "http://example.com:8080/mcp"
