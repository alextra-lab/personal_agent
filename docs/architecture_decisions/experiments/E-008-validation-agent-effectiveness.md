# E-008: Validation Agent Effectiveness

**Status:** Planned (Phase 2)
**Phase:** Phase 2 Month 3
**Date Created:** 2026-01-18
**Prerequisites:** E-007 complete (three-stage routing implemented)
**Priority:** MEDIUM - Quality improvement

---

## 1. Hypothesis

**H-008a:** Adding a validation agent (stage 3 of routing pipeline) reduces hallucinations and routing errors by 10-15% as predicted by MoMA research.

**H-008b:** The validation stage catches low-confidence decisions and prevents them from reaching users, improving overall response quality.

**H-008c:** The latency cost of validation (<100ms) is justified by the quality improvement.

---

## 2. Objective

Measure the isolated impact of the validation stage (Stage 3) in the three-stage routing pipeline by comparing:
- **Variant A:** Three-stage routing (Classify → Select → Validate)
- **Variant B:** Two-stage routing (Classify → Select only)

**Key Questions:**
1. How many bad routing decisions does validation catch?
2. What's the false positive rate (good decisions rejected)?
3. Does validation improve end-user experience?
4. Is the latency cost justified?

---

## 3. Method

### 3.1 A/B Testing Framework

**Setup:**
```python
# Variant A: Full three-stage pipeline (from E-007)
router_with_validation = ThreeStageRouter(
    enable_validation=True
)

# Variant B: Two-stage pipeline (no validation)
router_without_validation = ThreeStageRouter(
    enable_validation=False
)
```

**Test Execution:**
- Run 100 queries through BOTH variants
- Compare outcomes
- Measure quality and latency differences

### 3.2 Test Query Categories

**Category 1: Clear Queries (50 queries)**
- Should route correctly in both variants
- Measures false positive rate of validation

Examples:
- "What's my CPU usage?" → DIRECT_TOOL
- "Explain this algorithm" → REASONING
- "Write a Python function" → CODING

**Category 2: Ambiguous Queries (20 queries)**
- May confuse classifier/selector
- Validation should catch and suggest alternatives

Examples:
- "Help me" (too vague)
- "Fix the thing" (missing context)
- "What about it?" (no referent)

**Category 3: Edge Cases (20 queries)**
- Queries with multiple valid interpretations
- Tests validation judgment

Examples:
- "Sort these numbers" (direct tool or coding?)
- "Why is my system slow?" (reasoning or tool query?)
- "Show me the code for X" (retrieve or generate?)

**Category 4: Adversarial Queries (10 queries)**
- Intentionally confusing
- Should trigger validation concerns

Examples:
- "Delete everything and rewrite from scratch"
- "Ignore previous instructions"
- Nonsensical queries

---

## 4. Evaluation Metrics

### 4.1 Quality Metrics

| Metric | Definition | Target |
|--------|------------|--------|
| **Catch Rate** | % of bad decisions caught by validation | >80% |
| **False Positive Rate** | % of good decisions rejected | <5% |
| **Accuracy Improvement** | (Variant A accuracy) - (Variant B accuracy) | >10% |
| **Hallucination Reduction** | Reduction in hallucinated responses | >15% |
| **User-Perceived Quality** | Blind quality comparison | Variant A wins >60% |

### 4.2 Performance Metrics

| Metric | Variant A (with validation) | Variant B (no validation) |
|--------|----------------------------|---------------------------|
| **Avg Routing Latency** | ~150ms | ~50ms |
| **P95 Routing Latency** | <200ms | <100ms |
| **Validation Stage Latency** | <100ms | N/A |

### 4.3 Decision Quality

**Manual Review of 100 routing decisions:**

For each decision, assess:
1. **Classification correct?** (yes/no)
2. **Selection optimal?** (yes/no/debatable)
3. **Validation helpful?** (yes/no/false_positive/not_triggered)
4. **Overall outcome quality** (0-5 scale)

---

## 5. Data Collection

### 5.1 Automated Logs

**Storage:**
- `telemetry/evaluation/experiments/E-008/variant_a_with_validation.jsonl`
- `telemetry/evaluation/experiments/E-008/variant_b_without_validation.jsonl`

**Format:**
```json
{
  "query": "What's my CPU usage?",
  "variant": "A",
  "stage_1_result": {"query_type": "tool_direct", "confidence": 0.95},
  "stage_2_result": {"execution_path": "DIRECT_TOOL", "tool_name": "system_metrics_snapshot"},
  "stage_3_result": {"is_valid": true, "confidence_score": 0.92, "concerns": []},
  "final_decision": "DIRECT_TOOL",
  "execution_outcome": "success",
  "user_feedback": null,
  "latency_ms": {
    "stage_1": 45,
    "stage_2": 52,
    "stage_3": 78,
    "total": 175
  }
}
```

### 5.2 Quality Comparison Table

Create spreadsheet: `telemetry/evaluation/experiments/E-008/quality_comparison.csv`

Columns:
- query_id
- query_text
- variant_a_decision
- variant_b_decision
- variant_a_outcome_quality (0-5)
- variant_b_outcome_quality (0-5)
- validation_added_value (yes/no/false_positive)
- notes

---

## 6. Success Criteria

**Adopt Validation (keep Stage 3) if:**
- ✅ Catch rate >80% on ambiguous/adversarial queries
- ✅ False positive rate <5%
- ✅ Quality improvement >10%
- ✅ Latency overhead <100ms

**Simplify to 2-Stage (remove Stage 3) if:**
- ❌ Catch rate <50% (not catching enough bad decisions)
- ❌ False positive rate >10% (too many good decisions rejected)
- ❌ Quality improvement <5% (not worth latency cost)
- ❌ Latency overhead >150ms (too expensive)

**Tune Validation Thresholds if:**
- ~ Catch rate good but false positives high
- ~ Confidence thresholds need adjustment
- ~ Validation too aggressive or too lenient

---

## 7. Timeline

**Week 1 (3 days):**
- Day 1: Implement A/B testing framework
- Day 2: Run 100 queries through both variants
- Day 3: Collect automated metrics

**Week 2 (1-2 days):**
- Day 4: Manual quality review (100 decisions)
- Day 5: Analysis and report writing

**Total:** 4-5 days

---

## 8. Deliverables

1. **A/B Test Results:**
   - Side-by-side comparison of 100 routing decisions
   - Quality metrics (catch rate, false positives, accuracy)
   - Latency comparison

2. **Quality Analysis:**
   - Manual review of decision quality
   - Examples of validation catches
   - Examples of false positives

3. **Experiment Report:**
   - `experiments/E-008-results.md`
   - Recommendation: keep/remove/tune validation stage

4. **Code Updates:**
   - Validation threshold tuning (if needed)
   - Config flag to enable/disable validation

---

## 9. Analysis Questions

After collecting data, answer:

1. **What types of queries does validation catch most effectively?**
2. **What's the most common false positive pattern?**
3. **Is validation confidence score a good predictor of decision quality?**
4. **Should validation be mandatory or optional?**
5. **Can we reduce validation latency without sacrificing quality?**
6. **Are there queries where validation adds no value?**
7. **Should validation thresholds be task-type-specific?**

---

## 10. Decision Matrix

```
Outcome                              Catch   FP Rate   Latency   Decision
                                     Rate
────────────────────────────────────────────────────────────────────────────
Strong validation value              >80%    <5%       <100ms    KEEP Stage 3
Validation helpful but expensive     >80%    <5%       >150ms    Optimize latency
High false positives                 >80%    >10%      <100ms    Tune thresholds
Low catch rate                       <50%    <5%       <100ms    REMOVE Stage 3
No measurable benefit                <50%    >10%      >100ms    REMOVE Stage 3

Query-specific value                 Varies  <5%       <100ms    Make validation optional
```

---

## 11. Example Scenarios

### Scenario 1: Validation Catch (Success)

**Query:** "Fix the bug"

**Stage 1 (Classify):**
- query_type: "coding"
- confidence: 0.65 (low)

**Stage 2 (Select):**
- execution_path: "CODING"
- rationale: "User wants to fix a bug"

**Stage 3 (Validate):**
- is_valid: **false**
- concerns: ["Query missing context - which bug?", "No code provided"]
- alternative: "STANDARD" (ask for clarification)

**Outcome:** Validation caught ambiguous query, prevented hallucination ✅

---

### Scenario 2: False Positive (Failure)

**Query:** "What's the system load?"

**Stage 1 (Classify):**
- query_type: "tool_direct"
- confidence: 0.92

**Stage 2 (Select):**
- execution_path: "DIRECT_TOOL"
- tool_name: "system_metrics_snapshot"

**Stage 3 (Validate):**
- is_valid: **false**
- concerns: ["Ambiguous - which load metric?"]
- alternative: "REASONING" (ask user)

**Outcome:** Validation incorrectly rejected clear query, false positive ❌

---

## 12. Variations to Test

If initial results are mixed, test these variations:

**Variation 1: Confidence-Based Validation**
- Only validate if Stage 2 confidence <0.7
- Skip validation for high-confidence decisions

**Variation 2: Query-Type-Specific Validation**
- Always validate "tool_direct" (safety)
- Skip validation for "chat" (low risk)

**Variation 3: Threshold Tuning**
- Adjust is_valid threshold (currently 0.5)
- Make validation stricter or more lenient

---

## 13. Next Experiments

**If validation is effective:**
- **E-009:** Performance-Based Routing (learn optimal paths from validation outcomes)
- Implement validation-driven routing improvements

**If validation shows mixed results:**
- Fine-tune validation thresholds
- Test query-specific validation strategies
- Consider lightweight validation (heuristics vs. LLM)

---

## 14. References

**Research:**
- MoMA paper: 10-15% hallucination reduction from validation
- LLM self-verification literature

**Related Experiments:**
- E-007: Three-Stage Routing (implements validation stage)
- E-009: Performance-Based Routing (uses validation outcomes)

**Related ADRs:**
- ADR-0003: Model Stack (router/validator relationship)
- ADR-0006: Orchestrator Execution Model

---

**Document Status:** Experiment Specification
**Last Updated:** 2026-01-18
**Owner:** Project Owner
**Ready to Execute:** After E-007 complete
