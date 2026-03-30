# E-007: Three-Stage Routing (MoMA-Inspired)

**Status:** Planned (Phase 2)
**Phase:** Phase 2 Month 2-3
**Date Created:** 2026-01-18
**Prerequisites:** E-004 baseline complete, current router operational
**Priority:** **HIGH** - Core architectural enhancement

---

## 1. Hypothesis

**H-007a:** Implementing MoMA-inspired three-stage routing (Classify → Select → Validate) will improve routing accuracy and enable direct tool execution for 30-40% of queries.

**H-007b:** Adding a validation stage will catch hallucinations and low-confidence decisions before they reach the user, reducing error rate by >15%.

**H-007c:** The routing overhead will remain <200ms despite adding two additional stages.

---

## 2. Objective

Implement and validate a three-stage routing architecture inspired by the MoMA (Mixture of Monosemantically Aligned Agents) pattern:

**Stage 1: Classify** - Determine query type and complexity
**Stage 2: Select** - Choose optimal execution path (direct tool, reasoning, coding)
**Stage 3: Validate** - Verify decision quality before execution

**Key Goals:**
1. Enable direct tool execution for simple queries (bypass reasoning models)
2. Improve routing accuracy to >95%
3. Reduce hallucinations through validation
4. Maintain low latency (<200ms routing overhead)

---

## 3. Method

### 3.1 Three-Stage Architecture

**Stage 1: Query Classification**
```python
class ClassifyQuery(dspy.Signature):
    """Classify query type and complexity."""
    query: str = dspy.InputField()

    query_type: Literal["tool_direct", "reasoning", "coding", "chat"] = dspy.OutputField()
    complexity: Literal["simple", "medium", "complex"] = dspy.OutputField()
    confidence: float = dspy.OutputField()
    requires_tools: bool = dspy.OutputField()
```

**Stage 2: Path Selection**
```python
class SelectExecutionPath(dspy.Signature):
    """Select optimal execution path based on classification."""
    query: str = dspy.InputField()
    query_type: str = dspy.InputField()
    complexity: str = dspy.InputField()
    available_tools: str = dspy.InputField()

    execution_path: Literal["DIRECT_TOOL", "REASONING", "CODING", "STANDARD"] = dspy.OutputField()
    tool_name: str | None = dspy.OutputField()
    rationale: str = dspy.OutputField()
```

**Stage 3: Decision Validation**
```python
class ValidateRoutingDecision(dspy.Signature):
    """Validate routing decision quality and confidence."""
    query: str = dspy.InputField()
    execution_path: str = dspy.InputField()
    rationale: str = dspy.InputField()

    is_valid: bool = dspy.OutputField()
    confidence_score: float = dspy.OutputField()
    concerns: list[str] = dspy.OutputField()
    alternative: str | None = dspy.OutputField()
```

### 3.2 Implementation Plan

**Week 1 (Days 1-3): Implement Three-Stage Pipeline**
- [ ] Create `src/personal_agent/orchestrator/routing_pipeline.py`
- [ ] Implement `ThreeStageRouter` class
- [ ] Add telemetry for each stage
- [ ] Wire into orchestrator executor

**Week 2 (Days 4-5): Testing & Validation**
- [ ] Create test suite with 50+ routing scenarios
- [ ] Measure accuracy, latency, direct-tool-execution rate
- [ ] Compare to baseline (current single-stage router)

### 3.3 Test Scenarios

**Direct Tool Execution Candidates (should route to DIRECT_TOOL):**
- "What's my CPU usage?"
- "Show me disk space"
- "What's the system memory?"
- "Get current time"
- "List files in /tmp"

**Reasoning Tasks (should route to REASONING):**
- "Analyze the trend in these CPU metrics"
- "Why is my system slow?"
- "What's the root cause of high memory?"
- "Should I upgrade my RAM?"

**Coding Tasks (should route to CODING):**
- "Write a function to parse JSON"
- "Fix this Python bug: [code]"
- "Implement quicksort in Python"
- "Refactor this class"

**Ambiguous Queries (validation should catch):**
- "Help me" (too vague)
- "Do something" (no clear action)
- "Fix it" (missing context)

---

## 4. Success Criteria

| Metric | Target | Current Baseline | Measurement |
|--------|--------|------------------|-------------|
| **Routing Accuracy** | >95% | ~85% (E-004) | Manual review of 100 decisions |
| **Direct Tool Exec Rate** | 30-40% | 0% | % of queries taking direct path |
| **Validation Catch Rate** | >80% | N/A | % of bad decisions caught |
| **Routing Overhead** | <200ms | ~50ms | Stage 1 + 2 + 3 latency |
| **End-to-End Improvement** | 20-30% faster | Baseline | For direct-tool queries |
| **False Positive Rate** | <5% | N/A | Valid decisions rejected |

---

## 5. Data Collection

### 5.1 Automated Metrics

**Routing Pipeline Telemetry:**
```python
log.info("routing_stage_1_classify",
    trace_id=ctx.trace_id,
    query_type=classification.query_type,
    complexity=classification.complexity,
    confidence=classification.confidence,
    duration_ms=stage1_duration
)

log.info("routing_stage_2_select",
    trace_id=ctx.trace_id,
    execution_path=selection.execution_path,
    tool_name=selection.tool_name,
    duration_ms=stage2_duration
)

log.info("routing_stage_3_validate",
    trace_id=ctx.trace_id,
    is_valid=validation.is_valid,
    confidence_score=validation.confidence_score,
    concerns=validation.concerns,
    duration_ms=stage3_duration
)
```

**Storage:**
- `telemetry/evaluation/routing_pipeline/{timestamp}_routing_decisions.jsonl`
- One JSON object per routing decision with all 3 stages

### 5.2 Manual Quality Review

**Sample 100 routing decisions:**
- 30 direct tool candidates
- 30 reasoning tasks
- 30 coding tasks
- 10 ambiguous/invalid queries

**Rate each:**
- Was classification correct? (yes/no)
- Was path selection optimal? (yes/no/debatable)
- Did validation add value? (yes/no/false_positive)

---

## 6. Timeline

**Week 1 (5 days):**
- Day 1-2: Implement three-stage pipeline
- Day 3: Integration with orchestrator
- Day 4: Unit tests for each stage
- Day 5: End-to-end testing

**Week 2 (2 days):**
- Day 6: Run 100+ test scenarios
- Day 7: Manual quality review, compile report

**Total:** 7 days (5 days as estimated in roadmap)

---

## 7. Deliverables

1. **Implementation:**
   - `src/personal_agent/orchestrator/routing_pipeline.py` (ThreeStageRouter)
   - `src/personal_agent/orchestrator/routing_stages.py` (DSPy signatures)
   - Integration with `executor.py`

2. **Tests:**
   - `tests/test_orchestrator/test_routing_pipeline.py` (50+ scenarios)
   - `tests/evaluation/routing_pipeline_evaluation.py` (benchmark script)

3. **Documentation:**
   - `experiments/E-007-results.md` (results and analysis)
   - `./ADR-00XX-three-stage-routing.md` (if adopted)
   - Updated `orchestrator/AGENTS.md`

4. **Telemetry:**
   - Routing decision logs with all 3 stages
   - Performance metrics (latency per stage)
   - Quality metrics (accuracy, catch rate)

---

## 8. Analysis Questions

After collecting data, answer:

1. **What % of queries can take the direct tool path?**
2. **How many bad routing decisions did validation catch?**
3. **What's the latency breakdown across the 3 stages?**
4. **Are there query patterns that consistently confuse the classifier?**
5. **Does validation have a high false positive rate (rejecting good decisions)?**
6. **What's the end-to-end improvement for direct-tool queries?**
7. **Should we keep all 3 stages or simplify to 2?**

---

## 9. Decision Matrix

```
Scenario                              Accuracy   Efficiency   Latency   Decision
────────────────────────────────────────────────────────────────────────────────
All targets met                       ✅ >95%    ✅ 30%+      ✅ <200ms  ADOPT fully
High accuracy, low direct-tool rate   ✅ >95%    ❌ <20%      ✅         ADOPT with tuning
High latency (>300ms)                 ✅         ✅           ❌         Simplify to 2 stages
Low validation value (<50% catch)     ✅         ✅           ✅         Remove stage 3
Low accuracy (<90%)                   ❌         ~            ~          Investigate failure modes

Direct tool path rarely taken         ~          ❌           ~          Retrain classifier
High false positives (>10%)           ~          ❌           ~          Tune validation thresholds
```

---

## 10. Risks & Mitigation

**Risk 1: High Latency**
- **Mitigation:** Use fast router model (Qwen3-4B), optimize prompts, run stages in parallel where possible

**Risk 2: Validation False Positives**
- **Mitigation:** Tune validation thresholds, allow override for high-confidence decisions

**Risk 3: Complexity**
- **Mitigation:** Make each stage independently testable, comprehensive telemetry

**Risk 4: Direct Tool Execution Failures**
- **Mitigation:** Add error handling, fallback to reasoning model if tool fails

---

## 11. Next Experiments

**If E-007 succeeds:**
- **E-008:** Validation Agent Effectiveness (measure impact of stage 3 in isolation)
- **E-009:** Performance-Based Routing (learn from historical routing outcomes)

**If E-007 shows mixed results:**
- Investigate failure modes
- A/B test 2-stage vs 3-stage variants
- Fine-tune classifier (E-012)

---

## 12. References

**Research:**
- MoMA paper: Mixture of Monosemantically Aligned Agents pattern
- `../research/moma_routing_patterns.md` (if exists)

**Related ADRs:**
- ADR-0003: Model Stack (router role)
- ADR-0006: Orchestrator Execution Model

**Related Experiments:**
- E-004: Baseline Model Performance (provides comparison baseline)
- E-008: Validation Agent Effectiveness (isolates stage 3 impact)

---

**Document Status:** Experiment Specification
**Last Updated:** 2026-01-18
**Owner:** Project Owner
**Ready to Execute:** After E-004 complete
