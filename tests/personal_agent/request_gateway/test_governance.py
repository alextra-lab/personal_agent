"""Tests for Stage 3: Governance."""

import pytest

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.governance import evaluate_governance
from personal_agent.request_gateway.types import GovernanceContext


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


class TestGovernanceContextDeprecatedField:
    """Verify allowed_tool_categories is deprecated and always None (ADR-0063 §D1)."""

    def test_allowed_tool_categories_always_none(self) -> None:
        """TaskType→tool-filter wire severed in FRE-260: field is always None."""
        result = evaluate_governance(mode=Mode.NORMAL)
        assert result.allowed_tool_categories is None

    def test_allowed_tool_categories_none_in_lockdown(self) -> None:
        """Even in LOCKDOWN mode the deprecated field is None (mode gate is elsewhere)."""
        result = evaluate_governance(mode=Mode.LOCKDOWN)
        assert result.allowed_tool_categories is None
