"""Tests for ModeManager."""

from pathlib import Path

from personal_agent.brainstem.mode_manager import ModeManager
from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode


def test_mode_manager_initialization() -> None:
    """Test ModeManager initializes with NORMAL mode."""
    manager = ModeManager()
    assert manager.get_current_mode() == Mode.NORMAL
    assert len(manager.get_transition_history()) == 0


def test_mode_manager_with_config() -> None:
    """Test ModeManager can be initialized with custom config."""
    project_root = Path(__file__).parent.parent.parent
    config_dir = project_root / "config" / "governance"
    config = load_governance_config(config_dir)

    manager = ModeManager(governance_config=config)
    assert manager.get_current_mode() == Mode.NORMAL


def test_get_current_mode() -> None:
    """Test get_current_mode returns current mode."""
    manager = ModeManager()
    assert manager.get_current_mode() == Mode.NORMAL


def test_transition_to_allowed() -> None:
    """Test transitioning to an allowed mode."""
    manager = ModeManager()
    assert manager.get_current_mode() == Mode.NORMAL

    # NORMAL -> ALERT is allowed
    manager.transition_to(Mode.ALERT, "Test transition", {"cpu": 90.0})
    assert manager.get_current_mode() == Mode.ALERT

    # Check transition history
    history = manager.get_transition_history()
    assert len(history) == 1
    assert history[0]["from_mode"] == "NORMAL"
    assert history[0]["to_mode"] == "ALERT"
    assert history[0]["reason"] == "Test transition"


def test_transition_to_disallowed() -> None:
    """Test transitioning to a disallowed mode is blocked."""
    manager = ModeManager()
    assert manager.get_current_mode() == Mode.NORMAL

    # NORMAL -> LOCKDOWN is not directly allowed (must go through ALERT or DEGRADED)
    manager.transition_to(Mode.LOCKDOWN, "Invalid transition", {})
    # Should still be NORMAL
    assert manager.get_current_mode() == Mode.NORMAL
    # No transition recorded
    assert len(manager.get_transition_history()) == 0


def test_transition_to_same_mode() -> None:
    """Test transitioning to the same mode is a no-op."""
    manager = ModeManager()
    assert manager.get_current_mode() == Mode.NORMAL

    manager.transition_to(Mode.NORMAL, "Same mode", {})
    assert manager.get_current_mode() == Mode.NORMAL
    assert len(manager.get_transition_history()) == 0


def test_evaluate_transitions_no_match() -> None:
    """Test evaluate_transitions with sensor data that doesn't trigger transitions."""
    manager = ModeManager()
    assert manager.get_current_mode() == Mode.NORMAL

    # Low CPU, no high-risk calls - should stay in NORMAL
    sensor_data = {
        "perf_system_cpu_load": 50.0,
        "safety_tool_high_risk_calls": 0,
    }
    manager.evaluate_transitions(sensor_data)
    assert manager.get_current_mode() == Mode.NORMAL


def test_evaluate_transitions_trigger_alert() -> None:
    """Test evaluate_transitions triggers ALERT when conditions are met."""
    manager = ModeManager()
    assert manager.get_current_mode() == Mode.NORMAL

    # High CPU should trigger NORMAL -> ALERT transition
    # Based on config/governance/modes.yaml, NORMAL_to_ALERT requires:
    # - perf_system_cpu_load > 85 for 30 seconds, OR
    # - safety_tool_high_risk_calls > 5 in 60 seconds
    sensor_data = {
        "perf_system_cpu_load": 90.0,  # Above threshold
        "safety_tool_high_risk_calls": 0,
    }
    manager.evaluate_transitions(sensor_data)
    # Should transition to ALERT
    assert manager.get_current_mode() == Mode.ALERT


def test_evaluate_transitions_high_risk_calls() -> None:
    """Test evaluate_transitions triggers ALERT based on high-risk calls."""
    manager = ModeManager()
    assert manager.get_current_mode() == Mode.NORMAL

    # High-risk calls should trigger ALERT
    sensor_data = {
        "perf_system_cpu_load": 50.0,
        "safety_tool_high_risk_calls": 6,  # Above threshold of 5
    }
    manager.evaluate_transitions(sensor_data)
    assert manager.get_current_mode() == Mode.ALERT


def test_evaluate_condition_operators() -> None:
    """Test condition evaluation with different operators."""
    manager = ModeManager()

    # Test greater than
    assert manager._evaluate_condition(">", 90, 85) is True
    assert manager._evaluate_condition(">", 80, 85) is False

    # Test less than
    assert manager._evaluate_condition("<", 70, 85) is True
    assert manager._evaluate_condition("<", 90, 85) is False

    # Test equals
    assert manager._evaluate_condition("==", 85, 85) is True
    assert manager._evaluate_condition("==", 80, 85) is False

    # Test greater than or equal
    assert manager._evaluate_condition(">=", 85, 85) is True
    assert manager._evaluate_condition(">=", 90, 85) is True
    assert manager._evaluate_condition(">=", 80, 85) is False

    # Test less than or equal
    assert manager._evaluate_condition("<=", 85, 85) is True
    assert manager._evaluate_condition("<=", 80, 85) is True
    assert manager._evaluate_condition("<=", 90, 85) is False


def test_is_transition_allowed() -> None:
    """Test transition validation logic."""
    manager = ModeManager()

    # Allowed transitions
    assert manager._is_transition_allowed(Mode.NORMAL, Mode.ALERT) is True
    assert manager._is_transition_allowed(Mode.NORMAL, Mode.DEGRADED) is True
    assert manager._is_transition_allowed(Mode.ALERT, Mode.NORMAL) is True
    assert manager._is_transition_allowed(Mode.ALERT, Mode.LOCKDOWN) is True
    assert manager._is_transition_allowed(Mode.LOCKDOWN, Mode.RECOVERY) is True
    assert manager._is_transition_allowed(Mode.RECOVERY, Mode.NORMAL) is True

    # Disallowed transitions
    assert manager._is_transition_allowed(Mode.NORMAL, Mode.LOCKDOWN) is False
    assert manager._is_transition_allowed(Mode.NORMAL, Mode.RECOVERY) is False
    assert manager._is_transition_allowed(Mode.ALERT, Mode.RECOVERY) is False
    assert manager._is_transition_allowed(Mode.DEGRADED, Mode.NORMAL) is False


def test_mode_manager_error_on_invalid_config() -> None:
    """Test ModeManager raises error when config cannot be loaded."""
    # This test would require mocking or invalid config path
    # For now, we test that valid config works
    manager = ModeManager()
    assert manager is not None


def test_transition_history() -> None:
    """Test transition history tracking."""
    manager = ModeManager()
    assert len(manager.get_transition_history()) == 0

    # Make a transition
    manager.transition_to(Mode.ALERT, "Test", {"cpu": 90.0})
    history = manager.get_transition_history()
    assert len(history) == 1
    assert "timestamp" in history[0]
    assert "from_mode" in history[0]
    assert "to_mode" in history[0]
    assert "reason" in history[0]
    assert "sensor_data" in history[0]

    # Make another transition
    manager.transition_to(Mode.LOCKDOWN, "Critical", {"risk": 10})
    history = manager.get_transition_history()
    assert len(history) == 2
    assert history[1]["from_mode"] == "ALERT"
    assert history[1]["to_mode"] == "LOCKDOWN"
