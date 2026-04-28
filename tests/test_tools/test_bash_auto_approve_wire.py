"""Tests for the FRE-283 auto_approve_prefixes wire in _check_permissions.

Verifies that bash commands whose every pipeline segment matches the
per-mode auto_approve_prefixes list are allowed without an approval
round-trip, and that non-matching segments fall through to the normal
approval branch.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode
from personal_agent.tools.executor import PermissionResult, _check_permissions
from personal_agent.tools.types import ToolDefinition, ToolParameter


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def governance():  # type: ignore[no-untyped-def]
    return load_governance_config()


@pytest.fixture()
def bash_def() -> ToolDefinition:
    return ToolDefinition(
        name="bash",
        description="test bash primitive",
        category="system_dangerous",
        parameters=[
            ToolParameter(
                name="command",
                type="string",
                description="Shell command to execute",
                required=True,
            )
        ],
        risk_level="high",
        allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
        requires_approval=True,
    )


class TestBashAutoApproveWire:
    def test_all_allowed_segments_auto_approved_in_normal(
        self, governance, bash_def: ToolDefinition
    ) -> None:
        """All segments in allowlist → PermissionResult(allowed=True) with no approval round-trip."""
        # The real NORMAL allowlist includes curl, grep, wc.
        arguments = {"command": "curl http://localhost:9200/_cat/indices | grep agent | wc -l"}

        with patch("personal_agent.tools.executor.settings") as mock_settings:
            mock_settings.approval_ui_enabled = False
            result = run(
                _check_permissions(
                    tool_name="bash",
                    tool_def=bash_def,
                    arguments=arguments,
                    current_mode=Mode.NORMAL,
                    governance_config=governance,
                )
            )

        assert result.allowed is True

    def test_disallowed_segment_falls_to_approval_branch(
        self, governance, bash_def: ToolDefinition
    ) -> None:
        """A segment not in the allowlist causes the approval branch to fire."""
        # 'rm' is not in the auto-approve allowlist.
        arguments = {"command": "curl http://localhost | rm -f /tmp/x"}

        mock_transport = AsyncMock()
        mock_transport.request_tool_approval = AsyncMock(return_value=MagicMock(decision="approve"))

        with patch("personal_agent.tools.executor.settings") as mock_settings:
            mock_settings.approval_ui_enabled = True
            mock_settings.approval_timeout_seconds = 60.0
            result = run(
                _check_permissions(
                    tool_name="bash",
                    tool_def=bash_def,
                    arguments=arguments,
                    current_mode=Mode.NORMAL,
                    governance_config=governance,
                    transport=mock_transport,
                    session_id="test-session",
                )
            )

        # The approval round-trip was triggered (not auto-approved).
        mock_transport.request_tool_approval.assert_called_once()
        assert result.allowed is True  # user approved in mock

    def test_alert_mode_uses_narrower_allowlist(self, governance, bash_def: ToolDefinition) -> None:
        """ALERT mode: 'docker ps' not in ALERT allowlist, so approval branch fires."""
        # The real ALERT allowlist does not include 'docker ps'.
        arguments = {"command": "docker ps -a"}

        mock_transport = AsyncMock()
        mock_transport.request_tool_approval = AsyncMock(return_value=MagicMock(decision="approve"))

        with patch("personal_agent.tools.executor.settings") as mock_settings:
            mock_settings.approval_ui_enabled = True
            mock_settings.approval_timeout_seconds = 60.0
            result = run(
                _check_permissions(
                    tool_name="bash",
                    tool_def=bash_def,
                    arguments=arguments,
                    current_mode=Mode.ALERT,
                    governance_config=governance,
                    transport=mock_transport,
                    session_id="test-session",
                )
            )

        # 'docker ps' not in ALERT list → approval branch fires
        mock_transport.request_tool_approval.assert_called_once()

    def test_lockdown_blocks_before_allowlist(self, governance, bash_def: ToolDefinition) -> None:
        """LOCKDOWN mode: bash is forbidden at the mode-check before allowlist runs."""
        # bash_def.allowed_modes doesn't include LOCKDOWN, so the mode check
        # fires first and the allowlist is never consulted.
        arguments = {"command": "curl http://localhost"}

        with patch("personal_agent.tools.executor.settings") as mock_settings:
            mock_settings.approval_ui_enabled = False
            result = run(
                _check_permissions(
                    tool_name="bash",
                    tool_def=bash_def,
                    arguments=arguments,
                    current_mode=Mode.LOCKDOWN,
                    governance_config=governance,
                )
            )

        assert result.allowed is False
        assert "LOCKDOWN" in result.reason
