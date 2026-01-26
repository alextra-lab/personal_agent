"""Integration tests for brainstem module."""

from pathlib import Path

from personal_agent.brainstem import (
    ModeManager,
    get_current_mode,
    get_mode_manager,
    get_system_metrics_snapshot,
    poll_system_metrics,
)
from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode


def test_get_current_mode() -> None:
    """Test get_current_mode public API."""
    mode = get_current_mode()
    assert mode in Mode
    assert mode == Mode.NORMAL  # Should start in NORMAL


def test_get_mode_manager_singleton() -> None:
    """Test get_mode_manager returns singleton instance."""
    manager1 = get_mode_manager()
    manager2 = get_mode_manager()

    # Should be the same instance
    assert manager1 is manager2


def test_mode_manager_with_sensor_data() -> None:
    """Test ModeManager evaluates transitions with real sensor data."""
    manager = ModeManager()
    assert manager.get_current_mode() == Mode.NORMAL

    # Poll sensors
    sensor_data = poll_system_metrics()

    # Evaluate transitions (may or may not trigger depending on system state)
    manager.evaluate_transitions(sensor_data)

    # Mode should still be valid
    current_mode = manager.get_current_mode()
    assert current_mode in Mode


def test_full_workflow() -> None:
    """Test full workflow: poll sensors, evaluate, check mode."""
    # Get mode manager
    manager = get_mode_manager()

    # Poll system metrics
    sensor_data = poll_system_metrics()

    # Evaluate transitions
    manager.evaluate_transitions(sensor_data)

    # Get current mode
    mode = get_current_mode()
    assert mode in Mode

    # Get detailed snapshot
    snapshot = get_system_metrics_snapshot()
    assert isinstance(snapshot, dict)


def test_mode_manager_with_custom_config() -> None:
    """Test ModeManager works with custom governance config."""
    project_root = Path(__file__).parent.parent.parent
    config_dir = project_root / "config" / "governance"
    config = load_governance_config(config_dir)

    manager = ModeManager(governance_config=config)
    assert manager.get_current_mode() == Mode.NORMAL

    # Test transition evaluation
    sensor_data = {
        "perf_system_cpu_load": 90.0,
        "safety_tool_high_risk_calls": 0,
    }
    manager.evaluate_transitions(sensor_data)

    # Should transition to ALERT
    assert manager.get_current_mode() == Mode.ALERT
