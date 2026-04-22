"""Tests for Stage 3: Governance."""

import pytest

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.governance import evaluate_governance
from personal_agent.request_gateway.types import GovernanceContext, TaskType


class TestEvaluateGovernance:
    """Tests for evaluate_governance function."""

    def test_normal_mode_permits_expansion(self) -> None:
        """Verify NORMAL mode permits expansion."""
        result = evaluate_governance(mode=Mode.NORMAL)
        assert result.mode == Mode.NORMAL
        assert result.expansion_permitted is True

    def test_alert_mode_disables_expansion(self) -> None:
        """Verify ALERT mode disables expansion."""
        result = evaluate_governance(mode=Mode.ALERT)
        assert result.expansion_permitted is False

    def test_degraded_mode_disables_expansion(self) -> None:
        """Verify DEGRADED mode disables expansion."""
        result = evaluate_governance(mode=Mode.DEGRADED)
        assert result.expansion_permitted is False

    def test_lockdown_mode_disables_expansion(self) -> None:
        """Verify LOCKDOWN mode disables expansion."""
        result = evaluate_governance(mode=Mode.LOCKDOWN)
        assert result.expansion_permitted is False

    def test_recovery_mode_disables_expansion(self) -> None:
        """Verify RECOVERY mode disables expansion."""
        result = evaluate_governance(mode=Mode.RECOVERY)
        assert result.expansion_permitted is False

    def test_default_mode_is_normal(self) -> None:
        """Verify default mode is NORMAL when no mode is provided."""
        result = evaluate_governance()
        assert result.mode == Mode.NORMAL

    def test_returns_governance_context_type(self) -> None:
        """Verify return type is GovernanceContext."""
        result = evaluate_governance()
        assert isinstance(result, GovernanceContext)

    def test_normal_mode_result_is_frozen(self) -> None:
        """Verify GovernanceContext is immutable (frozen dataclass)."""
        result = evaluate_governance(mode=Mode.NORMAL)
        with pytest.raises(AttributeError):
            result.expansion_permitted = False  # type: ignore[misc]


class TestEvaluateGovernanceTaskTypePolicy:
    """Tests for per-TaskType tool allowlist (FRE-252)."""

    def test_no_task_type_returns_none_categories(self) -> None:
        """Backward compat: omitting task_type keeps allowed_tool_categories None."""
        result = evaluate_governance(mode=Mode.NORMAL)
        assert result.allowed_tool_categories is None

    def test_conversational_task_type_returns_empty_categories(self) -> None:
        """CONVERSATIONAL tasks need no tools."""
        result = evaluate_governance(mode=Mode.NORMAL, task_type=TaskType.CONVERSATIONAL)
        assert result.allowed_tool_categories == []

    def test_memory_recall_task_type_returns_read_only(self) -> None:
        """MEMORY_RECALL tasks only need read_only category."""
        result = evaluate_governance(mode=Mode.NORMAL, task_type=TaskType.MEMORY_RECALL)
        assert result.allowed_tool_categories is not None
        assert set(result.allowed_tool_categories) == {"read_only"}

    def test_tool_use_normal_mode_returns_broad_categories(self) -> None:
        """TOOL_USE in NORMAL mode allows read_only, network, system_write."""
        result = evaluate_governance(mode=Mode.NORMAL, task_type=TaskType.TOOL_USE)
        assert result.allowed_tool_categories is not None
        assert "read_only" in result.allowed_tool_categories
        assert "network" in result.allowed_tool_categories
        assert "system_write" in result.allowed_tool_categories

    def test_degraded_mode_narrows_tool_use_categories(self) -> None:
        """DEGRADED mode intersects with TOOL_USE to remove system_write and network."""
        result = evaluate_governance(mode=Mode.DEGRADED, task_type=TaskType.TOOL_USE)
        assert result.allowed_tool_categories is not None
        assert "system_write" not in result.allowed_tool_categories
        assert "network" not in result.allowed_tool_categories
        assert "read_only" in result.allowed_tool_categories

    def test_lockdown_mode_restricts_all_task_types_to_health_check(self) -> None:
        """LOCKDOWN mode allows only essential_health_check regardless of task type."""
        result = evaluate_governance(mode=Mode.LOCKDOWN, task_type=TaskType.TOOL_USE)
        assert result.allowed_tool_categories is not None
        assert "system_write" not in result.allowed_tool_categories
        assert "network" not in result.allowed_tool_categories
        assert "read_only" not in result.allowed_tool_categories
