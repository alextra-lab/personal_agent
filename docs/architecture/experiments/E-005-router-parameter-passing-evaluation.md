# Experiment E-005: Router Parameter Passing Evaluation

**Date:** 2025-12-31
**Status:** Proposed (Design Phase)
**Phase:** Phase 2
**Related:** Day 11.5 (Router Routing Logic), Router Self-Tuning Architecture

---

## 1. Research Question

**Can a 4B router model provide meaningful parameter recommendations that improve resource efficiency without degrading quality?**

Sub-questions:
1. Can router accurately estimate query complexity (reasoning_depth)?
2. Can router recommend useful max_tokens that reduce waste?
3. Can router recommend timeout_multipliers that prevent unnecessary retries?
4. Do these recommendations improve outcomes vs. fixed parameters?

---

## 2. Hypothesis

**Primary Hypothesis:**
> A 4B router model can estimate query complexity and recommend parameters (max_tokens, timeout_multiplier) that achieve:
> - **20-30% resource efficiency gain** (tokens, time)
> - **<5% quality degradation** (user satisfaction, task success)

**Null Hypothesis:**
> Router parameter recommendations provide no significant improvement over fixed parameters, or degrade quality.

---

## 3. Experiment Design

### 3.1 Three-Phase Approach

#### **Phase A: Baseline Collection** (Week 1-2 of Phase 2)
**Goal:** Establish baseline performance without parameter passing

**Setup:**
- Implement basic routing (Day 11.5) with **fixed parameters**
- All REASONING calls use: `max_tokens=2000, timeout=60s`
- All CODING calls use: `max_tokens=3000, timeout=45s`
- Collect 200+ queries with telemetry

**Metrics to Collect:**
```python
class BaselineMetrics(TypedDict):
    query_id: str
    user_query: str
    query_length: int
    selected_model: str

    # Fixed parameters used
    max_tokens_allocated: int
    timeout_allocated: float

    # Actual usage
    actual_tokens_used: int
    actual_latency_ms: int

    # Outcome
    task_success: bool
    response_quality: float  # 1-5 rating

    # Waste metrics
    token_utilization: float  # actual / allocated
    timeout_utilization: float  # actual_latency / timeout
    over_provisioned: bool  # utilization < 0.5
```

**Expected Baseline:**
- Token utilization: 40-70% (significant waste)
- Timeout utilization: 20-60% (most queries finish early)
- Over-provisioning rate: 30-50% of queries

---

#### **Phase B: Router Parameter Estimation** (Week 3 of Phase 2)
**Goal:** Test router's ability to estimate parameters in isolation

**Setup:**
- Enhance router prompt with parameter estimation:
  ```json
  {
    "routing_decision": "DELEGATE",
    "target_model": "REASONING",
    "estimated_complexity": 8,
    "recommended_params": {
      "max_tokens": 1500,
      "temperature": 0.7,
      "timeout_multiplier": 1.2
    }
  }
  ```
- **DO NOT USE** recommendations yet (still use fixed params)
- Collect router's estimates alongside actual usage

**Analysis:**
```python
# For each query, compare:
router_estimate = routing_result["recommended_params"]["max_tokens"]
actual_usage = execution_result["actual_tokens_used"]

# Compute estimation error
error = abs(router_estimate - actual_usage)
error_rate = error / actual_usage

# Aggregate metrics
mean_error_rate = sum(error_rates) / len(queries)
over_estimation_rate = sum(router_estimate > actual_usage) / len(queries)
under_estimation_rate = sum(router_estimate < actual_usage) / len(queries)
```

**Success Criteria:**
- Mean error rate <30% (router estimates within ±30% of actual)
- Under-estimation rate <10% (rarely runs out of tokens)
- Correlation coefficient >0.6 (estimates track complexity)

**If Phase B fails:** Router cannot reliably estimate parameters → Skip Phase C, use fixed parameters

---

#### **Phase C: A/B Testing with Parameter Passing** (Week 4 of Phase 2)
**Goal:** Validate that router recommendations improve efficiency

**Setup:**
- Split traffic 50/50:
  - **Control group:** Fixed parameters (baseline)
  - **Treatment group:** Router-recommended parameters
- Run 100 queries through each group
- Measure efficiency and quality

**Comparison Metrics:**

| Metric | Control (Fixed) | Treatment (Router) | Target |
|--------|----------------|-------------------|---------|
| **Token utilization** | 60% | 80%+ | +20% improvement |
| **Timeout utilization** | 40% | 60%+ | +20% improvement |
| **Over-provisioning rate** | 40% | <20% | <50% reduction |
| **Task success rate** | 90% | >88% | <2% degradation |
| **Response quality** | 4.2/5 | >4.0/5 | <5% degradation |
| **Average latency** | 5000ms | <5500ms | <10% increase |

**Statistical Significance:**
- Use two-sample t-test for continuous metrics
- Chi-square test for categorical metrics (success/failure)
- Require p<0.05 for significance

---

### 3.2 Router Parameter Estimation Prompt

**Enhanced router system prompt (Phase B):**

```python
ROUTER_SYSTEM_PROMPT_WITH_PARAMS = """
You are an intelligent task classifier for a personal AI agent.

**Models:**
- ROUTER (you): 4B, <1s, 8K context
- REASONING: 14B, 3-10s, 32K context
- CODING: 30B, 5-15s, 32K context

**Decision Framework:**
1. Classify query complexity (1-10 scale)
2. Select target model
3. Estimate resource requirements

**Complexity Scale:**
- 1-3: Simple (greeting, fact, definition) → ROUTER handles
- 4-6: Moderate (explanation, comparison) → REASONING, ~1000 tokens
- 7-9: Complex (deep analysis, multi-step) → REASONING, ~2000 tokens
- 10: Very complex (research, synthesis) → REASONING, ~3000+ tokens

**Parameter Estimation Guidelines:**

**max_tokens estimation:**
- Complexity 1-3: N/A (router handles)
- Complexity 4-5: 800-1200 tokens
- Complexity 6-7: 1500-2000 tokens
- Complexity 8-9: 2000-3000 tokens
- Complexity 10: 3000+ tokens
- Add 20% buffer for safety

**timeout_multiplier estimation:**
- Complexity 1-5: 0.8x (likely fast)
- Complexity 6-7: 1.0x (normal)
- Complexity 8-9: 1.5x (needs time)
- Complexity 10: 2.0x (extended thinking)

**temperature recommendation:**
- Factual/technical: 0.3-0.5
- Explanatory: 0.7
- Creative/open-ended: 0.8-0.9

**Output JSON:**
{
  "routing_decision": "HANDLE|DELEGATE",
  "target_model": "ROUTER|REASONING|CODING",
  "confidence": 0.0-1.0,
  "estimated_complexity": 1-10,
  "recommended_params": {
    "max_tokens": 1500,
    "temperature": 0.7,
    "timeout_multiplier": 1.2
  },
  "reasoning": "brief explanation"
}

**Examples:**

Q: "Hello"
A: {"routing_decision": "HANDLE", "estimated_complexity": 1}

Q: "What is Python?"
A: {
  "routing_decision": "DELEGATE",
  "target_model": "REASONING",
  "estimated_complexity": 6,
  "recommended_params": {
    "max_tokens": 1800,
    "temperature": 0.7,
    "timeout_multiplier": 1.0
  },
  "reasoning": "Moderate complexity explanation, ~1500 words"
}

Q: "Explain the philosophical implications of quantum mechanics"
A: {
  "routing_decision": "DELEGATE",
  "target_model": "REASONING",
  "estimated_complexity": 9,
  "recommended_params": {
    "max_tokens": 2500,
    "temperature": 0.8,
    "timeout_multiplier": 1.5
  },
  "reasoning": "Deep philosophical analysis requiring extended thinking"
}
"""
```

---

## 4. Decision Matrix

### 4.1 When to Implement Parameter Passing

| Condition | Threshold | Decision |
|-----------|-----------|----------|
| **Phase B: Estimation Accuracy** | Mean error <30% | Proceed to Phase C |
| **Phase B: Estimation Accuracy** | Mean error >30% | **ABORT** - Use fixed parameters |
| **Phase C: Efficiency Gain** | >15% improvement | **IMPLEMENT** parameter passing |
| **Phase C: Efficiency Gain** | 5-15% improvement | Implement if quality maintained |
| **Phase C: Efficiency Gain** | <5% improvement | **REJECT** - Not worth complexity |
| **Phase C: Quality Impact** | <2% degradation | Acceptable |
| **Phase C: Quality Impact** | 2-5% degradation | Review case-by-case |
| **Phase C: Quality Impact** | >5% degradation | **REJECT** - Quality too important |

### 4.2 Implementation Paths

**Path 1: Full Parameter Passing (if Phase C succeeds)**
```python
# Orchestrator uses all router recommendations
response = await llm_client.respond(
    role=routing_result["target_model"],
    messages=ctx.messages,
    max_tokens=routing_result["recommended_params"]["max_tokens"],
    temperature=routing_result["recommended_params"]["temperature"],
    timeout_s=base_timeout * routing_result["recommended_params"]["timeout_multiplier"]
)
```

**Path 2: Partial Parameter Passing (if mixed results)**
```python
# Use only well-calibrated parameters
if routing_result["confidence"] > 0.8:
    # High confidence: use router's max_tokens
    max_tokens = routing_result["recommended_params"]["max_tokens"]
else:
    # Low confidence: use fixed fallback
    max_tokens = DEFAULT_MAX_TOKENS_BY_MODEL[model_role]

response = await llm_client.respond(
    role=routing_result["target_model"],
    messages=ctx.messages,
    max_tokens=max_tokens,
    temperature=DEFAULT_TEMPERATURE,  # Keep fixed
    timeout_s=DEFAULT_TIMEOUT  # Keep fixed
)
```

**Path 3: No Parameter Passing (if Phase B/C fails)**
```python
# Use fixed parameters, log router estimates for analysis only
response = await llm_client.respond(
    role=routing_result["target_model"],
    messages=ctx.messages,
    max_tokens=DEFAULT_MAX_TOKENS_BY_MODEL[model_role],
    temperature=DEFAULT_TEMPERATURE,
    timeout_s=DEFAULT_TIMEOUT
)

# Log router's estimates for future analysis
log.info(
    "router_estimate_unused",
    estimated_tokens=routing_result["recommended_params"]["max_tokens"],
    actual_fixed_tokens=DEFAULT_MAX_TOKENS_BY_MODEL[model_role]
)
```

---

## 5. Risk Assessment

### 5.1 Risks of Implementing Parameter Passing

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Router under-estimates tokens** | Medium | High | Add 20% safety buffer, monitor truncation rate |
| **Router over-estimates timeout** | Low | Medium | Cap timeout_multiplier at 2.0x |
| **Parameter estimation degrades over time** | Medium | Medium | Continuous monitoring, auto-revert if accuracy drops |
| **Increased complexity in debugging** | High | Low | Log both router estimate and actual usage |
| **Router prompt becomes too complex** | Medium | Low | Keep parameter guidelines simple and clear |

### 5.2 Risks of NOT Implementing Parameter Passing

| Risk | Impact | Evidence Needed |
|------|--------|-----------------|
| **Resource waste continues** | Medium | Baseline shows >30% over-provisioning |
| **User waits unnecessarily** | Low | Most queries finish well before timeout |
| **Missed optimization opportunity** | Medium | Router CAN estimate accurately (Phase B) |

---

## 6. Implementation Plan (If Approved)

### 6.1 Phase A: Baseline (Week 1-2 of Phase 2)

**Day 1-2:**
- Implement fixed parameter constants
- Add telemetry for token/timeout utilization
- Create baseline data collection script

**Day 3-7:**
- Run 200+ queries with fixed parameters
- Collect comprehensive telemetry
- Analyze baseline metrics

**Day 8-10:**
- Generate baseline report
- Identify over-provisioning patterns
- Document opportunities for improvement

**Deliverable:** Baseline report with efficiency metrics

---

### 6.2 Phase B: Router Estimation Testing (Week 3)

**Day 1-2:**
- Update router prompt with parameter estimation
- Add parameter recommendation fields to routing types
- Deploy enhanced router (estimates logged but not used)

**Day 3-5:**
- Run 100+ queries, collect router estimates
- Compare estimates vs actual usage
- Compute error metrics

**Day 6-7:**
- Analyze estimation accuracy
- **GO/NO-GO DECISION:** Proceed to Phase C?
- If GO: Prepare A/B test setup
- If NO-GO: Document findings, use fixed parameters

**Deliverable:** Router estimation accuracy report + GO/NO-GO decision

---

### 6.3 Phase C: A/B Testing (Week 4, if Phase B succeeds)

**Day 1-2:**
- Implement parameter passing in orchestrator (feature flag)
- Create A/B test harness (50/50 split)
- Set up telemetry comparison

**Day 3-5:**
- Run 100 queries per group (200 total)
- Collect efficiency and quality metrics
- Monitor for issues

**Day 6-7:**
- Statistical analysis
- **FINAL DECISION:** Implement, partial implement, or reject?
- If implement: Roll out to 100% traffic
- If reject: Revert to fixed parameters

**Deliverable:** A/B test report + implementation decision

---

## 7. Evaluation Criteria

### 7.1 Success Criteria (All must be met)

✅ **Phase B:**
- [ ] Mean error rate <30%
- [ ] Under-estimation rate <10%
- [ ] Correlation coefficient >0.6

✅ **Phase C:**
- [ ] Efficiency improvement >15%
- [ ] Quality degradation <2%
- [ ] Statistical significance p<0.05
- [ ] No increase in truncation errors
- [ ] No increase in timeout failures

### 7.2 Failure Criteria (Any triggers rejection)

❌ **Phase B:**
- [ ] Mean error rate >40%
- [ ] Under-estimation rate >20%
- [ ] No correlation (coefficient <0.3)

❌ **Phase C:**
- [ ] Quality degradation >5%
- [ ] Efficiency improvement <5%
- [ ] Increased task failure rate
- [ ] Negative user feedback

---

## 8. Monitoring & Rollback

### 8.1 Continuous Monitoring (Post-Implementation)

**Metrics to Track:**
```python
# Weekly automated report
class ParameterPassingMonitor:
    def weekly_health_check(self):
        return {
            "estimation_accuracy": compute_error_rate(),
            "token_utilization": actual_tokens / estimated_tokens,
            "timeout_efficiency": actual_latency / allocated_timeout,
            "truncation_rate": queries_truncated / total_queries,
            "quality_score": avg_user_rating,
            "anomalies": detect_anomalies()
        }
```

**Alert Triggers:**
- Estimation error rate >35% for 7 days → Review router prompt
- Truncation rate >5% → Increase safety buffer
- Quality score drops >0.2 points → Consider rollback

### 8.2 Automatic Rollback Conditions

**Trigger immediate rollback if:**
- Truncation rate spikes >10%
- Task failure rate increases >20%
- Quality score drops >0.5 points
- Timeout failures increase >50%

**Rollback Process:**
```bash
# Disable feature flag
python -m personal_agent.config.set feature_flag.router_parameter_passing false

# Revert to fixed parameters
# Monitor for 24 hours
# Analyze what went wrong
```

---

## 9. Alternative Approaches (If Parameter Passing Fails)

### 9.1 Heuristic-Based Parameters

**Instead of router estimation, use simple heuristics:**

```python
def estimate_max_tokens(query: str, model_role: ModelRole) -> int:
    """Simple heuristic-based token estimation."""

    query_length = len(query.split())

    if model_role == ModelRole.REASONING:
        # Length-based estimation
        if query_length < 20:
            return 1000  # Short query, short response
        elif query_length < 50:
            return 2000  # Medium query
        else:
            return 3000  # Long/complex query

    elif model_role == ModelRole.CODING:
        # Code queries tend to need more tokens
        return 3000

    return 2000  # Default
```

**Pros:** Simple, predictable, no router complexity
**Cons:** Less adaptive, may still waste resources

### 9.2 Post-Hoc Parameter Learning

**Instead of router predicting upfront, learn from history:**

```python
class ParameterLearner:
    """Learn optimal parameters from historical usage."""

    def recommend_parameters(self, query_embedding: np.ndarray) -> dict:
        """Find similar past queries, use their actual usage."""

        # Find 10 most similar past queries
        similar_queries = self.vector_search(query_embedding, k=10)

        # Use actual token usage from similar queries
        avg_tokens = np.mean([q.actual_tokens_used for q in similar_queries])

        # Add 20% safety buffer
        recommended_tokens = int(avg_tokens * 1.2)

        return {"max_tokens": recommended_tokens}
```

**Pros:** Based on real usage, not estimates
**Cons:** Requires vector database, more infrastructure

---

## 10. Expected Timeline & Decision Points

```
Week 1-2 (Phase A): Baseline Collection
  ↓
  Decision Point 1: Is there significant over-provisioning?
    NO → Skip parameter passing (use fixed params)
    YES → Proceed to Phase B
  ↓
Week 3 (Phase B): Router Estimation Testing
  ↓
  Decision Point 2: Can router estimate accurately?
    NO → Reject parameter passing, explore alternatives
    YES → Proceed to Phase C
  ↓
Week 4 (Phase C): A/B Testing
  ↓
  Decision Point 3: Does parameter passing improve efficiency?
    NO → Reject, use fixed parameters
    YES → Implement parameter passing
  ↓
Week 5+: Monitoring & Iteration
```

**Total Time:** 4-5 weeks from start to decision

---

## 11. Recommendation

**My Recommendation: Proceed with Phased Evaluation**

**Reasoning:**
1. **Low Risk, High Reward:** Evaluation is phased with clear GO/NO-GO points
2. **Data-Driven:** We'll have hard evidence before committing
3. **Potential for 20-30% efficiency gain** (if router is accurate)
4. **Fallback Options:** Can revert to fixed params or try alternatives

**Immediate Next Steps:**
1. Complete Day 11.5 (basic routing without parameters)
2. Early Phase 2: Start Phase A baseline collection
3. Week 3 of Phase 2: Evaluate router estimation capability
4. Make data-driven decision on parameter passing

**Conservative Approach:**
- Don't implement parameter passing in Day 11.5 MVP
- Collect 2-4 weeks of baseline data first
- Only implement if Phase B shows <30% error rate
- Keep fixed parameters as permanent fallback option

---

## 12. Open Questions for Discussion

1. **What's our tolerance for quality degradation?**
   - Current target: <2% degradation acceptable
   - Is 5% acceptable for 25% efficiency gain?

2. **Should we weight certain queries higher?**
   - E.g., code queries more important than casual chat
   - Different thresholds for different channels?

3. **How quickly should we react to degradation?**
   - Current: Automatic rollback if >5% quality drop
   - Too aggressive? Too conservative?

4. **Should we fine-tune router specifically for parameter estimation?**
   - If Phase B shows promise but <30% accuracy
   - Collect training data, fine-tune Qwen3-4B

---

**Status:** Awaiting approval to begin Phase A (baseline collection)
**Owner:** Project Owner
**Next Action:** Review evaluation plan, approve/modify approach
