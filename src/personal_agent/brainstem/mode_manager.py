"""Mode manager for operational mode state machine.

This module implements the ModeManager class that maintains the current
operational mode and evaluates transitions based on sensor data and
governance configuration.
"""

from datetime import datetime, timezone
from typing import Any

from personal_agent.config.governance_loader import GovernanceConfigError, load_governance_config
from personal_agent.governance.models import GovernanceConfig, Mode, TransitionRule
from personal_agent.telemetry import MODE_TRANSITION, get_logger

log = get_logger(__name__)


class ModeManagerError(Exception):
    """Raised when mode manager operations fail."""

    pass


class ModeManager:
    """Manages operational mode state machine.

    The ModeManager is the sole authority for operational mode transitions.
    It evaluates sensor data against transition rules and maintains the
    current mode state.

    Attributes:
        current_mode: Current operational mode.
        governance_config: Loaded governance configuration.
        _transition_history: History of mode transitions for debugging.
    """

    def __init__(self, governance_config: GovernanceConfig | None = None) -> None:
        """Initialize mode manager.

        Args:
            governance_config: Optional governance configuration.
                If None, loads from default config path.

        Raises:
            ModeManagerError: If configuration cannot be loaded.
        """
        if governance_config is None:
            try:
                self.governance_config = load_governance_config()
            except GovernanceConfigError as e:
                raise ModeManagerError(f"Failed to load governance config: {e}") from None
        else:
            self.governance_config = governance_config

        # Start in NORMAL mode
        self.current_mode = Mode.NORMAL
        self._transition_history: list[dict[str, Any]] = []

        log.info(
            "mode_manager_initialized",
            initial_mode=self.current_mode.value,
            modes_available=list(self.governance_config.modes.keys()),
        )

    def get_current_mode(self) -> Mode:
        """Get current operational mode.

        Returns:
            Current operational mode.
        """
        return self.current_mode

    def evaluate_transitions(self, sensor_data: dict[str, Any]) -> None:
        """Evaluate sensor data and transition modes if needed.

        Checks all transition rules against current sensor data and
        transitions to a new mode if conditions are met.

        Args:
            sensor_data: Dictionary of sensor metrics keyed by metric name.
                Example: {"perf_system_cpu_load": 90.0, "safety_tool_high_risk_calls": 3}
        """
        # Check all transition rules
        for rule_name, rule in self.governance_config.transition_rules.items():
            # Parse source and target mode from rule name (e.g., "NORMAL_to_ALERT")
            parts = rule_name.split("_to_")
            if len(parts) != 2:
                log.warning("invalid_transition_rule_name", rule_name=rule_name)
                continue

            source_mode_str, target_mode_str = parts
            try:
                source_mode = Mode(source_mode_str)
                target_mode = Mode(target_mode_str)
            except ValueError:
                log.warning(
                    "invalid_mode_in_rule",
                    rule_name=rule_name,
                    source=source_mode_str,
                    target=target_mode_str,
                )
                continue

            # Only evaluate rules that apply to current mode
            if self.current_mode != source_mode:
                continue

            # Check if transition conditions are met
            if self._check_transition_rule(rule, sensor_data):
                # Transition to new mode
                reason = f"Transition rule '{rule_name}' conditions met"
                self.transition_to(target_mode, reason, sensor_data)
                return  # Only one transition per evaluation

    def _check_transition_rule(self, rule: TransitionRule, sensor_data: dict[str, Any]) -> bool:
        """Check if a transition rule's conditions are met.

        Args:
            rule: Transition rule to check.
            sensor_data: Current sensor data.

        Returns:
            True if rule conditions are met, False otherwise.
        """
        condition_results: list[bool] = []

        for condition in rule.conditions:
            metric_value = sensor_data.get(condition.metric)
            if metric_value is None:
                # Metric not available, condition fails
                condition_results.append(False)
                continue

            # Evaluate condition based on operator
            result = self._evaluate_condition(condition.operator, metric_value, condition.value)
            condition_results.append(result)

        # Apply logic (any vs all)
        if rule.logic == "any":
            return any(condition_results)
        elif rule.logic == "all":
            return all(condition_results)
        else:
            log.warning("unknown_transition_logic", logic=rule.logic)
            return False

    def _evaluate_condition(
        self, operator: str, metric_value: float | int, threshold: float | int
    ) -> bool:
        """Evaluate a single condition.

        Args:
            operator: Comparison operator (>, <, ==, >=, <=).
            metric_value: Current metric value.
            threshold: Threshold value to compare against.

        Returns:
            True if condition is met, False otherwise.
        """
        match operator:
            case ">":
                return metric_value > threshold
            case "<":
                return metric_value < threshold
            case "==":
                return metric_value == threshold
            case ">=":
                return metric_value >= threshold
            case "<=":
                return metric_value <= threshold
            case _:
                log.warning("unknown_operator", operator=operator)
                return False

    def transition_to(
        self, new_mode: Mode, reason: str, sensor_data: dict[str, Any] | None = None
    ) -> None:
        """Transition to a new operational mode.

        This is the authoritative method for mode transitions. It logs
        the transition and updates internal state.

        Args:
            new_mode: Target mode to transition to.
            reason: Human-readable reason for the transition.
            sensor_data: Optional sensor data that triggered the transition.
        """
        if new_mode == self.current_mode:
            # Already in this mode, no-op
            return

        # Validate transition is allowed (basic state machine validation)
        if not self._is_transition_allowed(self.current_mode, new_mode):
            log.warning(
                "transition_not_allowed",
                from_mode=self.current_mode.value,
                to_mode=new_mode.value,
                reason=reason,
            )
            return

        old_mode = self.current_mode
        self.current_mode = new_mode

        # Record transition history
        transition_record = {
            "timestamp": datetime.now(timezone.utc),
            "from_mode": old_mode.value,
            "to_mode": new_mode.value,
            "reason": reason,
            "sensor_data": sensor_data or {},
        }
        self._transition_history.append(transition_record)

        # Emit telemetry
        log.info(
            MODE_TRANSITION,
            from_mode=old_mode.value,
            to_mode=new_mode.value,
            reason=reason,
            sensor_data=sensor_data or {},
        )

    def _is_transition_allowed(self, from_mode: Mode, to_mode: Mode) -> bool:
        """Check if a mode transition is allowed.

        Validates transitions based on the state machine defined in
        HOMEOSTASIS_MODEL.md.

        Args:
            from_mode: Source mode.
            to_mode: Target mode.

        Returns:
            True if transition is allowed, False otherwise.
        """
        # Define allowed transitions based on state machine
        allowed_transitions: dict[Mode, set[Mode]] = {
            Mode.NORMAL: {Mode.ALERT, Mode.DEGRADED},
            Mode.ALERT: {Mode.NORMAL, Mode.DEGRADED, Mode.LOCKDOWN},
            Mode.DEGRADED: {Mode.LOCKDOWN},
            Mode.LOCKDOWN: {Mode.RECOVERY},
            Mode.RECOVERY: {Mode.NORMAL},
        }

        allowed_targets = allowed_transitions.get(from_mode, set())
        return to_mode in allowed_targets

    def get_transition_history(self) -> list[dict[str, Any]]:
        """Get history of mode transitions.

        Returns:
            List of transition records with timestamp, from_mode, to_mode, reason.
        """
        return self._transition_history.copy()
