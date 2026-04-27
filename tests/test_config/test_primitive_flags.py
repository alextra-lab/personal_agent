"""Tests for FRE-261 PIVOT-2 primitive-tool feature flags in AppConfig.

These are pure unit tests — no LLM, no infrastructure required.
"""

import pytest

from personal_agent.config import AppConfig, settings


class TestPrimitiveToolsFlag:
    """primitive_tools_enabled is off by default."""

    def test_primitive_tools_disabled_by_default(self) -> None:
        """AGENT_PRIMITIVE_TOOLS_ENABLED defaults to False."""
        assert settings.primitive_tools_enabled is False

    def test_primitive_tools_flag_reads_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENT_PRIMITIVE_TOOLS_ENABLED=true sets the flag."""
        monkeypatch.setenv("AGENT_PRIMITIVE_TOOLS_ENABLED", "true")
        config = AppConfig()
        assert config.primitive_tools_enabled is True

    def test_primitive_tools_flag_false_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENT_PRIMITIVE_TOOLS_ENABLED=false keeps the flag off."""
        monkeypatch.setenv("AGENT_PRIMITIVE_TOOLS_ENABLED", "false")
        config = AppConfig()
        assert config.primitive_tools_enabled is False


class TestApprovalUIFlag:
    """approval_ui_enabled is off by default."""

    def test_approval_ui_disabled_by_default(self) -> None:
        """AGENT_APPROVAL_UI_ENABLED defaults to False."""
        assert settings.approval_ui_enabled is False

    def test_approval_ui_reads_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENT_APPROVAL_UI_ENABLED=true sets the flag."""
        monkeypatch.setenv("AGENT_APPROVAL_UI_ENABLED", "true")
        config = AppConfig()
        assert config.approval_ui_enabled is True


class TestSandboxDefaults:
    """Sandbox-related settings have correct defaults."""

    def test_sandbox_scratch_root_default(self) -> None:
        """sandbox_scratch_root default contains 'agent_workspace'."""
        assert "agent_workspace" in settings.sandbox_scratch_root

    def test_sandbox_image_default(self) -> None:
        """sandbox_image default is the expected tag."""
        assert settings.sandbox_image == "seshat-sandbox-python:0.1"

    def test_sandbox_scratch_root_reads_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENT_SANDBOX_SCRATCH_ROOT is read from environment."""
        monkeypatch.setenv("AGENT_SANDBOX_SCRATCH_ROOT", "/custom/scratch")
        config = AppConfig()
        assert config.sandbox_scratch_root == "/custom/scratch"

    def test_sandbox_image_reads_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENT_SANDBOX_IMAGE is read from environment."""
        monkeypatch.setenv("AGENT_SANDBOX_IMAGE", "custom-sandbox:latest")
        config = AppConfig()
        assert config.sandbox_image == "custom-sandbox:latest"


class TestApprovalTimeoutDefault:
    """approval_timeout_seconds has the correct default."""

    def test_approval_timeout_default(self) -> None:
        """approval_timeout_seconds defaults to 60.0."""
        assert settings.approval_timeout_seconds == 60.0

    def test_approval_timeout_reads_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENT_APPROVAL_TIMEOUT_SECONDS is read from environment."""
        monkeypatch.setenv("AGENT_APPROVAL_TIMEOUT_SECONDS", "120.0")
        config = AppConfig()
        assert config.approval_timeout_seconds == 120.0
