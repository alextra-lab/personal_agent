# Experiment E-006: Router Output Format Detection

**Date:** 2025-12-31
**Status:** Proposed (Phase 2)
**Related:** E-005 (Parameter Passing Evaluation), Day 11.5 (Router Routing Logic)
**Insight Source:** User observation - output format often explicit in queries

---

## 1. Research Question

**Can router reliably detect explicit output format requests and allocate max_tokens accordingly?**

**Sub-questions:**
1. How often do queries contain explicit format indicators?
2. Can keyword matching achieve >90% accuracy for format detection?
3. Does format-based token allocation reduce waste vs. fixed parameters?
4. Does this work for inter-agent communication?

---

## 2. Hypothesis

**Primary Hypothesis:**
> Output format detection is MORE reliable than complexity estimation because format indicators are often explicit in queries.
>
> Expected accuracy: >90% (vs. ~70% for complexity estimation)

**Key Insight:**
- "Give me a detailed explanation" → EXPLICIT signal for high token allocation
- "Summarize in bullet points" → EXPLICIT signal for low token allocation
- **Much easier than estimating "how complex is this query?"**

---

## 3. Output Format Taxonomy

### 3.1 Format Categories

```python
class OutputFormat(str, Enum):
    """Explicit output format requested by user."""

    # Concise formats (low tokens)
    SUMMARY = "summary"              # 200-500 tokens
    BULLET_POINTS = "bullet_points"  # 300-800 tokens
    BRIEF = "brief"                  # 200-400 tokens
    QUICK_ANSWER = "quick_answer"    # 100-300 tokens

    # Standard formats (medium tokens)
    EXPLANATION = "explanation"      # 800-1500 tokens
    COMPARISON = "comparison"        # 800-1500 tokens
    LIST = "list"                    # 500-1000 tokens

    # Detailed formats (high tokens)
    DETAILED = "detailed"            # 1500-3000 tokens
    COMPREHENSIVE = "comprehensive"  # 2000-3000 tokens
    STEP_BY_STEP = "step_by_step"    # 1000-2000 tokens
    DEEP_DIVE = "deep_dive"          # 2000-3500 tokens

    # Structured formats
    TABLE = "table"                  # 500-1500 tokens
    CODE_WITH_EXPLANATION = "code_with_explanation"  # 1000-2500 tokens

    # Unknown (default)
    UNSPECIFIED = "unspecified"      # 1000-1500 tokens (conservative)
```

### 3.2 Keyword Mapping

```python
FORMAT_KEYWORDS = {
    OutputFormat.SUMMARY: [
        "summary", "summarize", "sum up", "in short",
        "tldr", "tl;dr", "quick overview", "high level"
    ],

    OutputFormat.BULLET_POINTS: [
        "bullet points", "bullets", "list of", "key points",
        "main points", "main ideas", "enumerated"
    ],

    OutputFormat.BRIEF: [
        "brief", "briefly", "concise", "short", "quick",
        "in a few words", "simple answer"
    ],

    OutputFormat.DETAILED: [
        "detailed", "in detail", "thoroughly", "elaborate",
        "explain fully", "complete explanation"
    ],

    OutputFormat.COMPREHENSIVE: [
        "comprehensive", "complete", "exhaustive", "full",
        "everything about", "all aspects of"
    ],

    OutputFormat.STEP_BY_STEP: [
        "step by step", "step-by-step", "walkthrough",
        "guide me through", "how to", "instructions"
    ],

    OutputFormat.COMPARISON: [
        "compare", "contrast", "difference between",
        "vs", "versus", "pros and cons", "advantages and disadvantages"
    ],

    OutputFormat.TABLE: [
        "in a table", "as a table", "tabular format",
        "organize in table", "table format"
    ]
}
```

---

## 4. Enhanced Router Prompt

**System Prompt (Output Format Aware):**

```python
ROUTER_SYSTEM_PROMPT_FORMAT_AWARE = """
You are an intelligent task classifier for a personal AI agent.

**Models:**
- ROUTER (you): 4B, <1s, handles simple queries
- REASONING: 14B, 3-10s, for complex analysis
- CODING: 30B, 5-15s, for code tasks

**Decision Framework:**
1. Check for **explicit output format** in query
2. Classify query complexity
3. Select target model
4. Estimate max_tokens based on requested format

**Output Format Detection:**

**Concise Formats (200-500 tokens):**
- Keywords: "summary", "brief", "quick", "short", "tldr"
- Example: "Briefly explain Python" → 300 tokens

**Standard Formats (800-1500 tokens):**
- Keywords: "explain", "what is", "how does", "compare"
- Example: "Explain quantum mechanics" → 1200 tokens

**Detailed Formats (1500-3000 tokens):**
- Keywords: "detailed", "comprehensive", "thorough", "deep dive"
- Example: "Give me a detailed analysis" → 2500 tokens

**Structured Formats:**
- "bullet points" → 500 tokens
- "step by step" → 1500 tokens
- "in a table" → 800 tokens
- "with code examples" → 2000 tokens

**Token Allocation Formula:**
```
base_tokens = FORMAT_TOKEN_MAP[detected_format]
complexity_multiplier = 1.0 + (complexity_score / 10) * 0.5
max_tokens = base_tokens * complexity_multiplier
```

**Examples:**

1. Query: "Summarize Python in bullet points"
   Format: BULLET_POINTS (explicit)
   Output:
   ```json
   {
     "routing_decision": "DELEGATE",
     "target_model": "REASONING",
     "detected_format": "bullet_points",
     "format_confidence": 0.95,
     "recommended_params": {
       "max_tokens": 500,
       "temperature": 0.7
     }
   }
   ```

2. Query: "Give me a comprehensive, detailed explanation of quantum entanglement"
   Format: COMPREHENSIVE + DETAILED (explicit)
   Output:
   ```json
   {
     "routing_decision": "DELEGATE",
     "target_model": "REASONING",
     "detected_format": "comprehensive",
     "format_confidence": 0.98,
     "recommended_params": {
       "max_tokens": 2800,
       "temperature": 0.7
     }
   }
   ```

3. Query: "What is Python?"
   Format: UNSPECIFIED (default)
   Output:
   ```json
   {
     "routing_decision": "DELEGATE",
     "target_model": "REASONING",
     "detected_format": "unspecified",
     "format_confidence": 0.5,
     "recommended_params": {
       "max_tokens": 1500,
       "temperature": 0.7
     }
   }
   ```

**Output JSON Schema:**
{
  "routing_decision": "HANDLE|DELEGATE",
  "target_model": "ROUTER|REASONING|CODING",
  "confidence": 0.0-1.0,

  // NEW: Format detection
  "detected_format": "summary|detailed|bullet_points|...",
  "format_confidence": 0.0-1.0,
  "format_keywords_matched": ["detailed", "comprehensive"],

  "recommended_params": {
    "max_tokens": 1500,
    "temperature": 0.7
  }
}
"""
```

---

## 5. Evaluation Plan

### 5.1 Phase A: Format Detection Accuracy (Week 1)

**Test Queries (50 samples):**
```python
test_queries = [
    # Explicit concise
    ("Summarize Python in 3 sentences", OutputFormat.SUMMARY, 300),
    ("Brief overview of quantum mechanics", OutputFormat.BRIEF, 400),
    ("Quick answer: what is AI?", OutputFormat.QUICK_ANSWER, 200),

    # Explicit bullet points
    ("List the key features of Python in bullet points", OutputFormat.BULLET_POINTS, 500),
    ("Give me the main points about X", OutputFormat.BULLET_POINTS, 600),

    # Explicit detailed
    ("Explain quantum mechanics in detail", OutputFormat.DETAILED, 2500),
    ("Give me a comprehensive guide to Python", OutputFormat.COMPREHENSIVE, 2800),
    ("I need a thorough analysis of X", OutputFormat.DETAILED, 2200),

    # Ambiguous / unspecified
    ("What is Python?", OutputFormat.UNSPECIFIED, 1500),
    ("Tell me about quantum mechanics", OutputFormat.UNSPECIFIED, 1500),
]
```

**Metrics:**
```python
def evaluate_format_detection(test_queries):
    results = []

    for query, expected_format, expected_tokens in test_queries:
        routing_result = router.classify(query)

        detected_format = routing_result["detected_format"]
        format_confidence = routing_result["format_confidence"]
        recommended_tokens = routing_result["recommended_params"]["max_tokens"]

        # Accuracy: Did we detect the right format?
        format_correct = (detected_format == expected_format)

        # Token accuracy: Are we within ±30% of expected?
        token_error = abs(recommended_tokens - expected_tokens) / expected_tokens
        token_acceptable = token_error < 0.3

        results.append({
            "query": query,
            "expected_format": expected_format,
            "detected_format": detected_format,
            "format_correct": format_correct,
            "format_confidence": format_confidence,
            "expected_tokens": expected_tokens,
            "recommended_tokens": recommended_tokens,
            "token_error": token_error,
            "token_acceptable": token_acceptable
        })

    # Aggregate metrics
    format_accuracy = sum(r["format_correct"] for r in results) / len(results)
    token_accuracy = sum(r["token_acceptable"] for r in results) / len(results)

    # High-confidence accuracy
    high_confidence = [r for r in results if r["format_confidence"] > 0.8]
    high_confidence_accuracy = sum(r["format_correct"] for r in high_confidence) / len(high_confidence)

    return {
        "format_detection_accuracy": format_accuracy,
        "token_estimation_accuracy": token_accuracy,
        "high_confidence_accuracy": high_confidence_accuracy,
        "results": results
    }
```

**Success Criteria:**
- Format detection accuracy >90% (for queries with explicit format keywords)
- Token estimation accuracy >80% (within ±30% of expected)
- High-confidence accuracy >95%

---

### 5.2 Phase B: Real-World Validation (Week 2)

**Collect 100 real user queries, analyze:**

1. **Format indicator prevalence:**
   - What % of queries have explicit format keywords?
   - Expected: 30-50% (significant portion)

2. **Format detection in practice:**
   - Run router on real queries
   - Manually validate format detection accuracy
   - Measure token waste reduction

3. **Baseline comparison:**
   ```python
   # Baseline: Fixed max_tokens=2000 for all REASONING queries
   baseline_waste = (2000 - actual_tokens_used) / 2000

   # Format-aware: Variable max_tokens based on detected format
   format_aware_waste = (recommended_tokens - actual_tokens_used) / recommended_tokens

   # Compare
   waste_reduction = (baseline_waste - format_aware_waste) / baseline_waste * 100
   ```

**Expected Results:**
- Format detection works well for 30-50% of queries (those with explicit indicators)
- For those queries: 40-60% token waste reduction
- Overall: 15-25% token waste reduction across all queries

---

### 5.3 Phase C: Inter-Agent Communication (Week 3-4)

**Test inter-agent format specification:**

```python
# Scenario 1: Agent needs detailed analysis
agent_request = {
    "task": "analyze_system_health",
    "query": "Provide a detailed analysis of current system health",
    "format": "detailed"  # Explicit format specification
}

# Router processes
routing_result = router.classify(agent_request["query"])
assert routing_result["detected_format"] == "detailed"
assert routing_result["recommended_params"]["max_tokens"] > 2000

# Scenario 2: Agent needs brief summary
agent_request = {
    "task": "summarize_logs",
    "query": "Summarize recent errors in bullet points",
    "format": "bullet_points"  # Explicit format specification
}

routing_result = router.classify(agent_request["query"])
assert routing_result["detected_format"] == "bullet_points"
assert routing_result["recommended_params"]["max_tokens"] < 800
```

**Benefits for Inter-Agent Communication:**
1. ✅ **Explicit format control** - Agent can request specific output formats
2. ✅ **Token efficiency** - Don't waste tokens on overly detailed responses when brief summary needed
3. ✅ **Structured communication** - Agents speak a common "format language"
4. ✅ **Composability** - Chain multiple agents with format specifications

---

## 6. Implementation Strategy

### 6.1 Hybrid Approach (Recommended)

**Combine format detection + complexity estimation:**

```python
async def estimate_max_tokens(
    query: str,
    detected_format: OutputFormat,
    format_confidence: float,
    complexity_score: int
) -> int:
    """Hybrid token estimation using format + complexity."""

    if format_confidence > 0.8:
        # High confidence: use format-based estimation
        base_tokens = FORMAT_TOKEN_MAP[detected_format]

        # Minor adjustment for complexity
        complexity_multiplier = 1.0 + (complexity_score / 10) * 0.3
        return int(base_tokens * complexity_multiplier)

    else:
        # Low confidence: use complexity-based estimation
        return complexity_score * 200  # Fallback heuristic
```

**Why Hybrid?**
- Format detection: HIGH accuracy when format is explicit (90%+)
- Complexity estimation: MEDIUM accuracy, but works for all queries (70%)
- Combined: Best of both worlds

---

### 6.2 Phased Rollout

**Phase 1 (Week 1-2):** Format detection only
- Implement format keyword matching
- Test on 100 queries
- Measure accuracy

**Phase 2 (Week 3):** Integrate with routing
- Add format detection to router prompt
- Log format + recommended tokens (not used yet)
- Compare to baseline

**Phase 3 (Week 4):** A/B test
- 50/50: Fixed tokens vs format-aware tokens
- Measure waste reduction + quality impact
- Make GO/NO-GO decision

**Phase 4 (Month 3):** Inter-agent format specs
- Add format field to internal request schema
- Test agent-to-agent communication with formats
- Validate composability

---

## 7. Decision Matrix

| Metric | Threshold | Decision |
|--------|-----------|----------|
| **Format Detection Accuracy** (explicit) | >90% | ✅ Proceed |
| **Format Detection Accuracy** (explicit) | <80% | ❌ Reject |
| **Format Indicator Prevalence** | >30% | ✅ Useful for significant % of queries |
| **Format Indicator Prevalence** | <20% | ⚠️ Limited benefit |
| **Waste Reduction** (explicit queries) | >40% | ✅ Implement |
| **Waste Reduction** (explicit queries) | <20% | ❌ Not worth complexity |
| **Quality Impact** | <2% degradation | ✅ Acceptable |
| **Quality Impact** | >5% degradation | ❌ Reject |

---

## 8. Example: Format-Aware Router Response

**User Query:** "Give me a detailed, comprehensive explanation of how quantum entanglement works, with examples"

**Router Analysis:**
```json
{
  "routing_decision": "DELEGATE",
  "target_model": "REASONING",
  "confidence": 0.95,

  // Format detection
  "detected_format": "comprehensive",
  "format_confidence": 0.98,
  "format_keywords_matched": ["detailed", "comprehensive", "with examples"],

  // Token recommendation
  "recommended_params": {
    "max_tokens": 2800,
    "temperature": 0.7,
    "timeout_multiplier": 1.5
  },

  "reasoning": "User explicitly requested 'detailed, comprehensive' explanation with examples. High token allocation appropriate."
}
```

**Comparison to Fixed Allocation:**
- Fixed: 2000 tokens (may truncate comprehensive response)
- Format-aware: 2800 tokens (appropriate for explicit "comprehensive" request)

---

## 9. Integration with Self-Tuning

**Self-tuning can learn optimal format-to-token mappings:**

```python
# Initial mapping (hand-tuned)
FORMAT_TOKEN_MAP = {
    OutputFormat.SUMMARY: 400,
    OutputFormat.DETAILED: 2500,
    # ...
}

# After 1000 queries, analyze:
actual_usage = analyze_telemetry()
# {OutputFormat.SUMMARY: avg 320 tokens, OutputFormat.DETAILED: avg 2100 tokens}

# Agent proposes update:
proposal = {
    "type": "format_token_map_update",
    "current": FORMAT_TOKEN_MAP,
    "proposed": {
        OutputFormat.SUMMARY: 350,  # Reduce from 400
        OutputFormat.DETAILED: 2200,  # Reduce from 2500
    },
    "rationale": "Actual usage 10-15% lower than allocated",
    "expected_savings": "12% token reduction, no quality impact"
}

# Validate via A/B test → Apply if successful
```

---

## 10. Recommendation

**Strong Recommendation: Implement Format Detection (Phase 2)**

**Reasoning:**
1. **Higher accuracy than complexity estimation** (90% vs 70%)
2. **Explicit signals in queries** (30-50% of queries have format indicators)
3. **Clear benefit for inter-agent communication**
4. **Low risk** (fallback to default tokens if format undetected)
5. **Synergistic with self-tuning** (agent can optimize format-to-token mappings)

**Implementation Priority:**
1. ✅ **High:** Format detection (this experiment)
2. ⚠️ **Medium:** Complexity estimation (E-005, more risky)
3. ✅ **High:** Hybrid approach (format + complexity, best of both)

**Timeline:**
- Week 1-2 of Phase 2: Implement and test format detection
- Week 3: Integrate with routing
- Week 4: A/B test and deploy
- Month 3: Enable inter-agent format specifications

---

## 11. Open Questions

1. **Should we support custom format specifications in API?**
   ```python
   # Explicit format override
   result = orchestrator.handle_user_request(
       message="Explain Python",
       format="bullet_points",  # User/agent can override
       max_tokens=500
   )
   ```

2. **Should format confidence affect routing decisions?**
   - Example: If format="brief" with high confidence, maybe ROUTER can handle (vs delegating to REASONING)?

3. **How do we handle conflicting signals?**
   - Query: "Give me a brief but comprehensive explanation"
   - Contradiction: "brief" (200 tokens) vs "comprehensive" (2500 tokens)
   - Resolution: Weighted average? Default to higher token allocation (safer)?

4. **Should we track format compliance in quality metrics?**
   - If user asks for "bullet points", did we actually deliver bullet points?
   - Validation: Check response format matches requested format

---

**Status:** Awaiting approval to implement in Phase 2
**Recommendation:** **YES** - Implement format detection, HIGH confidence in success
**Expected Impact:** 15-25% token waste reduction, improved inter-agent communication
