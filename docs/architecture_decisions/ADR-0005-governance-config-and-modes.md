# ADR-0005: Governance Configuration & Operational Modes

**Status:** Proposed
**Date:** 2025-12-28
**Decision Owner:** Project Owner

---

## 1. Context

The Personal Local AI Collaborator is designed to operate under **explicit governance** that adapts to system conditions and the project owner's preferences. The system must:

1. **Enforce operational modes** that regulate autonomy, risk tolerance, and resource usage
2. **Gate tool access** based on mode, risk level, and context
3. **Control model behavior** (which roles allowed, temperature limits, token budgets)
4. **Support human approval workflows** for high-risk operations
5. **Enable mode transitions** driven by the Brainstem Service based on sensor readings
6. **Remain inspectable and auditable**: policies must be readable, versionable, and testable

Multiple components depend on governance:

- **Brainstem Service**: Decides current operational mode and emits control signals
- **Orchestrator Core**: Enforces mode constraints when planning and executing tasks
- **Local LLM Client**: Applies mode-specific limits (max tokens, temperature, allowed roles)
- **Tool Execution Layer**: Checks tool permissions and risk assessments
- **Safety Gates**: Applies mode-aware policies for output filtering and action blocking

The governance model must be:

- **Configuration-driven**, not hard-coded
- **Evolvable** via structured proposals and human approval (Captain's Log integration)
- **Explainable**: every constraint must have a rationale
- **Testable**: policies can be validated in isolation

This ADR defines:

- Operational mode definitions and semantics
- Policy representation format
- Runtime enforcement architecture
- Configuration file structure
- Evolution and approval workflow

---

## 2. Decision

### 2.1 Operational Modes (State Machine)

We define **five operational modes** that form a state machine managed by the Brainstem Service:

| Mode | Semantics | Tool Access | Model Constraints | Human Approval | Use Case |
|------|-----------|-------------|-------------------|----------------|----------|
| **NORMAL** | Default healthy operation | All allowed tools | Standard limits | Optional for high-risk | Day-to-day collaboration |
| **ALERT** | System under stress or elevated risk | High-risk tools require approval | Reduced max tokens, lower temp | Required for high-risk | Anomalies detected, resource pressure |
| **DEGRADED** | Partial system failure or overload | Essential tools only | Smaller models, aggressive limits | Required for most actions | Model server down, high CPU, errors |
| **LOCKDOWN** | Critical safety event | Read-only tools only | Minimal inference | Required for ALL actions | Security threat, repeated policy violations |
| **RECOVERY** | Stabilizing after incident | Gradually restored based on checks | Conservative limits | Required initially | Post-lockdown, self-checks running |

#### Mode Transition Rules (Enforced by Brainstem)

```
[*] --> NORMAL

NORMAL --> ALERT:
  - CPU > 85% sustained 30s
  - High-risk tool call rate > 5/min
  - Model error rate > 20%
  - 3+ policy violations in 10min

ALERT --> NORMAL:
  - All triggers below threshold for 2min
  - Human approval

NORMAL --> DEGRADED:
  - Model server unavailable
  - Memory pressure critical
  - Disk usage > 95%

ALERT --> DEGRADED:
  - Multiple failure signals
  - Cascading errors

ALERT --> LOCKDOWN:
  - Security anomaly detected
  - Data exfiltration pattern
  - Repeated dangerous action attempts (>5)

DEGRADED --> LOCKDOWN:
  - Safety-critical failure

LOCKDOWN --> RECOVERY:
  - Human approval
  - Basic self-checks pass

RECOVERY --> NORMAL:
  - Extended self-checks pass
  - No alerts for 10min
  - Human approval
```

These thresholds are **configurable** (see below) and can be tuned experimentally.

---

### 2.2 Policy Representation: YAML Configuration Files

Governance policies are represented as **YAML files** in `config/governance/`:

```
config/
  governance/
    modes.yaml          # Mode definitions and transition thresholds
    tools.yaml          # Tool permissions and risk classifications
    models.yaml         # Model constraints per mode
    safety.yaml         # Content filtering, output policies, rate limits
```

#### Example: `config/governance/modes.yaml`

```yaml
modes:
  NORMAL:
    description: "Default healthy operation"
    max_concurrent_tasks: 5
    background_monitoring_enabled: true
    thresholds:
      cpu_load_percent: 85
      memory_used_percent: 80
      tool_error_rate: 0.15
      policy_violations_per_10min: 3

  ALERT:
    description: "Elevated risk or resource pressure"
    max_concurrent_tasks: 3
    background_monitoring_enabled: true
    require_approval_for:
      - "high_risk_tools"
      - "config_changes"
    thresholds:
      # More aggressive thresholds for escalation
      cpu_load_percent: 90
      repeated_high_risk_calls: 5

  DEGRADED:
    description: "Partial failure, limited capabilities"
    max_concurrent_tasks: 1
    background_monitoring_enabled: false
    allowed_tool_categories:
      - "read_only"
      - "essential_health_check"
    require_approval_for:
      - "all_actions"  # Except essential monitoring

  LOCKDOWN:
    description: "Critical safety event, minimal operation"
    max_concurrent_tasks: 0
    background_monitoring_enabled: false
    allowed_tool_categories:
      - "read_only"
    require_approval_for:
      - "everything"

  RECOVERY:
    description: "Stabilizing after incident"
    max_concurrent_tasks: 2
    background_monitoring_enabled: true
    allowed_tool_categories:
      - "read_only"
      - "self_check"
      - "health_check"
    require_approval_for:
      - "high_risk_tools"
      - "any_write_operation"

transition_rules:
  NORMAL_to_ALERT:
    conditions:
      - metric: "perf_system_cpu_load"
        operator: ">"
        value: 85
        duration_seconds: 30
      - metric: "safety_tool_high_risk_calls"
        operator: ">"
        value: 5
        window_seconds: 60
    logic: "any"  # Any condition triggers transition

  ALERT_to_NORMAL:
    conditions:
      - metric: "perf_system_cpu_load"
        operator: "<"
        value: 70
        duration_seconds: 120
      - metric: "safety_policy_violations"
        operator: "=="
        value: 0
        window_seconds: 300
    logic: "all"  # All conditions must be met
    requires_human_approval: true
```

#### Example: `config/governance/tools.yaml`

```yaml
tool_categories:
  read_only:
    description: "Tools that only observe, never modify"
    risk_level: "low"
    examples: ["read_file", "list_directory", "system_metrics_snapshot"]

  system_write:
    description: "Tools that modify system state"
    risk_level: "high"
    requires_approval_in_modes: ["ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"]
    examples: ["write_file", "execute_shell_command", "modify_config"]

  network:
    description: "Tools that access the network"
    risk_level: "medium"
    requires_outbound_gateway: true
    examples: ["web_search", "http_request"]

  essential_health_check:
    description: "Minimal health checks, always allowed"
    risk_level: "low"
    examples: ["check_disk_space", "check_process_health"]

tools:
  read_file:
    category: "read_only"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"]
    max_file_size_mb: 10

  write_file:
    category: "system_write"
    allowed_in_modes: ["NORMAL"]
    requires_approval_in_modes: ["ALERT", "DEGRADED", "RECOVERY"]
    forbidden_in_modes: ["LOCKDOWN"]
    allowed_paths:
      - "$HOME/Dev/personal_agent/**"
      - "$HOME/Documents/agent_workspace/**"
    forbidden_paths:
      - "/System/**"
      - "/Library/**"
      - "$HOME/.ssh/**"

  execute_shell_command:
    category: "system_write"
    allowed_in_modes: ["NORMAL"]
    requires_approval: true  # Always requires approval
    allowed_commands:
      - "git"
      - "ls"
      - "cat"
      - "grep"
    forbidden_commands:
      - "rm -rf"
      - "sudo"
      - "curl"  # Use web_search tool instead

  web_search:
    category: "network"
    allowed_in_modes: ["NORMAL", "ALERT"]
    requires_outbound_gateway: true
    rate_limit_per_hour: 100
```

#### Example: `config/governance/models.yaml`

```yaml
# Model behavior constraints per mode
mode_constraints:
  NORMAL:
    allowed_roles: ["router", "reasoning", "coding"]
    max_tokens:
      router: 1024
      reasoning: 8192
      coding: 8192
    temperature:
      router: 0.3
      reasoning: 0.7
      coding: 0.5
    timeout_seconds:
      router: 10
      reasoning: 120
      coding: 90

  ALERT:
    allowed_roles: ["router", "reasoning"]  # No coding in ALERT
    max_tokens:
      router: 512
      reasoning: 4096
    temperature:
      router: 0.2
      reasoning: 0.5  # More conservative
    timeout_seconds:
      router: 8
      reasoning: 60

  DEGRADED:
    allowed_roles: ["router"]  # Only fast router model
    max_tokens:
      router: 512
    temperature:
      router: 0.1  # Very deterministic
    timeout_seconds:
      router: 5

  LOCKDOWN:
    allowed_roles: []  # No LLM inference allowed
    # Exception: supervisor model for analysis, if separate

  RECOVERY:
    allowed_roles: ["router", "reasoning"]
    max_tokens:
      router: 512
      reasoning: 2048
    temperature:
      router: 0.2
      reasoning: 0.4
    timeout_seconds:
      router: 10
      reasoning: 45
```

#### Example: `config/governance/safety.yaml`

```yaml
content_filtering:
  enabled: true
  secret_patterns:
    - regex: "-----BEGIN .* PRIVATE KEY-----"
      action: "block"
      redaction: "[REDACTED: PRIVATE KEY]"
    - regex: "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Z|a-z]{2,}"
      action: "warn"  # Warn but don't block
    - regex: "sk-[a-zA-Z0-9]{32,}"  # API keys
      action: "block"
      redaction: "[REDACTED: API KEY]"

  forbidden_content_patterns:
    - pattern: "delete_all"
      context: "shell_command"
      action: "block"
    - pattern: "rm -rf /"
      context: "shell_command"
      action: "block"

outbound_gateway:
  enabled: true
  allowed_domains:
    - "*.wikipedia.org"
    - "*.stackexchange.com"
    - "docs.python.org"
    - "github.com"
  blocked_domains:
    - "*.onion"
  require_approval_for_new_domains: true
  max_request_size_kb: 1024

rate_limits:
  per_mode:
    NORMAL:
      tool_calls_per_minute: 30
      llm_calls_per_minute: 60
      outbound_requests_per_hour: 100
    ALERT:
      tool_calls_per_minute: 15
      llm_calls_per_minute: 30
      outbound_requests_per_hour: 20
    DEGRADED:
      tool_calls_per_minute: 5
      llm_calls_per_minute: 10
      outbound_requests_per_hour: 0

human_approval:
  timeout_seconds: 300  # 5 minutes before expiring approval request
  require_approval_for:
    - category: "system_write"
      modes: ["ALERT", "DEGRADED", "RECOVERY"]
    - category: "network"
      modes: ["ALERT"]
    - risk_level: "high"
      modes: ["NORMAL", "ALERT", "DEGRADED", "RECOVERY"]
```

---

### 2.3 Runtime Enforcement Architecture

#### Brainstem: Mode Authority

- **Owns current mode state**: Maintains `current_mode: Mode` enum
- **Evaluates transition rules**: Polls sensors, checks thresholds, emits `mode_transition` events
- **Publishes mode**: Exposes read-only API `get_current_mode() -> Mode`
- **Emits control signals**: Publishes constraints (e.g., max concurrent tasks, allowed tools) derived from mode config

#### Orchestrator: Enforces Mode Constraints

Before executing any workflow:

1. Query `brainstem.get_current_mode()`
2. Load mode constraints from `config/governance/modes.yaml`
3. Filter available tools based on `allowed_in_modes` and `forbidden_in_modes`
4. Apply concurrency limits (`max_concurrent_tasks`)
5. If action requires approval, pause and request human confirmation via UI

During execution:

- Pass mode context to Local LLM Client
- Enforce tool permissions before invocation
- Check rate limits via a simple in-memory counter (reset per time window)

#### Local LLM Client: Applies Model Constraints

Before making model calls:

1. Check `mode_constraints[current_mode][allowed_roles]`
2. If requested `role` not allowed, raise error or fallback to allowed role
3. Apply `max_tokens`, `temperature`, `timeout_seconds` overrides
4. Log constraint application for audit trail

#### Tool Layer: Checks Permissions

Before executing tool:

1. Load tool definition from `config/governance/tools.yaml`
2. Check `allowed_in_modes` and `forbidden_in_modes`
3. Verify path/command allowlists if applicable
4. If `requires_approval`, pause and request confirmation
5. If `requires_outbound_gateway`, route through safety gate
6. Check rate limits

#### Safety Gates: Content Filtering

- Outbound Gatekeeper: Scans web requests against `outbound_gateway` policies
- Output Filter: Scans assistant responses against `content_filtering` patterns
- Blocks or redacts matches, emits policy violation events

---

### 2.4 Configuration Loading & Validation

**Startup sequence:**

1. Brainstem loads all `config/governance/*.yaml` files
2. Validates schema using Pydantic models (e.g., `GovernanceConfig`, `ModeDefinition`, `ToolPolicy`)
3. If validation fails, log errors and refuse to start (fail-safe)
4. Expose configs to other components via API or shared config object

**Runtime updates:**

- Configuration files are **read-only at runtime** (no hot-reloading in MVP)
- Changes require:
  1. Edit config file
  2. Propose change via Captain's Log or governance PR
  3. Human approval
  4. Restart agent (or trigger reload command)

**Future:** Support hot-reloading for non-critical changes (e.g., rate limits) with validation + approval workflow.

---

### 2.5 Policy Evolution Workflow

Changes to governance policies follow the **structured proposal model**:

1. **Agent generates proposal**:
   - Via self-reflection or in response to repeated issues
   - Creates a YAML diff or new config version
   - Writes justification to Captain's Log

2. **Human review**:
   - Project owner inspects proposed changes
   - Can test in isolation (e.g., load config in test harness)
   - Approves, rejects, or modifies

3. **Deployment**:
   - Approved config committed to git
   - Agent restarted or reload triggered
   - Change logged to telemetry with justification

Example Captain's Log entry:

```yaml
entry_id: "CL-2025-12-28-001"
timestamp: "2025-12-28T14:32:00Z"
type: "config_proposal"
title: "Reduce ALERT mode CPU threshold from 85% to 80%"
rationale: |
  Over the past week, the system transitioned to ALERT 12 times, all triggered
  by sustained CPU load. In 10 of those cases, the load was between 80-85%.
  Lowering the threshold provides earlier warning and smoother degradation.
proposed_change:
  file: "config/governance/modes.yaml"
  section: "modes.NORMAL.thresholds.cpu_load_percent"
  old_value: 85
  new_value: 80
metrics_supporting_change:
  - "perf_system_cpu_load: 10 sustained spikes 80-85% over 7 days"
  - "mode transitions: 12 NORMAL->ALERT, 0 false positives"
status: "awaiting_approval"
```

---

## 3. Decision Drivers

### Why YAML for Policies?

- **Human-readable**: Easy for project owner to inspect, edit, diff
- **Git-friendly**: Version control, blame, history
- **Standard tooling**: Can validate, lint, format with existing tools
- **Extensibility**: Easy to add new fields without breaking parsers
- **Testability**: Can load in isolation for unit tests

### Why Explicit Mode State Machine?

- **Clarity**: No ambiguity about current system state
- **Predictability**: Well-defined transitions, no hidden logic
- **Safety**: Brainstem is the single source of truth, prevents race conditions
- **Debuggability**: Mode transitions logged, can reconstruct system history

### Why Separate Config Files per Domain?

- **Modularity**: Changes to tool policies don't affect mode thresholds
- **Clarity**: Each file has a focused purpose
- **Evolution**: Can replace one config subsystem without touching others

---

## 4. Implementation Plan

### Phase 1: Core Config Loading (Week 1)

1. **Define Pydantic models**:
   - `src/personal_agent/governance/models.py`:
     - `Mode(str, Enum)`, `ModeDefinition`, `ToolPolicy`, `ModelConstraints`, etc.
   - Schema validation with useful error messages

2. **Create default config files**:
   - `config/governance/modes.yaml` (with placeholder thresholds)
   - `config/governance/tools.yaml` (initial tool set: read_file, write_file)
   - `config/governance/models.yaml` (constraints per mode)
   - `config/governance/safety.yaml` (basic patterns)

3. **Implement config loader**:
   - `src/personal_agent/governance/config_loader.py`:
     - `load_governance_config() -> GovernanceConfig`
     - Validates on load, raises errors with actionable messages

### Phase 2: Brainstem Mode Management (Week 2)

1. **Implement mode state machine**:
   - `src/personal_agent/brainstem/mode_manager.py`:
     - `ModeManager` class with `current_mode: Mode`
     - `evaluate_transitions()` method (checks thresholds)
     - `transition_to(new_mode, reason)` method (logs, emits event)

2. **Wire to sensors**: Integrate with telemetry metric readers (from ADR-0004)

### Phase 3: Orchestrator & Tool Enforcement (Week 2-3)

1. **Orchestrator enforcement**:
   - Query `mode_manager.get_current_mode()` before task execution
   - Filter tools, apply concurrency limits
   - Request human approval when required

2. **Tool layer enforcement**:
   - Check tool permissions before execution
   - Apply path/command allowlists
   - Emit policy violation events

3. **Local LLM Client enforcement**:
   - Apply model constraints (max tokens, temperature, role filtering)

### Phase 4: Safety Gates (Week 3-4)

1. **Outbound Gatekeeper**: Implement domain filtering and content scanning
2. **Output Filter**: Implement secret pattern detection and redaction

---

## 5. Consequences

### Positive

✅ **Explicit governance**: No ambiguity about what's allowed
✅ **Evolvable via structured proposals**: Policies improve over time with justification
✅ **Auditable**: All constraint enforcement logged
✅ **Testable**: Policies can be validated in isolation
✅ **Safety-first**: Mode-based degradation prevents runaway behavior
✅ **Human control**: Approval workflows for high-risk actions

### Negative / Trade-offs

⚠️ **Configuration complexity**: Many YAML files to manage (mitigated by clear structure)
⚠️ **No hot-reload in MVP**: Changes require restart (acceptable for solo use)
⚠️ **Approval workflow UX**: Need clean UI for approval requests (CLI prompts for MVP)
⚠️ **Threshold tuning**: Initial thresholds are guesses, require experimentation

---

## 6. Open Questions & Future Work

- **Dynamic threshold learning**: Can the agent propose threshold adjustments based on telemetry analysis?
- **Per-user/per-context policies**: Future multi-user scenarios may need policy variants
- **Policy testing framework**: How do we validate that a policy change achieves desired behavior?
- **Emergency override**: Should there be a "break glass" mechanism for project owner to bypass all restrictions?
- **Approval UI/UX**: CLI prompts vs notification center vs web UI?

---

## 7. References

- `../architecture/BRAINSTEM_SERVICE_v0.1.md` — Mode management philosophy
- `../architecture/HOMEOSTASIS_MODEL.md` — Control loop integration
- `../architecture/CONTROL_LOOPS_SENSORS_v0.1.md` — Sensor definitions for thresholds
- `./GOVERNANCE_MODEL.md` — High-level governance principles
- `functional-spec/functional_spec_v0.1.md` — Autonomy boundaries

---

## 8. Acceptance Criteria

This ADR is accepted when:

1. ✅ Governance config files exist with realistic placeholder values
2. ✅ Pydantic models validate all configs successfully
3. ✅ Brainstem can load configs and maintain current mode state
4. ✅ Orchestrator queries mode and enforces at least one constraint (e.g., tool filtering)
5. ✅ At least one mode transition is logged and observable in telemetry

---

**Next ADRs to unblock**: ADR-0006 (Orchestrator Runtime Structure), Tool Execution Spec
