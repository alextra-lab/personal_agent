# E-009: Performance-Based Routing

**Status:** Planned (Phase 3)
**Phase:** Phase 3 Month 4-5
**Date Created:** 2026-01-18
**Prerequisites:** E-007 complete, 1000+ routing decisions collected, telemetry analysis framework operational
**Priority:** MEDIUM - Advanced optimization

---

## 1. Hypothesis

**H-009a:** Routing decisions based on historical performance data (learned from 1000+ past decisions) will improve task success rates by >5% compared to static rule-based routing.

**H-009b:** Machine learning models trained on routing outcomes can identify patterns that humans miss (e.g., subtle query features predicting model success).

**H-009c:** Performance-based routing will reduce average task latency by routing to faster models when quality is equivalent.

---

## 2. Objective

Implement a data-driven routing strategy that learns from historical routing outcomes to optimize future routing decisions.

**Key Goals:**
1. Collect 1000+ routing decisions with outcomes (success/failure, latency, quality scores)
2. Train a routing classifier on this data
3. A/B test learned routing vs. static routing
4. Measure improvement in success rate, latency, and quality

**Decision Criteria:**
- If learned routing improves success rate by >5%: **Adopt performance-based routing**
- If minimal improvement (<3%): **Keep static routing** (simpler, more explainable)
- If mixed results: **Hybrid approach** (use learned routing for specific query types)

---

## 3. Method

### 3.1 Data Collection (Weeks 1-4, passive)

**Prerequisites:** E-007 three-stage routing operational in production

**Collect for each routing decision:**
```python
{
  "trace_id": "uuid",
  "timestamp": "ISO-8601",
  "query": "user query text",
  "query_features": {
    "length": 42,
    "has_code": false,
    "has_question": true,
    "complexity_estimate": 0.6,
    "keywords": ["system", "cpu", "usage"]
  },
  "routing_decision": {
    "execution_path": "DIRECT_TOOL",
    "tool_name": "system_metrics_snapshot",
    "confidence": 0.92,
    "stage_1_result": {...},
    "stage_2_result": {...},
    "stage_3_result": {...}
  },
  "execution_outcome": {
    "success": true,
    "latency_ms": 234,
    "error": null,
    "tool_execution_time_ms": 120,
    "llm_calls": 0
  },
  "quality_metrics": {
    "user_feedback": "positive",  // if available
    "response_length": 150,
    "hallucination_detected": false
  }
}
```

**Storage:**
- `telemetry/evaluation/routing_outcomes/YYYY-MM-DD-routing-outcomes.jsonl`
- Append-only log of all routing decisions + outcomes

**Target:** 1000+ routing decisions across diverse query types

---

### 3.2 Feature Engineering (Week 5)

**Query Features:**
- Query length (tokens)
- Presence of code blocks
- Question vs. command
- Sentiment/urgency
- Keyword extraction
- Similarity to past queries

**Context Features:**
- Current system mode (NORMAL/ALERT/DEGRADED)
- Recent error rate
- Current model load (VRAM usage)
- Time of day (if relevant)

**Historical Features:**
- Past success rate for similar queries
- Past latency for similar queries
- Model-specific performance on query type

---

### 3.3 Model Training (Week 5)

**Approach 1: Classification Model**

Train a classifier to predict optimal execution path:

```python
from sklearn.ensemble import RandomForestClassifier

# Features: query_length, has_code, complexity, keywords_vector, context
X_train = extract_features(routing_decisions[:800])

# Labels: execution_path (DIRECT_TOOL, REASONING, CODING, STANDARD)
y_train = [decision["routing_decision"]["execution_path"] for decision in routing_decisions[:800]]

classifier = RandomForestClassifier(n_estimators=100)
classifier.fit(X_train, y_train)
```

**Approach 2: Regression Model**

Train a model to predict success probability for each path:

```python
# For each query, predict P(success | DIRECT_TOOL), P(success | REASONING), etc.
# Choose path with highest predicted success probability
```

**Approach 3: Fine-Tune Router LLM**

Fine-tune Qwen3-4B router on routing outcomes:

```python
# Create training data:
# Input: query + context
# Output: optimal execution path (ground truth from outcomes)

# Fine-tune on LM Studio or external service
# Replace base router with fine-tuned version
```

---

### 3.4 A/B Testing (Week 6)

**Variant A: Static Routing** (baseline)
- Current three-stage routing from E-007
- Rule-based classification/selection

**Variant B: Performance-Based Routing**
- Learned classifier predicts execution path
- Falls back to static routing if confidence <0.7

**Test Protocol:**
- 200 new queries (not in training set)
- 100 to Variant A, 100 to Variant B
- Compare success rates, latency, quality

---

## 4. Success Criteria

| Metric | Static Routing (Baseline) | Performance-Based Target | Winner If... |
|--------|---------------------------|--------------------------|--------------|
| **Success Rate** | ~90% (E-007) | >95% | Learned >5% improvement |
| **Avg Latency** | ~8s | <7s | Learned routes to faster models |
| **Routing Accuracy** | ~95% (E-007) | >97% | Learned catches edge cases |
| **Hallucination Rate** | <5% | <3% | Learned avoids hallucination-prone paths |
| **User Satisfaction** | Baseline | +10% | Blind preference test |

**Adopt Learned Routing If:**
- ✅ Success rate improvement >5%
- ✅ No regression in latency (within 10%)
- ✅ Model explainability acceptable (can understand predictions)

**Keep Static Routing If:**
- ❌ Success rate improvement <3%
- ❌ Learned model too opaque (can't explain decisions)
- ❌ Requires ongoing retraining (high maintenance)

---

## 5. Data Analysis

### 5.1 Exploratory Analysis (Week 5, before training)

**Questions to answer:**
1. What query features correlate with routing success?
2. Are there query types where static routing consistently fails?
3. What's the confusion matrix for current routing decisions?
4. Which models have highest success rates for which query types?

**Visualization:**
- Success rate heatmap (query_type × execution_path)
- Latency distribution by execution path
- Feature importance (which features predict success?)

---

### 5.2 Model Evaluation

**Metrics:**
- Accuracy, precision, recall, F1 per execution path
- Confusion matrix
- Feature importance (which features drive decisions?)
- Calibration (are predicted probabilities accurate?)

**Cross-Validation:**
- 5-fold cross-validation on 1000 routing decisions
- Test on held-out set (200 decisions)

---

## 6. Timeline

**Weeks 1-4 (Passive Data Collection):**
- Run E-007 three-stage routing in production
- Collect 1000+ routing decisions with outcomes
- No active experiment work during this phase

**Week 5 (Active Development - 5 days):**
- Day 1: Exploratory data analysis
- Day 2: Feature engineering
- Day 3: Train classification/regression models
- Day 4: Evaluate models, select best approach
- Day 5: Implement learned routing variant

**Week 6 (Testing - 2 days):**
- Day 6: Run A/B test (200 queries)
- Day 7: Analysis and report

**Total Active Work:** 1 week (after 4 weeks passive data collection)

---

## 7. Deliverables

1. **Data Pipeline:**
   - `src/personal_agent/telemetry/routing_outcome_collector.py` (logs outcomes)
   - `telemetry/evaluation/routing_outcomes/` (outcome logs)

2. **Model Training:**
   - `experiments/E-009/train_routing_classifier.py` (training script)
   - `models/routing_classifier.pkl` (trained model)
   - `experiments/E-009/feature_importance.png` (analysis)

3. **A/B Test Results:**
   - `experiments/E-009-results.md` (comparison report)
   - Side-by-side success rates, latency, quality

4. **Code:**
   - `src/personal_agent/orchestrator/performance_based_router.py` (learned routing)
   - Config flag: `routing.use_learned_model: true/false`

---

## 8. Analysis Questions

After A/B testing, answer:

1. **Which query types benefit most from learned routing?**
2. **What features are most predictive of routing success?**
3. **Are there surprising patterns the learned model discovered?**
4. **Does learned routing handle edge cases better than static routing?**
5. **How often does learned routing disagree with static routing?**
6. **When learned routing disagrees, is it usually right?**
7. **Is the model explainable enough for production use?**

---

## 9. Decision Matrix

```
Outcome                              Success   Latency   Explainability   Decision
                                     Rate      Impact
──────────────────────────────────────────────────────────────────────────────────
Clear win, explainable               +8%       Neutral   ✅               ADOPT learned
Clear win, opaque                    +8%       Neutral   ❌               Investigate features
Marginal win                         +3%       Neutral   ✅               Hybrid approach
No improvement                       +1%       Neutral   ~                KEEP static
Latency regression                   +5%       +20%      ~                Optimize model

Query-specific benefit               Varies    Neutral   ✅               Use for specific types
Requires frequent retraining         +5%       Neutral   ~                Cost-benefit analysis
```

---

## 10. Risks & Mitigation

**Risk 1: Overfitting to Training Data**
- **Mitigation:** Cross-validation, hold-out test set, monitor performance on new queries

**Risk 2: Concept Drift**
- **Mitigation:** Continuous monitoring, retrain quarterly, alert on accuracy degradation

**Risk 3: Model Opacity**
- **Mitigation:** Use interpretable models (Random Forest, not deep nets), feature importance analysis

**Risk 4: Insufficient Training Data**
- **Mitigation:** Require 1000+ decisions before training, ensure diverse query types

**Risk 5: Maintenance Burden**
- **Mitigation:** Automate retraining pipeline, make it optional (fallback to static routing)

---

## 11. Variations to Test

**If initial results are mixed:**

**Variation 1: Hybrid Routing**
- Use learned routing for specific query types (e.g., ambiguous queries)
- Use static routing for clear queries (simpler, faster)

**Variation 2: Confidence-Weighted Ensemble**
- Combine static routing + learned routing predictions
- Weight by confidence scores

**Variation 3: Active Learning**
- Start with static routing
- Collect edge cases where static routing fails
- Train learned model specifically on edge cases

---

## 12. Next Experiments

**If E-009 succeeds:**
- **E-012:** Router Fine-Tuning (fine-tune Qwen3-4B on routing outcomes)
- Continuous learning pipeline for routing optimization

**If E-009 shows mixed results:**
- Investigate feature engineering improvements
- Test ensemble approaches (static + learned)
- Focus on query-specific learned routing

---

## 13. References

**Research:**
- Active learning for agent routing
- Transfer learning for task classification
- Explainable AI for routing decisions

**Related Experiments:**
- E-004: Baseline Model Performance (initial routing performance)
- E-007: Three-Stage Routing (provides training data)
- E-008: Validation Agent Effectiveness (validation outcomes inform routing)

**Related ADRs:**
- ADR-0003: Model Stack (router role)
- ADR-0006: Orchestrator Execution Model

---

**Document Status:** Experiment Specification
**Last Updated:** 2026-01-18
**Owner:** Project Owner
**Ready to Execute:** After 1000+ routing decisions collected (4+ weeks after E-007 deployment)
