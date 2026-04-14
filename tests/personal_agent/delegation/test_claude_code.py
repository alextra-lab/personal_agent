"""Tests for ClaudeCodeAdapter.

Covers:
- available() returning True/False based on PATH
- delegate() success path via mocked subprocess
- delegate() when binary not found
- delegate() timeout path
- delegate() with MCP server URL injected into command
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.delegation.adapters.claude_code import ClaudeCodeAdapter
from personal_agent.request_gateway.delegation_types import (
    DelegationContext,
    DelegationPackage,
)
from personal_agent.telemetry.trace import TraceContext


def _make_package(task: str = "Do something") -> DelegationPackage:
    return DelegationPackage(
        task_id="del-test001",
        target_agent="claude-code",
        task_description=task,
        context=DelegationContext(service_path="src/"),
        created_at=datetime(2026, 4, 14, tzinfo=timezone.utc),
    )


def _make_trace() -> TraceContext:
    return TraceContext(trace_id="trace-abc123")


class TestClaudeCodeAdapterAvailable:
    """Tests for available() method."""

    def test_available_when_binary_in_path(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            adapter = ClaudeCodeAdapter()
            assert adapter.available() is True

    def test_unavailable_when_binary_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            adapter = ClaudeCodeAdapter()
            assert adapter.available() is False


class TestClaudeCodeAdapterDelegate:
    """Tests for delegate() method."""

    @pytest.mark.asyncio
    async def test_success_path(self) -> None:
        """Successful subprocess execution returns success=True outcome."""
        adapter = ClaudeCodeAdapter()
        package = _make_package("Write a unit test")
        trace = _make_trace()

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b"def test_example(): pass\n", b"")
        )

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
        ):
            outcome = await adapter.delegate(package, timeout=30.0, trace_ctx=trace)

        assert outcome.success is True
        assert outcome.task_id == "del-test001"
        assert "test_example" in outcome.what_worked
        # Verify command structure: [claude, --print, <task>]
        args = mock_exec.call_args[0]
        assert args[0] == "claude"
        assert args[1] == "--print"
        assert args[2] == "Write a unit test"

    @pytest.mark.asyncio
    async def test_failure_path(self) -> None:
        """Non-zero returncode returns success=False with stderr in what_was_missing."""
        adapter = ClaudeCodeAdapter()
        package = _make_package("Bad task")

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"Error: something went wrong")
        )

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            outcome = await adapter.delegate(package, timeout=30.0)

        assert outcome.success is False
        assert "Error: something went wrong" in outcome.what_was_missing

    @pytest.mark.asyncio
    async def test_unavailable_returns_failure(self) -> None:
        """When binary not found, returns failure outcome without spawning process."""
        with patch("shutil.which", return_value=None):
            adapter = ClaudeCodeAdapter()
            package = _make_package()
            outcome = await adapter.delegate(package)

        assert outcome.success is False
        assert "PATH" in outcome.what_was_missing
        assert outcome.rounds_needed == 0

    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self) -> None:
        """asyncio.TimeoutError is caught and returned as failure outcome."""
        adapter = ClaudeCodeAdapter()
        package = _make_package("Slow task")

        mock_proc = MagicMock()

        async def _timeout_communicate() -> tuple[bytes, bytes]:
            raise asyncio.TimeoutError()

        mock_proc.communicate = _timeout_communicate

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", side_effect=asyncio.TimeoutError),
        ):
            outcome = await adapter.delegate(package, timeout=0.001)

        assert outcome.success is False
        assert "timed out" in outcome.what_was_missing.lower()

    @pytest.mark.asyncio
    async def test_mcp_server_url_injected(self) -> None:
        """When mcp_server_url is set, --mcp-server flag appears in command."""
        adapter = ClaudeCodeAdapter(mcp_server_url="http://localhost:9000/mcp")
        package = _make_package("Task needing knowledge")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"done", b""))

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
        ):
            outcome = await adapter.delegate(package, timeout=30.0)

        assert outcome.success is True
        args = mock_exec.call_args[0]
        assert "--mcp-server" in args
        mcp_idx = list(args).index("--mcp-server")
        assert args[mcp_idx + 1] == "http://localhost:9000/mcp"

    @pytest.mark.asyncio
    async def test_no_mcp_url_by_default(self) -> None:
        """Without mcp_server_url, --mcp-server flag is not in command."""
        adapter = ClaudeCodeAdapter()
        package = _make_package("Simple task")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"result", b""))

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
        ):
            await adapter.delegate(package, timeout=30.0)

        args = mock_exec.call_args[0]
        assert "--mcp-server" not in args
