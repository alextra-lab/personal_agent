# Brainstem

Autonomic control layer - manages operational modes and transitions.

**Spec**: `../../docs/architecture/BRAINSTEM_SERVICE_v0.1.md`
**Model**: `../../docs/architecture/HOMEOSTASIS_MODEL.md`

## Responsibilities

- Maintain current operational mode state
- Evaluate sensor data for mode transitions
- Apply mode transition rules
- Emit telemetry for all mode changes

## Structure

```
brainstem/
├── __init__.py          # Exports: ModeManager, get_current_mode, ThresholdOptimizer
├── mode_manager.py      # ModeManager (state machine)
├── scheduler.py         # Second brain consolidation + lifecycle (Phase 2.2/2.3)
├── optimizer.py         # Adaptive threshold tuning (Phase 2.3, FRE-11)
└── sensors.py           # Sensor polling
```

## Get Current Mode

```python
from personal_agent.brainstem import get_current_mode, OperationalMode

mode = get_current_mode()

if mode == OperationalMode.LOCKDOWN:
    raise OperationBlockedError("System in LOCKDOWN mode")
```

## Mode Transitions

```python
from personal_agent.brainstem import ModeManager

mode_mgr = ModeManager(config)

sensor_data = {
    "cpu_percent": 90.0,
    "memory_percent": 75.0,
    "error_rate": 0.02,
}

mode_mgr.check_transition(sensor_data)  # May change mode
```

## Modes

| Mode | Behavior | Trigger |
|------|----------|---------|
| NORMAL | Full capabilities | Default, system healthy |
| ALERT | Increased scrutiny | CPU >85%, error rate >5% |
| DEGRADED | Reduced load | CPU >95%, persistent issues |
| LOCKDOWN | Analysis only | Security threat, critical failure |
| RECOVERY | Self-checks | Post-LOCKDOWN restoration |

See `../../docs/architecture/HOMEOSTASIS_MODEL.md` for transition diagram.

## Adaptive Threshold Tuning (Phase 2.3, FRE-11)

`ThresholdOptimizer` analyzes ES telemetry and proposes data-backed threshold changes:

```python
from personal_agent.brainstem import ThresholdOptimizer

optimizer = ThresholdOptimizer()
analysis = await optimizer.analyze_resource_patterns(days=7)
report = await optimizer.detect_false_positives()
proposal = await optimizer.propose_threshold_adjustment("cpu_threshold")
result = await optimizer.run_ab_test(proposal)
# proposal.captains_log_payload for Captain's Log config_proposal entry
```

Metrics: `cpu_threshold`, `memory_threshold`, `idle_time_seconds`, `min_consolidation_interval_seconds`. Depends on `telemetry.queries.TelemetryQueries`.

## Dependencies

- `governance`: Mode configuration (thresholds)
- `telemetry`: Mode transition logging
- `psutil`: System sensor polling (optional)

## Search

```bash
rg -n "get_current_mode|check_mode" src/
rg -n "mode_transition|check_transition" src/
rg -n "OperationalMode\.(NORMAL|ALERT|DEGRADED|LOCKDOWN|RECOVERY)" src/
```

## Critical

- Only ModeManager changes mode - **immutable** elsewhere
- Log all transitions with reason
- **Never** set mode directly - use `check_transition`
- Validate transitions are allowed before applying

## Testing

- Test mode transition logic (valid and invalid)
- Test sensor threshold evaluation
- Test mode affects governance permissions
- Test LOCKDOWN → RECOVERY → NORMAL flow
- Test ThresholdOptimizer analysis and proposals (`tests/test_brainstem/test_optimizer.py`)

## Pre-PR

```bash
pytest tests/test_brainstem/ -v
mypy src/personal_agent/brainstem/
ruff check src/personal_agent/brainstem/
```
