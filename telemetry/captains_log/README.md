# Captain's Log — Agent Self-Improvement Proposals

> **Metaphor**: Like a starship captain's log, this is the agent's **self-documenting mechanism** for observations, reflections, and improvement proposals.
> **Purpose**: Enable the agent to **monitor itself, generate ideas, and propose enhancements** to the project owner.
> **Status**: Active improvement engine

---

## 🎯 What Is the Captain's Log?

The Captain's Log is where the **agent documents its own thinking** about:

- **System behavior observations** ("I noticed CPU usage spikes during reasoning tasks")
- **Improvement proposals** ("Consider reducing ALERT threshold from 85% to 80%")
- **New ideas to explore** ("Could parallel tool execution improve latency?")
- **Configuration adjustments** ("Rate limit for web search seems too aggressive")
- **Learning reflections** ("Tool X failed 3 times due to permission issue—propose allowlist update")

**It is NOT**:

- Execution logs (that's telemetry)
- Session logs (that's `docs/plans/sessions/`)
- Human-written documentation (that's specs and ADRs)

**It IS**:

- Agent-generated introspection
- Data-backed proposals
- Hypothesis seeds for experiments

---

## 📝 Entry Format

Each Captain's Log entry follows a structured YAML format:

```yaml
entry_id: "CL-YYYY-MM-DD-NNN"
timestamp: "2025-12-28T14:32:00Z"
type: "reflection" | "config_proposal" | "hypothesis" | "observation" | "idea"
title: "Short, actionable title"

# Main content
rationale: |
  Multi-line explanation of why this entry exists.
  What observation, pattern, or issue triggered it?

# For config_proposal type
proposed_change:
  file: "config/governance/modes.yaml"
  section: "modes.NORMAL.thresholds.cpu_load_percent"
  old_value: 85
  new_value: 80

# Supporting evidence
supporting_metrics:
  - "perf_system_cpu_load: 10 sustained spikes at 80-85% over 7 days"
  - "mode_transitions: 12 NORMAL→ALERT, 0 false positives"

# Expected impact
impact_assessment: |
  Expected to reduce late mode transitions by ~30%.
  Risk: slightly more frequent ALERT mode (acceptable tradeoff).

# Status tracking
status: "awaiting_approval" | "approved" | "rejected" | "implemented"
reviewer_notes: |
  (Filled in by project owner during review)

# Links
related_adrs: ["ADR-0005"]
related_experiments: []
telemetry_refs:
  - trace_id: "abc123-trace-showing-cpu-spike"
```

---

## 🗂️ Entry Types

### 1. `reflection`

**Purpose**: Post-task self-analysis

**Triggers**:

- After completing a complex task
- After a failure or error
- Periodically (e.g., end of day)

**Example**:

```yaml
entry_id: "CL-2025-12-28-001"
type: "reflection"
title: "System health check task analysis"
rationale: |
  Completed system health check successfully. Tool execution was smooth,
  but LLM synthesis took 4.2s (longer than typical 2-3s).
  May indicate model overload or prompt complexity.
```

---

### 2. `config_proposal`

**Purpose**: Suggest a governance or configuration change

**Triggers**:

- Repeated pattern in telemetry (e.g., frequent mode transitions)
- Observed inefficiency (e.g., overly restrictive rate limits)
- Safety issue (e.g., tool allowed in wrong mode)

**Example**:

```yaml
entry_id: "CL-2025-12-29-001"
type: "config_proposal"
title: "Increase web search rate limit from 20 to 50 requests/hour in NORMAL mode"
rationale: |
  Over 5 days, web search hit rate limit 8 times in NORMAL mode.
  All were legitimate research queries, not abuse.
  Current limit (20/hour) is too restrictive for research-heavy workflows.
proposed_change:
  file: "config/governance/safety.yaml"
  section: "rate_limits.per_mode.NORMAL.outbound_requests_per_hour"
  old_value: 20
  new_value: 50
supporting_metrics:
  - "web_search rate limit hit: 8 times in 5 days"
  - "0 instances of suspicious query patterns"
status: "awaiting_approval"
```

---

### 3. `hypothesis`

**Purpose**: Propose a testable hypothesis for experimentation

**Triggers**:

- Observing a behavior that could be improved
- Wondering if alternative approach would work better
- Generating ideas for future experiments

**Example**:

```yaml
entry_id: "CL-2025-12-30-001"
type: "hypothesis"
title: "Parallel tool execution could reduce latency by 30%"
rationale: |
  Currently, when multiple tools are needed (e.g., read CPU + read memory),
  they execute sequentially. Average latency: 450ms total.
  Hypothesis: executing tools in parallel would reduce to ~200ms.
experiment_design:
  - Implement async tool execution
  - Measure latency for multi-tool tasks
  - Compare before/after
expected_outcome: "30% latency reduction"
status: "awaiting_approval"
related_adrs: ["ADR-0006"]
```

---

### 4. `observation`

**Purpose**: Document interesting patterns without immediate action

**Triggers**:

- Noticing recurring behaviors
- Detecting anomalies
- Logging curiosities for future investigation

**Example**:

```yaml
entry_id: "CL-2025-12-31-001"
type: "observation"
title: "Reasoning model performs better after 10am"
rationale: |
  Noticed that LLM response quality (subjectively) seems higher after 10am.
  Possible causes: system warmed up, fewer background processes, or coincidence.
  Not actionable yet, but worth monitoring.
```

---

### 5. `idea`

**Purpose**: Brainstorm new capabilities or explorations

**Triggers**:

- Creative thinking during reflection
- User pain points observed
- Research discoveries

**Example**:

```yaml
entry_id: "CL-2026-01-01-001"
type: "idea"
title: "Add 'explain mode transition' feature"
rationale: |
  When mode transitions occur (e.g., NORMAL → ALERT), it would be helpful
  for the agent to proactively explain WHY to the user, referencing specific
  metrics that triggered the change.
potential_implementation:
  - Hook mode_manager.transition_to() to generate explanation
  - Use reasoning model to summarize trigger metrics
  - Display in CLI or log to Captain's Log
status: "awaiting_approval"
```

---

## 🔄 Lifecycle

1. **Agent generates entry**: Triggered by reflection, observation, or pattern detection
2. **Entry written to file**: `captains_log/CL-YYYY-MM-DD-NNN-title.yaml`
3. **Git commit** (optional in MVP): Commit with message like "Captain's Log: [title]"
4. **Project owner reviews**: Reads entry, evaluates proposal
5. **Decision**:
   - **Approve**: Update `status: approved`, implement change
   - **Reject**: Update `status: rejected`, add `reviewer_notes` explaining why
   - **Defer**: Leave as `awaiting_approval`, revisit later
6. **Implementation** (if approved): Make change, update `status: implemented`
7. **Close loop**: Reference Captain's Log entry in ADR or experiment if relevant

---

## 📊 Metrics to Track

- **Entries per week**: How active is agent self-reflection?
- **Approval rate**: What % of proposals are accepted?
- **Implementation rate**: What % of approved proposals get implemented?
- **Impact**: Do implemented proposals measurably improve system?

**Goal**: 1-2 high-quality proposals per week, >50% approval rate.

---

## 🛠️ Implementation Notes

### When to Trigger Captain's Log Entry

**Automatic triggers** (orchestrator logic):

- After every task (lightweight reflection)
- After errors or failures (detailed analysis)
- When metrics cross interesting thresholds (observations)

**Manual triggers** (project owner request):

- "Reflect on the last 24 hours of operation"
- "Propose improvements to tool permissions"
- "Generate hypothesis for reducing latency"

### How Agent Generates Entries

1. **Gather context**: Query recent telemetry, metrics, errors
2. **Reason**: Use reasoning model to analyze patterns
3. **Structure**: Format as YAML following template
4. **Validate**: Ensure all required fields present
5. **Write**: Save to `captains_log/` directory
6. **Log**: Emit telemetry event `captains_log_entry_created`

---

## 🎓 For Project Owner

### How to Review Entries

1. **Read entry**: Understand rationale and proposal
2. **Check evidence**: Verify supporting metrics are real
3. **Assess risk**: Would change cause problems?
4. **Evaluate benefit**: Is juice worth the squeeze?
5. **Decide**: Approve, reject, or defer

### Review Checklist

- [ ] Proposal is clear and specific
- [ ] Evidence supports the claim
- [ ] Change aligns with project principles
- [ ] Risk is acceptable
- [ ] Benefit justifies effort

### Example Review

```yaml
# Added to entry by project owner
reviewer_notes: |
  Approved. The evidence is solid—8 rate limit hits in 5 days is too frequent.
  Increasing to 50/hour is reasonable. Will implement in config update session.
status: "approved"
implementation_plan: "Session 2026-01-02: Update safety.yaml, restart agent"
```

---

## 🔗 Integration with Other Systems

- **Telemetry**: Captain's Log queries telemetry for evidence
- **Hypotheses**: Entries of type `hypothesis` feed into `HYPOTHESIS_LOG.md`
- **Experiments**: Approved hypotheses become experiments (`experiments/E-XXX-*.md`)
- **ADRs**: Major proposals may warrant full ADR (reference Captain's Log entry)
- **Governance**: Config proposals directly modify `config/governance/`

---

## 📂 Directory Structure

```
docs/architecture_decisions/captains_log/
├── README.md (this file)
├── CL-2025-12-28-001-system-health-reflection.yaml
├── CL-2025-12-29-001-web-search-rate-limit-increase.yaml
├── CL-2025-12-30-001-parallel-tool-execution-hypothesis.yaml
└── ...
```

**Naming convention**: `CL-YYYY-MM-DD-NNN-short-title.yaml`

---

## ✅ Success Criteria

Captain's Log is successful when:

- ✅ Agent generates actionable proposals (not vague ideas)
- ✅ Proposals are data-backed (not speculation)
- ✅ Project owner finds entries useful (>50% approval rate)
- ✅ Implemented proposals measurably improve system
- ✅ Captain's Log becomes a natural part of development workflow

---

## 🚀 Future Enhancements

- **Auto-prioritization**: Agent scores proposals by expected impact
- **Batch proposals**: Group related changes for efficient review
- **Visualization**: Dashboard showing proposal pipeline (awaiting → approved → implemented)
- **Learning loop**: Agent learns from approval/rejection patterns

---

**The Captain's Log is the agent's voice in its own evolution.**
