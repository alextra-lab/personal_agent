# Router Self-Tuning Architecture

**Version:** 0.1
**Date:** 2025-12-31
**Status:** Proposed (Phase 2-3)
**Owner:** Project Owner

---

## 1. Vision

The agent monitors its own routing decisions, analyzes performance patterns, and **proposes improvements** to its routing configuration. This creates a **metacognitive feedback loop** where the agent:

1. **Observes** routing decisions and outcomes
2. **Analyzes** patterns (accuracy, resource usage, user satisfaction)
3. **Proposes** configuration changes (prompt tweaks, thresholds, parameters)
4. **Validates** improvements via A/B testing
5. **Applies** validated changes with human approval

**Core Principle:** The agent **never modifies itself autonomously** - it proposes changes and seeks approval (governance-aware self-improvement).

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    ROUTER SELF-TUNING LOOP                      │
└─────────────────────────────────────────────────────────────────┘

   ┌──────────────┐
   │  1. OBSERVE  │  Collect routing telemetry
   └──────┬───────┘
          │
          ▼
   ┌──────────────┐
   │  2. ANALYZE  │  Detect patterns, compute metrics
   └──────┬───────┘
          │
          ▼
   ┌──────────────┐
   │ 3. HYPOTHESIZE│ Generate improvement proposals
   └──────┬───────┘
          │
          ▼
   ┌──────────────┐
   │ 4. VALIDATE  │  A/B test proposed changes
   └──────┬───────┘
          │
          ▼
   ┌──────────────┐
   │  5. APPLY    │  Human approval → Update config
   └──────────────┘
```

---

## 3. Components

### 3.1 Routing Telemetry Collector

**Purpose:** Capture all routing decisions with context and outcomes.

**Data Schema:**
```python
class RoutingTelemetryEntry(TypedDict):
    # Decision metadata
    trace_id: str
    timestamp: datetime
    session_id: str

    # Input
    user_query: str
    query_length: int
    query_type: str  # question, command, request
    channel: str  # CHAT, CODE_TASK, etc.

    # Router decision
    router_decision: str  # HANDLE, DELEGATE
    selected_model: str  # ROUTER, REASONING, CODING
    confidence: float
    reasoning_depth: int
    estimated_tokens: int

    # Recommended parameters (Phase 2)
    recommended_max_tokens: int | None
    recommended_temperature: float | None
    recommended_timeout_multiplier: float | None

    # Actual execution
    actual_model_used: str
    actual_tokens_used: int
    actual_latency_ms: int
    actual_timeout: float

    # Outcome
    task_success: bool
    user_feedback: float | None  # 1-5 rating (if available)
    validation_passed: bool | None  # Phase 2: validation model check

    # Ground truth (manual validation)
    correct_model: str | None  # Human-labeled correct model
    routing_accuracy: bool | None  # Was router decision correct?
```

**Storage:**
```python
# Append-only log in structured format
# Location: logs/routing_telemetry.jsonl

{"trace_id": "abc123", "timestamp": "2025-12-31T10:00:00Z", ...}
{"trace_id": "def456", "timestamp": "2025-12-31T10:01:23Z", ...}
```

**Collection:**
```python
async def log_routing_decision(
    ctx: ExecutionContext,
    routing_result: RoutingResult,
    execution_result: OrchestratorResult
):
    """Log routing decision with full context."""

    entry = RoutingTelemetryEntry(
        trace_id=ctx.trace_id,
        timestamp=datetime.now(timezone.utc),
        user_query=ctx.user_message,
        router_decision=routing_result["decision"],
        selected_model=routing_result["target_model"],
        confidence=routing_result["confidence"],
        actual_tokens_used=execution_result["tokens"],
        task_success=execution_result["state"] == TaskState.COMPLETED,
        # ... all fields
    )

    telemetry_logger.info("routing_telemetry", **entry)
```

---

### 3.2 Routing Performance Analyzer

**Purpose:** Compute metrics and detect patterns from telemetry.

**Metrics Computed:**

1. **Overall Accuracy:**
   ```python
   routing_accuracy = (correct_decisions / total_decisions) * 100
   # Target: >90%
   ```

2. **Per-Model Accuracy:**
   ```python
   accuracy_by_model = {
       "ROUTER": correct_router_decisions / total_router_decisions,
       "REASONING": correct_reasoning_decisions / total_reasoning_decisions,
       "CODING": correct_coding_decisions / total_coding_decisions
   }
   ```

3. **Confidence Calibration:**
   ```python
   # Are high-confidence decisions actually more accurate?
   confidence_buckets = {
       "0.9-1.0": accuracy_in_high_confidence,
       "0.7-0.9": accuracy_in_medium_confidence,
       "0.0-0.7": accuracy_in_low_confidence
   }
   # Expected: Higher confidence → Higher accuracy
   ```

4. **Parameter Effectiveness (Phase 2):**
   ```python
   # Did router's parameter recommendations improve outcomes?
   parameter_impact = {
       "max_tokens_utilization": actual_tokens / recommended_tokens,
       "timeout_efficiency": actual_latency / (timeout * timeout_multiplier),
       "over_provisioning_rate": queries_with_excess_resources / total_queries
   }
   ```

5. **Resource Efficiency:**
   ```python
   efficiency_metrics = {
       "avg_latency_per_model": ...,
       "token_usage_per_model": ...,
       "unnecessary_delegations": router_could_handle_but_delegated / total
   }
   ```

**Pattern Detection:**
```python
class RoutingPatternAnalyzer:
    """Detect patterns in routing telemetry."""

    async def analyze_misclassifications(
        self,
        entries: list[RoutingTelemetryEntry],
        window_days: int = 7
    ) -> list[RoutingPattern]:
        """Find common patterns in misclassified queries."""

        misclassified = [e for e in entries if e.routing_accuracy == False]

        patterns = []

        # Pattern 1: Specific query types consistently misrouted
        query_type_errors = group_by(misclassified, key="query_type")
        for query_type, errors in query_type_errors.items():
            if len(errors) > 5 and error_rate(errors) > 0.3:
                patterns.append(RoutingPattern(
                    pattern_type="query_type_misclassification",
                    affected_query_type=query_type,
                    error_count=len(errors),
                    error_rate=error_rate(errors),
                    example_queries=errors[:3]
                ))

        # Pattern 2: Low-confidence decisions are wrong
        low_confidence_errors = [e for e in misclassified if e.confidence < 0.7]
        if len(low_confidence_errors) > 10:
            patterns.append(RoutingPattern(
                pattern_type="low_confidence_threshold_too_low",
                suggestion="Increase confidence threshold from 0.7 to 0.8"
            ))

        # Pattern 3: Router under-utilizing REASONING model
        # (simple queries correctly handled, but missing complex ones)
        false_handles = [e for e in misclassified
                        if e.router_decision == "HANDLE"
                        and e.correct_model == "REASONING"]
        if len(false_handles) > threshold:
            patterns.append(RoutingPattern(
                pattern_type="reasoning_underutilization",
                suggestion="Adjust complexity threshold or add examples"
            ))

        return patterns
```

---

### 3.3 Improvement Proposal Generator

**Purpose:** Generate actionable configuration changes based on patterns.

**Proposal Types:**

#### Type 1: Prompt Refinement
```python
class PromptRefinementProposal(TypedDict):
    proposal_id: str
    type: Literal["prompt_refinement"]

    # What to change
    component: Literal["system_prompt", "examples", "decision_criteria"]

    # Current vs proposed
    current_prompt: str
    proposed_prompt: str
    diff: str  # Unified diff

    # Why
    rationale: str
    supporting_evidence: list[str]  # trace_ids of failures
    expected_improvement: float  # % accuracy increase

    # Validation
    validation_plan: str
    estimated_validation_time: str

# Example proposal
{
    "proposal_id": "PR-2025-12-31-001",
    "type": "prompt_refinement",
    "component": "examples",
    "current_prompt": "...",
    "proposed_prompt": """
    # Add new example for philosophical questions
    Q: "What is the meaning of life?"
    A: {"model": "REASONING", "reason": "Deep philosophical analysis"}
    """,
    "rationale": "Router is misclassifying philosophical questions as simple Q&A.
                  12 cases in past week (trace_ids: abc123, def456, ...)",
    "expected_improvement": 5.0,  # 5% accuracy increase
    "validation_plan": "A/B test on 50 philosophical queries"
}
```

#### Type 2: Threshold Adjustment
```python
class ThresholdAdjustmentProposal(TypedDict):
    proposal_id: str
    type: Literal["threshold_adjustment"]

    parameter: str  # "confidence_threshold", "reasoning_depth_threshold", etc.
    current_value: float
    proposed_value: float

    rationale: str
    supporting_metrics: dict[str, float]

# Example
{
    "parameter": "confidence_threshold",
    "current_value": 0.7,
    "proposed_value": 0.75,
    "rationale": "45% of decisions with confidence 0.70-0.75 were incorrect.
                  Raising threshold to 0.75 would default these to REASONING.",
    "supporting_metrics": {
        "accuracy_below_075": 0.65,
        "accuracy_above_075": 0.92,
        "queries_affected": 23
    }
}
```

#### Type 3: Parameter Recommendation Tuning (Phase 2)
```python
class ParameterTuningProposal(TypedDict):
    proposal_id: str
    type: Literal["parameter_tuning"]

    parameter: str  # "max_tokens_formula", "timeout_multiplier_formula"
    current_formula: str
    proposed_formula: str

    rationale: str
    efficiency_gain: float  # % resource savings

# Example
{
    "parameter": "max_tokens_formula",
    "current_formula": "reasoning_depth * 200",
    "proposed_formula": "reasoning_depth * 150 + 300",
    "rationale": "Over-provisioning by avg 30%.
                  Formula adjustment maintains quality with 20% token savings.",
    "efficiency_gain": 20.0
}
```

#### Type 4: Model Selection Refinement
```python
class ModelSelectionProposal(TypedDict):
    proposal_id: str
    type: Literal["model_selection_refinement"]

    query_pattern: str
    current_routing: str  # e.g., "REASONING"
    proposed_routing: str  # e.g., "ROUTER"

    rationale: str
    quality_impact: str  # "neutral", "improved", "degraded"
    efficiency_gain: float

# Example
{
    "query_pattern": "greeting + small_talk",
    "current_routing": "REASONING (via delegation)",
    "proposed_routing": "ROUTER (direct handle)",
    "rationale": "78% of greetings delegated to REASONING unnecessarily.
                  Router responses equally rated by users.",
    "quality_impact": "neutral",
    "efficiency_gain": 85.0  # 85% faster
}
```

**Proposal Generator:**
```python
class ImprovementProposalGenerator:
    """Generate configuration improvement proposals."""

    def __init__(self, llm_client: LocalLLMClient):
        self.llm_client = llm_client

    async def generate_proposals(
        self,
        patterns: list[RoutingPattern],
        metrics: RoutingMetrics,
        telemetry: list[RoutingTelemetryEntry]
    ) -> list[ImprovementProposal]:
        """Generate improvement proposals from patterns."""

        # Use REASONING model to analyze patterns and propose changes
        analysis_prompt = f"""
        You are analyzing routing performance for an AI agent.

        Current Metrics:
        - Overall accuracy: {metrics.overall_accuracy}%
        - Router accuracy: {metrics.router_accuracy}%
        - Reasoning accuracy: {metrics.reasoning_accuracy}%
        - Confidence calibration: {metrics.confidence_calibration}

        Detected Patterns:
        {format_patterns(patterns)}

        Recent Failures:
        {format_failures(telemetry)}

        Propose 1-3 specific, actionable improvements to:
        1. Router prompt (add examples, adjust criteria)
        2. Decision thresholds (confidence, complexity)
        3. Parameter formulas (max_tokens, timeout)

        For each proposal, provide:
        - Specific change (diff or formula)
        - Rationale with evidence (trace_ids)
        - Expected impact (% improvement)
        - Validation plan

        Output JSON array of proposals.
        """

        response = await self.llm_client.respond(
            role=ModelRole.REASONING,
            messages=[{"role": "user", "content": analysis_prompt}],
            max_tokens=3000
        )

        proposals = parse_proposals_from_response(response)

        # Validate proposals are safe and actionable
        validated_proposals = [p for p in proposals if self.validate_proposal(p)]

        return validated_proposals

    def validate_proposal(self, proposal: ImprovementProposal) -> bool:
        """Ensure proposal is safe and reasonable."""

        # Safety checks
        if proposal["type"] == "threshold_adjustment":
            # Don't allow extreme threshold changes
            if abs(proposal["proposed_value"] - proposal["current_value"]) > 0.2:
                return False

        if proposal["type"] == "prompt_refinement":
            # Don't allow prompt to grow too large
            if len(proposal["proposed_prompt"]) > 5000:
                return False

        # Require evidence
        if len(proposal.get("supporting_evidence", [])) < 3:
            return False

        return True
```

---

### 3.4 Proposal Validation Framework

**Purpose:** A/B test proposals before applying them.

**Validation Process:**
```python
class ProposalValidator:
    """Validate improvement proposals via A/B testing."""

    async def validate_proposal(
        self,
        proposal: ImprovementProposal,
        test_queries: list[str],
        baseline_config: RouterConfig
    ) -> ValidationReport:
        """Run A/B test comparing current vs proposed config."""

        # Create test config with proposal applied
        test_config = apply_proposal(baseline_config, proposal)

        results_baseline = []
        results_test = []

        for query in test_queries:
            # Run with baseline config
            result_a = await self.run_routing_with_config(query, baseline_config)
            results_baseline.append(result_a)

            # Run with test config
            result_b = await self.run_routing_with_config(query, test_config)
            results_test.append(result_b)

        # Compare metrics
        metrics_baseline = compute_metrics(results_baseline)
        metrics_test = compute_metrics(results_test)

        # Statistical significance test
        p_value = statistical_test(results_baseline, results_test)

        return ValidationReport(
            proposal_id=proposal["proposal_id"],
            baseline_accuracy=metrics_baseline.accuracy,
            test_accuracy=metrics_test.accuracy,
            improvement=metrics_test.accuracy - metrics_baseline.accuracy,
            p_value=p_value,
            statistically_significant=p_value < 0.05,
            recommendation="APPROVE" if metrics_test.accuracy > metrics_baseline.accuracy else "REJECT"
        )
```

**Validation Report Format:**
```python
class ValidationReport(TypedDict):
    proposal_id: str

    # Metrics comparison
    baseline_accuracy: float
    test_accuracy: float
    improvement: float  # Percentage points

    # Statistical significance
    p_value: float
    statistically_significant: bool
    sample_size: int

    # Detailed breakdown
    accuracy_by_query_type: dict[str, dict[str, float]]
    latency_impact: float  # % change
    resource_impact: float  # % change

    # Examples
    improved_cases: list[str]  # trace_ids
    degraded_cases: list[str]  # trace_ids

    # Recommendation
    recommendation: Literal["APPROVE", "REJECT", "NEEDS_MORE_DATA"]
    rationale: str
```

---

### 3.5 Configuration Update Manager

**Purpose:** Apply validated proposals with human approval.

**Approval Workflow:**
```python
class ConfigurationUpdateManager:
    """Manage router configuration updates."""

    async def propose_update(
        self,
        proposal: ImprovementProposal,
        validation_report: ValidationReport
    ) -> UpdateRequest:
        """Create update request for human approval."""

        # Generate Captain's Log entry for review
        log_entry = f"""
        # Router Configuration Update Proposal

        **Proposal ID:** {proposal['proposal_id']}
        **Date:** {datetime.now().isoformat()}

        ## Current Issue
        {proposal['rationale']}

        ## Proposed Change
        ```
        {proposal['diff']}
        ```

        ## Validation Results
        - Baseline accuracy: {validation_report['baseline_accuracy']}%
        - Test accuracy: {validation_report['test_accuracy']}%
        - Improvement: +{validation_report['improvement']}%
        - Statistical significance: p={validation_report['p_value']}
          {'✅ Significant' if validation_report['statistically_significant'] else '⚠️ Not significant'}

        ## Recommendation
        {validation_report['recommendation']}: {validation_report['rationale']}

        ## Action Required
        - [ ] Review proposal and validation results
        - [ ] Approve or reject update
        - [ ] If approved, configuration will be updated automatically

        **Approval Command:**
        ```bash
        python -m personal_agent.config.apply_proposal {proposal['proposal_id']}
        ```
        """

        # Save to Captain's Log for review
        await self.captain_log.add_entry(
            category="router_tuning",
            content=log_entry,
            requires_approval=True
        )

        return UpdateRequest(
            proposal_id=proposal["proposal_id"],
            status="awaiting_approval",
            log_entry_id=log_entry.id
        )

    async def apply_update(
        self,
        proposal_id: str,
        approved_by: str = "project_owner"
    ):
        """Apply approved configuration update."""

        proposal = self.load_proposal(proposal_id)

        # Backup current configuration
        backup_path = self.backup_current_config()

        # Apply changes
        if proposal["type"] == "prompt_refinement":
            self.update_router_prompt(proposal)
        elif proposal["type"] == "threshold_adjustment":
            self.update_threshold(proposal)
        elif proposal["type"] == "parameter_tuning":
            self.update_parameter_formula(proposal)

        # Log update
        log.info(
            "router_config_updated",
            proposal_id=proposal_id,
            approved_by=approved_by,
            backup_path=backup_path,
            timestamp=datetime.now()
        )

        # Add to Captain's Log
        await self.captain_log.add_entry(
            category="router_tuning",
            content=f"Applied configuration update {proposal_id}",
            metadata={"backup": backup_path}
        )
```

---

## 4. Implementation Phases

### Phase 1: Telemetry Collection (Week 4 or early Phase 2)
**Duration:** 2-3 days

- Implement `RoutingTelemetryEntry` schema
- Add telemetry logging to orchestrator
- Create telemetry storage (JSONL files)
- Build basic query tool for telemetry data

**Deliverable:** Can query routing decisions from past week

---

### Phase 2: Performance Analysis (Phase 2, Month 2)
**Duration:** 3-4 days

- Implement `RoutingPerformanceAnalyzer`
- Compute accuracy, confidence calibration, efficiency metrics
- Pattern detection for common failure modes
- Dashboard/CLI for viewing metrics

**Deliverable:** Weekly routing performance reports

---

### Phase 3: Proposal Generation (Phase 2-3)
**Duration:** 5-7 days

- Implement `ImprovementProposalGenerator`
- Use REASONING model to analyze patterns
- Generate actionable proposals (prompts, thresholds, parameters)
- Proposal validation and safety checks

**Deliverable:** Agent proposes configuration improvements

---

### Phase 4: A/B Testing Framework (Phase 3)
**Duration:** 3-5 days

- Implement `ProposalValidator`
- Run A/B tests on test query sets
- Statistical significance testing
- Validation reports with recommendations

**Deliverable:** Can validate proposals empirically

---

### Phase 5: Configuration Management (Phase 3)
**Duration:** 2-3 days

- Implement `ConfigurationUpdateManager`
- Human approval workflow via Captain's Log
- Safe configuration updates with backups
- Rollback mechanism

**Deliverable:** Can apply validated proposals with governance

---

## 5. Success Metrics

| Metric | Target | Timeline |
|--------|--------|----------|
| **Telemetry Coverage** | 100% of routing decisions | Phase 1 |
| **Manual Accuracy Baseline** | >85% | Phase 1 (100 queries) |
| **Automated Accuracy Tracking** | Continuous | Phase 2 |
| **First Proposal Generated** | 1 actionable proposal | Phase 3 |
| **First Proposal Validated** | A/B test complete | Phase 3 |
| **First Proposal Applied** | Config updated | Phase 3 |
| **Routing Accuracy Improvement** | +5-10% | Phase 4-5 |

---

## 6. Governance & Safety

### 6.1 Constraints

**Never Autonomous:**
- Agent **proposes** changes, never applies without approval
- All updates require human review via Captain's Log
- Backup created before every configuration change
- Rollback mechanism always available

**Bounded Changes:**
- Threshold changes limited to ±20% of current value
- Prompt changes limited to ±30% of current length
- Parameter formula changes validated for safety

**Validation Required:**
- All proposals require A/B testing
- Minimum sample size: 50 queries
- Statistical significance threshold: p<0.05
- Manual review of degraded cases

### 6.2 Rollback Mechanism

```bash
# Automatic backup before changes
python -m personal_agent.config.backup  # Creates timestamped backup

# Rollback to previous version
python -m personal_agent.config.rollback  # Restores last backup

# Rollback to specific version
python -m personal_agent.config.rollback --timestamp 2025-12-31T10:00:00Z
```

---

## 7. Integration with Existing Systems

### 7.1 Captain's Log Integration

Router tuning proposals appear in Captain's Log:
- Category: `router_tuning`
- Requires approval: `True`
- Contains: Proposal details, validation results, approval command

### 7.2 Telemetry Integration

Router telemetry uses existing telemetry infrastructure:
- Logged to `logs/routing_telemetry.jsonl`
- Structured logging with `trace_id` correlation
- Queryable via telemetry CLI tools

### 7.3 Brainstem/Mode Integration (Future)

Mode-aware proposal generation:
- In DEGRADED mode: Propose simpler routing (favor ROUTER over REASONING)
- In NORMAL mode: Optimize for quality
- In ALERT mode: Pause self-tuning (too risky during incidents)

---

## 8. Example End-to-End Flow

**Week 1: Collect telemetry**
```bash
# 100 queries processed
# Telemetry logged to routing_telemetry.jsonl
```

**Week 2: Analyze performance**
```bash
python -m personal_agent.telemetry.analyze_routing --window 7d

# Output:
# Overall accuracy: 87%
# Router accuracy: 92%
# Reasoning accuracy: 83% ⚠️
#
# Pattern detected: Philosophical questions misrouted as simple Q&A
# Examples: trace_abc123, trace_def456, trace_ghi789
```

**Week 3: Generate proposal**
```bash
python -m personal_agent.router.propose_improvements

# Output:
# Proposal PR-001 generated:
# - Add 3 examples of philosophical questions
# - Adjust reasoning_depth threshold from 5 to 4
# - Expected improvement: +8% accuracy
#
# Proposal saved to proposals/PR-001.yaml
```

**Week 4: Validate proposal**
```bash
python -m personal_agent.router.validate_proposal PR-001

# Output:
# A/B Test Results (n=50 queries):
# - Baseline accuracy: 87.0%
# - Test accuracy: 94.0%
# - Improvement: +7.0% (p=0.02, significant)
#
# Recommendation: APPROVE
# Validation report saved to proposals/PR-001-validation.yaml
```

**Week 5: Review & apply**
```bash
# Review in Captain's Log
cat captains_log/router_tuning/PR-001-proposal.md

# Approve and apply
python -m personal_agent.config.apply_proposal PR-001

# Output:
# ✅ Configuration backed up to backups/router_config_2025-12-31.yaml
# ✅ Proposal PR-001 applied
# ✅ Router prompt updated with 3 new examples
# ✅ Threshold adjusted: reasoning_depth 5 → 4
# ✅ Captain's Log updated
```

**Week 6: Monitor impact**
```bash
python -m personal_agent.telemetry.analyze_routing --window 7d

# Output:
# Overall accuracy: 94% (+7% vs baseline) ✅
# Router accuracy: 92% (stable)
# Reasoning accuracy: 95% (+12% vs baseline) ✅
```

---

## 9. Open Questions

1. **How frequently should proposals be generated?**
   - Answer: Weekly analysis, proposals only if accuracy <90% or patterns detected

2. **Should we support multi-proposal validation (testing multiple changes)?**
   - Answer: Phase 4 enhancement, start with single-proposal testing

3. **Can the agent learn from user feedback ratings?**
   - Answer: Yes! Phase 3 can incorporate user ratings into accuracy metrics

4. **Should we version control router configurations?**
   - Answer: Yes, use Git to track all configuration changes

5. **What if a proposal degrades performance?**
   - Answer: Validation catches this, but if it slips through: automatic rollback if accuracy drops >5% in week after change

---

## 10. Related Documents

**Architecture:**
- `./HOMEOSTASIS_MODEL.md` — Metacognitive feedback loops
- `./ORCHESTRATOR_CORE_SPEC_v0.1.md` — Routing within orchestrator
- `../plans/router_routing_logic_implementation_plan.md` — Day 11.5 implementation

**ADRs:**
- `ADR-0003-model-stack.md` — Router role definition
- `ADR-0004-telemetry-and-metrics.md` — Telemetry infrastructure
- `ADR-0008-model-stack-course-correction.md` — Routing patterns

**Research:**
- `../research/router_prompt_patterns_best_practices_2025-12-31.md` — Router prompts

**Roadmap:**
- `../plans/IMPLEMENTATION_ROADMAP.md` — Phase 2-3 timeline

---

**Status:** Proposed for Phase 2-3 (after Day 11.5 basic routing works)
**Next Actions:**
1. Complete Day 11.5 (basic routing)
2. Collect 100+ routing decisions (Week 4)
3. Build telemetry analyzer (early Phase 2)
4. Implement proposal generator (Phase 2-3)
